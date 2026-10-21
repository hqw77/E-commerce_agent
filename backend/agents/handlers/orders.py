"""订单、物流、退款进度与商品查询处理."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agents.handlers.refunds import handle_refund
from api.schemas import ChatRequest, Citation, ClarificationRequest, Intent, NextAction, RiskLevel, RoutePlan, ToolCallTrace
from hooks.manager import HookManager
from integrations.ecommerce_client import product_query_keyword
from observability.trace import record_trace_events, trace_store
from rag.knowledge import promotion_policy_result
from tools.langchain_runtime import LangChainToolRunner
from tools.runtime_context import logistics_status_label, order_no, order_status_label
from tools.tool_runtime import get_order_detail, get_order_logistics, get_refund_status, search_products


@dataclass
class ToolQueryOutcome:
    answer: str
    risk_level: RiskLevel = "low"
    next_action: NextAction = "answer_user"
    needs_human_approval: bool = False
    degraded: bool = False
    degradation_reason: str | None = None
    workflow: dict[str, Any] | None = None
    verified_order_id: str | None = None
    verified_product_name: str | None = None
    citations: list[Citation] | None = None
    rag_retrieval: dict[str, Any] | None = None
    rag_rerank: dict[str, Any] | None = None
    tool_calling_state: dict[str, Any] | None = None
    tool_agent_prompt_fragments: list[dict[str, Any]] = field(default_factory=list)
    tool_agent_model_calls: int = 0


def handle_tool_query(
    *,
    request: ChatRequest,
    intent: Intent,
    order_id: str | None,
    clarification: ClarificationRequest | None,
    route_plan: RoutePlan,
    runner: LangChainToolRunner,
    hooks: HookManager,
    citations: list[Citation],
    tool_calls: list[ToolCallTrace],
) -> ToolQueryOutcome:
    """执行订单/物流/退款状态/商品查询,返回本轮结构化结果."""
    tool_result = runner.run(
        request=request,
        intent=intent,
        required_tools=route_plan.required_tools,
        hooks=hooks,
        expected_order_id=order_id,
        expected_product_keyword=product_query_keyword(request.user_message) if intent == "product_query" else None,
    )
    tool_calling_state = tool_result.state
    trace_store.add(
        request.session_id,
        "langchain_tool_agent_completed",
        {
            "session_id": request.session_id,
            "create_agent": tool_calling_state.get("create_agent"),
            "selected_tools": tool_calling_state.get("selected_tools", []),
            "message_types": tool_calling_state.get("message_types", []),
            "fallback_used": tool_calling_state.get("fallback_used"),
            "status": "success" if tool_result.executed else "fallback",
        },
    )

    order = None
    detail_call = None
    if tool_result.executed:
        order = tool_result.order
        tool_calls.extend(tool_result.tool_calls)
        detail_call = tool_calls[0]
        for call in tool_result.tool_calls:
            trace_store.add(
                request.session_id,
                "tool_started",
                {"session_id": request.session_id, "tool_name": call.tool_name, "order_id": order_id},
            )
            trace_store.add(
                request.session_id,
                "tool_finished",
                {
                    "session_id": request.session_id,
                    "tool_name": call.tool_name,
                    "order_id": order_id,
                    "status": call.status,
                    "risk_level": call.risk_level,
                    "next_action": call.next_action,
                },
            )
    elif clarification is None and intent == "product_query":
        hooks.pre_tool_call("search_products", {"keyword": request.user_message}, request.runtime_user_id)
        trace_store.add(
            request.session_id,
            "tool_started",
            {"session_id": request.session_id, "tool_name": "search_products"},
        )
        _, product_call = search_products(request.user_message)
        tool_calls.append(product_call)
        hooks.post_tool_call(product_call)
        trace_store.add(
            request.session_id,
            "tool_finished",
            {
                "session_id": request.session_id,
                "tool_name": product_call.tool_name,
                "status": product_call.status,
                "risk_level": product_call.risk_level,
            },
        )
    elif clarification is None:
        hooks.pre_tool_call("get_order_detail", {"order_id": order_id}, request.runtime_user_id)
        trace_store.add(
            request.session_id,
            "tool_started",
            {"session_id": request.session_id, "tool_name": "get_order_detail", "order_id": order_id},
        )
        order, detail_call = get_order_detail(order_id, request.runtime_user_id, request.runtime_context)
        tool_calls.append(detail_call)
        hooks.post_tool_call(detail_call)
        trace_store.add(
            request.session_id,
            "tool_finished",
            {
                "session_id": request.session_id,
                "tool_name": detail_call.tool_name,
                "order_id": order_id,
                "status": detail_call.status,
                "risk_level": detail_call.risk_level,
                "next_action": detail_call.next_action,
            },
        )

    verified_order_id = order_no(order) if order is not None else None
    verified_product_name = None
    updated_citations: list[Citation] | None = None
    rag_retrieval: dict[str, Any] | None = None
    rag_rerank: dict[str, Any] | None = None
    degraded = False
    degradation_reason: str | None = None
    workflow: dict[str, Any] | None = None

    if clarification:
        answer = clarification.message
        if clarification.candidates:
            choices = "；".join(f"{candidate.value}（{candidate.hint}）" for candidate in clarification.candidates)
            answer = f"{answer} 当前账号下可选订单：{choices}。"
        risk_level: RiskLevel = "medium"
        next_action: NextAction = "ask_clarification"
        needs_human_approval = False
    elif intent == "order_query" and order:
        logistics_call = next((call for call in tool_calls if call.tool_name == "get_order_logistics"), None)
        if logistics_call is None:
            hooks.pre_tool_call("get_order_logistics", {"order_id": order_id}, request.runtime_user_id)
            logistics_call = get_order_logistics(order)
            tool_calls.append(logistics_call)
            hooks.post_tool_call(logistics_call)
            trace_store.add(
                request.session_id,
                "tool_finished",
                {
                    "session_id": request.session_id,
                    "tool_name": logistics_call.tool_name,
                    "order_id": order_id,
                    "status": logistics_call.status,
                    "risk_level": logistics_call.risk_level,
                },
            )
        answer = f"我帮你查到了，订单 {order_no(order)} 目前{order_status_label(order)}，物流状态是{logistics_status_label(order)}。"
        risk_level = "low"
        next_action = "answer_user"
        needs_human_approval = False
    elif intent == "refund_status_query" and order:
        status_call = next((call for call in tool_calls if call.tool_name == "get_refund_status"), None)
        if status_call is None:
            hooks.pre_tool_call("get_refund_status", {"order_id": order_id}, request.runtime_user_id)
            trace_store.add(
                request.session_id,
                "tool_started",
                {"session_id": request.session_id, "tool_name": "get_refund_status", "order_id": order_id},
            )
            status_call = get_refund_status(order, request.runtime_user_id)
            tool_calls.append(status_call)
            hooks.post_tool_call(status_call)
            trace_store.add(
                request.session_id,
                "tool_finished",
                {
                    "session_id": request.session_id,
                    "tool_name": status_call.tool_name,
                    "order_id": order_id,
                    "status": status_call.status,
                    "risk_level": status_call.risk_level,
                },
            )
        answer = status_call.output_summary
        risk_level = status_call.risk_level
        next_action = status_call.next_action or "answer_user"
        needs_human_approval = False
        if status_call.status == "error":
            degraded = True
            degradation_reason = status_call.error_type or "refund_status_unavailable"
    elif intent == "product_query":
        product_call = next((call for call in tool_calls if call.tool_name == "search_products"), None)
        if product_call is None:
            _, product_call = search_products(request.user_message)
            tool_calls.append(product_call)
        if any(term in request.user_message for term in ["活动", "优惠", "满减", "会员"]):
            knowledge_result = promotion_policy_result(request.session_id, request.user_message)
            record_trace_events(request.session_id, knowledge_result.trace_events)
            updated_citations = knowledge_result.citations
            rag_retrieval = knowledge_result.retrieval_debug
            rag_rerank = knowledge_result.rerank
            policy_answer = (
                f"平台通用规则：{knowledge_result.answer} "
                "这不代表该规则一定适用于当前商品活动，具体组合以商品页和结算页为准。"
                if updated_citations
                else "活动规则请以商品页与结算页为准。"
            )
            answer = f"{product_call.output_summary} {policy_answer}"
        else:
            answer = product_call.output_summary
        risk_level = "low" if product_call.status == "success" else "medium"
        next_action = product_call.next_action or "answer_user"
        needs_human_approval = False
        if product_call.status == "success":
            verified_product_name = str(product_call.arguments.get("product_name") or "") or None
    elif intent == "refund_request":
        refund_outcome = handle_refund(
            request=request,
            order=order,
            order_id=order_id,
            citations=citations,
        )
        answer = refund_outcome.answer
        risk_level = refund_outcome.risk_level
        next_action = refund_outcome.next_action
        needs_human_approval = refund_outcome.needs_human_approval
        workflow = refund_outcome.workflow
    else:
        assert detail_call is not None
        answer = detail_call.output_summary
        risk_level = "medium"
        next_action = "ask_clarification" if detail_call.error_type == "missing_order_id" else "transfer_to_human"
        needs_human_approval = False

    return ToolQueryOutcome(
        answer=answer,
        risk_level=risk_level,
        next_action=next_action,
        needs_human_approval=needs_human_approval,
        degraded=degraded,
        degradation_reason=degradation_reason,
        workflow=workflow,
        verified_order_id=verified_order_id,
        verified_product_name=verified_product_name,
        citations=updated_citations,
        rag_retrieval=rag_retrieval,
        rag_rerank=rag_rerank,
        tool_calling_state=tool_calling_state,
        tool_agent_prompt_fragments=tool_result.prompt_fragments,
        tool_agent_model_calls=tool_result.model_calls,
    )
