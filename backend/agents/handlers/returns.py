"""高风险售后流程:签收后退货."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from api.schemas import ChatRequest, Citation, ClarificationRequest, ToolCallTrace
from hooks.manager import HookManager
from observability.trace import trace_store
from rag.knowledge import RETURN_POLICY
from tools.planning import extract_return_reason
from tools.runtime_context import order_no
from tools.tool_runtime import get_order_detail
from workflows.after_sale_graph import RECEIVED_RETURN_GRAPH


@dataclass
class ReturnOutcome:
    answer: str
    risk_level: str = "high"
    next_action: str = "answer_user"
    needs_human_approval: bool = False
    workflow: dict[str, Any] | None = None
    verified_order_id: str | None = None


def handle_return(
    *,
    request: ChatRequest,
    clarification: ClarificationRequest | None,
    order_id: str | None,
    hooks: HookManager,
    citations: list[Citation],
    tool_calls: list[ToolCallTrace],
) -> ReturnOutcome:
    if clarification:
        return ReturnOutcome(
            answer=clarification.message,
            risk_level="high",
            next_action="ask_clarification",
        )

    hooks.pre_tool_call("get_order_detail", {"order_id": order_id}, request.runtime_user_id)
    trace_store.add(
        request.session_id,
        "tool_started",
        {"session_id": request.session_id, "tool_name": "get_order_detail", "order_id": order_id},
    )
    order, detail_call = get_order_detail(order_id, request.runtime_user_id, request.runtime_context)
    hooks.post_tool_call(detail_call)
    tool_calls.append(detail_call)
    trace_store.add(
        request.session_id,
        "tool_finished",
        {
            "session_id": request.session_id,
            "tool_name": detail_call.tool_name,
            "order_id": order_id,
            "status": detail_call.status,
            "risk_level": detail_call.risk_level,
        },
    )
    if order is None:
        return ReturnOutcome(
            answer=detail_call.output_summary,
            risk_level="high",
            next_action="transfer_to_human",
        )

    verified_order_id = order_no(order)
    citations.append(RETURN_POLICY)
    trace_store.add(
        request.session_id,
        "rag_pre_retrieved",
        {
            "session_id": request.session_id,
            "hit_count": 1,
            "retrieval_stage": "pre_retrieval",
            "policy_id": "return_after_delivery",
        },
    )
    workflow = RECEIVED_RETURN_GRAPH.run(
        order,
        return_reason=extract_return_reason(request.user_message),
        citations=citations,
    )
    eligible = workflow["eligibility_status"] == "eligible_for_application"
    reason_labels = {
        "order_not_received": "订单尚未签收",
        "product_not_returnable": "商品不支持无理由退货",
        "product_returnability_unknown": "暂未核实商品可退属性",
        "signed_time_missing": "缺少可信签收时间",
        "signed_time_in_future": "签收时间晚于当前业务日期，需要核实",
        "return_window_expired": "签收已超过 7 天",
        "return_reason_missing": "还需要补充明确的退货原因",
    }
    answer = (
        f"订单 {order_no(order)} 已根据签收后退货 SOP，完成签收时间、商品可退属性、退货原因和七天窗口检查，"
        "可以准备退货申请；当前还不是退货成功，仍需人工复核。"
        if eligible
        else f"订单 {order_no(order)} 暂不满足签收后退货条件："
        f"{reason_labels.get(workflow['eligibility_reason'], workflow['eligibility_reason'])}。"
    )
    trace_store.add(
        request.session_id,
        "received_return_workflow_completed",
        {"session_id": request.session_id, **workflow},
    )
    return ReturnOutcome(
        answer=answer,
        risk_level="high",
        next_action="transfer_to_human" if eligible else "answer_user",
        needs_human_approval=eligible,
        workflow=workflow,
        verified_order_id=verified_order_id,
    )
