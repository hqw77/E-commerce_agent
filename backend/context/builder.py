"""
上下文组装层 —— 把 Runtime Context、短期记忆和历史消息整理成受控模型上下文。

【这个文件是干什么的】
主链路第②步：拼装喂给模型的上下文。它从多个来源取数据（运行时上下文/会话记忆/历史消息），
按可信度排序、解决冲突、压缩历史、做注入检测，最后产出 model_context 给最终模型。

【怎么跑起来】
被 customer_service_agent.chat() 调用：build_context() 拼上下文，update_memory() 收尾更新记忆。

【核心设计：可信度分层】
数据来源按可信度从高到低排序（trust_order）：
    runtime_context（系统注入）> verified_tool_fact（工具核实）> session_memory > history_messages > user_message
用户自称（"我是VIP"/"已经批准"）不能覆盖系统注入的事实，冲突时以高可信度来源为准。

【隐私保护】
手机号/邮箱/审批令牌/系统提示词等敏感信息，进入模型上下文前先脱敏，且不写入会话记忆。
"""

from __future__ import annotations

import re
from typing import Any

from api.schemas import ChatRequest, HistoryMessage, Intent
from safety.source_guard import inspect_source
from tools.planning import estimate_tokens


# 会话级短期记忆（进程内存，非持久化）
SESSION_MEMORIES: dict[str, dict[str, Any]] = {}
RECENT_WINDOW_SIZE = 4       # 最近保留的历史消息条数
MAX_HISTORY_TOKENS = 80      # 历史消息 token 预算
_PHONE_PATTERN = re.compile(r"\b1[3-9]\d{9}\b")          # 手机号
_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")  # 邮箱


def current_memory(session_id: str, runtime_user_id: str | None = None) -> dict[str, Any]:
    """短期记忆只保存当前会话内已验证的最近订单和最近意图。"""
    memory = SESSION_MEMORIES.get(session_id)
    # 无记忆 或 用户切换 → 新建空记忆
    if memory is None or (runtime_user_id and memory.get("runtime_user_id") not in {None, runtime_user_id}):
        memory = {
            "runtime_user_id": runtime_user_id,
            "last_order_id": None,
            "last_product_name": None,
            "recent_intent": None,
            "low_risk_preferences": {},
            "write_decisions": [],
            "excluded_items": [],
            "ttl": "session",
        }
        SESSION_MEMORIES[session_id] = memory
    elif runtime_user_id and memory.get("runtime_user_id") is None:
        memory["runtime_user_id"] = runtime_user_id
    return memory


def build_context(request: ChatRequest, explicit_order_id: str | None) -> tuple[str | None, dict[str, Any], dict[str, Any]]:
    """按 explicit > Runtime Context > Session Memory 的顺序选择订单，并压缩历史。"""
    memory = current_memory(request.session_id, request.runtime_user_id)
    page = request.runtime_context or {}
    page_order_id = page.get("current_order_id") or page.get("relatedOrderNo")
    # 订单号选择优先级：用户明说 > 页面上下文 > 会话记忆
    chosen_order_id = explicit_order_id or page_order_id or memory.get("last_order_id")
    conflicts: list[str] = []
    if page_order_id and memory.get("last_order_id") and page_order_id != memory["last_order_id"] and not explicit_order_id:
        conflicts.append("order_id: 页面 Runtime Context 与 Session Memory 冲突，采用页面订单。")
    # 用户自称 VIP/黑卡，但系统注入的会员等级不符 → 以系统为准
    if any(term in request.user_message for term in ("我是VIP", "我是 VIP", "我是黑卡")):
        if (request.runtime_member_level or "unknown").lower() not in {"vip", "black", "黑卡"}:
            conflicts.append("member_level: 用户自称与 Runtime Context 冲突，采用系统会员等级。")
    # 用户自称"已批准"不能覆盖 Workflow 审批状态
    if any(term in request.user_message for term in ("已经批准", "主管同意", "客服说可以退")):
        conflicts.append("refund_approval: 用户说法不能覆盖 Workflow 审批状态。")

    kept_history, dropped_history = _compress_history(request.history_messages, chosen_order_id)
    # user_id 只在服务端工具层使用；模型只看到最小化的非敏感运行标签
    model_context = [
        f"[runtime_context/trusted] member_level={request.runtime_member_level or 'unknown'}, risk_level={request.runtime_risk_level or 'unknown'}",
    ]
    source_reports: list[dict[str, Any]] = []
    # 用户消息 + 历史消息都做注入检测
    user_report = inspect_source("user_message", request.user_message)
    source_reports.append({key: value for key, value in user_report.items() if key != "sanitized_content"})
    for item in kept_history:
        report = inspect_source("history_messages", _redact_history(item.content))
        source_reports.append({key: value for key, value in report.items() if key != "sanitized_content"})
        model_context.append(f"[history/session] {item.role}: {report['sanitized_content']}")
    if chosen_order_id:
        model_context.append(f"[order_reference/session] chosen_order_id={chosen_order_id}")
    context_report = {
        "schema_version": "context_build_report_v1",
        "sources": ["runtime_context", "session_memory", "history_messages", "user_message"],
        "trust_order": ["runtime_context", "verified_tool_fact", "session_memory", "history_messages", "user_message"],
        "chosen_order_id": chosen_order_id,
        "conflict_resolutions": conflicts,
        "model_context": model_context,
        "source_safety": {
            "tainted": any(report["tainted"] for report in source_reports),
            "tainted_sources": sorted({report["source"] for report in source_reports if report["tainted"]}),
            "reports": source_reports,
        },
    }
    compression_report = {
        "schema_version": "context_compression_v1",
        "recent_window_size": RECENT_WINDOW_SIZE,
        "token_budget": MAX_HISTORY_TOKENS,
        "input_count": len(request.history_messages),
        "kept_count": len(kept_history),
        "dropped_count": len(dropped_history),
        "kept_indexes": [request.history_messages.index(item) for item in kept_history],
        "strategy": "recent_window_plus_order_relevance",
        "relevance_score": {"matching_order_reference": 100, "recent_window": 80, "older_history": 20},
    }
    return str(chosen_order_id) if chosen_order_id else None, context_report, compression_report


