"""
Pydantic 请求响应模型 —— 所有公开 API 契约集中定义在这里。

【这个文件是干什么的】
整个项目的「数据契约」：定义了所有接口的请求体、响应体、以及各模块间传递的数据结构
（意图、路由计划、引用、工具追踪、Trace 事件、评测、反馈、恢复等）。
它没有业务逻辑，纯字段定义，是各模块共享的「类型字典」。

【怎么用】
各模块 `from api.schemas import *` 引入。核心类型：
  - Intent / RiskLevel / NextAction：三个 Literal 枚举（限定取值范围）
  - RoutePlan：决策层的输出（意图 → 工具/RAG/Workflow 的映射）
  - Citation / ToolCallTrace：RAG 引用 和 Tool 调用的可观察记录
  - ChatRequest / ChatResponse：主接口 /chat 的输入输出
  - ChatResumeRequest / ChatResumeResponse：HITL 恢复接口的输入输出

【注意】
本文件实际用到 Any / Literal / Path / datetime / BaseModel / Field；
其余 import（json/os/re/timezone/uuid4/httpx/yaml/fastapi 等 9 个）是模板残留，未使用。
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


# ============ 枚举类型（Literal 限定取值范围） ============

ReasoningView = Literal["default", "off", "summary", "teaching"]  # 是否返回推理过程：默认/关闭/摘要/教学模式
Intent = Literal[
    "general_chat",          # 普通聊天
    "order_query",           # 订单查询
    "refund_status_query",   # 退款状态查询
    "refund_request",        # 退款请求
    "return_request",        # 退货请求
    "faq_query",             # FAQ查询
    "promotion_query",       # 促销查询
    "product_query",         # 商品查询
    "low_confidence_query",  # 低置信查询
    "degradation_request",   # 降级请求：系统异常、工具超时时走的降级路
    "security_request",      # 安全请求：试图获取系统提示词、隐藏推理等安全攻击
    "unknown",               # 未知意图
]
RiskLevel = Literal["low", "medium", "high"]  # 风险等级：低/中/高
NextAction = Literal["answer_user", "ask_clarification", "transfer_to_human"]  # 下一步：直接回答/追问澄清/转人工


# ============ 路径 / 常量 ============

CAPABILITIES_PATH = Path(__file__).with_name("agent_capabilities.json")  # 能力清单文件
CASES_PATH = Path(__file__).with_name("cases.yml")  # 评测用例文件
DEFAULT_COURSE_ENV_PATH = Path(__file__).resolve().parents[2] / "course.env"  # 环境变量文件
DEFAULT_ECOMMERCE_BASE_URL = "http://127.0.0.1:8081"  # Java 电商后端默认地址
TRACE_SCHEMA_VERSION = "trace_event_v1"  # Trace 事件 schema 版本


# ============ 入口请求 / 响应 ============

class HistoryMessage(BaseModel):
    """历史消息（用于会话上下文）。"""
    role: Literal["user", "assistant"]  # 角色：用户/助手
    content: str  # 消息内容


class ChatRequest(BaseModel):
    """POST /chat 的请求体。"""
    session_id: str = Field(..., description="当前对话会话 ID")
    runtime_user_id: str = Field(..., description="当前登录用户 ID，由调用方确认")
    runtime_nickname: str | None = Field(default=None, description="当前用户昵称")
    runtime_member_level: str | None = Field(default=None, description="当前会员等级，由调用方确认")
    runtime_risk_level: str | None = Field(default=None, description="当前账号风险等级，由调用方确认")
    user_message: str = Field(..., description="用户输入的问题")
    reasoning_view: ReasoningView = "default"  # 是否返回推理过程
    debug: bool = True  # 调试开关
    runtime_context: dict[str, Any] | None = None  # 可信运行时上下文（会员/订单列表等）
    history_messages: list[HistoryMessage] = Field(default_factory=list)  # 历史消息


class ChatResponse(BaseModel):
    """POST /chat 的响应体。"""
    session_id: str
    answer: str                                    # 最终回答
    citations: list[Citation]                      # 引用来源列表
    tool_calls: list[ToolCallTrace]                # 工具调用追踪
    clarification: ClarificationRequest | None = None  # 澄清追问（如缺订单号）
    reasoning_summary: list[str]                   # 推理摘要（公开）
    reasoning_content: str | None = None           # 推理原文（仅教学模式返回）
    session_state: dict[str, Any]                  # 完整会话状态（供前端展示）


class ChatResumeRequest(BaseModel):
    """POST /chat/resume 的请求体（HITL 审批恢复）。"""
    session_id: str = Field(..., description="要恢复的会话 ID")
    workflow_id: str = Field(..., description="待恢复 workflow ID")
    resume_token: str = Field(..., description="暂停时返回的恢复令牌")
    reviewer_id: str = Field(..., description="审批人 ID")
    reviewer_role: str = Field(..., description="审批人角色")
    decision: Literal["approved", "rejected", "needs_more_info"]  # 审批决定：通过/拒绝/需补充
    reviewer_note: str | None = None  # 审批备注


class ChatResumeResponse(BaseModel):
    """POST /chat/resume 的响应体。"""
    session_id: str
    workflow_id: str
    status: Literal["completed", "paused", "rejected", "blocked"]  # 完成/暂停/拒绝/阻断
    answer: str
    resume_result: dict[str, Any]  # 恢复结果详情
    workflow: dict[str, Any] | None  # workflow 摘要
    business_recheck: dict[str, Any]  # 业务事实复核结果
    session_state: dict[str, Any]


# ============ 路由计划（决策层输出） ============

class ToolCandidate(BaseModel):
    """工具候选（白名单里的一个工具 + 元数据）。"""
    name: str  # 工具名
    domain: str  # 所属领域（order/logistics/after_sale/product）
    risk_level: RiskLevel  # 风险等级
    reason: str  # 说明（为什么这个工具安全）


class RoutePlan(BaseModel):
    """模型意图经过业务白名单约束后的结构化执行计划。"""
    intent: Intent  # 意图
    needs_rag: bool  # 是否需要 RAG 检索
    needs_business_tools: bool  # 是否需要业务工具
    required_tools: list[str] = Field(default_factory=list)  # 可执行工具白名单
    tool_candidates: list[ToolCandidate] = Field(default_factory=list)  # 工具候选（含元数据）
    knowledge_domains: list[str] = Field(default_factory=list)  # RAG 知识域
    entity_refs: list[str] = Field(default_factory=list)  # 实体引用（如订单号）
    risk_level: RiskLevel = "low"  # 风险等级
    requires_workflow: bool = False  # 是否走 Workflow（售后流程）
    confidence: float = Field(default=1.0, ge=0, le=1)  # 置信度
    source: Literal["llm_with_policy_constraints", "deterministic_fallback"]  # 来源：模型/规则
    fallback_policy: str = "safe_deterministic_path"  # 兜底策略


class ClarificationCandidate(BaseModel):
    """澄清候选（如：当前账号下的多个订单）。"""
    value: str  # 实际值（订单号）
    label: str  # 显示文本
    hint: str  # 辅助提示（如商品名）


class ClarificationRequest(BaseModel):
    """工具必填参数不足时，由后端校验并生成的结构化追问。"""
    clarification_field: str  # 需要澄清的字段（如 order_id）
    message: str  # 追问话术
    candidates: list[ClarificationCandidate] = Field(default_factory=list)  # 候选列表


# ============ 知识引用 / 工具追踪（可观察证据） ============

class Citation(BaseModel):
    """RAG 检索命中的知识引用（回答的可追溯依据）。"""
    source: str                                    # 来源标识
    title: str                                     # 文档标题
    snippet: str                                   # 内容片段
    score: float                                   # 相关性分数
    retrieval_stage: Literal["pre_retrieval", "tool_retrieval"] | None = None  # 检索阶段
    metadata: dict[str, Any] | None = None         # 扩展元数据（如 policy_id）


class ToolCallTrace(BaseModel):
    """一次工具调用的完整可观察记录。"""
    tool_name: str  # 工具名
    arguments: dict[str, Any]  # 调用参数
    output_summary: str  # 结果摘要
    status: Literal["success", "error"]  # 成功/失败
    tool_source: Literal["function", "mcp", "preload"] | None = "function"  # 工具来源
    risk_level: RiskLevel | None = None  # 风险等级
    needs_human_approval: bool | None = None  # 是否需要人工审批
    next_action: str | None = None  # 下一步动作
    error_type: str | None = None  # 错误类型
    candidates: list[dict[str, Any]] = Field(default_factory=list)  # 候选
    clarification_field: str | None = None  # 澄清字段
    clarification_prompt: str | None = None  # 澄清提示


# ============ Trace 事件 ============

class TraceEvent(BaseModel):
    """公开 Trace 事件（不含 hidden CoT）。"""
    event_type: str  # 事件类型
    timestamp: datetime  # 时间戳
    agent_mode: str | None = None  # Agent 模式
    step: int | None = None  # 主链路步骤号
    schema_version: str  # schema 版本
    category: str  # 分类
    stage: str  # 阶段
    name: str  # 事件名
    status: str  # 状态
    target: dict[str, Any]  # 目标
    ids: dict[str, Any]  # 相关 ID
    summary: dict[str, Any]  # 摘要
    signals: list[str]  # 信号
    safety: dict[str, Any]  # 安全信息
    payload: dict[str, Any]  # 载荷


# ============ 评测（回归验证） ============

class EvalRunRequest(BaseModel):
    case_id: str | None = None  # 指定评测用例（None=跑全部）


class EvalCaseResult(BaseModel):
    case_id: str  # 用例 ID
    passed: bool  # 是否通过
    user_message: str  # 测试输入
    expected_signals: list[str]  # 期望信号
    actual_answer: str  # 实际回答
    actual_tools: list[str]  # 实际调用工具
    missing_signals: list[str]  # 缺失信号
    actual_citations: list[str] = []  # 实际引用
    actual_trace_events: list[str] = []  # 实际 trace 事件
    missing_tools: list[str] = []  # 缺失工具
    unexpected_tools: list[str] = []  # 意外工具
    missing_citations: list[str] = []  # 缺失引用
    forbidden_citation_hits: list[str] = []  # 不该命中的引用
    missing_trace_events: list[str] = []  # 缺失 trace 事件
    missing_session_state: list[str] = []  # 缺失会话状态
    forbidden_text_hits: list[str] = []  # 不该出现的文本
    failure_categories: list[str] = []  # 失败分类


class EvalRunResponse(BaseModel):
    total: int  # 总用例数
    passed: int  # 通过数
    failed: int  # 失败数
    summary: dict[str, Any]  # 汇总
    results: list[EvalCaseResult]  # 各用例结果


# ============ 反馈归因 ============

class FeedbackRequest(BaseModel):
    session_id: str = Field(..., description="发生反馈的会话 ID")
    case_id: str | None = Field(default=None, description="如果这次事故能对应已有 eval case，就绑定它")
    rating: Literal["negative", "neutral", "positive"] = "negative"  # 评价：差/中/好
    user_comment: str  # 用户评论
    observed_answer: str  # 观察到的回答


class FailureAttribution(BaseModel):
    """把一次失败归因到具体模块。"""
    module: Literal["Prompt", "RAG", "Tool", "Context", "Workflow", "EvaluationExpectation"]  # 归因模块
    category: str  # 失败分类
    evidence: list[str]  # 证据
    suggested_fix: str  # 建议修复


class FeedbackRecord(BaseModel):
    feedback_id: str  # 反馈 ID
    session_id: str  # 会话 ID
    case_id: str | None  # 关联用例
    rating: str  # 评价
    user_comment: str  # 用户评论
    trace_event_names: list[str]  # 相关 trace 事件
    eval_failure_categories: list[str]  # 失败分类
    attributions: list[FailureAttribution]  # 归因
    backfilled_case: dict[str, Any]  # 回填的用例


class FeedbackSubmitResponse(BaseModel):
    record: FeedbackRecord  # 反馈记录
    eval_report: EvalRunResponse | None = None  # 关联评测报告
