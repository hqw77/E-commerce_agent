"""
轻量规划工具 —— 决策层：意图识别 + 路由计划 + 参数抽取 + 澄清追问。

【这个文件是干什么的】
主链路 9 步里的「决策」部分，回答一个问题：用户这句话该走哪条路（RAG / Tool / Workflow / 拦截 / 兜底）。
被 customer_service_agent.chat() 调用，产出意图(intent)和路由计划(RoutePlan)，
再交给大脑按意图分派。

【核心流程（按调用顺序）】
  ① classify_guard_intent      精确拦截高风险/边界（降级/安全/退货/退款），命中就覆盖模型
  ② classify_intent            宽泛兜底分类，先调 guard，再按关键词分级，最后兜底 general_chat
  ③ extract_order_id           正则抽订单号（不用 LLM，防幻觉）
  ④ build_route_plan           把意图映射成 RoutePlan（工具白名单 + 缺参数不执行）
  ⑤ build_order_clarification  缺订单号时生成「追问用户」的澄清
  ⑥ estimate_tokens            粗估 token（成本展示用）

【注意】
本文件实际只用 re 和 api.schemas 两个导入；其余 import（json/os/datetime/httpx/yaml/fastapi 等 14 个）
是模板残留，并未在本文件使用，属待清理项，不影响运行。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import httpx
import yaml
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from api.schemas import *


def classify_guard_intent(user_message: str) -> Intent | None:
    """
    精确拦截层（优先级最高）：只识别足够明确、允许覆盖模型路由的边界场景。
    返回命中的 intent；未命中返回 None，交给 classify_intent 兜底。
    覆盖 4 类：降级 / 安全攻击 / 退货 / 退款（含状态查询）。
    """
    # 降级：系统级故障信号
    if "SERVICE_TIMEOUT" in user_message:
        return "degradation_request"

    # 安全：Prompt 注入攻击（套系统提示词/隐藏推理/工具 schema/内部策略）
    if any(term in user_message for term in ["系统提示词", "hidden reasoning", "隐藏推理", "工具 schema", "内部策略"]):
        return "security_request"

    # 退货：明确的退货表达（必须走 Workflow，不能被模型误判为普通查询）
    if any(
        term in user_message
        for term in [
            "我要退货",
            "我想退货",
            "我要申请退货",
            "我想申请退货",
            "帮我退货",
            "申请退货",
            "七天无理由",
            "寄回",
            "能退货吗",
            "可以退货吗",
        ]
    ):
        return "return_request"

    # 退款：明确的退款表达（高风险资金动作，走 Workflow）
    if any(
        term in user_message
        for term in [
            "我要退款",
            "我想退款",
            "我要申请退款",
            "我想申请退款",
            "帮我退款",
            "帮我申请退款",
            "给我退款",
            "直接退款",
            "直接给我退",
            "把钱退给我",
            "取消订单",
        ]
    ):
        return "refund_request"

    # 退款状态查询：必须同时含「退款/退钱」+「进度/状态」等词，避免把「我要退款」误判成查询
    if any(subject in user_message for subject in ["退款", "退钱"]) and any(
        term in user_message
        for term in [
            "进度",
            "状态",
            "情况",
            "处理到哪",
            "什么时候到账",
            "怎么样",
            "结果",
            "是否到账",
            "到账了吗",
            "审核",
            "是否通过",
            "有没有通过",
            "退了吗",
        ]
    ):
        return "refund_status_query"

    # 退款第二组关键词，覆盖更多表达
    if any(
        term in user_message
        for term in [
            "申请退款",
            "发起退款",
            "办理退款",
            "退钱",
            "能退款吗",
            "可以退款吗",
            "还能退款吗",
            "能不能退款",
        ]
    ):
        return "refund_request"

    return None


def classify_intent(user_message: str) -> Intent:
    """
    宽泛兜底分类：先调 guard（精确），再按关键词分级（宽泛），最后兜底 general_chat。
    保证永远返回一个 intent，不会返回 None。
    """
    # 先让 Guard 精确拦截（安全/高风险优先）
    guard_intent = classify_guard_intent(user_message)
    if guard_intent is not None:
        return guard_intent

    if any(term in user_message for term in ["服务抽风", "工具超时", "接口不可用"]):
        return "degradation_request"
    if "退货" in user_message:
        return "return_request"
    if any(term in user_message for term in ["退款", "退钱"]):
        return "refund_request"
    if any(term in user_message for term in ["订单", "物流", "快递"]):
        return "order_query"
    if "发票" in user_message:
        return "faq_query"
    # 低置信测试词：故意制造「知识库里不存在」的场景，测系统的兜底能力
    if any(term in user_message for term in ["火星会员", "隐藏券", "不存在的活动", "未知活动"]):
        return "low_confidence_query"
    # 活动类：同时提到商品 → 更具体的 product_query；否则 promotion_query
    if any(term in user_message for term in ["活动", "满减", "会员券", "优惠券", "会员规则", "大促"]):
        if any(term in user_message for term in ["商品", "耳机", "音箱", "库存", "价格", "多少钱", "有货"]):
            return "product_query"
        return "promotion_query"
    if any(term in user_message for term in ["商品", "耳机", "音箱", "库存", "价格", "多少钱", "有货", "推荐"]):
        return "product_query"
    return "general_chat"


def extract_order_id(user_message: str) -> str | None:
    """
    正则抽取订单号。为什么不用 LLM 抽：LLM 会「编造」订单号（幻觉），
    订单号有固定格式（SO... 或 ORD...），用正则更可靠。
    抽不到返回 None，触发后续的澄清追问，而不是让模型代填。
    """
    match = re.search(
        r"\b(?:SO[A-Za-z0-9_-]{6,}|ORD\d{4,})\b",
        user_message,
        flags=re.IGNORECASE,
    )
    return match.group(0) if match else None


def extract_return_reason(user_message: str) -> str | None:
    """
    白名单制抽取退货原因。退货涉及资金，必须由用户明确表达，不能让模型「猜」。
    只接受预定义的 6 种原因，返回第一个匹配的，否则 None。
    """
    reason_terms = ("七天无理由", "质量问题", "商品破损", "发错货", "少件", "与描述不符")
    return next((term for term in reason_terms if term in user_message), None)


def build_route_plan(
    *,
    intent: Intent,
    user_message: str,
    order_id: str | None,
    model_used: bool,
) -> RoutePlan:
    """
    把意图收敛成白名单 RoutePlan。两个核心设计：
    1. 工具白名单：candidate_catalog 预定义所有可用工具，模型不能「发明」新工具
    2. 参数完整性：缺订单号时不执行需要订单号的工具，改走澄清
    """
    # 工具白名单：4 个只读工具。高风险操作（退款/退货）不在工具层，而是走 Workflow
    candidate_catalog = {
        "get_order_detail": ToolCandidate(
            name="get_order_detail",
            domain="order",
            risk_level="low",
            reason="读取当前用户订单事实，不执行业务写操作。",
        ),
        "get_order_logistics": ToolCandidate(
            name="get_order_logistics",
            domain="logistics",
            risk_level="low",
            reason="读取当前用户订单及物流状态。",
        ),
        "get_refund_status": ToolCandidate(
            name="get_refund_status",
            domain="after_sale",
            risk_level="low",
            reason="只读查询当前用户订单的售后申请状态，不创建退款申请。",
        ),
        "search_products": ToolCandidate(
            name="search_products",
            domain="product",
            risk_level="low",
            reason="查询商品价格、库存和活动等实时事实。",
        ),
    }

    required_tools: list[str] = []
    knowledge_domains: list[str] = []
    risk_level: RiskLevel = "low"
    requires_workflow = False

    # 意图 → 路由映射
    if intent == "order_query":
        required_tools = ["get_order_logistics"]
    elif intent == "refund_status_query":
        required_tools = ["get_refund_status"]
    elif intent == "refund_request":
        required_tools = ["get_order_detail"]
        knowledge_domains = ["after_sale_policy"]
        risk_level = "high"
        requires_workflow = True
    elif intent == "return_request":
        required_tools = ["get_order_detail"]
        knowledge_domains = ["received_return_policy"]
        risk_level = "high"
        requires_workflow = True
    elif intent == "product_query":
        required_tools = ["search_products"]
        knowledge_domains = (
            ["promotion_and_member_policy"]
            if any(term in user_message for term in ["活动", "优惠", "满减", "会员"])
            else []
        )
    elif intent in {"faq_query", "promotion_query", "low_confidence_query"}:
        knowledge_domains = ["faq"] if intent == "faq_query" else ["promotion_and_member_policy"]
    elif intent in {"security_request", "degradation_request"}:
        risk_level = "high" if intent == "security_request" else "medium"

    # 需要订单号才能执行的工具
    order_bound_tools = {"get_order_detail", "get_order_logistics", "get_refund_status"}
    # 缺订单号时，这些工具不允许执行（防止模型凭空生成参数）
    executable_tools = (
        required_tools
        if order_id or not any(name in order_bound_tools for name in required_tools)
        else []
    )

    return RoutePlan(
        intent=intent,
        needs_rag=bool(knowledge_domains),
        needs_business_tools=bool(required_tools),
        required_tools=executable_tools,
        tool_candidates=[candidate_catalog[name] for name in required_tools],
        knowledge_domains=knowledge_domains,
        entity_refs=[order_id] if order_id else [],
        risk_level=risk_level,
        requires_workflow=requires_workflow,
        confidence=0.9 if model_used else 0.75,
        source="llm_with_policy_constraints" if model_used else "deterministic_fallback",
        fallback_policy=(
            "ask_order_id"
            if required_tools
            and not order_id
            and any(name in order_bound_tools for name in required_tools)
            else "safe_deterministic_path"
        ),
    )


def build_order_clarification(request: ChatRequest, route_plan: RoutePlan) -> ClarificationRequest | None:
    """
    缺订单号时生成「追问用户」的澄清。
    为什么不让模型代选订单：模型可能选错（幻觉），且订单涉及隐私资金，
    必须从可信 Runtime Context 读订单列表，让用户自己选。
    """
    if route_plan.fallback_policy != "ask_order_id" or not route_plan.tool_candidates:
        return None

    orders = (request.runtime_context or {}).get("currentUserOrders", [])

    candidates: list[ClarificationCandidate] = []
    for order in orders:
        # 只返回当前用户的订单（安全检查）
        if str(order.get("userId")) != request.runtime_user_id:
            continue
        order_id = str(order.get("orderNo") or "").strip()
        if not order_id:
            continue
        items = order.get("items") or []
        product_names = "、".join(
            str(item.get("productName"))
            for item in items[:2]
            if item.get("productName")
        )
        candidates.append(
            ClarificationCandidate(
                value=order_id,
                label=order_id,
                hint=product_names or "当前账号订单",
            )
        )

    action = "退款" if route_plan.intent == "refund_request" else "查询"
    return ClarificationRequest(
        clarification_field="order_id",
        message=f"你要{action}哪一个订单？请选择订单号，或直接补充订单号。",
        candidates=candidates,
    )


def estimate_tokens(text: str) -> int:
    """粗估 token（约 2 字符/token），只用于成本展示，不是精确计费。"""
    return max(1, len(text) // 2)
