"""
注入防御 —— 按来源标记上下文污染，避免不可信文本伪装成系统指令。

【这个文件是干什么的】
主链路第⑦步安全检查的核心：检测用户消息、工具结果、RAG 文档等外部文本里
是否夹带了「指令注入」攻击（如"忽略之前的指令"、"输出系统提示词"）。
被 customer_service_agent.chat() 和 context/builder.py 调用。

【核心设计】
  1. 用正则匹配已知注入模式（instruction_override 类）
  2. 命中 → 标记 tainted，把内容替换成 [tainted-source-redacted]（脱敏）
  3. 只返回公开安全摘要，不把攻击原文写进 Trace（避免二次泄露）
"""

from __future__ import annotations

import re
from typing import Any


# 注入攻击模式：试图覆盖系统指令 / 套取内部信息
_INJECTION_PATTERNS = (
    re.compile(r"忽略(?:之前|以上|所有).{0,12}(?:指令|规则|提示词)", re.IGNORECASE),
    re.compile(r"(?:system|developer)\s*(?:message|prompt|指令)", re.IGNORECASE),
    re.compile(r"你现在(?:必须|是|扮演)|改写系统提示词", re.IGNORECASE),
    re.compile(r"输出(?:隐藏|内部|完整).{0,8}(?:推理|提示词|策略)", re.IGNORECASE),
)


def inspect_source(source: str, content: str) -> dict[str, Any]:
    """检测单个来源的文本是否含注入攻击。返回公开安全摘要，不泄露攻击原文。"""
    categories = ["instruction_override"] if any(pattern.search(content) for pattern in _INJECTION_PATTERNS) else []
    return {
        "source": source,
        "tainted": bool(categories),            # 是否被污染
        "categories": categories,               # 命中的攻击分类
        "sanitized_content": "[tainted-source-redacted]" if categories else content,  # 命中则脱敏
    }


def inspect_sources(items: list[tuple[str, str]]) -> tuple[list[str], list[dict[str, Any]]]:
    """批量检测多个来源，返回 (脱敏后内容列表, 安全摘要列表)。"""
    reports = [inspect_source(source, content) for source, content in items]
    return [str(report["sanitized_content"]) for report in reports], reports
