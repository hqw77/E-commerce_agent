"""服务间鉴权 -- HMAC 签名 + 时间戳 + nonce 防重放.

生产拓扑:商城/网关(BFF)持有内部密钥,对每次调用签名;Agent 只在内网,
校验签名后把用户身份注入请求,不再信任请求体里的 runtime_user_id.

开发模式:AGENT_AUTH_MODE=off(默认),保留本地调试的可用性.
生产模式:AGENT_AUTH_MODE=hmac,必须在启动时配置 AGENT_INTERNAL_SECRET.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request

from api.schemas import ChatRequest, ChatResumeRequest

_MAX_SKEW_SECONDS = 300
_NONCE_TTL_SECONDS = 300
_nonces: "OrderedDict[str, float]" = OrderedDict()
_nonce_lock = threading.Lock()


@dataclass(frozen=True)
class Principal:
    """一次内部调用经过验签后的身份."""

    user_id: str
    role: str = "user"


def auth_mode() -> str:
    """返回鉴权模式:off(本地调试)或 hmac(生产)."""
    return os.getenv("AGENT_AUTH_MODE", "off").strip().lower() or "off"


def internal_secret() -> str:
    """内部签名密钥;生产必须由 Secret Manager/环境变量注入."""
    return os.getenv("AGENT_INTERNAL_SECRET", "").strip()


def signature_payload(*, user_id: str, role: str, timestamp: str, nonce: str, path: str) -> str:
    """签名原文;网关和 Agent 必须使用完全相同的拼接顺序."""
    return f"{user_id}|{role}|{timestamp}|{nonce}|{path}"


def build_signature(*, secret: str, user_id: str, role: str, timestamp: str, nonce: str, path: str) -> str:
    """给网关/测试生成签名."""
    message = signature_payload(user_id=user_id, role=role, timestamp=timestamp, nonce=nonce, path=path)
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def reset_nonce_cache() -> None:
    """测试用:清空 nonce 重放缓存."""
    with _nonce_lock:
        _nonces.clear()


def _remember_nonce(nonce: str, now: float) -> bool:
    """记录 nonce;重复出现返回 False.多副本生产应换成 Redis SETNX."""
    with _nonce_lock:
        while _nonces and now - next(iter(_nonces.values())) > _NONCE_TTL_SECONDS:
            _nonces.popitem(last=False)
        if nonce in _nonces:
            return False
        _nonces[nonce] = now
        return True


def verify_signature(
    *,
    secret: str,
    user_id: str,
    role: str,
    timestamp: str,
    nonce: str,
    path: str,
    signature: str | None,
) -> tuple[bool, str]:
    """校验时间窗,签名和 nonce;返回 (是否通过, 原因)."""
    if not secret:
        return False, "missing_secret"
    try:
        issued_at = int(timestamp)
    except (TypeError, ValueError):
        return False, "invalid_timestamp"
    if abs(int(time.time()) - issued_at) > _MAX_SKEW_SECONDS:
        return False, "timestamp_expired"
    expected = build_signature(
        secret=secret,
        user_id=user_id,
        role=role,
        timestamp=timestamp,
        nonce=nonce,
        path=path,
    )
    if not hmac.compare_digest(expected, signature or ""):
        return False, "invalid_signature"
    # 先验签再记 nonce,避免攻击者用无效签名把正常 nonce 消耗掉.
    if not _remember_nonce(nonce, time.time()):
        return False, "nonce_replayed"
    return True, "ok"


def get_principal(
    request: Request,
    x_agent_user_id: str | None = Header(default=None, alias="X-Agent-User-Id"),
    x_agent_role: str | None = Header(default=None, alias="X-Agent-Role"),
    x_agent_timestamp: str | None = Header(default=None, alias="X-Agent-Timestamp"),
    x_agent_nonce: str | None = Header(default=None, alias="X-Agent-Nonce"),
    x_agent_signature: str | None = Header(default=None, alias="X-Agent-Signature"),
) -> Principal | None:
    """FastAPI 依赖:off 模式返回 None(本地调试),hmac 模式必须验签通过."""
    mode = auth_mode()
    if mode == "off":
        return None
    if mode != "hmac":
        raise HTTPException(status_code=500, detail="unsupported_auth_mode")
    if not all([x_agent_user_id, x_agent_timestamp, x_agent_nonce, x_agent_signature]):
        raise HTTPException(status_code=401, detail="missing_service_credentials")
    # 只使用实际请求路径参与验签;网关注入的 X-Original-Path 不可信,不能替代它.
    path = request.url.path
    ok, reason = verify_signature(
        secret=internal_secret(),
        user_id=x_agent_user_id,
        role=x_agent_role or "user",
        timestamp=x_agent_timestamp,
        nonce=x_agent_nonce,
        path=path,
        signature=x_agent_signature,
    )
    if not ok:
        raise HTTPException(status_code=401, detail=reason)
    return Principal(user_id=x_agent_user_id.strip(), role=(x_agent_role or "user").strip())


def enforce_chat_identity(request: ChatRequest, principal: Principal | None) -> None:
    """把请求体里的用户身份与凭证身份对齐;不一致直接拒绝."""
    if principal is None:
        return
    if request.runtime_user_id and request.runtime_user_id != principal.user_id:
        raise HTTPException(status_code=403, detail="runtime_user_id_mismatch")
    request.runtime_user_id = principal.user_id


def enforce_reviewer_identity(request: ChatResumeRequest, principal: Principal | None) -> None:
    """审批恢复只能由审批角色发起;审批人身份来自凭证,不来自请求体."""
    if principal is None:
        return
    if principal.role not in {"after_sale_manager", "admin"}:
        raise HTTPException(status_code=403, detail="reviewer_role_required")
    request.reviewer_id = principal.user_id
    request.reviewer_role = principal.role


def require_admin(principal: Principal | None = Depends(get_principal)) -> None:
    """管理面接口(评测/反馈/Trace)在 hmac 模式下只允许 admin."""
    if auth_mode() == "off":
        return
    if principal is None or principal.role != "admin":
        raise HTTPException(status_code=403, detail="admin_required")
