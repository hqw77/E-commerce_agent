"""Agent 编排层 —— 整个项目的「大脑」。

【这个文件是干什么的】
这是整个 Agent 的唯一调度中心：它不自己实现 RAG、Tool、Workflow,而是把这些能力「编排」起来——
先判断用户意图，再按意图分发到不同模块处理，最后统一做安全检查、生成话术、记录 Trace 和成本。
读这个文件的诀窍:chat() 就是一个巨大的 if/elif 分派表，看懂「什么意图走什么路」就看懂了大半。

【怎么跑起来】
本文件不直接运行，它是 HTTP 服务的核心。调用链：
    POST /chat → api/routes.py chat() → 本文件的 Lesson41Agent.chat()
启动整个服务:cd backend && python main.py（入口在 main.py）

【核心流程：chat() 的 9 步】
  ① 意图识别（规则兜底 → 模型判断 → guard 覆盖，双保险）
  ② 上下文组装（抽订单号 + 拼 Runtime Context + Session Memory）
  ③ 生成路由计划 RoutePlan（决定走 RAG / Tool / Workflow 哪条路）
  ④ 初始化输出容器（citations / tool_calls / workflow）
  ⑤ 记初始 Trace
  ⑥ 按意图分发（核心决策表：RAG / Tool / Workflow / 拦截 / 兜底）
  ⑦ 安全检查（Tool/RAG 外部文本防注入）
  ⑧ 生成最终话术
  ⑨ 收尾记录（记忆 + 钩子 + 成本 + Trace）
"""

from __future__ import annotations

import os
from typing import Any

# 从这 30 行 import 就能看出整个项目的模块划分——每个 import 对应一个能力模块：
#  - api / config        : 接口结构 + 配置（加载 course.env、模型地址等）
#  - models              : 两个模型客户端（路由模型 + 最终回答模型）
#  - tools               : 工具层（意图分类、路由计划、实际查订单/物流/退款）
#  - rag / workflows     : 知识检索 + 售后工作流（LangGraph）
#  - context / state     : 上下文组装 + 会话记忆
#  - safety / hooks      : 注入防御 + 工具调用钩子
#  - observability / cost: Trace 记录 + 成本治理
from api.schemas import *
from config.settings import load_course_env
from context.builder import SESSION_MEMORIES, build_context, update_memory
from cost.governance import build_cost_summary
from hooks.manager import HookManager
from integrations.ecommerce_client import product_query_keyword
from mcp_catalog.catalog import MCP_CATALOG
from models.answer_client import FinalAnswerModelClient, FinalAnswerModelResult
from models.router_client import ModelRouteResult, RouteModelClient
from observability.trace import public_trace_summary, record_initial_chat_trace, record_trace_events, trace_store
from rag.knowledge import REFUND_POLICY, RETURN_POLICY, invoice_faq_result, low_confidence_result, promotion_policy_result
from safety.source_guard import inspect_source
from state.session_state import COMMON_HIT_CACHE, MESSAGE_COUNT_BY_SESSION
from tools.langchain_runtime import LangChainToolRunner
from tools.planning import (
    build_order_clarification,
    build_route_plan,
    classify_guard_intent,
    classify_intent,
    estimate_tokens,
    extract_order_id,
    extract_return_reason,
)
from tools.runtime_context import (
    general_chat_answer,
    is_runtime_identity_query,
    logistics_status_label,
    order_no,
    order_status_label,
    runtime_context_summary,
    runtime_identity_answer,
)
from tools.tool_runtime import get_order_detail, get_order_logistics, get_refund_status, search_products
from workflows.after_sale_graph import RECEIVED_RETURN_GRAPH, REFUND_APPROVAL_GRAPH
from workflows.resume import resume_from_checkpoint

