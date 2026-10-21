"""生产配置校验 -- 启动即失败,避免带着演示开关/默认密钥上线."""

from __future__ import annotations

import os

DEMO_FLAGS = (
    "AGENT_DEMO_MODE",
    "AGENT_OFFLINE_FACTS",
    "AGENT_OFFLINE_RAG",
    "AGENT_DISABLE_LLM",
    "AGENT_BUSINESS_DATE",
)


def app_env() -> str:
    """运行环境:dev / staging / production."""
    return os.getenv("AGENT_ENV", "dev").strip().lower() or "dev"


def approval_mode() -> str:
    """legacy(user confirm 即完成)或 strict(user consent + admin approval)."""
    return os.getenv("AGENT_APPROVAL_MODE", "legacy").strip().lower() or "legacy"


def validate_startup_config() -> None:
    """在 FastAPI 启动时执行;配置不合法直接拒绝启动."""
    env = app_env()
    if env not in {"dev", "staging", "production"}:
        raise RuntimeError(f"AGENT_ENV 不支持: {env}")

    errors: list[str] = []
    auth_mode = os.getenv("AGENT_AUTH_MODE", "off").strip().lower()
    if auth_mode == "hmac" and not os.getenv("AGENT_INTERNAL_SECRET", "").strip():
        errors.append("AGENT_AUTH_MODE=hmac 时必须配置 AGENT_INTERNAL_SECRET")

    if env == "production":
        if auth_mode != "hmac":
            errors.append("生产环境必须设置 AGENT_AUTH_MODE=hmac")
        if not os.getenv("AGENT_OPENAI_API_KEY", "").strip():
            errors.append("生产环境缺少 AGENT_OPENAI_API_KEY")
        if not (os.getenv("ECOMMERCE_BASE_URL") or os.getenv("AGENT_ECOMMERCE_BASE_URL")):
            errors.append("生产环境必须显式配置 ECOMMERCE_BASE_URL")
        cors = os.getenv("AGENT_CORS_ORIGINS", "").strip()
        if not cors or "*" in cors:
            errors.append("生产环境 AGENT_CORS_ORIGINS 必须是精确域名白名单")
        if os.getenv("AGENT_RESUME_TOKEN_MODE", "legacy").strip().lower() != "hmac":
            errors.append("生产环境必须设置 AGENT_RESUME_TOKEN_MODE=hmac")
        if not (os.getenv("AGENT_RESUME_SECRET") or os.getenv("AGENT_INTERNAL_SECRET")):
            errors.append("生产环境必须配置 AGENT_RESUME_SECRET 或 AGENT_INTERNAL_SECRET")
        if os.getenv("AGENT_STRICT_BUSINESS_RECHECK", "0") != "1":
            errors.append("生产环境必须设置 AGENT_STRICT_BUSINESS_RECHECK=1")
        if os.getenv("AGENT_STATE_BACKEND", "memory").strip().lower() != "sql":
            errors.append("生产环境必须设置 AGENT_STATE_BACKEND=sql")
        database_url = os.getenv("AGENT_DATABASE_URL", "").strip().lower()
        if not database_url.startswith("mysql"):
            errors.append("生产环境 AGENT_DATABASE_URL 必须是 MySQL URL")
        if approval_mode() != "strict":
            errors.append("生产环境必须设置 AGENT_APPROVAL_MODE=strict")
        for flag in DEMO_FLAGS:
            if os.getenv(flag, "").strip():
                errors.append(f"生产环境禁止演示/离线开关 {flag}")

    if errors:
        raise RuntimeError("配置校验失败: " + "; ".join(errors))
