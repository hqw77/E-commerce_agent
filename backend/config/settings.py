"""
配置中心 —— 集中处理能力清单、cases 路径、course.env 和模型地址。

【这个文件是干什么的】
整个项目的配置入口：读环境变量（course.env）、能力清单、模型地址/名称、Java 后端地址。
其它模块通过 `from config.settings import ...` 获取配置，不直接读环境变量（解耦）。

【怎么用】
各模块调用这些函数拿配置：openai_base_url()/openai_model_name()（模型）、
ecommerce_base_url()（Java 后端）、load_course_env()（加载 course.env 到环境变量）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


CAPABILITIES_PATH = Path(__file__).resolve().parents[1] / "agent_capabilities.json"  # 能力清单
CASES_PATH = Path(__file__).resolve().parents[1] / "cases.yml"  # 评测用例
DEFAULT_COURSE_ENV_PATH = Path(__file__).resolve().parents[2] / "course.env"  # 环境变量文件（项目根目录下）
DEFAULT_ECOMMERCE_BASE_URL = "http://127.0.0.1:8081"  # Java 电商后端默认地址
TRACE_SCHEMA_VERSION = "trace_event_v1"  # Trace schema 版本


def api_key_is_missing(api_key: str | None) -> bool:
    """判断环境里的模型 Key 是否仍是空值或占位值（未配置真实 key）。"""
    if not api_key:
        return True
    normalized = api_key.strip()
    return normalized in {
        "",
        "你的模型平台 Key",
        "your-api-key",
        "your_api_key",
        "YOUR_API_KEY",
        "sk-your-api-key",
        "sk-xxx",
        "替换成你的真实Key",
    }


def load_agent_capabilities() -> dict[str, Any]:
    """读取当前能力清单，让前端和文档知道是综合演练版。"""
    with CAPABILITIES_PATH.open(encoding="utf-8") as file:
        return json.load(file)


def load_course_env() -> Path | None:
    """加载 course.env，把里面的 KEY=VALUE 注入环境变量（已存在的环境变量不覆盖）。"""
    env_path = Path(os.getenv("AGENT_ENV", str(DEFAULT_COURSE_ENV_PATH))).expanduser()
    if not env_path.exists():
        return None
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        # setdefault：不覆盖 shell 里已显式设置的同名变量
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    return env_path


def ecommerce_base_url() -> str:
    """返回 Java 电商后端地址，避免工具层直接读环境变量。"""
    return os.getenv("ECOMMERCE_BASE_URL", os.getenv("AGENT_ECOMMERCE_BASE_URL", DEFAULT_ECOMMERCE_BASE_URL)).rstrip("/")


def openai_base_url() -> str:
    """返回 OpenAI 兼容模型服务地址（默认硅基流动）。"""
    return os.getenv("AGENT_OPENAI_BASE_URL", "https://api.siliconflow.cn/v1").rstrip("/")


def openai_model_name() -> str:
    """返回客服 Agent 默认使用的聊天模型名称。"""
    return os.getenv("AGENT_OPENAI_MODEL", "Qwen/Qwen3-8B")


def embedding_model_name() -> str:
    """返回知识检索使用的真实 Embedding 模型名称。"""
    return os.getenv("AGENT_EMBEDDING_MODEL", "BAAI/bge-m3")
