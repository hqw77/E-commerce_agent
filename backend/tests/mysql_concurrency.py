"""MySQL 并发审批冒烟:8 个请求同时批同一单,只能有一条最终审批.

用法:
  $env:AGENT_STATE_BACKEND="sql"
  $env:AGENT_DATABASE_URL="mysql+pymysql://root:password@127.0.0.1:3306/ecommerce_agent_test?charset=utf8mb4"
  python tests/mysql_concurrency.py
"""

from __future__ import annotations

import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select

from db import repository
from db.database import init_db, reset_engine, session_scope, state_backend
from db.models import ApprovalRecord


def build_checkpoint(session_id: str, workflow_id: str, order_id: str) -> dict:
    workflow = {
        "workflow_id": workflow_id,
        "session_id": session_id,
        "workflow_type": "unshipped_refund",
        "status": "paused",
        "pending_action": "require_approval",
        "order_id": order_id,
        "resume_token": f"resume-{session_id}-{workflow_id}-{order_id}",
        "idempotency_key": f"hitl:{session_id}:{order_id}",
        "frozen_fields": {
            "runtime_user_id": "U1001",
            "order_id": order_id,
            "order_status": "PENDING_SHIPMENT",
            "logistics_status": "NOT_SHIPPED",
        },
    }
    return {
        "session_id": session_id,
        "workflow": workflow,
        "frozen_fields": workflow["frozen_fields"],
        "resume_token": workflow["resume_token"],
        "idempotency_key": workflow["idempotency_key"],
        "citations": [],
        "order_snapshot": {},
        "user_consented_at": None,
    }


def main() -> int:
    if state_backend() != "sql":
        print("SKIP: AGENT_STATE_BACKEND != sql")
        return 0
    print("database:", os.getenv("AGENT_DATABASE_URL", "").split("@")[-1])
    reset_engine()
    init_db()

    suffix = uuid.uuid4().hex[:8]
    session_id = f"s-concurrency-{suffix}"
    workflow_id = f"wf-{session_id}"
    order_id = f"SO{suffix}"
    checkpoint = build_checkpoint(session_id, workflow_id, order_id)
    repository.save_checkpoint(checkpoint)
    repository.record_consent(session_id=session_id, workflow_id=workflow_id, user_id="U1001")

    def decide(index: int):
        try:
            result = repository.decide(
                session_id=session_id,
                workflow_id=workflow_id,
                decision="approved",
                reviewer_id=f"manager-{index:02d}",
                reviewer_role="after_sale_manager",
                note=f"concurrent-{index}",
                idem_key=checkpoint["idempotency_key"],
                require_consent=True,
            )
            return result.idempotent_replay
        except Exception as exc:  # noqa: BLE001
            return f"error:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(decide, range(8)))

    non_replay = [item for item in results if item is False]
    errors = [item for item in results if isinstance(item, str) and item.startswith("error:")]
    with session_scope() as session:
        approval_count = session.scalar(
            select(func.count()).select_from(ApprovalRecord).where(ApprovalRecord.workflow_id == workflow_id)
        )
    loaded = repository.load_checkpoint(session_id, workflow_id)
    status = (loaded or {}).get("workflow", {}).get("status")

    print("results=", results)
    print("approval_count=", approval_count, "status=", status)
    if errors:
        print("errors=", errors)

    # 模拟进程重启:释放连接池后重新读取
    reset_engine()
    loaded_after_restart = repository.load_checkpoint(session_id, workflow_id)
    restart_status = (loaded_after_restart or {}).get("workflow", {}).get("status")
    print("after_restart_status=", restart_status)

    ok = (
        len(non_replay) == 1
        and approval_count == 1
        and status == "completed"
        and restart_status == "completed"
        and not errors
    )
    print("OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
