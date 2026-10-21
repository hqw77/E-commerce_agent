"""高风险售后流程:未发货退款审批."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from api.schemas import ChatRequest, Citation
from observability.trace import trace_store
from rag.knowledge import REFUND_POLICY
from tools.runtime_context import order_no
from workflows.after_sale_graph import REFUND_APPROVAL_GRAPH


@dataclass
class RefundOutcome:
    answer: str
    risk_level: str = "high"
    next_action: str = "answer_user"
    needs_human_approval: bool = False
    workflow: dict[str, Any] | None = None


def handle_refund(
    *,
    request: ChatRequest,
    order: dict[str, Any] | None,
    order_id: str | None,
    citations: list[Citation],
) -> RefundOutcome:
    citations.append(REFUND_POLICY)
    trace_store.add(
        request.session_id,
        "rag_pre_retrieved",
        {
            "session_id": request.session_id,
            "hit_count": 1,
            "retrieval_stage": "pre_retrieval",
            "policy_id": "refund_before_shipping",
        },
    )
    eligible, eligibility_reason = REFUND_APPROVAL_GRAPH.assess_eligibility(order)
    if not eligible:
        trace_store.add(
            request.session_id,
            "refund_eligibility_blocked",
            {
                "session_id": request.session_id,
                "order_id": order_id,
                "reason": eligibility_reason,
                "status": "blocked",
            },
        )
        answer = (
            "该订单已经发货，不能进入未发货退款审批；请按签收后的退货或售后流程处理。"
            if eligibility_reason == "order_already_shipped_or_not_eligible"
            else "订单事实或支付状态没有通过退款资格校验，暂不能创建退款审批，请转人工核验。"
        )
        return RefundOutcome(answer=answer, risk_level="high", next_action="transfer_to_human")

    workflow = REFUND_APPROVAL_GRAPH.run(
        request=request,
        order=order,
        order_id=order_id,
        citations=citations,
    )
    for node_name in workflow.get("node_history", []):
        trace_store.add(
            request.session_id,
            "workflow_node_finished",
            {
                "session_id": request.session_id,
                "workflow_id": workflow["workflow_id"],
                "graph_name": workflow.get("graph_name"),
                "node_name": node_name,
                "status": "completed",
            },
        )
    trace_store.add(
        request.session_id,
        "workflow_completed",
        {
            "session_id": request.session_id,
            **workflow,
            "risk_level": "high",
            "needs_human_approval": True,
        },
    )
    trace_store.add(
        request.session_id,
        "human_approval_required",
        {
            "session_id": request.session_id,
            "workflow_id": workflow["workflow_id"],
            "pending_action": "require_approval",
            "risk_level": "high",
            "needs_human_approval": True,
        },
    )
    return RefundOutcome(
        answer=f"{order_no(order)} 可以进入未发货退款申请判断，但资金动作必须等待人工审批。",
        risk_level="high",
        next_action="transfer_to_human",
        needs_human_approval=True,
        workflow=workflow,
    )