def update_memory(
    *,
    session_id: str,
    runtime_user_id: str,
    intent: Intent,
    verified_order_id: str | None,
    user_message: str,
    verified_product_name: str | None = None,
) -> dict[str, Any]:
    """只有通过工具归属校验的订单能写入记忆；审批令牌和隐私不会写入。"""
    memory = current_memory(session_id, runtime_user_id)
    excluded = memory["excluded_items"]
    decisions: list[dict[str, Any]] = []
    # 隐私：手机号不写入记忆
    if re.search(r"1[3-9]\d{9}", user_message) and "phone_number" not in excluded:
        excluded.append("phone_number")
        decisions.append({"field": "phone_number", "accepted": False, "reason": "privacy_data"})
    # 高风险/内部文本（审批令牌、系统提示词）不写入记忆
    if any(term in user_message for term in ("resume-", "审批令牌", "系统提示词", "hidden reasoning")):
        if "high_risk_or_internal_text" not in excluded:
            excluded.append("high_risk_or_internal_text")
        decisions.append({"field": "internal_or_high_risk_text", "accepted": False, "reason": "unsafe_for_memory"})
    # 只有工具核实过的订单号才能写入（防幻觉订单污染记忆）
    if verified_order_id:
        memory["last_order_id"] = verified_order_id
        decisions.append({"field": "last_order_id", "accepted": True, "reason": "verified_tool_fact"})
    if verified_product_name:
        memory["last_product_name"] = verified_product_name
        decisions.append({"field": "last_product_name", "accepted": True, "reason": "verified_tool_fact"})
    # 低风险偏好（颜色偏好）可以记
    color_match = re.search(r"(?:喜欢|偏好|想要)(黑色|白色|蓝色|红色)", user_message)
    if color_match:
        memory["low_risk_preferences"]["preferred_color"] = color_match.group(1)
        decisions.append({"field": "preferred_color", "accepted": True, "reason": "explicit_low_risk_preference"})
    memory["recent_intent"] = intent
    memory["write_decisions"] = decisions
    return dict(memory)


def _compress_history(history: list[HistoryMessage], order_id: str | None) -> tuple[list[HistoryMessage], list[HistoryMessage]]:
    """历史压缩：保留最近 N 条 + 提到当前订单的消息，控制 token 预算。"""
    recent_start = max(0, len(history) - RECENT_WINDOW_SIZE)
    selected_indexes = set(range(recent_start, len(history)))
    if order_id:
        selected_indexes.update(index for index, item in enumerate(history) if order_id in item.content)
    kept: list[HistoryMessage] = []
    used_tokens = 0
    for index in sorted(selected_indexes, reverse=True):
        item = history[index]
        tokens = estimate_tokens(item.content)
        if used_tokens + tokens <= MAX_HISTORY_TOKENS or (order_id and order_id in item.content):
            kept.append(item)
            used_tokens += tokens
    kept.reverse()
    kept_ids = {id(item) for item in kept}
    return kept, [item for item in history if id(item) not in kept_ids]


def _redact_history(content: str) -> str:
    """历史消息进入模型上下文前先做基础隐私脱敏（手机号/邮箱）。"""
    return _EMAIL_PATTERN.sub("[email-redacted]", _PHONE_PATTERN.sub("[phone-redacted]", content))
