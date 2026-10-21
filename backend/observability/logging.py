"""请求级结构化日志 -- JSON 输出 + Request ID 上下文."""

from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid

from fastapi import Request

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def current_request_id() -> str:
    """当前请求的 Request ID;没有上下文时返回 '-'."""
    return _request_id.get()


def set_request_id(value: str) -> contextvars.Token[str]:
    return _request_id.set(value)


def reset_request_id(token: contextvars.Token[str]) -> None:
    _request_id.reset(token)


class JsonFormatter(logging.Formatter):
    """把日志压成一行 JSON,方便 Loki/ELK 采集."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "method", "path", "status", "duration_ms", "client"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    """进程启动时调用一次;保留 uvicorn 自己的 access log 也无妨."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


async def request_logging_middleware(request: Request, call_next):
    """绑定 Request ID,记录耗时与状态码;异常仍交给全局 handler."""
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
    token = set_request_id(request_id)
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-Id"] = request_id
        return response
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logging.getLogger("agent.request").info(
            "http_request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": status,
                "duration_ms": duration_ms,
                "client": request.client.host if request.client else None,
            },
        )
        reset_request_id(token)
