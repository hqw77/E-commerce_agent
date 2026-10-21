"""后端入口:本地调试默认关闭 reload,生产用 uvicorn/gunicorn 托管."""

from __future__ import annotations

import os

import uvicorn

from api.routes import app


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=os.getenv("AGENT_HOST", "0.0.0.0"),
        port=int(os.getenv("AGENT_PORT", "8000")),
        reload=os.getenv("AGENT_RELOAD", "0") == "1",
        workers=int(os.getenv("AGENT_WORKERS", "1")),
    )
