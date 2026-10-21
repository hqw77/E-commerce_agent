from __future__ import annotations

import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from api.auth import build_signature
from api.routes import app
from db import repository
from db.database import init_db, reset_engine, session_scope
from db.models import ApprovalRecord


ORDER_ID = "SO20260601090000008-a1000008"


def _chat_payload(session_id: str) -> dict:
    return {
        "session_id": session_id,
        "runtime_user_id": "U1001",
        "runtime_nickname": "张三",
        "runtime_member_level": "gold",
        "runtime_risk_level": "low",
        "user_message": f"{ORDER_ID} 还没发货，我现在能退款吗？",
        "runtime_context": {
            "currentUserOrders": [
                {
                    "orderNo": ORDER_ID,
                    "userId": "U1001",
                    "paymentStatus": "PAID",
                    "fulfillmentStatus": "PAID_PENDING_SHIPMENT",
                    "items": [{"productName": "降噪蓝牙耳机"}],
                }
            ]
        },
    }


def _start_refund(client: TestClient, session_id: str) -> dict:
    response = client.post("/chat", json=_chat_payload(session_id))
    assert response.status_code == 200, response.text
    workflow = response.json()["session_state"]["workflow"]
    assert workflow and workflow["pending_action"] == "require_approval"
    return workflow


@pytest.fixture()
def strict_client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'approval.db').as_posix()}"
    monkeypatch.setenv("AGENT_STATE_BACKEND", "sql")
    monkeypatch.setenv("AGENT_DATABASE_URL", url)
    monkeypatch.setenv("AGENT_APPROVAL_MODE", "strict")
    monkeypatch.setenv("AGENT_RESUME_TOKEN_MODE", "legacy")
    monkeypatch.setenv("AGENT_OFFLINE_RAG", "1")
    monkeypatch.setenv("AGENT_DEMO_MODE", "1")
    monkeypatch.setenv("AGENT_DISABLE_LLM", "1")
    monkeypatch.setenv("AGENT_STRICT_BUSINESS_RECHECK", "0")
    reset_engine()
    init_db()
    with TestClient(app) as client:
        yield client
    reset_engine()


