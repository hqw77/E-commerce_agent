from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from db.database import session_scope, state_backend
from db.models import ApprovalRecord, AuditLog, IdempotencyRecord, WorkflowCheckpoint, utcnow
from state import session_state


class RepositoryError(Exception):
    """仓储层业务异常基类."""


class CheckpointNotFound(RepositoryError):
    pass


class ConsentRequired(RepositoryError):
    pass


class SelfApprovalForbidden(RepositoryError):
    pass


class OwnerMismatch(RepositoryError):
    pass


@dataclass
class DecisionResult:
    checkpoint: dict[str, Any]
    approval: dict[str, Any] | None
    idempotent_replay: bool
    request_id: str | None


_memory_lock = threading.RLock()
_FINAL_STATUSES = {"completed", "rejected"}
_VALID_DECISIONS = {"approved", "rejected", "needs_more_info"}


def _is_sql() -> bool:
    return state_backend() == "sql"


def _jsonable(value: Any) -> Any:
    """把 Pydantic/日期/自定对象转成 MySQL JSON 能存的结构."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _payload_of(checkpoint: dict[str, Any]) -> dict[str, Any]:
    return checkpoint.get("workflow") or {}


def _frozen_user(checkpoint: dict[str, Any]) -> str:
    return str((checkpoint.get("frozen_fields") or {}).get("runtime_user_id") or "")


def _status_of(checkpoint: dict[str, Any]) -> str:
    return str(_payload_of(checkpoint).get("status") or "")


def _apply_decision_to_checkpoint(
    checkpoint: dict[str, Any],
    *,
    decision: str,
    reviewer_id: str,
    reviewer_role: str,
    note: str | None,
) -> dict[str, Any]:
    workflow = dict(_payload_of(checkpoint))
    if decision == "approved":
        workflow["status"] = "completed"
        workflow["pending_action"] = "approval_accepted"
    elif decision == "rejected":
        workflow["status"] = "rejected"
        workflow["pending_action"] = "approval_rejected"
    else:
        workflow["status"] = "paused"
        workflow["pending_action"] = "need_more_info"
    workflow["reviewer_id"] = reviewer_id
    workflow["reviewer_role"] = reviewer_role
    workflow["reviewer_note"] = note
    workflow["decided_at"] = _now_iso()
    checkpoint = dict(checkpoint)
    checkpoint["workflow"] = workflow
    checkpoint["updated_at"] = workflow["decided_at"]
    checkpoint["approval"] = {
        "workflow_id": workflow.get("workflow_id"),
        "reviewer_id": reviewer_id,
        "reviewer_role": reviewer_role,
        "decision": decision,
        "note": note,
        "decided_at": workflow["decided_at"],
    }
    return checkpoint


def save_checkpoint(checkpoint: dict[str, Any]) -> None:
    """保存或更新一个 workflow checkpoint."""
    workflow = _payload_of(checkpoint)
    workflow_id = str(workflow.get("workflow_id") or "")
    if not workflow_id:
        raise RepositoryError("checkpoint 缺少 workflow_id")
    checkpoint.setdefault("created_at", _now_iso())
    checkpoint["updated_at"] = _now_iso()
    if _is_sql():
        payload = _jsonable(checkpoint)
        with session_scope() as session:
            row = session.scalar(select(WorkflowCheckpoint).where(WorkflowCheckpoint.workflow_id == workflow_id))
            if row is None:
                row = WorkflowCheckpoint(
                    workflow_id=workflow_id,
                    session_id=str(checkpoint.get("session_id") or workflow.get("session_id") or ""),
                    user_id=_frozen_user(checkpoint),
                    order_no=workflow.get("order_id"),
                    status=str(workflow.get("status") or "paused"),
                    pending_action=workflow.get("pending_action"),
                    idempotency_key=str(checkpoint.get("idempotency_key") or workflow.get("idempotency_key") or ""),
                    payload=payload,
                )
                session.add(row)
            else:
                row.payload = payload
                row.status = str(workflow.get("status") or row.status)
                row.pending_action = workflow.get("pending_action")
                row.updated_at = utcnow()
        return
    with _memory_lock:
        session_state.WORKFLOW_CHECKPOINTS[(str(checkpoint.get("session_id")), workflow_id)] = checkpoint


def load_checkpoint(session_id: str, workflow_id: str) -> dict[str, Any] | None:
    if _is_sql():
        with session_scope() as session:
            row = session.scalar(select(WorkflowCheckpoint).where(WorkflowCheckpoint.workflow_id == workflow_id))
            return dict(row.payload) if row and row.payload else None
    with _memory_lock:
        checkpoint = session_state.WORKFLOW_CHECKPOINTS.get((session_id, workflow_id))
        return checkpoint


def find_checkpoint_by_workflow(workflow_id: str) -> dict[str, Any] | None:
    if _is_sql():
        with session_scope() as session:
            row = session.scalar(select(WorkflowCheckpoint).where(WorkflowCheckpoint.workflow_id == workflow_id))
            return dict(row.payload) if row and row.payload else None
    with _memory_lock:
        for (_session_id, wid), checkpoint in session_state.WORKFLOW_CHECKPOINTS.items():
            if wid == workflow_id:
                return checkpoint
    return None


def _iso_value(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _summary_from_checkpoint(
    checkpoint: dict[str, Any],
    *,
    created_at: Any = None,
    updated_at: Any = None,
) -> dict[str, Any]:
    workflow = _payload_of(checkpoint)
    frozen = checkpoint.get("frozen_fields") or {}
    return {
        "workflow_id": str(workflow.get("workflow_id") or ""),
        "session_id": str(checkpoint.get("session_id") or workflow.get("session_id") or ""),
        "user_id": _frozen_user(checkpoint),
        "order_no": workflow.get("order_id") or frozen.get("order_id"),
        "workflow_type": str(workflow.get("workflow_type") or ""),
        "status": str(workflow.get("status") or ""),
        "pending_action": str(workflow.get("pending_action") or ""),
        "user_consented_at": checkpoint.get("user_consented_at"),
        "created_at": checkpoint.get("created_at") or _iso_value(created_at),
        "updated_at": checkpoint.get("updated_at") or _iso_value(updated_at),
        "order_status": frozen.get("order_status"),
        "logistics_status": frozen.get("logistics_status"),
    }


def list_pending(limit: int = 50) -> list[dict[str, Any]]:
    """管理端待审批列表;不返回 resume_token 和订单快照."""
    limit = max(1, min(int(limit), 200))
    if _is_sql():
        with session_scope() as session:
            rows = session.scalars(
                select(WorkflowCheckpoint)
                .where(
                    WorkflowCheckpoint.pending_action == "require_approval",
                    WorkflowCheckpoint.status.in_(("paused", "pending_approval")),
                )
                .order_by(WorkflowCheckpoint.updated_at.desc())
                .limit(limit)
            ).all()
            return [
                _summary_from_checkpoint(dict(row.payload), created_at=row.created_at, updated_at=row.updated_at)
                for row in rows
                if row.payload
            ]
    with _memory_lock:
        items = [
            _summary_from_checkpoint(checkpoint)
            for checkpoint in session_state.WORKFLOW_CHECKPOINTS.values()
            if _payload_of(checkpoint).get("pending_action") == "require_approval"
            and _status_of(checkpoint) in {"paused", "pending_approval"}
        ]
    items.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    return items[:limit]


def get_workflow_status(workflow_id: str) -> dict[str, Any] | None:
    """用户侧 workflow 状态;包含 owner 供路由做归属校验."""
    checkpoint = find_checkpoint_by_workflow(workflow_id)
    if checkpoint is None:
        return None
    workflow = _payload_of(checkpoint)
    summary = _summary_from_checkpoint(checkpoint)
    approval = checkpoint.get("approval") or {}
    return {
        **summary,
        "user_id": _frozen_user(checkpoint),
        "decided_at": approval.get("decided_at") or workflow.get("decided_at"),
        "decision": approval.get("decision"),
        "reviewer_role": approval.get("reviewer_role"),
    }


def record_consent(*, session_id: str, workflow_id: str, user_id: str) -> dict[str, Any]:
    """记录用户同意提交;不执行审批."""
    if _is_sql():
        with session_scope() as session:
            row = session.scalar(
                select(WorkflowCheckpoint)
                .where(WorkflowCheckpoint.workflow_id == workflow_id)
                .with_for_update()
            )
            if row is None or not row.payload:
                raise CheckpointNotFound(workflow_id)
            checkpoint = dict(row.payload)
            owner = _frozen_user(checkpoint)
            if owner and user_id and owner != user_id:
                raise OwnerMismatch(workflow_id)
            if not checkpoint.get("user_consented_at"):
                checkpoint["user_consented_at"] = _now_iso()
            workflow = dict(_payload_of(checkpoint))
            workflow["status"] = "pending_approval"
            workflow["pending_action"] = "require_approval"
            checkpoint["workflow"] = workflow
            checkpoint["updated_at"] = _now_iso()
            row.payload = _jsonable(checkpoint)
            row.status = "pending_approval"
            row.pending_action = "require_approval"
            row.updated_at = utcnow()
            session.add(
                AuditLog(
                    request_id=None,
                    actor=user_id,
                    action="user_consent",
                    target=workflow_id,
                    before_state=None,
                    after_state={"user_consented_at": checkpoint["user_consented_at"]},
                )
            )
            return checkpoint
    with _memory_lock:
        checkpoint = session_state.WORKFLOW_CHECKPOINTS.get((session_id, workflow_id))
        if checkpoint is None:
            raise CheckpointNotFound(workflow_id)
        owner = _frozen_user(checkpoint)
        if owner and user_id and owner != user_id:
            raise OwnerMismatch(workflow_id)
        if not checkpoint.get("user_consented_at"):
            checkpoint["user_consented_at"] = _now_iso()
        workflow = dict(_payload_of(checkpoint))
        workflow["status"] = "pending_approval"
        workflow["pending_action"] = "require_approval"
        checkpoint["workflow"] = workflow
        checkpoint["updated_at"] = _now_iso()
        session_state.AUDIT_LOGS.append(
            {
                "actor": user_id,
                "action": "user_consent",
                "target": workflow_id,
                "after_state": {"user_consented_at": checkpoint["user_consented_at"]},
            }
        )
        return checkpoint


def get_submitted_action(idem_key: str) -> dict[str, Any] | None:
    if _is_sql():
        with session_scope() as session:
            row = session.get(IdempotencyRecord, idem_key)
            if row is None:
                return None
            return {"request_id": row.request_id, "status": row.status, "payload": row.payload}
    with _memory_lock:
        return session_state.SUBMITTED_ACTIONS.get(idem_key)


def decide(
    *,
    session_id: str,
    workflow_id: str,
    decision: str,
    reviewer_id: str,
    reviewer_role: str,
    note: str | None,
    idem_key: str,
    require_consent: bool,
    enforce_four_eyes: bool = True,
) -> DecisionResult:
    """执行审批决定;带行锁,幂等和四眼校验."""
    if decision not in _VALID_DECISIONS:
        raise RepositoryError(f"invalid decision: {decision}")
    if _is_sql():
        return _decide_sql(
            session_id=session_id,
            workflow_id=workflow_id,
            decision=decision,
            reviewer_id=reviewer_id,
            reviewer_role=reviewer_role,
            note=note,
            idem_key=idem_key,
            require_consent=require_consent,
            enforce_four_eyes=enforce_four_eyes,
        )
    return _decide_memory(
        session_id=session_id,
        workflow_id=workflow_id,
        decision=decision,
        reviewer_id=reviewer_id,
        reviewer_role=reviewer_role,
        note=note,
        idem_key=idem_key,
        require_consent=require_consent,
        enforce_four_eyes=enforce_four_eyes,
    )


def _decide_memory(
    *,
    session_id: str,
    workflow_id: str,
    decision: str,
    reviewer_id: str,
    reviewer_role: str,
    note: str | None,
    idem_key: str,
    require_consent: bool,
    enforce_four_eyes: bool,
) -> DecisionResult:
    with _memory_lock:
        checkpoint = session_state.WORKFLOW_CHECKPOINTS.get((session_id, workflow_id))
        if checkpoint is None:
            raise CheckpointNotFound(workflow_id)
        current_status = _status_of(checkpoint)
        if current_status in _FINAL_STATUSES:
            existing = session_state.APPROVALS.get(workflow_id)
            return DecisionResult(
                checkpoint=checkpoint,
                approval=existing,
                idempotent_replay=True,
                request_id=(session_state.SUBMITTED_ACTIONS.get(idem_key) or {}).get("request_id"),
            )
        if enforce_four_eyes and reviewer_id and reviewer_id == _frozen_user(checkpoint):
            raise SelfApprovalForbidden(workflow_id)
        if require_consent and not checkpoint.get("user_consented_at"):
            raise ConsentRequired(workflow_id)
        updated = _apply_decision_to_checkpoint(
            checkpoint,
            decision=decision,
            reviewer_id=reviewer_id,
            reviewer_role=reviewer_role,
            note=note,
        )
        session_state.WORKFLOW_CHECKPOINTS[(session_id, workflow_id)] = updated
        session_state.APPROVALS[workflow_id] = updated["approval"]
        request_id = f"refund-{workflow_id}" if decision == "approved" else None
        if request_id:
            session_state.SUBMITTED_ACTIONS.setdefault(idem_key, {"request_id": request_id})
        session_state.AUDIT_LOGS.append(
            {
                "actor": reviewer_id,
                "action": f"approval_{decision}",
                "target": workflow_id,
                "after_state": updated["approval"],
            }
        )
        return DecisionResult(
            checkpoint=updated,
            approval=updated["approval"],
            idempotent_replay=False,
            request_id=request_id,
        )


def _decide_sql(
    *,
    session_id: str,
    workflow_id: str,
    decision: str,
    reviewer_id: str,
    reviewer_role: str,
    note: str | None,
    idem_key: str,
    require_consent: bool,
    enforce_four_eyes: bool,
) -> DecisionResult:
    with session_scope() as session:
        row = session.scalar(
            select(WorkflowCheckpoint)
            .where(WorkflowCheckpoint.workflow_id == workflow_id)
            .with_for_update()
        )
        if row is None or not row.payload:
            raise CheckpointNotFound(workflow_id)
        checkpoint = dict(row.payload)
        current_status = _status_of(checkpoint) or row.status
        if current_status in _FINAL_STATUSES:
            existing = session.scalar(
                select(ApprovalRecord)
                .where(ApprovalRecord.workflow_id == workflow_id)
                .order_by(ApprovalRecord.id.desc())
            )
            idem = session.get(IdempotencyRecord, idem_key)
            return DecisionResult(
                checkpoint=checkpoint,
                approval=_approval_dict(existing) if existing else None,
                idempotent_replay=True,
                request_id=idem.request_id if idem else None,
            )
        if enforce_four_eyes and reviewer_id and reviewer_id == _frozen_user(checkpoint):
            raise SelfApprovalForbidden(workflow_id)
        if require_consent and not checkpoint.get("user_consented_at"):
            raise ConsentRequired(workflow_id)
        updated = _apply_decision_to_checkpoint(
            checkpoint,
            decision=decision,
            reviewer_id=reviewer_id,
            reviewer_role=reviewer_role,
            note=note,
        )
        request_id = f"refund-{workflow_id}" if decision == "approved" else None
        session.add(
            ApprovalRecord(
                workflow_id=workflow_id,
                session_id=session_id,
                reviewer_id=reviewer_id,
                reviewer_role=reviewer_role,
                decision=decision,
                note=note,
                snapshot=_jsonable({"frozen_fields": updated.get("frozen_fields"), "workflow": updated.get("workflow")}),
            )
        )
        if request_id:
            session.merge(
                IdempotencyRecord(
                    idem_key=idem_key,
                    scope="workflow_action",
                    request_id=request_id,
                    payload=_jsonable(updated.get("approval")),
                    status="succeeded",
                )
            )
        row.payload = _jsonable(updated)
        row.status = str(_payload_of(updated).get("status") or row.status)
        row.pending_action = _payload_of(updated).get("pending_action")
        row.updated_at = utcnow()
        session.add(
            AuditLog(
                request_id=None,
                actor=reviewer_id,
                action=f"approval_{decision}",
                target=workflow_id,
                before_state=_jsonable({"status": current_status}),
                after_state=_jsonable(updated.get("approval")),
            )
        )
        return DecisionResult(
            checkpoint=updated,
            approval=updated.get("approval"),
            idempotent_replay=False,
            request_id=request_id,
        )


def _approval_dict(row: ApprovalRecord | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "workflow_id": row.workflow_id,
        "reviewer_id": row.reviewer_id,
        "reviewer_role": row.reviewer_role,
        "decision": row.decision,
        "note": row.note,
        "decided_at": row.created_at.isoformat() if row.created_at else None,
    }


def is_resume_token_used(token_hash: str) -> bool:
    key = f"resume-token:{token_hash}"
    if _is_sql():
        with session_scope() as session:
            return session.get(IdempotencyRecord, key) is not None
    now = time.time()
    for item in [item for item, expires_at in session_state.USED_RESUME_TOKENS.items() if expires_at <= now]:
        session_state.USED_RESUME_TOKENS.pop(item, None)
    return key in session_state.USED_RESUME_TOKENS


def mark_resume_token_used(token_hash: str, ttl_seconds: int = 1800) -> None:
    key = f"resume-token:{token_hash}"
    if _is_sql():
        with session_scope() as session:
            session.merge(
                IdempotencyRecord(
                    idem_key=key,
                    scope="resume_token",
                    request_id=None,
                    payload=None,
                    status="used",
                )
            )
        return
    session_state.USED_RESUME_TOKENS[key] = time.time() + ttl_seconds


def clear_resume_tokens() -> None:
    """测试用."""
    session_state.USED_RESUME_TOKENS.clear()
    if _is_sql():
        with session_scope() as session:
            session.query(IdempotencyRecord).filter(IdempotencyRecord.scope == "resume_token").delete()


def audit(
    *,
    actor: str,
    action: str,
    target: str,
    request_id: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    if _is_sql():
        with session_scope() as session:
            session.add(
                AuditLog(
                    request_id=request_id,
                    actor=actor,
                    action=action,
                    target=target,
                    before_state=_jsonable(before) if before else None,
                    after_state=_jsonable(after) if after else None,
                )
            )
        return
    session_state.AUDIT_LOGS.append(
        {
            "request_id": request_id,
            "actor": actor,
            "action": action,
            "target": target,
            "before_state": before,
            "after_state": after,
        }
    )
