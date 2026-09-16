"""
Workflow 路的售后流程 —— 用 LangGraph 状态图固定售后节点顺序。

【这个文件是干什么的】
主链路第 ⑥ 步分派到 Workflow 路后，由本文件执行售后流程。
它用 LangGraph 把售后流程「固化」成状态图，节点按固定顺序执行，不能乱跳。
被 customer_service_agent.chat() 调用（return_request / refund_request 分支）。

【怎么跑起来】
调用链：
    customer_service_agent.chat() → REFUND_APPROVAL_GRAPH.run() 或 RECEIVED_RETURN_GRAPH.run()
      → 状态图节点依次执行 → 最后一个节点生成 checkpoint（等人工审批）

【核心流程：两个图】
  1. RefundApprovalGraph（未发货退款）：
       validate_order_fact → attach_refund_policy → pause_for_human_approval → END
  2. ReceivedReturnGraph（签收后退货）：
       validate_received_order → check_return_window → stop_before_return_submission → END

【关键设计：为什么用状态图 + HITL】
  1. 固定流程：售后 SOP 有严格顺序（先验订单 → 再挂政策 → 最后等审批），
     用 LangGraph 状态图固化节点顺序，模型不能乱跳、不能跳过审批。
  2. HITL：最后一个节点「暂停等人工审批」，高风险资金动作绝不自动执行。
  3. 只做资格判断：ReceivedReturnGraph 只判断「能不能退」，不自动提交退货。
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from api.schemas import ChatRequest, Citation
from tools.runtime_context import logistics_status_from_order, order_status
from workflows.resume import build_refund_workflow_checkpoint


def course_today() -> date:
    """固定基准日期（避免已发布案例随机器日期失效）；可用 AGENT_TODAY 覆盖。"""
    configured = os.getenv("AGENT_TODAY")
    return date.fromisoformat(configured) if configured else date(2026, 7, 15)


# ============ 图 1：未发货退款审批 ============

class RefundApprovalState(TypedDict, total=False):
    """退款审批状态：状态图各节点读写的共享状态。"""
    request: ChatRequest
    order: dict[str, Any] | None
    order_id: str | None
    citations: list[Citation]
    order_fact_status: str
    policy_ids: list[str]
    node_history: list[str]
    workflow: dict[str, Any]


class RefundApprovalGraph:
    """未发货退款：验订单 → 挂政策 → 暂停等人工审批（HITL）。"""

    graph_name = "v1_refund_approval_graph"

    def __init__(self) -> None:
        # 定义状态图：3 个节点 + 固定边（顺序不能乱）
        graph = StateGraph(RefundApprovalState)
        graph.add_node("validate_order_fact", self._validate_order_fact)
        graph.add_node("attach_refund_policy", self._attach_refund_policy)
        graph.add_node("pause_for_human_approval", self._pause_for_human_approval)
        graph.set_entry_point("validate_order_fact")
        graph.add_edge("validate_order_fact", "attach_refund_policy")
        graph.add_edge("attach_refund_policy", "pause_for_human_approval")
        graph.add_edge("pause_for_human_approval", END)
        self.compiled_graph = graph.compile()

    def run(
        self,
        *,
        request: ChatRequest,
        order: dict[str, Any] | None,
        order_id: str | None,
        citations: list[Citation],
    ) -> dict[str, Any]:
        """运行状态图，返回 workflow 摘要。运行前先做资格校验。"""
        eligible, reason = self.assess_eligibility(order)
        if not eligible:
            raise ValueError(reason)
        result = self.compiled_graph.invoke(
            {
                "request": request,
                "order": order,
                "order_id": order_id,
                "citations": citations,
                "node_history": [],
            }
        )
        return result["workflow"]

    @staticmethod
    def assess_eligibility(order: dict[str, Any] | None) -> tuple[bool, str]:
        """资格校验：未发货退款必须满足「订单存在 + 已支付 + 尚未发货」。"""
        if order is None:
            return False, "order_not_verified"
        payment_status = str(order.get("paymentStatus") or order.get("payment_status") or "").upper()
        fulfillment = order_status(order).upper()
        logistics = logistics_status_from_order(order).upper()
        if payment_status and payment_status != "PAID":
            return False, "payment_not_paid"
        if fulfillment not in {"PAID_PENDING_SHIPMENT", "PENDING_SHIPMENT"} or logistics not in {"NOT_SHIPPED", "UNKNOWN"}:
            return False, "order_already_shipped_or_not_eligible"
        return True, "eligible"

    @staticmethod
    def _append_node(state: RefundApprovalState, node: str) -> list[str]:
        """把当前节点追加进 node_history，用于可观测性。"""
        return [*state.get("node_history", []), node]

    def _validate_order_fact(self, state: RefundApprovalState) -> dict[str, Any]:
        """节点1：验订单事实（订单存在才 verified，否则人工复核）。"""
        return {
            "order_fact_status": "verified" if state.get("order") is not None else "manual_review_required",
            "node_history": self._append_node(state, "validate_order_fact"),
        }

    def _attach_refund_policy(self, state: RefundApprovalState) -> dict[str, Any]:
        """节点2：挂上退款政策引用（policy_id）。"""
        policy_ids = [
            str((citation.metadata or {}).get("policy_id"))
            for citation in state.get("citations", [])
            if (citation.metadata or {}).get("policy_id")
        ]
        return {
            "policy_ids": policy_ids,
            "node_history": self._append_node(state, "attach_refund_policy"),
        }

    def _pause_for_human_approval(self, state: RefundApprovalState) -> dict[str, Any]:
        """节点3：生成 checkpoint 并暂停，等人工审批（HITL 关键节点）。"""
        workflow = build_refund_workflow_checkpoint(
            request=state["request"],
            order=state.get("order"),
            order_id=state.get("order_id"),
            citations=state.get("citations", []),
        )
        node_history = self._append_node(state, "pause_for_human_approval")
        workflow.update(
            {
                "used_langgraph": True,
                "graph_name": self.graph_name,
                "current_node": "pause_for_human_approval",
                "node_history": node_history,
                "order_fact_status": state.get("order_fact_status"),
                "policy_ids": state.get("policy_ids", []),
            }
        )
        return {"workflow": workflow, "node_history": node_history}


REFUND_APPROVAL_GRAPH = RefundApprovalGraph()


# ============ 图 2：签收后退货 ============

class ReceivedReturnState(TypedDict, total=False):
    """签收后退货状态。"""
    order: dict[str, Any]
    order_id: str
    eligible: bool
    reason: str
    signed_days: int | None
    return_reason: str | None
    citations: list[Citation]
    policy_ids: list[str]
    node_history: list[str]
    workflow: dict[str, Any]


class ReceivedReturnGraph:
    """签收后退货：验签收 → 查退货窗口 → 只做资格准备，不自动提交退货。"""

    graph_name = "v1_received_return_graph"

    def __init__(self) -> None:
        graph = StateGraph(ReceivedReturnState)
        graph.add_node("validate_received_order", self._validate_received_order)
        graph.add_node("check_return_window", self._check_return_window)
        graph.add_node("stop_before_return_submission", self._stop_before_submission)
        graph.set_entry_point("validate_received_order")
        graph.add_edge("validate_received_order", "check_return_window")
        graph.add_edge("check_return_window", "stop_before_return_submission")
        graph.add_edge("stop_before_return_submission", END)
        self.compiled_graph = graph.compile()

    def run(self, order: dict[str, Any], *, return_reason: str | None, citations: list[Citation]) -> dict[str, Any]:
        result = self.compiled_graph.invoke(
            {
                "order": order,
                "order_id": str(order.get("orderNo") or order.get("order_id")),
                "return_reason": return_reason,
                "citations": citations,
                "node_history": [],
            }
        )
        return result["workflow"]

    @staticmethod
    def _nodes(state: ReceivedReturnState, node: str) -> list[str]:
        return [*state.get("node_history", []), node]

    def _validate_received_order(self, state: ReceivedReturnState) -> dict[str, Any]:
        """节点1：验签收（订单状态必须是已签收/已完成）。"""
        status = order_status(state["order"]).upper()
        received = status in {"DELIVERED", "SIGNED", "COMPLETED"}
        return {
            "eligible": received,
            "reason": "order_received" if received else "order_not_received",
            "node_history": self._nodes(state, "validate_received_order"),
        }

    def _check_return_window(self, state: ReceivedReturnState) -> dict[str, Any]:
        """节点2：查退货窗口（签收时间 + 可退属性 + 7 天窗口 + 退货原因）。"""
        order = state["order"]
        raw_signed_at = order.get("deliveredAt") or order.get("signedAt") or order.get("signed_date")
        signed_days: int | None = None
        if raw_signed_at:
            try:
                signed_days = (course_today() - datetime.fromisoformat(str(raw_signed_at).replace("Z", "+00:00")).date()).days
            except ValueError:
                signed_days = None
        returnable = order.get("returnable")
        if returnable is None:
            item_returnability = [
                item.get("returnable")
                for item in order.get("items", [])
                if isinstance(item, dict)
            ]
            if item_returnability and all(value is True for value in item_returnability):
                returnable = True
            elif any(value is False for value in item_returnability):
                returnable = False
        has_reason = bool(state.get("return_reason"))
        # 资格 = 已签收 + 可退 + 有签收时间 + 7 天内 + 有退货原因
        eligible = bool(state.get("eligible")) and returnable is True and signed_days is not None and 0 <= signed_days <= 7 and has_reason
        # 逐条判断失败原因（用于可观测性 + 话术）
        if not state.get("eligible"):
            reason = "order_not_received"
        elif returnable is False:
            reason = "product_not_returnable"
        elif returnable is not True:
            reason = "product_returnability_unknown"
        elif signed_days is None:
            reason = "signed_time_missing"
        elif signed_days < 0:
            reason = "signed_time_in_future"
        elif signed_days > 7:
            reason = "return_window_expired"
        elif not has_reason:
            reason = "return_reason_missing"
        else:
            reason = "eligible_for_application"
        return {
            "eligible": eligible,
            "reason": reason,
            "signed_days": signed_days,
            "node_history": self._nodes(state, "check_return_window"),
        }

    def _stop_before_submission(self, state: ReceivedReturnState) -> dict[str, Any]:
        """节点3：停在提交前，只生成资格结论，不自动提交退货申请。"""
        eligible = bool(state.get("eligible"))
        nodes = self._nodes(state, "stop_before_return_submission")
        policy_ids = [
            str((citation.metadata or {}).get("policy_id"))
            for citation in state.get("citations", [])
            if (citation.metadata or {}).get("policy_id")
        ]
        workflow = {
            "workflow_id": f"return-{state['order_id']}",
            "workflow_type": "received_return",
            "used_langgraph": True,
            "graph_name": self.graph_name,
            "current_node": "stop_before_return_submission",
            "node_history": nodes,
            "status": "paused" if eligible else "blocked",
            "pending_action": "prepare_return_application" if eligible else "explain_boundary",
            "eligibility_status": "eligible_for_application" if eligible else "not_eligible",
            "eligibility_reason": state.get("reason"),
            "signed_days": state.get("signed_days"),
            "return_reason": state.get("return_reason"),
            "policy_ids": policy_ids,
            "evidence_checklist": ["订单状态", "物流状态", "售后政策依据", "签收时间", "商品可退属性", "退货原因"],
            "needs_human_approval": eligible,
        }
        return {"workflow": workflow, "node_history": nodes}


RECEIVED_RETURN_GRAPH = ReceivedReturnGraph()