def test_strict_requires_user_consent(strict_client: TestClient):
    workflow = _start_refund(strict_client, "s-no-consent")
    response = strict_client.post(
        f"/admin/approvals/{workflow['workflow_id']}",
        json={"session_id": "s-no-consent", "decision": "approved", "reviewer_id": "manager-01"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["resume_result"]["reason"] == "user_consent_required"


def test_consent_then_admin_approval(strict_client: TestClient):
    workflow = _start_refund(strict_client, "s-approved")
    consent = strict_client.post(
        "/chat/consent",
        json={
            "session_id": "s-approved",
            "workflow_id": workflow["workflow_id"],
            "resume_token": workflow["resume_token"],
            "user_id": "U1001",
        },
    )
    assert consent.status_code == 200, consent.text
    assert consent.json()["status"] == "pending_approval"

    approval = strict_client.post(
        f"/admin/approvals/{workflow['workflow_id']}",
        json={"session_id": "s-approved", "decision": "approved", "reviewer_id": "manager-01", "note": "ok"},
    )
    assert approval.status_code == 200, approval.text
    payload = approval.json()
    assert payload["status"] == "completed"
    assert payload["resume_result"]["accepted"] is True
    assert payload["workflow"]["status"] == "completed"

    stored = repository.load_checkpoint("s-approved", workflow["workflow_id"])
    assert stored and stored["workflow"]["status"] == "completed"

    with session_scope() as session:
        count = session.scalar(
            select(func.count()).select_from(ApprovalRecord).where(
                ApprovalRecord.workflow_id == workflow["workflow_id"]
            )
        )
    assert count == 1

    replay = strict_client.post(
        f"/admin/approvals/{workflow['workflow_id']}",
        json={"session_id": "s-approved", "decision": "approved", "reviewer_id": "manager-02"},
    )
    assert replay.status_code == 200
    assert replay.json()["resume_result"]["idempotent_replay"] is True


def test_requester_cannot_approve_own_workflow(strict_client: TestClient):
    workflow = _start_refund(strict_client, "s-self")
    strict_client.post(
        "/chat/consent",
        json={
            "session_id": "s-self",
            "workflow_id": workflow["workflow_id"],
            "resume_token": workflow["resume_token"],
            "user_id": "U1001",
        },
    )
    response = strict_client.post(
        f"/admin/approvals/{workflow['workflow_id']}",
        json={"session_id": "s-self", "decision": "approved", "reviewer_id": "U1001"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "blocked"
    assert response.json()["resume_result"]["reason"] == "self_approval_forbidden"


def test_consent_rejects_other_user(strict_client: TestClient):
    workflow = _start_refund(strict_client, "s-owner")
    response = strict_client.post(
        "/chat/consent",
        json={
            "session_id": "s-owner",
            "workflow_id": workflow["workflow_id"],
            "resume_token": workflow["resume_token"],
            "user_id": "U1002",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "workflow_owner_mismatch"


def test_pending_approval_list(strict_client: TestClient):
    workflow = _start_refund(strict_client, "s-list")
    listed = strict_client.get("/admin/approvals")
    assert listed.status_code == 200, listed.text
    ids = [item["workflow_id"] for item in listed.json()]
    assert workflow["workflow_id"] in ids

    strict_client.post(
        "/chat/consent",
        json={
            "session_id": "s-list",
            "workflow_id": workflow["workflow_id"],
            "resume_token": workflow["resume_token"],
            "user_id": "U1001",
        },
    )
    strict_client.post(
        f"/admin/approvals/{workflow['workflow_id']}",
        json={"session_id": "s-list", "decision": "approved", "reviewer_id": "manager-01"},
    )
    listed_after = strict_client.get("/admin/approvals")
    assert listed_after.status_code == 200
    assert workflow["workflow_id"] not in [item["workflow_id"] for item in listed_after.json()]


def test_user_workflow_status_follows_consent_and_approval(strict_client: TestClient):
    workflow = _start_refund(strict_client, "s-status")
    first = strict_client.get(f"/chat/workflow/{workflow['workflow_id']}")
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "paused"

    strict_client.post(
        "/chat/consent",
        json={
            "session_id": "s-status",
            "workflow_id": workflow["workflow_id"],
            "resume_token": workflow["resume_token"],
            "user_id": "U1001",
        },
    )
    second = strict_client.get(f"/chat/workflow/{workflow['workflow_id']}")
    assert second.status_code == 200
    assert second.json()["status"] == "pending_approval"

    strict_client.post(
        f"/admin/approvals/{workflow['workflow_id']}",
        json={"session_id": "s-status", "decision": "approved", "reviewer_id": "manager-01"},
    )
    third = strict_client.get(f"/chat/workflow/{workflow['workflow_id']}")
    assert third.status_code == 200
    assert third.json()["status"] == "completed"
    assert third.json()["decision"] == "approved"
    assert third.json()["reviewer_role"] == "after_sale_manager"


def _signed_headers(user_id: str, role: str, path: str, secret: str = "test-secret") -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = uuid4().hex
    signature = build_signature(
        secret=secret,
        user_id=user_id,
        role=role,
        timestamp=timestamp,
        nonce=nonce,
        path=path,
    )
    return {
        "X-Agent-User-Id": user_id,
        "X-Agent-Role": role,
        "X-Agent-Timestamp": timestamp,
        "X-Agent-Nonce": nonce,
        "X-Agent-Signature": signature,
    }


def test_hmac_signed_consent_and_admin_approval(strict_client: TestClient, monkeypatch):
    workflow = _start_refund(strict_client, "s-hmac")
    monkeypatch.setenv("AGENT_AUTH_MODE", "hmac")
    monkeypatch.setenv("AGENT_INTERNAL_SECRET", "test-secret")

    consent_path = "/chat/consent"
    consent = strict_client.post(
        consent_path,
        headers=_signed_headers("U1001", "user", consent_path),
        json={
            "session_id": "s-hmac",
            "workflow_id": workflow["workflow_id"],
            "resume_token": workflow["resume_token"],
        },
    )
    assert consent.status_code == 200, consent.text
    assert consent.json()["status"] == "pending_approval"

    listed = strict_client.get(
        "/admin/approvals",
        headers=_signed_headers("manager-01", "admin", "/admin/approvals"),
    )
    assert listed.status_code == 200, listed.text
    assert workflow["workflow_id"] in [item["workflow_id"] for item in listed.json()]

    approval_path = f"/admin/approvals/{workflow['workflow_id']}"
    approval = strict_client.post(
        approval_path,
        headers=_signed_headers("manager-01", "admin", approval_path),
        json={"session_id": "s-hmac", "decision": "approved"},
    )
    assert approval.status_code == 200, approval.text
    assert approval.json()["status"] == "completed"
    assert approval.json()["resume_result"]["accepted"] is True


def test_rejected_approval_is_final(strict_client: TestClient):
    workflow = _start_refund(strict_client, "s-rejected")
    strict_client.post(
        "/chat/consent",
        json={
            "session_id": "s-rejected",
            "workflow_id": workflow["workflow_id"],
            "resume_token": workflow["resume_token"],
            "user_id": "U1001",
        },
    )
    response = strict_client.post(
        f"/admin/approvals/{workflow['workflow_id']}",
        json={"session_id": "s-rejected", "decision": "rejected", "reviewer_id": "manager-01"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"

    replay = strict_client.post(
        f"/admin/approvals/{workflow['workflow_id']}",
        json={"session_id": "s-rejected", "decision": "approved", "reviewer_id": "manager-01"},
    )
    assert replay.status_code == 200
    assert replay.json()["resume_result"]["idempotent_replay"] is True
    assert replay.json()["workflow"]["status"] == "rejected"


def test_transfer_to_human_creates_deduped_ticket(strict_client: TestClient):
    payload = {
        "session_id": "s-handoff",
        "runtime_user_id": "U1001",
        "runtime_nickname": "张三",
        "runtime_member_level": "gold",
        "runtime_risk_level": "low",
        "user_message": f"SERVICE_TIMEOUT：请帮我查一下订单 {ORDER_ID} 的物流",
    }
    first = strict_client.post("/chat", json=payload)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["session_state"]["next_action"] == "transfer_to_human"
    ticket = body["handoff_ticket"]
    assert ticket is not None
    assert ticket["status"] == "open"
    assert ticket["priority"] == "P2"
    assert ticket["intent"] == "degradation_request"
    assert ticket["order_no"] == ORDER_ID

    second = strict_client.post("/chat", json=payload)
    assert second.status_code == 200
    assert second.json()["handoff_ticket"]["ticket_id"] == ticket["ticket_id"]

    listed = strict_client.get("/admin/handoff-tickets?status=open")
    assert listed.status_code == 200, listed.text
    assert ticket["ticket_id"] in [item["ticket_id"] for item in listed.json()]

    fetched = strict_client.get(f"/admin/handoff-tickets/{ticket['ticket_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["session_id"] == "s-handoff"
