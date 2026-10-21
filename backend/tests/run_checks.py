"""无 pytest 环境下的最小自检脚本;有 pytest 时优先 `pytest -q`."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from api.auth import (
    Principal,
    build_signature,
    enforce_chat_identity,
    enforce_reviewer_identity,
    reset_nonce_cache,
    verify_signature,
)
from api.schemas import ChatRequest, ChatResumeRequest
from tools.planning import build_route_plan, classify_guard_intent, extract_order_id
from workflows import tokens as resume_tokens
from workflows.after_sale_graph import REFUND_APPROVAL_GRAPH


def check(name: str, fn) -> None:
    fn()
    print(f"PASS {name}")


def test_planning() -> None:
    assert extract_order_id("查 SO20260602103000009-a1000009 物流") == "SO20260602103000009-a1000009"
    assert extract_order_id("没有订单号") is None
    assert classify_guard_intent("我要退款") == "refund_request"
    assert classify_guard_intent("我想申请退货") == "return_request"
    plan = build_route_plan(intent="order_query", user_message="物流", order_id=None, model_used=False)
    assert plan.fallback_policy == "ask_order_id"
    assert plan.required_tools == []


def test_auth() -> None:
    reset_nonce_cache()
    ts = str(int(time.time()))
    nonce = "n-check"
    sig = build_signature(secret="s", user_id="U1001", role="user", timestamp=ts, nonce=nonce, path="/chat")
    ok, reason = verify_signature(
        secret="s", user_id="U1001", role="user", timestamp=ts, nonce=nonce, path="/chat", signature=sig
    )
    assert ok, reason
    ok, reason = verify_signature(
        secret="s", user_id="U1001", role="user", timestamp=ts, nonce=nonce, path="/chat", signature=sig
    )
    assert not ok and reason == "nonce_replayed"

    req = ChatRequest(session_id="s", runtime_user_id="U1002", user_message="hi")
    try:
        enforce_chat_identity(req, Principal(user_id="U1001"))
        raise AssertionError("expected 403")
    except HTTPException as exc:
        assert exc.status_code == 403
    enforce_chat_identity(req, Principal(user_id="U1002"))
    assert req.runtime_user_id == "U1002"

    resume = ChatResumeRequest(
        session_id="s", workflow_id="wf", resume_token="t",
        reviewer_id="spoof", reviewer_role="user", decision="approved",
    )
    enforce_reviewer_identity(resume, Principal(user_id="manager-01", role="after_sale_manager"))
    assert resume.reviewer_id == "manager-01"
    assert resume.reviewer_role == "after_sale_manager"


def test_workflow() -> None:
    ok, reason = REFUND_APPROVAL_GRAPH.assess_eligibility(
        {"paymentStatus": "PAID", "fulfillmentStatus": "PAID_PENDING_SHIPMENT"}
    )
    assert ok and reason == "eligible"
    blocked, blocked_reason = REFUND_APPROVAL_GRAPH.assess_eligibility(
        {"paymentStatus": "PAID", "fulfillmentStatus": "SHIPPED", "logisticsStatus": "IN_TRANSIT"}
    )
    assert not blocked and blocked_reason == "order_already_shipped_or_not_eligible"


def test_resume_tokens() -> None:
    import os

    old_mode = os.environ.get("AGENT_RESUME_TOKEN_MODE")
    old_secret = os.environ.get("AGENT_RESUME_SECRET")
    os.environ["AGENT_RESUME_TOKEN_MODE"] = "hmac"
    os.environ["AGENT_RESUME_SECRET"] = "check-secret"
    resume_tokens.reset_used_tokens()
    try:
        token = resume_tokens.issue_resume_token(session_id="s", workflow_id="wf", order_id="SO-1")
        ok, reason = resume_tokens.verify_resume_token(token, session_id="s", workflow_id="wf", order_id="SO-1")
        assert ok, reason
        ok, reason = resume_tokens.verify_resume_token(token, session_id="s", workflow_id="wf", order_id="SO-2")
        assert not ok and reason == "order_mismatch"
    finally:
        if old_mode is None:
            os.environ.pop("AGENT_RESUME_TOKEN_MODE", None)
        else:
            os.environ["AGENT_RESUME_TOKEN_MODE"] = old_mode
        if old_secret is None:
            os.environ.pop("AGENT_RESUME_SECRET", None)
        else:
            os.environ["AGENT_RESUME_SECRET"] = old_secret
        resume_tokens.reset_used_tokens()


def test_api() -> None:
    from fastapi.testclient import TestClient
    from api.routes import app

    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.headers.get("X-Request-Id")


def test_hmac_guard() -> None:
    import os

    from fastapi.testclient import TestClient
    from api.routes import app

    old_mode = os.environ.get("AGENT_AUTH_MODE")
    old_secret = os.environ.get("AGENT_INTERNAL_SECRET")
    os.environ["AGENT_AUTH_MODE"] = "hmac"
    os.environ["AGENT_INTERNAL_SECRET"] = "check-secret"
    try:
        with TestClient(app) as client:
            response = client.post(
                "/chat",
                json={"session_id": "s", "runtime_user_id": "U1001", "user_message": "hi"},
            )
            assert response.status_code == 401
            response = client.post("/eval/run", json={})
            assert response.status_code == 401
    finally:
        if old_mode is None:
            os.environ.pop("AGENT_AUTH_MODE", None)
        else:
            os.environ["AGENT_AUTH_MODE"] = old_mode
        if old_secret is None:
            os.environ.pop("AGENT_INTERNAL_SECRET", None)
        else:
            os.environ["AGENT_INTERNAL_SECRET"] = old_secret


def test_production_guard() -> None:
    import os

    from config.production import validate_startup_config

    old_env = os.environ.get("AGENT_ENV")
    old_mode = os.environ.get("AGENT_AUTH_MODE")
    old_secret = os.environ.get("AGENT_INTERNAL_SECRET")
    os.environ["AGENT_ENV"] = "production"
    os.environ["AGENT_AUTH_MODE"] = "off"
    os.environ.pop("AGENT_INTERNAL_SECRET", None)
    try:
        try:
            validate_startup_config()
            raise AssertionError("expected RuntimeError")
        except RuntimeError:
            pass
    finally:
        for key, value in (
            ("AGENT_ENV", old_env),
            ("AGENT_AUTH_MODE", old_mode),
            ("AGENT_INTERNAL_SECRET", old_secret),
        ):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    check("planning", test_planning)
    check("auth", test_auth)
    check("workflow", test_workflow)
    check("resume_tokens", test_resume_tokens)
    check("api", test_api)
    check("hmac_guard", test_hmac_guard)
    check("production_guard", test_production_guard)
    print("all checks passed")
