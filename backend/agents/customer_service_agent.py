# Agent 编排层.

from __future__ import annotations

import os
from typing import Any

# 模块边界:
#  - api / config        : 接口契约、运行配置
#  - models              : 路由模型 + 最终回答模型
#  - tools               : 意图分类、路由计划、订单/物流/退款/商品工具
#  - rag / workflows     : 知识检索 + 售后工作流
#  - context / state     : 上下文组装 + 会话记忆
#  - safety / hooks      : 注入防御 + 工具调用钩子
#  - observability / cost: Trace 记录 + 成本治理
from agents.handlers.knowledge import handle_knowledge
from agents.handlers.orders import handle_tool_query
from agents.handlers.refunds import handle_refund
from agents.handlers.returns import handle_return
from agents.routing import apply_route_guard
from agents.safety_pipeline import sanitize_external_sources
from api.schemas import *
from config.settings import load_agent_env
from context.builder import SESSION_MEMORIES, build_context, update_memory
from cost.governance import build_cost_summary
from db.repository import create_handoff_ticket
from hooks.manager import HookManager
from mcp_catalog.catalog import MCP_CATALOG
from models.answer_client import FinalAnswerModelClient, FinalAnswerModelResult
from models.router_client import RouteModelClient
from observability.trace import public_trace_summary, record_initial_chat_trace, trace_store
from state.session_state import COMMON_HIT_CACHE, MESSAGE_COUNT_BY_SESSION
from tools.langchain_runtime import LangChainToolRunner
from tools.planning import (
    build_order_clarification,
    build_route_plan,
    classify_intent,
    estimate_tokens,
    extract_order_id,
)
from tools.runtime_context import (
    general_chat_answer,
    is_runtime_identity_query,
    runtime_context_summary,
    runtime_identity_answer,
)
from workflows.resume import resume_from_checkpoint

