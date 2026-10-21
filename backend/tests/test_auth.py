import time

import pytest
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

SECRET = "test-secret"


def _signed_headers(*, user_id: str = "U1001", role: str = "user", path: str = "/chat", nonce: str = "n-1"):
    timestamp = str(int(time.time()))
    signature = build_signature(
        secret=SECRET,
        user_id=user_id,
        role=role,
        timestamp=timestamp,
        nonce=nonce,
        path=path,
    )
    return timestamp, nonce, signature


def _chat_request(user_id: str = "U1001") -> ChatRequest:
    return ChatRequest(session_id="s-1", runtime_user_id=user_id, user_message="你好")


def test_valid_signature_passes():
    reset_nonce_cache()
    timestamp, nonce, signature = _signed_headers()
    ok, reason = verify_signature(
        secret=SECRET,
        user_id="U1001",
        role="user",
        timestamp=timestamp,
        nonce=nonce,
        path="/chat",
        signature=signature,
    )
    assert ok, reason


def test_replayed_nonce_is_rejected():
    reset_nonce_cache()
    timestamp, nonce, signature = _signed_headers(nonce="n-replay")
    verify_signature(
        secret=SECRET,
        user_id="U1001",
        role="user",
        timestamp=timestamp,
        nonce=nonce,
        path="/chat",
        signature=signature,
    )
    ok, reason = verify_signature(
        secret=SECRET,
        user_id="U1001",
        role="user",
        timestamp=timestamp,
        nonce=nonce,
        path="/chat",
        signature=signature,
    )
    assert not ok
    assert reason == "nonce_replayed"


def test_expired_timestamp_is_rejected():
    reset_nonce_cache()
    old = str(int(time.time()) - 3600)
    signature = build_signature(
        secret=SECRET,
        user_id="U1001",
        role="user",
        timestamp=old,
        nonce="n-old",
        path="/chat",
    )
    ok, reason = verify_signature(
        secret=SECRET,
        user_id="U1001",
        role="user",
        timestamp=old,
        nonce="n-old",
        path="/chat",
        signature=signature,
    )
    assert not ok
    assert reason == "timestamp_expired"


def test_chat_identity_mismatch_is_forbidden():
    with pytest.raises(HTTPException) as exc:
        enforce_chat_identity(_chat_request("U1002"), Principal(user_id="U1001"))
    assert exc.value.status_code == 403


def test_empty_identity_is_filled_by_principal():
    request = _chat_request("")
    enforce_chat_identity(request, Principal(user_id="U1001"))
    assert request.runtime_user_id == "U1001"


def test_reviewer_role_is_required():
    request = ChatResumeRequest(
        session_id="s-1",
        workflow_id="wf-1",
        resume_token="token",
        reviewer_id="U1001",
        reviewer_role="user",
        decision="approved",
    )
    with pytest.raises(HTTPException) as exc:
        enforce_reviewer_identity(request, Principal(user_id="U1001", role="user"))
    assert exc.value.status_code == 403


def test_reviewer_identity_is_overridden_by_principal():
    request = ChatResumeRequest(
        session_id="s-1",
        workflow_id="wf-1",
        resume_token="token",
        reviewer_id="spoofed",
        reviewer_role="user",
        decision="approved",
    )
    enforce_reviewer_identity(request, Principal(user_id="manager-01", role="after_sale_manager"))
    assert request.reviewer_id == "manager-01"
    assert request.reviewer_role == "after_sale_manager"