class Lesson41Agent:
    """综合演练版：复用前面能力，服务大促综合演练和项目验证。"""

    def __init__(self) -> None:
        # 初始化三个"能力对象"（都是无状态的客户端/运行器，方法里现用现调）：
        #  1. route_model_client  —— 用模型判断意图、生成路由（决定走 RAG 还是 Tool）
        #  2. answer_model_client —— 用模型把检索/工具结果"润色"成最终客服话术
        #  3. langchain_tool_runner —— 用 LangChain 框架驱动工具调用（选工具、调工具）
        self.route_model_client = RouteModelClient()
        self.answer_model_client = FinalAnswerModelClient()
        self.langchain_tool_runner = LangChainToolRunner()

    def chat(self, request: ChatRequest) -> ChatResponse:
        """编排综合演练聊天链路，串联 Tool、RAG、Workflow/HITL、降级、安全、Trace 和成本治理。"""

        # ============ chat() 完整流程（面试讲架构就讲这个）============
        # 1. 意图识别：规则兜底 → 模型判断 → 规则 guard 覆盖（双保险）
        # 2. 上下文组装：提取订单号、拼 Runtime Context + Session Memory
        # 3. 生成路由计划 RoutePlan：决定走 RAG / Tool / Workflow 哪条路
        # 4. 按意图分发（核心）：下面的 if/elif 就是"决策表"
        # 5. 安全检查：Tool/RAG 拿到的外部文本做注入检测
        # 6. 生成最终话术：某些边界场景跳过模型（安全/降级/缓存命中）
        # 7. 收尾记录：写记忆、执行钩子、算成本、记 Trace
        # 8. 返回 ChatResponse：把所有中间证据打包给前端
        load_course_env()
        MESSAGE_COUNT_BY_SESSION[request.session_id] = MESSAGE_COUNT_BY_SESSION.get(request.session_id, 0) + 1
        message_count = MESSAGE_COUNT_BY_SESSION[request.session_id]
        # ① 意图识别（三步双保险，防止模型判断失误）：
        #   a. classify_intent：先用规则快速判断一个"兜底意图"（不花钱、快）
        #   b. plan_intent：再用真实模型判断意图（更准，但要花钱/花时间）
        #   c. _apply_route_guard：明确的边界规则覆盖模型结果（如明显注入就强制拦截）
        fallback_intent = classify_intent(request.user_message)
        route_result = self.route_model_client.plan_intent(request.user_message, fallback_intent=fallback_intent)
        route_result = self._apply_route_guard(
            user_message=request.user_message,
            route_result=route_result,
        )
        intent = route_result.intent
        # ② 上下文组装：提取订单号 + 拼上下文（Runtime Context + Session Memory）
        explicit_order_id = extract_order_id(request.user_message)
        order_id, context_report, compression_report = build_context(request, explicit_order_id)

        # ③ 生成路由计划：RoutePlan 声明"这次需要哪些工具/要不要 RAG/要不要工作流"
        route_was_guarded = str(route_result.fallback_reason or "").startswith("rule_guard_")
        route_plan = build_route_plan(
            intent=intent,
            user_message=request.user_message,
            order_id=order_id,
            model_used=route_result.used_model and not route_was_guarded,
        )
        # 运行时上下文（会员等级、风险等级、页面来源）和"是不是在问身份类问题"
        runtime_context = runtime_context_summary(request)
        uses_runtime_identity = is_runtime_identity_query(request.user_message)
        # ④ 初始化本轮的"输出容器"：后面每个分支往里面填，最后统一打包返回
        #   citations  = 知识引用（答案的依据）
        #   tool_calls = 工具调用记录（查了什么、结果如何）
        #   workflow   = 售后工作流结果（如果走了 Workflow 分支）
        #   degraded   = 是否降级（业务服务挂了）
        citations: list[Citation] = []
        tool_calls: list[ToolCallTrace] = []
        workflow: dict[str, Any] | None = None
        cache_hit = False
        degraded = False
        degradation_reason: str | None = None
        rag_rerank: dict[str, Any] | None = None
        rag_retrieval: dict[str, Any] | None = None
        verified_order_id: str | None = None
        verified_product_name: str | None = None
        clarification = build_order_clarification(request, route_plan)
        hooks = HookManager()
        tool_calling_state: dict[str, Any] = {
            "create_agent": False,
            "skip_reason": "route_does_not_need_business_tools",
            "available_tools": route_plan.required_tools,
            "selected_tools": [],
            "message_types": [],
            "tool_message_count": 0,
            "fallback_used": False,
        }
        tool_agent_prompt_fragments: list[dict[str, Any]] = []
        tool_agent_model_calls = 0

        # ⑤ 开始记 Trace：把这次请求的初始信息（意图、上下文、token 估算）先落一份公开日志
        record_initial_chat_trace(
            session_id=request.session_id,
            runtime_user_id=request.runtime_user_id,
            runtime_nickname=request.runtime_nickname,
            runtime_member_level=request.runtime_member_level,
            runtime_risk_level=request.runtime_risk_level,
            intent=intent,
            estimated_tokens=estimate_tokens(request.user_message),
            route_result=route_result,
            context_report=context_report,
            compression_report=compression_report,
        )
        trace_store.add(
            request.session_id,
            "route_plan_built",
            {
                "session_id": request.session_id,
                "intent": route_plan.intent,
                "source": route_plan.source,
                "required_tools": route_plan.required_tools,
                "needs_rag": route_plan.needs_rag,
                "needs_business_tools": route_plan.needs_business_tools,
                "requires_workflow": route_plan.requires_workflow,
                "risk_level": route_plan.risk_level,
            },
        )
        if clarification:
            trace_store.add(
                request.session_id,
                "tool_clarification_required",
                {
                    "session_id": request.session_id,
                    "clarification_field": clarification.clarification_field,
                    "candidate_count": len(clarification.candidates),
                    "candidate_order_ids": [candidate.value for candidate in clarification.candidates],
                    "status": "waiting_for_user",
                },
            )

        # ============ ⑥ 核心：按意图分发（面试必背的"决策表"）============
        # 意图(intent)             → 走哪条路
        # ─────────────────────────────────────────────
        # uses_runtime_identity    → 直接答（"我是谁/会员等级"这类身份问题）
        # degradation_request      → 降级（业务服务不可用，转人工）
        # security_request         → 拦截（检测到 prompt 注入，拒绝回答）
        # low_confidence_query     → 兜底（知识库没可靠答案，不硬答）
        # faq_query                → RAG（发票等固定 FAQ，带缓存）
        # promotion_query          → RAG（活动/优惠规则检索 + 重排）
        # return_request           → Workflow（签收后退货 SOP）
        # order/refund/product 等  → Tool（查 Java 后端的订单/物流/退款/商品）
        # 其他                     → 闲聊兜底
        if uses_runtime_identity:
            answer = runtime_identity_answer(request)
            risk_level = "low"
            next_action = "answer_user"
            needs_human_approval = False
            trace_store.add(
                request.session_id,
                "runtime_identity_answered",
                {
                    "session_id": request.session_id,
                    "intent": intent,
                    "runtime_user_id": request.runtime_user_id,
                    "used_runtime_context": True,
                },
            )
        elif intent == "degradation_request":
            degraded = True
            degradation_reason = "business_tool_unavailable"
            degradation_source = (
                "explicit_course_fault_injection"
                if "故障注入演示" in request.user_message
                else "user_reported_service_failure"
            )
            trace_store.add(
                request.session_id,
                "degradation_triggered",
                {
                    "session_id": request.session_id,
                    "intent": intent,
                    "degraded": True,
                    "reason": degradation_reason,
                    "source": degradation_source,
                },
            )
            answer = "订单或物流服务暂时不可用，本轮不继续猜测业务事实。建议稍后重试，或转人工客服继续核验。"
            risk_level = "medium"
            next_action = "transfer_to_human"
            needs_human_approval = False
        elif intent == "security_request":
            trace_store.add(
                request.session_id,
                "prompt_security_blocked",
                {"session_id": request.session_id, "intent": intent, "risk_level": "high", "status": "blocked"},
            )
            answer = "我不能提供受保护系统信息、受保护推理摘要、工具细节或内部策略。"
            risk_level: RiskLevel = "high"
            next_action: NextAction = "answer_user"
            needs_human_approval = False
        elif intent == "low_confidence_query":
            knowledge_result = low_confidence_result(request.session_id, intent)
            record_trace_events(request.session_id, knowledge_result.trace_events)
            answer = knowledge_result.answer
            citations = knowledge_result.citations
            risk_level = knowledge_result.risk_level
            next_action = knowledge_result.next_action
            needs_human_approval = knowledge_result.needs_human_approval
        elif intent == "faq_query":
            # RAG 路线：从知识库检索发票 FAQ，结果带 citations（引用）和缓存标记
            knowledge_result = invoice_faq_result(request.session_id)
            record_trace_events(request.session_id, knowledge_result.trace_events)
            answer = knowledge_result.answer
            citations = knowledge_result.citations
            risk_level = knowledge_result.risk_level
            next_action = knowledge_result.next_action
            needs_human_approval = knowledge_result.needs_human_approval
            cache_hit = knowledge_result.cache_hit
            rag_retrieval = knowledge_result.retrieval_debug
        elif intent == "promotion_query":
            # RAG 路线（促销规则）：混合检索 + 重排；没检索到就降级为低置信
            knowledge_result = promotion_policy_result(request.session_id, request.user_message)
            if not knowledge_result.citations:
                intent = "low_confidence_query"
            record_trace_events(request.session_id, knowledge_result.trace_events)
            answer = knowledge_result.answer
            citations = knowledge_result.citations
            rag_rerank = knowledge_result.rerank
            rag_retrieval = knowledge_result.retrieval_debug
            risk_level = knowledge_result.risk_level
            next_action = knowledge_result.next_action
            needs_human_approval = knowledge_result.needs_human_approval
        elif intent == "return_request":
            # Workflow 路线：签收后退货。先查订单，再跑 LangGraph 工作流判断资格
            if clarification:
                answer = clarification.message
                risk_level = "high"
                next_action = "ask_clarification"
                needs_human_approval = False
            else:
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
                    answer = detail_call.output_summary
                    risk_level = "high"
                    next_action = "transfer_to_human"
                    needs_human_approval = False
                else:
                    verified_order_id = order_no(order)
                    return_reason = extract_return_reason(request.user_message)
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
                    workflow = RECEIVED_RETURN_GRAPH.run(order, return_reason=return_reason, citations=citations)
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
                        f"订单 {order_no(order)} 已根据跨境电商公司签收后退货 SOP，完成签收时间、商品可退属性、退货原因和七天窗口检查，可以准备退货申请；当前还不是退货成功，仍需人工复核。"
                        if eligible
                        else f"订单 {order_no(order)} 暂不满足签收后退货条件：{reason_labels.get(workflow['eligibility_reason'], workflow['eligibility_reason'])}。"
                    )
                    risk_level = "high"
                    next_action = "transfer_to_human" if eligible else "answer_user"
                    needs_human_approval = eligible
                    trace_store.add(request.session_id, "received_return_workflow_completed", {"session_id": request.session_id, **workflow})
        elif intent in {"order_query", "refund_status_query", "refund_request", "product_query"}:
            # Tool 路线：需要查真实业务数据（订单/物流/退款/商品），用 LangChain 工具运行器调用
            tool_result = self.langchain_tool_runner.run(
                request=request,
                intent=intent,
                required_tools=route_plan.required_tools,
                hooks=hooks,
                expected_order_id=order_id,
                expected_product_keyword=product_query_keyword(request.user_message) if intent == "product_query" else None,
            )
            tool_calling_state = tool_result.state
            tool_agent_prompt_fragments = tool_result.prompt_fragments
            tool_agent_model_calls = tool_result.model_calls
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
                products, product_call = search_products(request.user_message)
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
                order = None
                detail_call = None
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
            else:
                order = None
                detail_call = None
            if order is not None:
                verified_order_id = order_no(order)
            if clarification:
                answer = clarification.message
                if clarification.candidates:
                    choices = "；".join(f"{candidate.value}（{candidate.hint}）" for candidate in clarification.candidates)
                    answer = f"{answer} 当前账号下可选订单：{choices}。"
                risk_level = "medium"
                next_action = "ask_clarification"
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
                    products, product_call = search_products(request.user_message)
                    tool_calls.append(product_call)
                if any(term in request.user_message for term in ["活动", "优惠", "满减", "会员"]):
                    knowledge_result = promotion_policy_result(request.session_id, request.user_message)
                    record_trace_events(request.session_id, knowledge_result.trace_events)
                    citations = knowledge_result.citations
                    rag_retrieval = knowledge_result.retrieval_debug
                    rag_rerank = knowledge_result.rerank
                    policy_answer = (
                        f"平台通用规则：{knowledge_result.answer} "
                        "这不代表该规则一定适用于当前商品活动，具体组合以商品页和结算页为准。"
                        if citations
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
                # 高风险边界：退款只做"资格判断"，不执行资金动作，必须过人工审批
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
                    workflow = None
                    answer = (
                        "该订单已经发货，不能进入未发货退款审批；请按签收后的退货或售后流程处理。"
                        if eligibility_reason == "order_already_shipped_or_not_eligible"
                        else "订单事实或支付状态没有通过退款资格校验，暂不能创建退款审批，请转人工核验。"
                    )
                    risk_level = "high"
                    next_action = "transfer_to_human"
                    needs_human_approval = False
                    trace_store.add(
                        request.session_id,
                        "refund_eligibility_blocked",
                        {"session_id": request.session_id, "order_id": order_id, "reason": eligibility_reason, "status": "blocked"},
                    )
                else:
                    workflow = REFUND_APPROVAL_GRAPH.run(
                        request=request,
                        order=order,
                        order_id=order_id,
                        citations=citations,
                    )
                if workflow is None:
                    pass
                else:
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
                    answer = f"{order_no(order)} 可以进入未发货退款申请判断，但资金动作必须等待人工审批。"
                    risk_level = "high"
                    next_action = "transfer_to_human"
                    needs_human_approval = True
            else:
                answer = detail_call.output_summary
                risk_level = "medium"
                next_action = "ask_clarification" if detail_call.error_type == "missing_order_id" else "transfer_to_human"
                needs_human_approval = False
        else:
            # 兜底：既不是知识问题也不是业务问题，走通用闲聊回答
            answer = general_chat_answer(request.user_message)
            risk_level = "low"
            next_action = "answer_user"
            needs_human_approval = False

        # ============ ⑦ 安全检查（横切能力，包裹在主流程外面）============
        # Tool/RAG 文本也是外部数据，进入最终模型前必须按来源做污染检查。
        # 为什么要检查：外部数据（商品描述、订单备注、知识文档）可能被塞进恶意指令，
        # 直接喂给模型会被"提示注入"。这里标记污染并做脱敏/隔离。
        external_reports: list[dict[str, Any]] = []
        for call in tool_calls:
            report = inspect_source("tool_result", call.output_summary)
            if report["tainted"]:
                call.output_summary = str(report["sanitized_content"])
            external_reports.append({key: value for key, value in report.items() if key != "sanitized_content"})
        for citation in citations:
            report = inspect_source("rag_document", citation.snippet)
            if report["tainted"]:
                citation.snippet = str(report["sanitized_content"])
            external_reports.append({key: value for key, value in report.items() if key != "sanitized_content"})
        if external_reports:
            source_safety = context_report["source_safety"]
            source_safety["reports"].extend(external_reports)
            source_safety["tainted"] = source_safety["tainted"] or any(report["tainted"] for report in external_reports)
            source_safety["tainted_sources"] = sorted(
                set(source_safety["tainted_sources"])
                | {report["source"] for report in external_reports if report["tainted"]}
            )
            trace_store.add(
                request.session_id,
                "context_source_safety_checked",
                {
                    "session_id": request.session_id,
                    "tainted": source_safety["tainted"],
                    "tainted_sources": source_safety["tainted_sources"],
                    "source_count": len(source_safety["reports"]),
                },
            )

        if rag_retrieval:
            trace_store.add(
                request.session_id,
                "rag_hybrid_retrieved",
                {
                    "session_id": request.session_id,
                    "mode": rag_retrieval.get("mode"),
                    "rewritten_query": (rag_retrieval.get("plan") or {}).get("rewritten_query"),
                    "index_version": rag_retrieval.get("index_version"),
                    "index_chunk_count": rag_retrieval.get("index_chunk_count"),
                    "index_cache_hit": rag_retrieval.get("index_cache_hit"),
                    "retrieval_cache_hit": rag_retrieval.get("retrieval_cache_hit"),
                    "vector_policy_ids": rag_retrieval.get("vector_policy_ids", []),
                    "keyword_policy_ids": rag_retrieval.get("keyword_policy_ids", []),
                    "hit_count": len(citations),
                    "retrieval_stage": "hybrid_retrieval",
                },
            )

        # ============ ⑧ 生成最终话术 ============
        # 把前面拿到的 answer（可能来自 Tool/RAG/Workflow/兜底）交给最终模型润色，
        # 但安全/降级/缓存命中/澄清这些边界场景会跳过模型（保留确定性，见 _compose_final_answer）
        model_answer = self._compose_final_answer(
            request=request,
            intent=intent,
            answer=answer,
            risk_level=risk_level,
            next_action=next_action,
            tool_calls=tool_calls,
            citations=citations,
            workflow=workflow,
            cache_hit=cache_hit,
            degraded=degraded,
            skip_final_model=uses_runtime_identity,
            enable_reasoning=request.reasoning_view == "teaching",
            context_report=context_report,
        )
        answer = model_answer.answer
        if (
            intent == "faq_query"
            and not cache_hit
            and (model_answer.used_model or os.getenv("AGENT_DISABLE_LLM") == "1")
        ):
            # 在线缓存模型基于证据生成的最终话术；离线测试则缓存显式兜底话术。
            COMMON_HIT_CACHE["faq:invoice_issue"] = {
                "answer": answer,
                "citation": "invoice_issue",
                "source": "model_final_answer" if model_answer.used_model else "explicit_offline_fallback",
            }
        reasoning_content = model_answer.reasoning_content if request.reasoning_view == "teaching" else None
        prompt_fragments = [
            *route_result.prompt_fragments,
            *tool_agent_prompt_fragments,
            *model_answer.prompt_fragments,
        ]
        selected_mcp_tool = next((call.tool_name for call in reversed(tool_calls) if call.status == "success"), None)
        mcp_binding = MCP_CATALOG.binding_summary(selected_mcp_tool, risk_level)
        trace_store.add(request.session_id, "mcp_binding_resolved", {"session_id": request.session_id, **mcp_binding})

        if prompt_fragments:
            trace_store.add(
                request.session_id,
                "prompt_context_built",
                {
                    "session_id": request.session_id,
                    "registry_schema": "prompt_registry_v1",
                    "selected_fragments": prompt_fragments,
                    "prompt_body_exposed": False,
                },
            )

        # ============ ⑨ 收尾记录（都不影响主流程，是"横切能力"）============
        #   - update_memory      : 把本轮订单/商品写进会话记忆，下次能想起来
        #   - hooks.on_completion : 完成钩子（安全摘要等）
        #   - build_cost_summary  : 统计这轮花了多少 token/钱
        memory = update_memory(
            session_id=request.session_id,
            runtime_user_id=request.runtime_user_id,
            intent=intent,
            verified_order_id=verified_order_id,
            user_message=request.user_message,
            verified_product_name=verified_product_name,
        )
        hook_completion = hooks.on_completion(risk_level=risk_level, next_action=next_action, degraded=degraded)
        for hook_event in hooks.events:
            trace_store.add(request.session_id, "hook_executed", {"session_id": request.session_id, **hook_event})
        cost_summary = build_cost_summary(
            request=request,
            intent=intent,
            tool_calls=tool_calls,
            citations=citations,
            workflow=workflow,
            answer=answer,
            cache_hit=cache_hit,
            route_model_used=route_result.used_model,
            answer_model_used=model_answer.used_model,
            reasoning_content_returned=bool(reasoning_content),
            reasoning_source=model_answer.reasoning_source,
            degraded=degraded,
            degradation_reason=degradation_reason,
            prompt_fragments=prompt_fragments,
            tool_agent_model_calls=tool_agent_model_calls,
        )
        trace_store.add(
            request.session_id,
            "cost_recorded",
            cost_summary,
        )
        trace_store.add(
            request.session_id,
            "final_answer_generated",
            {
                "session_id": request.session_id,
                "intent": intent,
                "status": "success",
                "risk_level": risk_level,
                "used_model": model_answer.used_model,
                "reasoning_content_returned": bool(reasoning_content),
            },
        )

        return ChatResponse(
            session_id=request.session_id,
            answer=answer,
            citations=citations,
            tool_calls=tool_calls,
            clarification=clarification,
            reasoning_summary=[
                "Trace 记录的是公开执行摘要：Runtime Context、Context、Tool、RAG、Workflow/HITL、Hooks 和 Cost。",
                "tool_calls 与 citations 是可观察证据，不是 hidden CoT。",
                "教学模式会尝试展示主链路最终模型返回的 reasoning_content；系统提示词、密钥、隐私原文和内部堆栈不会写入公开 trace。",
            ],
            reasoning_content=reasoning_content,
            session_state={
                "agent_version": "v1",
                "message_count": message_count,
                "intent": intent,
                "model": {
                    "route_planner": {
                        "used_model": route_result.used_model,
                        "model_name": route_result.model_name,
                        "fallback_reason": route_result.fallback_reason,
                        "prompt_fragments": route_result.prompt_fragments,
                    },
                    "final_answer": {
                        "used_model": model_answer.used_model,
                        "model_name": model_answer.model_name,
                        "fallback_reason": model_answer.fallback_reason,
                        "prompt_fragments": model_answer.prompt_fragments,
                    },
                },
                "prompt_registry": {
                    "schema_version": "prompt_registry_v1",
                    "selected_fragments": prompt_fragments,
                    "selected_fragment_ids": [fragment["name"] for fragment in prompt_fragments],
                    "prompt_body_exposed": False,
                },
                "route_plan": route_plan.model_dump(),
                "tool_calling": {
                    **tool_calling_state,
                    "clarification": clarification.model_dump() if clarification else None,
                },
                "mcp": mcp_binding,
                "frameworks": {
                    "langchain": {
                        "used": route_result.used_model or model_answer.used_model,
                        "route_chain": route_result.framework,
                        "final_answer_chain": model_answer.framework,
                        "prompt_registry": "prompts/prompt_registry.yml",
                        "selected_fragment_ids": [fragment["name"] for fragment in prompt_fragments],
                        "create_agent": bool(tool_calling_state.get("create_agent")),
                    },
                    "langgraph": {
                        "used": bool(workflow and workflow.get("used_langgraph")),
                        "graph_name": workflow.get("graph_name") if workflow else None,
                        "current_node": workflow.get("current_node") if workflow else None,
                        "node_history": workflow.get("node_history", []) if workflow else [],
                    },
                },
                "risk_level": risk_level,
                "next_action": next_action,
                "needs_human_approval": needs_human_approval,
                "runtime_context": runtime_context,
                "memory": memory,
                "context_report": context_report,
                "compression_report": compression_report,
                "hook_events": hooks.events,
                "hook_completion": hook_completion,
                "workflow": workflow,
                "rag": {
                    "low_confidence": intent == "low_confidence_query",
                    "hit_count": len(citations),
                    "citation_ids": [citation.metadata.get("policy_id") for citation in citations if citation.metadata],
                    "rerank_mode": rag_rerank["mode"] if rag_rerank else None,
                    "reranked_policy_ids": rag_rerank["policy_ids"] if rag_rerank else [],
                    "rerank_scores": rag_rerank["scores"] if rag_rerank else {},
                    "rerank_reasons": rag_rerank["reasons"] if rag_rerank else {},
                    "retrieval_mode": rag_retrieval.get("mode") if rag_retrieval else None,
                    "rewritten_query": (rag_retrieval.get("plan") or {}).get("rewritten_query") if rag_retrieval else None,
                    "index_version": rag_retrieval.get("index_version") if rag_retrieval else None,
                    "index_chunk_count": rag_retrieval.get("index_chunk_count") if rag_retrieval else 0,
                    "index_cache_hit": rag_retrieval.get("index_cache_hit") if rag_retrieval else False,
                    "retrieval_cache_hit": rag_retrieval.get("retrieval_cache_hit") if rag_retrieval else False,
                    "vector_policy_ids": rag_retrieval.get("vector_policy_ids", []) if rag_retrieval else [],
                    "keyword_policy_ids": rag_retrieval.get("keyword_policy_ids", []) if rag_retrieval else [],
                    "source_scores": rag_retrieval.get("source_scores", {}) if rag_retrieval else {},
                    "embedding": rag_retrieval.get("embedding") if rag_retrieval else None,
                },
                "degraded": degraded,
                "cost_summary": cost_summary,
                "trace": public_trace_summary(request.session_id),
                "next_gap": "用同一阶段 Agent 做大促综合演练，并把证据整理成项目表达。",
            },
        )

    def _compose_final_answer(
        self,
        *,
        request: ChatRequest,
        intent: Intent,
        answer: str,
        risk_level: str,
        next_action: str,
        tool_calls: list[ToolCallTrace],
        citations: list[Citation],
        workflow: dict[str, Any] | None,
        cache_hit: bool,
        degraded: bool,
        skip_final_model: bool = False,
        enable_reasoning: bool = False,
        context_report: dict[str, Any],
    ) -> FinalAnswerModelResult:
        """让真实模型生成最终话术，但安全、低置信、降级和缓存命中保留确定性边界。

        关键设计：不是所有情况都调用模型。以下情况"跳过模型"，直接返回确定性 answer：
          - runtime identity / 澄清 / 安全 / 低置信 / 降级 / 缓存命中
        为什么跳过：这些场景答案必须确定、可控、不花模型钱，交给模型反而可能胡说。
        """
        skip_reason: str | None = None
        if skip_final_model:
            skip_reason = "runtime_context_direct_answer"
        elif next_action == "ask_clarification":
            skip_reason = "clarification_required"
        elif intent in {"security_request", "low_confidence_query"}:
            skip_reason = "safety_or_low_confidence_boundary"
        elif degraded:
            skip_reason = "degraded_path"
        elif cache_hit:
            skip_reason = "common_hit_cache"
        if skip_reason:
            result = FinalAnswerModelResult(answer=answer, fallback_reason=skip_reason)
            trace_store.add(
                request.session_id,
                "model_answer_skipped",
                {"session_id": request.session_id, "intent": intent, "reason": skip_reason},
            )
            return result

        result = self.answer_model_client.compose_answer(
            request=request,
            intent=intent,
            deterministic_answer=answer,
            risk_level=risk_level,
            next_action=next_action,
            tool_calls=tool_calls,
            citations=citations,
            workflow=workflow,
            enable_reasoning=enable_reasoning,
            model_context=context_report["model_context"],
        )
        trace_store.add(
            request.session_id,
            "model_answer_generated",
            {
                "session_id": request.session_id,
                "intent": intent,
                "used_model": result.used_model,
                "model_name": result.model_name,
                "fallback_reason": result.fallback_reason,
            },
        )
        return result

    @staticmethod
    def _apply_route_guard(
        *,
        user_message: str,
        route_result: ModelRouteResult,
    ) -> ModelRouteResult:
        """仅让明确边界覆盖模型，宽泛关键词只用于模型不可用时的 fallback。

        这是"规则 guard"：模型判断可能有误，但某些输入（如明显的注入攻击、明确的
        高风险词）必须被规则强制覆盖，不能交给模型决定。
        """
        guard_intent = classify_guard_intent(user_message)
        if guard_intent is None or route_result.intent == guard_intent:
            return route_result
        return ModelRouteResult(
            intent=guard_intent,
            used_model=route_result.used_model,
            model_name=route_result.model_name,
            fallback_reason=f"rule_guard_{guard_intent}",
            framework=route_result.framework,
            prompt_fragments=route_result.prompt_fragments,
        )

    def resume(self, request: ChatResumeRequest) -> ChatResumeResponse:
        """恢复暂停的 HITL workflow，具体校验和幂等由 workflow 层负责。

        售后流程跑到"等人工审批"时会被挂起，用户/管理员通过 /chat/resume 恢复。
        这里只转发给 workflow 层，校验和幂等（防止重复审批）都在 workflows/resume.py。
        """
        return resume_from_checkpoint(request, trace_store)