class AfterSaleAgent:
    """售后客服 Agent:复用工具、RAG、Workflow 能力."""

    def __init__(self) -> None:
        # 初始化三个"能力对象"(都是无状态的客户端/运行器,方法里现用现调):
        #  1. route_model_client  -- 用模型判断意图,生成路由(决定走 RAG 还是 Tool)
        #  2. answer_model_client -- 用模型把检索/工具结果"润色"成最终客服话术
        #  3. langchain_tool_runner -- 用 LangChain 框架驱动工具调用(选工具,调工具)
        self.route_model_client = RouteModelClient()
        self.answer_model_client = FinalAnswerModelClient()
        self.langchain_tool_runner = LangChainToolRunner()

    def chat(self, request: ChatRequest) -> ChatResponse:
        """编排售后客服聊天链路,串联 Tool,RAG,Workflow/HITL,降级,安全,Trace 和成本治理."""

        # chat() 主流程:
        # 1. 意图识别:规则兜底 -> 模型判断 -> 规则 guard 覆盖
        # 2. 上下文组装:提取订单号,拼 Runtime Context + Session Memory
        # 3. 生成路由计划 RoutePlan:决定走 RAG / Tool / Workflow 哪条路
        # 4. 按意图分发:下面的 if/elif 是执行决策表
        # 5. 安全检查:Tool/RAG 拿到的外部文本做注入检测
        # 6. 生成最终话术:边界场景跳过模型(安全/降级/缓存命中)
        # 7. 收尾记录:写记忆,执行钩子,算成本,记 Trace
        # 8. 返回 ChatResponse:把所有中间证据打包给前端
        load_agent_env()
        MESSAGE_COUNT_BY_SESSION[request.session_id] = MESSAGE_COUNT_BY_SESSION.get(request.session_id, 0) + 1
        message_count = MESSAGE_COUNT_BY_SESSION[request.session_id]
        # ① 意图识别(三步双保险,防止模型判断失误):
        #   a. classify_intent:先用规则快速判断一个"兜底意图"(不花钱,快)
        #   b. plan_intent:再用真实模型判断意图(更准,但要花钱/花时间)
        #   c. apply_route_guard:明确的边界规则覆盖模型结果(如明显注入就强制拦截)
        fallback_intent = classify_intent(request.user_message)
        route_result = self.route_model_client.plan_intent(request.user_message, fallback_intent=fallback_intent)
        route_result = apply_route_guard(
            user_message=request.user_message,
            route_result=route_result,
        )
        intent = route_result.intent
        # ② 上下文组装:提取订单号 + 拼上下文(Runtime Context + Session Memory)
        explicit_order_id = extract_order_id(request.user_message)
        order_id, context_report, compression_report = build_context(request, explicit_order_id)

        # ③ 生成路由计划:RoutePlan 声明"这次需要哪些工具/要不要 RAG/要不要工作流"
        route_was_guarded = str(route_result.fallback_reason or "").startswith("rule_guard_")
        route_plan = build_route_plan(
            intent=intent,
            user_message=request.user_message,
            order_id=order_id,
            model_used=route_result.used_model and not route_was_guarded,
        )
        # 运行时上下文(会员等级,风险等级,页面来源)和"是不是在问身份类问题"
        runtime_context = runtime_context_summary(request)
        uses_runtime_identity = is_runtime_identity_query(request.user_message)
        # ④ 初始化本轮的"输出容器":后面每个分支往里面填,最后统一打包返回
        #   citations  = 知识引用(答案的依据)
        #   tool_calls = 工具调用记录(查了什么,结果如何)
        #   workflow   = 售后工作流结果(如果走了 Workflow 分支)
        #   degraded   = 是否降级(业务服务挂了)
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

        # ⑤ 开始记 Trace:把这次请求的初始信息(意图,上下文,token 估算)先落一份公开日志
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

        # ============ ⑥ 核心:按意图分发(面试必背的"决策表")============
        # 意图(intent)             -> 走哪条路
        # ─────────────────────────────────────────────
        # uses_runtime_identity    -> 直接答("我是谁/会员等级"这类身份问题)
        # degradation_request      -> 降级(业务服务不可用,转人工)
        # security_request         -> 拦截(检测到 prompt 注入,拒绝回答)
        # low_confidence_query     -> 兜底(知识库没可靠答案,不硬答)
        # faq_query                -> RAG(发票等固定 FAQ,带缓存)
        # promotion_query          -> RAG(活动/优惠规则检索 + 重排)
        # return_request           -> Workflow(签收后退货 SOP)
        # order/refund/product 等  -> Tool(查 Java 后端的订单/物流/退款/商品)
        # 其他                     -> 闲聊兜底
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
                "demo_fault_injection"
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
        elif intent in {"low_confidence_query", "faq_query", "promotion_query"}:
            knowledge_outcome = handle_knowledge(intent, request)
            assert knowledge_outcome is not None
            intent = knowledge_outcome.resolved_intent or intent
            answer = knowledge_outcome.answer
            citations = knowledge_outcome.citations
            risk_level = knowledge_outcome.risk_level
            next_action = knowledge_outcome.next_action
            needs_human_approval = knowledge_outcome.needs_human_approval
            cache_hit = knowledge_outcome.cache_hit
            rag_retrieval = knowledge_outcome.rag_retrieval
            rag_rerank = knowledge_outcome.rag_rerank
        elif intent == "return_request":
            return_outcome = handle_return(
                request=request,
                clarification=clarification,
                order_id=order_id,
                hooks=hooks,
                citations=citations,
                tool_calls=tool_calls,
            )
            answer = return_outcome.answer
            risk_level = return_outcome.risk_level
            next_action = return_outcome.next_action
            needs_human_approval = return_outcome.needs_human_approval
            workflow = return_outcome.workflow
            verified_order_id = return_outcome.verified_order_id
        elif intent in {"order_query", "refund_status_query", "refund_request", "product_query"}:
            tool_outcome = handle_tool_query(
                request=request,
                intent=intent,
                order_id=order_id,
                clarification=clarification,
                route_plan=route_plan,
                runner=self.langchain_tool_runner,
                hooks=hooks,
                citations=citations,
                tool_calls=tool_calls,
            )
            answer = tool_outcome.answer
            risk_level = tool_outcome.risk_level
            next_action = tool_outcome.next_action
            needs_human_approval = tool_outcome.needs_human_approval
            degraded = tool_outcome.degraded
            degradation_reason = tool_outcome.degradation_reason
            workflow = tool_outcome.workflow
            verified_order_id = tool_outcome.verified_order_id
            verified_product_name = tool_outcome.verified_product_name
            if tool_outcome.citations is not None:
                citations = tool_outcome.citations
            rag_retrieval = tool_outcome.rag_retrieval
            rag_rerank = tool_outcome.rag_rerank
            tool_calling_state = tool_outcome.tool_calling_state or tool_calling_state
            tool_agent_prompt_fragments = tool_outcome.tool_agent_prompt_fragments
            tool_agent_model_calls = tool_outcome.tool_agent_model_calls
        else:
            # 兜底:既不是知识问题也不是业务问题,走通用闲聊回答
            answer = general_chat_answer(request.user_message)
            risk_level = "low"
            next_action = "answer_user"
            needs_human_approval = False

        # ============ ⑦ 安全检查(横切能力,包裹在主流程外面)============
        sanitize_external_sources(
            session_id=request.session_id,
            tool_calls=tool_calls,
            citations=citations,
            context_report=context_report,
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
        # 把前面拿到的 answer(可能来自 Tool/RAG/Workflow/兜底)交给最终模型润色,
        # 但安全/降级/缓存命中/澄清这些边界场景会跳过模型(保留确定性,见 _compose_final_answer)
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
            # 在线缓存模型基于证据生成的最终话术;离线测试则缓存显式兜底话术.
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

        # ============ ⑨ 收尾记录(都不影响主流程,是"横切能力")============
        #   - update_memory      : 把本轮订单/商品写进会话记忆,下次能想起来
        #   - hooks.on_completion : 完成钩子(安全摘要等)
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

        # ============ ⑩ 转人工工单 ============
        # transfer_to_human 不再只返回一个字符串:落一张可查询、可审计的工单.
        # 退款/退货已经在 workflow 审批队列里的场景不重复建单,避免双队列.
        handoff_ticket: HandoffTicket | None = None
        if next_action == "transfer_to_human" and not (workflow and needs_human_approval):
            priority = {"high": "P1", "medium": "P2", "low": "P3"}.get(risk_level, "P3")
            handoff_ticket = HandoffTicket(
                **create_handoff_ticket(
                    session_id=request.session_id,
                    user_id=request.runtime_user_id,
                    intent=intent,
                    priority=priority,
                    reason=degradation_reason or f"agent_handoff:{intent}",
                    summary=answer,
                    order_no=order_id or None,
                )
            )
            trace_store.add(
                request.session_id,
                "handoff_ticket_created",
                {
                    "session_id": request.session_id,
                    "ticket_id": handoff_ticket.ticket_id,
                    "priority": handoff_ticket.priority,
                    "intent": intent,
                    "status": handoff_ticket.status,
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
            handoff_ticket=handoff_ticket,
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
                "handoff_ticket": handoff_ticket.model_dump() if handoff_ticket else None,
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
                "next_gap": "后续补充真实业务接入和线上可观测。",
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
        """让真实模型生成最终话术,但安全,低置信,降级和缓存命中保留确定性边界.

        关键设计:不是所有情况都调用模型.以下情况"跳过模型",直接返回确定性 answer:
          - runtime identity / 澄清 / 安全 / 低置信 / 降级 / 缓存命中
        为什么跳过:这些场景答案必须确定,可控,不花模型钱,交给模型反而可能胡说.
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

    def resume(self, request: ChatResumeRequest) -> ChatResumeResponse:
        """恢复暂停的 HITL workflow,具体校验和幂等由 workflow 层负责.

        售后流程跑到"等人工审批"时会被挂起,用户/管理员通过 /chat/resume 恢复.
        这里只转发给 workflow 层,校验和幂等(防止重复审批)都在 workflows/resume.py.
        """
        return resume_from_checkpoint(request, trace_store)
