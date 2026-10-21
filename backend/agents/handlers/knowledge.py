"""知识类意图处理:FAQ、活动规则、低置信兜底."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from api.schemas import ChatRequest, Citation, Intent, NextAction, RiskLevel
from observability.trace import record_trace_events
from rag.knowledge import invoice_faq_result, low_confidence_result, promotion_policy_result


@dataclass
class KnowledgeOutcome:
    answer: str
    citations: list[Citation] = field(default_factory=list)
    risk_level: RiskLevel = "low"
    next_action: NextAction = "answer_user"
    needs_human_approval: bool = False
    cache_hit: bool = False
    rag_retrieval: dict[str, Any] | None = None
    rag_rerank: dict[str, Any] | None = None
    resolved_intent: Intent | None = None


def handle_knowledge(intent: Intent, request: ChatRequest) -> KnowledgeOutcome | None:
    """处理 FAQ / 促销 / 低置信意图;非知识意图返回 None."""
    if intent == "low_confidence_query":
        result = low_confidence_result(request.session_id, intent)
        record_trace_events(request.session_id, result.trace_events)
        return KnowledgeOutcome(
            answer=result.answer,
            citations=result.citations,
            risk_level=result.risk_level,
            next_action=result.next_action,
            needs_human_approval=result.needs_human_approval,
        )

    if intent == "faq_query":
        result = invoice_faq_result(request.session_id)
        record_trace_events(request.session_id, result.trace_events)
        return KnowledgeOutcome(
            answer=result.answer,
            citations=result.citations,
            risk_level=result.risk_level,
            next_action=result.next_action,
            needs_human_approval=result.needs_human_approval,
            cache_hit=result.cache_hit,
            rag_retrieval=result.retrieval_debug,
        )

    if intent == "promotion_query":
        result = promotion_policy_result(request.session_id, request.user_message)
        record_trace_events(request.session_id, result.trace_events)
        resolved_intent: Intent = "low_confidence_query" if not result.citations else intent
        return KnowledgeOutcome(
            answer=result.answer,
            citations=result.citations,
            risk_level=result.risk_level,
            next_action=result.next_action,
            needs_human_approval=result.needs_human_approval,
            rag_retrieval=result.retrieval_debug,
            rag_rerank=result.rerank,
            resolved_intent=resolved_intent,
        )

    return None
