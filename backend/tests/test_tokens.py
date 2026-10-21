import time

from workflows import tokens


def _set_hmac(monkeypatch):
    monkeypatch.setenv("AGENT_RESUME_TOKEN_MODE", "hmac")
    monkeypatch.setenv("AGENT_RESUME_SECRET", "test-resume-secret")
    tokens.reset_used_tokens()


def test_hmac_token_round_trip(monkeypatch):
    _set_hmac(monkeypatch)
    token = tokens.issue_resume_token(
        session_id="s-1",
        workflow_id="wf-1",
        order_id="SO20260601090000008-a1000008",
    )
    ok, reason = tokens.verify_resume_token(
        token,
        session_id="s-1",
        workflow_id="wf-1",
        order_id="SO20260601090000008-a1000008",
    )
    assert ok, reason


def test_hmac_token_is_bound_to_order(monkeypatch):
    _set_hmac(monkeypatch)
    token = tokens.issue_resume_token(session_id="s-1", workflow_id="wf-1", order_id="SO-1")
    ok, reason = tokens.verify_resume_token(
        token,
        session_id="s-1",
        workflow_id="wf-1",
        order_id="SO-2",
    )
    assert not ok
    assert reason == "order_mismatch"


def test_tampered_token_is_rejected(monkeypatch):
    _set_hmac(monkeypatch)
    token = tokens.issue_resume_token(session_id="s-1", workflow_id="wf-1", order_id="SO-1")
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    ok, reason = tokens.verify_resume_token(
        tampered,
        session_id="s-1",
        workflow_id="wf-1",
        order_id="SO-1",
    )
    assert not ok
    assert reason == "invalid_signature"


def test_expired_token_is_rejected(monkeypatch):
    _set_hmac(monkeypatch)
    token = tokens.issue_resume_token(session_id="s-1", workflow_id="wf-1", order_id="SO-1", ttl_seconds=60)
    # 直接改 payload 会破坏签名,所以用 monkeypatch 时间不可行;这里构造一个已过期 payload 的签名 token
    import base64
    import json

    body = base64.urlsafe_b64encode(
        json.dumps(
            {"sid": "s-1", "wid": "wf-1", "oid": "SO-1", "exp": int(time.time()) - 1, "nonce": "n"},
            separators=(",", ":"),
        ).encode()
    ).decode().rstrip("=")
    signature = tokens._sign(body, "test-resume-secret")
    expired = f"v1.{body}.{signature}"
    ok, reason = tokens.verify_resume_token(
        expired,
        session_id="s-1",
        workflow_id="wf-1",
        order_id="SO-1",
    )
    assert not ok
    assert reason == "expired"


def test_token_single_use(monkeypatch):
    _set_hmac(monkeypatch)
    token = tokens.issue_resume_token(session_id="s-1", workflow_id="wf-1", order_id="SO-1")
    assert not tokens.is_token_used(token)
    tokens.mark_token_used(token)
    assert tokens.is_token_used(token)


def test_legacy_mode_keeps_compat_token(monkeypatch):
    monkeypatch.setenv("AGENT_RESUME_TOKEN_MODE", "legacy")
    token = tokens.issue_resume_token(session_id="s-1", workflow_id="wf-1", order_id="SO-1")
    assert token == "resume-s-1-wf-1-SO-1"
    ok, reason = tokens.verify_resume_token(token, session_id="s-1", workflow_id="wf-1", order_id="SO-1")
    assert ok and reason == "legacy"
