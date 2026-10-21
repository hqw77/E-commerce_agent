from __future__ import annotations

import pytest
from sqlalchemy import func, select

from db import repository
from db.database import init_db, reset_engine, session_scope
from db.models import ApprovalRecord, WorkflowCheckpoint


def _checkpoint(session_id: str = "s-1", workflow_id: str = "wf-s-1", user_id: str = "U1001"):
    order_id = "SO20260601090000008-a1000008"
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
            "runtime_user_id": user_id,
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


@pytest.fixture()
def sqlite_state(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'state.db').as_posix()}"
    monkeypatch.setenv("AGENT_STATE_BACKEND", "sql")
    monkeypatch.setenv("AGENT_DATABASE_URL", url)
    reset_engine()
    init_db()
    yield
    reset_engine()


def test_checkpoint_survives_engine_reset(sqlite_state):
    repository.save_checkpoint(_checkpoint())
    reset_engine()
    loaded = repository.load_checkpoint("s-1", "wf-s-1")
    assert loaded is not None
    assert loaded["workflow"]["workflow_id"] == "wf-s-1"
    assert loaded["frozen_fields"]["runtime_user_id"] == "U1001"


def test_consent_requires_owner(sqlite_state):
    repository.save_checkpoint(_checkpoint())
    with pytest.raises(repository.OwnerMismatch):
        repository.record_consent(session_id="s-1", workflow_id="wf-s-1", user_id="U1002")
    updated = repository.record_consent(session_id="s-1", workflow_id="wf-s-1", user_id="U1001")
    assert updated["user_consented_at"]
    assert updated["workflow"]["status"] == "pending_approval"


def test_strict_requires_consent(sqlite_state, monkeypatch):
    monkeypatch.setenv("AGENT_APPROVAL_MODE", "strict")
    repository.save_checkpoint(_checkpoint())
    with pytest.raises(repository.ConsentRequired):
        repository.decide(
            session_id="s-1",
            workflow_id="wf-s-1",
            decision="approved",
            reviewer_id="manager-01",
            reviewer_role="after_sale_manager",
            note=None,
            idem_key="hitl:s-1:order",
            require_consent=True,
        )


def test_four_eyes_blocks_self_approval(sqlite_state):
    repository.save_checkpoint(_checkpoint())
    repository.record_consent(session_id="s-1", workflow_id="wf-s-1", user_id="U1001")
    with pytest.raises(repository.SelfApprovalForbidden):
        repository.decide(
            session_id="s-1",
            workflow_id="wf-s-1",
            decision="approved",
            reviewer_id="U1001",
            reviewer_role="after_sale_manager",
            note=None,
            idem_key="hitl:s-1:order",
            require_consent=False,
        )


def test_approval_is_idempotent_and_persisted(sqlite_state):
    repository.save_checkpoint(_checkpoint())
    repository.record_consent(session_id="s-1", workflow_id="wf-s-1", user_id="U1001")
    first = repository.decide(
        session_id="s-1",
        workflow_id="wf-s-1",
        decision="approved",
        reviewer_id="manager-01",
        reviewer_role="after_sale_manager",
        note="ok",
        idem_key="hitl:s-1:order",
        require_consent=False,
    )
    assert first.idempotent_replay is False
    assert first.checkpoint["workflow"]["status"] == "completed"
    assert first.request_id

    second = repository.decide(
        session_id="s-1",
        workflow_id="wf-s-1",
        decision="approved",
        reviewer_id="manager-02",
        reviewer_role="after_sale_manager",
        note="retry",
        idem_key="hitl:s-1:order",
        require_consent=False,
    )
    assert second.idempotent_replay is True
    assert second.request_id == first.request_id

    with session_scope() as session:
        count = session.scalar(select(func.count()).select_from(ApprovalRecord).where(
            ApprovalRecord.workflow_id == "wf-s-1"
        ))
        row = session.scalar(select(WorkflowCheckpoint).where(WorkflowCheckpoint.workflow_id == "wf-s-1"))
    assert count == 1
    assert row is not None and row.status == "completed"


def test_rejected_is_final(sqlite_state):
    repository.save_checkpoint(_checkpoint())
    result = repository.decide(
        session_id="s-1",
        workflow_id="wf-s-1",
        decision="rejected",
        reviewer_id="manager-01",
        reviewer_role="after_sale_manager",
        note="no",
        idem_key="hitl:s-1:order",
        require_consent=False,
    )
    assert result.checkpoint["workflow"]["status"] == "rejected"
    replay = repository.decide(
        session_id="s-1",
        workflow_id="wf-s-1",
        decision="approved",
        reviewer_id="manager-01",
        reviewer_role="after_sale_manager",
        note="late",
        idem_key="hitl:s-1:order",
        require_consent=False,
    )
    assert replay.idempotent_replay is True
    assert replay.checkpoint["workflow"]["status"] == "rejected"
