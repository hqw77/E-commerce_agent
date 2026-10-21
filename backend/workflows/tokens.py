"""HITL 恢复令牌 -- HMAC 签名,过期,一次性消费.

开发默认 legacy(兼容 token),生产通过 AGENT_RESUME_TOKEN_MODE=hmac 切到签名令牌.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from db import repository

_MAX_TTL_SECONDS = 3600
_DEFAULT_TTL_SECONDS = 1800


def token_mode() -> str:
    """legacy(本地调试)或 hmac(生产)."""
    return os.getenv("AGENT_RESUME_TOKEN_MODE", "legacy").strip().lower() or "legacy"


def token_secret() -> str:
    """优先使用独立令牌密钥;退化到内部服务密钥."""
    return (os.getenv("AGENT_RESUME_SECRET") or os.getenv("AGENT_INTERNAL_SECRET") or "").strip()


def _b64encode(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(body: str) -> dict:
    padded = body + "=" * (-len(body) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    return json.loads(raw.decode("utf-8"))


def _sign(body: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()[:32]


def issue_resume_token(
    *,
    session_id: str,
    workflow_id: str,
    order_id: str | None,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> str:
    """生成一次性恢复令牌:v1.<payload>.<signature>."""
    if token_mode() != "hmac":
        return f"resume-{session_id}-{workflow_id}-{order_id or 'missing'}"
    secret = token_secret()
    if not secret:
        raise RuntimeError("AGENT_RESUME_TOKEN_MODE=hmac 时必须配置 AGENT_RESUME_SECRET 或 AGENT_INTERNAL_SECRET")
    ttl = max(60, min(int(ttl_seconds), _MAX_TTL_SECONDS))
    payload = {
        "sid": session_id,
        "wid": workflow_id,
        "oid": order_id,
        "exp": int(time.time()) + ttl,
        "nonce": secrets.token_urlsafe(8),
    }
    body = _b64encode(payload)
    return f"v1.{body}.{_sign(body, secret)}"


def verify_resume_token(
    token: str,
    *,
    session_id: str,
    workflow_id: str,
    order_id: str | None,
) -> tuple[bool, str]:
    """校验签名,过期和绑定关系;legacy 模式直接放行."""
    if token_mode() != "hmac":
        return True, "legacy"
    secret = token_secret()
    if not secret:
        return False, "missing_secret"
    parts = (token or "").split(".")
    if len(parts) != 3 or parts[0] != "v1":
        return False, "invalid_format"
    body, signature = parts[1], parts[2]
    if not hmac.compare_digest(_sign(body, secret), signature):
        return False, "invalid_signature"
    try:
        payload = _b64decode(body)
    except (ValueError, json.JSONDecodeError):
        return False, "invalid_payload"
    if payload.get("sid") != session_id or payload.get("wid") != workflow_id:
        return False, "subject_mismatch"
    if payload.get("oid") != order_id:
        return False, "order_mismatch"
    if int(payload.get("exp", 0)) < int(time.time()):
        return False, "expired"
    return True, "ok"


def _token_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_token_used(token: str) -> bool:
    """一次性消费检查;sql 模式落 idempotency 表,memory 模式落进程内存."""
    return repository.is_resume_token_used(_token_key(token))


def mark_token_used(token: str, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> None:
    repository.mark_resume_token_used(_token_key(token), ttl_seconds)


def reset_used_tokens() -> None:
    """测试用."""
    repository.clear_resume_tokens()
