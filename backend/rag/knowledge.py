"""
内置知识片段。综合演练把售后、发票、活动和会员规则集中成可引用 Citation。

本模块是 RAG 系统与 Agent 编排层之间的业务适配层。
主要职责：
1. 预加载知识文档为 Citation 对象
2. 证据门控（防止幻觉）
3. 为不同意图提供确定性的知识路径
4. 轻量级 Reranker（重排序）
5. 低置信度兜底策略
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 导入 API 契约层定义
# Citation: 引用来源的数据结构
# Intent: 用户意图类型
# RiskLevel: 风险等级 (low/medium/high)
# NextAction: 下一步动作 (answer_user/ask_clarification/transfer_to_human)
from api.schemas import *

# 导入文档加载器：解析 Markdown + YAML frontmatter，返回 Citation
from rag.documents import load_knowledge_citation

# 导入混合检索引擎：双路召回（向量 + 关键词）
from rag.hybrid_retrieval import retrieve_knowledge

# 导入全局缓存：用于 FAQ 答案缓存，避免重复检索
from state.session_state import COMMON_HIT_CACHE


# ============================================================================
# 第一部分：知识预加载
# ============================================================================
# 模块加载时即解析知识文件为 Citation 对象，全局复用
# 优点：一次解析，多次使用，避免重复 I/O

REFUND_POLICY = load_knowledge_citation("after_sale_policy.md")
"""退款政策 Citation，policy_id: refund_before_shipping"""

RETURN_POLICY = load_knowledge_citation("received_return_policy.md")
"""退货政策 Citation，policy_id: return_after_delivery"""

INVOICE_FAQ = load_knowledge_citation("payment_invoice_policy.md")
"""发票 FAQ Citation，policy_id: invoice_issue"""

PROMOTION_POLICY = load_knowledge_citation("promotion_policy.md")
"""促销活动规则 Citation，policy_id: promotion_618_stack_rule"""

MEMBER_COUPON_POLICY = load_knowledge_citation("member_coupon_policy.md")
"""会员券规则 Citation，policy_id: member_coupon_gold_rule"""


# ============================================================================
# 第二部分：数据类定义
# ============================================================================

@dataclass(frozen=True)
class KnowledgePathResult:
    """
    稳定知识路径的确定性结果，供 Agent 编排层直接拼装响应。

    这是一个不可变数据类（frozen=True），确保线程安全。
    包含完整响应闭环信息 + 可观测性数据。
    """

    answer: str
    """最终返回给用户的回答文本"""

    citations: list[Citation]
    """引用的知识来源列表（支持多个来源）"""

    risk_level: RiskLevel
    """风险等级：low / medium / high"""

    next_action: NextAction
    """下一步动作：answer_user / ask_clarification / transfer_to_human"""

    needs_human_approval: bool
    """是否需要人工审批（高风险操作需要）"""

    cache_hit: bool = False
    """是否命中缓存（用于性能监控）"""

    rerank: dict[str, Any] | None = None
    """重排序调试信息（包含加权前后对比）"""

    retrieval_debug: dict[str, Any] | None = None
    """检索调试信息（包含召回详情）"""

    trace_events: tuple[tuple[str, dict[str, Any]], ...] = ()
    """
    链路追踪事件列表，格式：(event_name, event_data)
    用于可观测性和调试
    """


# ============================================================================
# 第三部分：低置信度兜底策略
# ============================================================================

def low_confidence_result(session_id: str, intent: Intent) -> KnowledgePathResult:
    """
    纯知识低置信场景保守兜底，不让模型编造隐藏规则。

    触发条件：
    - 检索结果为空
    - 证据门控拦截
    - 候选证据不足

    设计思想：
    - 安全第一：宁可转人工，也不编造信息
    - 明确拒绝：清晰告知用户无法提供帮助
    - 引导行动：建议查看官方渠道或转人工
    """
    return KnowledgePathResult(
        # 明确的拒绝话术，不给用户虚假期望
        answer="没有检索到跨境电商公司已发布的可信活动或会员规则，我不能编造隐藏券规则。建议以活动页和结算页展示为准，或转人工客服进一步核实。",
        citations=[],  # 无引用来源
        risk_level="medium",  # 中等风险：需要人工关注
        next_action="transfer_to_human",  # 转人工客服
        needs_human_approval=False,  # 不需要审批（直接转人工）
        trace_events=(
            (
                "rag_low_confidence_fallback",  # 事件类型
                {
                    "session_id": session_id,
                    "intent": intent,
                    "hit_count": 0,  # 命中数为0
                    "retrieval_stage": "pre_retrieval",
                    "pending_action": "transfer_to_human",
                    "status": "low_confidence",
                },
            ),
        ),
    )


# ============================================================================
# 第四部分：发票 FAQ 处理
# ============================================================================

def invoice_faq_result(session_id: str) -> KnowledgePathResult:
    """
    发票 FAQ 读取最终回答缓存；首次模型回答由编排层在生成后写入。

    工作流程：
    1. 检查 COMMON_HIT_CACHE 缓存
    2. 命中 → 直接返回（快速响应）
    3. 未命中 → 执行 RAG 检索
    4. 提取对应的 Citation（默认使用预加载的 INVOICE_FAQ）
    5. 返回标准答案

    缓存设计：
    - 缓存键：faq:invoice_issue
    - 缓存内容：{"answer": "..."}
    - 缓存写入：由 Agent 编排层在生成后写入（职责分离）
    """
    # Step 1: 检查缓存
    cache_key = "faq:invoice_issue"
    cached = COMMON_HIT_CACHE.get(cache_key)

    if cached:
        # 缓存命中：直接返回，快速响应
        return KnowledgePathResult(
            answer=str(cached["answer"]),
            citations=[INVOICE_FAQ],  # 使用预加载的 Citation
            risk_level="low",
            next_action="answer_user",
            needs_human_approval=False,
            cache_hit=True,  # 标记缓存命中
        )

    # Step 2: 缓存未命中，执行 RAG 检索
    # 使用固定的查询语句，确保结果稳定
    retrieval = retrieve_knowledge("电子发票通常多久能准备好", "faq_query")

    # Step 3: 从检索结果中提取对应的 Citation
    # 优先使用检索到的，否则使用预加载的 INVOICE_FAQ 作为后备
    citation = next(
        (item for item in retrieval.citations
         if (item.metadata or {}).get("policy_id") == "invoice_issue"),
        INVOICE_FAQ,  # 默认值：使用预加载的
    )

    # Step 4: 构建标准答案
    # 注意：这里答案固定，但检索到的 citation 可能不同
    answer = "电子发票通常在订单完成后 24 小时内开具，你可以在订单详情页查看和下载。"

    return KnowledgePathResult(
        answer=answer,
        citations=[citation],
        risk_level="low",
        next_action="answer_user",
        needs_human_approval=False,
        retrieval_debug=retrieval.debug,  # 传递检索调试信息
        trace_events=(
            (
                "rag_pre_retrieved",
                {
                    "session_id": session_id,
                    "hit_count": 1,
                    "retrieval_stage": "pre_retrieval",
                    "policy_id": "invoice_issue",
                },
            ),
        ),
    )


# ============================================================================
# 第五部分：促销策略处理（核心函数）
# ============================================================================

def promotion_policy_result(session_id: str, user_message: str) -> KnowledgePathResult:
    """
    活动和会员券问题先过证据门，再用 reranker 决定最终引用顺序。

    这是本模块最核心的函数，完整演示了 RAG 业务适配的完整流程：
    1. 证据门控：拦截虚构内容
    2. RAG 检索：调用混合检索引擎
    3. 策略过滤：只保留相关策略
    4. 叠加检查：确保叠加问题有足够证据
    5. 轻量重排：基于业务规则重新排序
    6. 答案生成：根据证据组合生成答案

    参数:
        session_id: 会话ID，用于追踪
        user_message: 用户原始输入

    返回:
        KnowledgePathResult: 完整响应结果
    """
    # Step 1: 归一化处理
    # 去除空格、转小写，便于后续匹配
    normalized = user_message.replace(" ", "").lower()

    # Step 2: 证据门控（Evidence Gate）
    # 检查是否包含不存在的业务概念
    # 设计理念：宁可错杀，不可放过
    if any(term in normalized for term in ("隐藏券", "火星会员", "不存在的活动", "未知活动", "未发布")):
        # 触发门控：直接拦截，不进行检索
        candidates: list[tuple[Citation, float, list[str]]] = []
        retrieval_debug = {
            "mode": "evidence_gate_blocked_before_retrieval",
            "reason": "unsupported_policy_claim"
        }
    else:
        # Step 3: 执行 RAG 检索
        # 使用 promotion_query 意图，确保检索到促销相关文档
        retrieval = retrieve_knowledge(user_message, "promotion_query")
        retrieval_debug = retrieval.debug

        # Step 4: 策略ID过滤
        # 根据问题内容决定需要哪些策略
        allowed_policy_ids = _promotion_scope_policy_ids(normalized)

        # 构建候选列表：(Citation, 原始分数, 召回原因)
        candidates = [
            (
                citation,
                citation.score,  # 原始相关性分数
                list(
                    # 从 debug 信息中提取召回原因（vector召回 / keyword召回）
                    retrieval.debug.get("source_scores", {})
                    .get((citation.metadata or {}).get("policy_id"), {})
                    .get("sources", [])
                ),
            )
            for citation in retrieval.citations
            # 只保留匹配的 policy_id
            if (citation.metadata or {}).get("policy_id") in allowed_policy_ids
        ]

        # Step 5: 叠加场景特殊检查
        # 如果用户问"叠加"，但只有1条证据，说明证据不足
        if "叠加" in normalized and len(candidates) < 2:
            candidates = []  # 清空候选，触发兜底

    # Step 6: 轻量级重排序
    # 基于业务规则重新排序候选文档
    reranked = _rerank_promotion_candidates(user_message, candidates)
    citations = [citation for citation, _score, _reasons in reranked]

    # Step 7: 无证据处理
    # 如果没有任何有效证据，走兜底策略
    if not citations:
        result = low_confidence_result(session_id, "promotion_query")
        return KnowledgePathResult(
            answer=result.answer,
            citations=result.citations,
            risk_level=result.risk_level,
            next_action=result.next_action,
            needs_human_approval=result.needs_human_approval,
            retrieval_debug=retrieval_debug,  # 保留检索 debug
            trace_events=(
                (
                    "rag_evidence_gate_blocked",
                    {
                        "session_id": session_id,
                        "intent": "promotion_query",
                        "hit_count": 0,
                        "retrieval_stage": "pre_retrieval",
                        "status": "low_confidence",
                        "reason": "no_trusted_policy_citation",
                    },
                ),
                *result.trace_events,  # 展开兜底结果的事件
            ),
        )

    # Step 8: 构建成功响应
    # 生成重排序调试信息
    rerank_debug = _build_rerank_debug(reranked)
    rerank_debug["retrieval"] = retrieval_debug  # 合并检索 debug

    # 根据证据组合生成对应的回答
    answer = _promotion_policy_answer(citations)

    return KnowledgePathResult(
        answer=answer,
        citations=citations,
        risk_level="low",  # 有证据支撑，风险低
        next_action="answer_user",  # 直接回答用户
        needs_human_approval=False,
        rerank=rerank_debug,
        retrieval_debug=retrieval_debug,
        trace_events=(
            # 事件1：检索完成
            (
                "rag_pre_retrieved",
                {
                    "session_id": session_id,
                    "hit_count": len(citations),
                    "retrieval_stage": "pre_retrieval",
                    "policy_id": "promotion_618_stack_rule",
                    "candidate_policy_ids": [
                        citation.metadata.get("policy_id")
                        for citation, _score, _reasons in candidates
                        if citation.metadata
                    ],
                },
            ),
            # 事件2：重排序完成
            (
                "rag_reranked",
                {
                    "session_id": session_id,
                    "mode": rerank_debug["mode"],
                    "reranked_policy_ids": rerank_debug["policy_ids"],
                    "top_policy_id": rerank_debug["policy_ids"][0] if rerank_debug["policy_ids"] else None,
                    "rerank_reasons": rerank_debug["reasons"],
                },
            ),
        ),
    )


# ============================================================================
# 第六部分：私有辅助函数
# ============================================================================

def _promotion_scope_policy_ids(normalized_query: str) -> set[str]:
    """
    单一问题只引用对应规则；明确问叠加时才联合两类证据。

    策略矩阵：
    - 仅问促销（618/大促/满减等）→ 只返回 promotion_618_stack_rule
    - 仅问会员（会员券/金卡等）→ 只返回 member_coupon_gold_rule
    - 两者都问 → 返回两个 policy_id
    - 都不问 → 默认返回两个（保险策略）

    参数:
        normalized_query: 已归一化的查询字符串

    返回:
        set[str]: 允许的 policy_id 集合
    """
    # 检测是否问促销相关
    asks_promotion = any(term in normalized_query for term in ("618", "大促", "活动", "满减", "300减40"))

    # 检测是否问会员相关
    asks_member = any(term in normalized_query for term in ("会员", "会员券", "金卡", "优惠券"))

    # 根据检测结果决定返回哪些策略
    if asks_promotion and not asks_member:
        return {"promotion_618_stack_rule"}  # 仅促销
    if asks_member and not asks_promotion:
        return {"member_coupon_gold_rule"}   # 仅会员
    return {"promotion_618_stack_rule", "member_coupon_gold_rule"}  # 两者都有 或 都没有


def _rerank_promotion_candidates(
    user_message: str,
    candidates: list[tuple[Citation, float, list[str]]],
) -> list[tuple[Citation, float, list[str]]]:
    """
    轻量 reranker：在候选池里按当前问题的业务约束重新排序。

    这里刻意不调用外部商业模型，避免综合演练依赖网络；
    但保留 reranker 的核心闭环：初召回候选、按问题重排、citation 跟随最终排序。

    加权规则：
    1. 问大促 + 大促规则命中 → +0.18（强相关）
    2. 问会员 + 会员规则命中 → +0.16（强相关）
    3. 问题包含"叠加" → +0.08（鼓励联合引用）

    参数:
        user_message: 用户原始输入
        candidates: [(Citation, 原始分数, 召回原因), ...]

    返回:
        重排序后的 [(Citation, 最终分数, 加权原因), ...]
    """
    # 归一化处理
    normalized = user_message.replace(" ", "").lower()
    reranked: list[tuple[Citation, float, list[str]]] = []

    # 遍历每个候选
    for citation, score, reasons in candidates:
        policy_id = citation.metadata.get("policy_id") if citation.metadata else ""
        final_score = score  # 从原始分数开始
        final_reasons = list(reasons)  # 复制召回原因

        # 规则1：大促规则加权
        # 如果问的是大促相关，且命中的是大促规则，给予较高加权
        if policy_id == "promotion_618_stack_rule" and any(
            term in normalized for term in ("618", "满减", "300减40", "大促")
        ):
            final_score += 0.18
            final_reasons.append("当前大促规则加权")

        # 规则2：会员券规则加权
        # 如果问的是会员相关，且命中的是会员规则，给予较高加权
        if policy_id == "member_coupon_gold_rule" and any(
            term in normalized for term in ("金卡", "会员券")
        ):
            final_score += 0.16
            final_reasons.append("会员券条件匹配")

        # 规则3：叠加问题加权
        # 如果问的是叠加，无论什么规则都加一点，鼓励联合引用
        if "叠加" in normalized:
            final_score += 0.08
            final_reasons.append("叠加问题需要联合引用")

        # 截断到 [0, 1] 范围，保留3位小数
        reranked.append((citation, round(min(1.0, final_score), 3), final_reasons))

    # 按最终分数降序排列
    return sorted(reranked, key=lambda item: item[1], reverse=True)


def _build_rerank_debug(reranked: list[tuple[Citation, float, list[str]]]) -> dict[str, Any]:
    """
    把 rerank 结果压成公开调试状态，方便观察最终闭环。

    将重排序结果转换为结构化的调试信息，包含：
    - 模式标识
    - policy_id 列表
    - 每个 policy_id 的最终分数
    - 每个 policy_id 的加权原因

    参数:
        reranked: 重排序后的列表

    返回:
        调试信息字典
    """
    # 提取 policy_id 列表（按排序顺序）
    policy_ids = [
        citation.metadata.get("policy_id")
        for citation, _score, _reasons in reranked
        if citation.metadata
    ]

    return {
        "mode": "lightweight",  # 标识这是轻量 reranker
        "policy_ids": policy_ids,  # 排序后的 policy_id 列表
        "scores": {
            citation.metadata.get("policy_id"): score
            for citation, score, _reasons in reranked
            if citation.metadata
        },
        "reasons": {
            citation.metadata.get("policy_id"): reasons
            for citation, _score, reasons in reranked
            if citation.metadata
        },
    }


def _promotion_policy_answer(citations: list[Citation]) -> str:
    """
    按实际命中的 citation 组织回答，避免单一证据问题被迫套完整叠加规则。

    答案生成策略（证据驱动）：
    1. 大促 + 会员都存在 → 返回完整叠加规则
    2. 仅大促 → 只返回大促规则
    3. 仅会员 → 只返回会员规则
    4. 无证据 → 返回兜底话术

    参数:
        citations: 命中的 Citation 列表

    返回:
        对应的回答文本
    """
    # 提取所有 policy_id
    policy_ids = {citation.metadata.get("policy_id") for citation in citations if citation.metadata}

    # 策略1：大促 + 会员同时存在 → 完整叠加规则
    if {"promotion_618_stack_rule", "member_coupon_gold_rule"}.issubset(policy_ids):
        return (
            "根据跨境电商公司 618 大促规则，满 300 减 40 可以与平台会员券叠加，"
            "但不能与同类型满减券重复叠加；金卡会员券需要在有效期内由本人账号使用。"
        )

    # 策略2：仅大促 → 只回答大促规则
    if "promotion_618_stack_rule" in policy_ids:
        return "根据跨境电商公司 618 大促规则，满 300 减 40 活动可用，但不能与同类型满减券重复叠加。"

    # 策略3：仅会员 → 只回答会员规则
    if "member_coupon_gold_rule" in policy_ids:
        return "根据跨境电商公司会员券使用规则，金卡会员可领取平台会员券；会员券需在有效期内由本人账号使用，不能转让。"

    # 策略4：无证据 → 兜底话术（理论上不会执行到这里）
    return "没有检索到跨境电商公司已发布的可信活动或会员规则，我不能编造隐藏券规则。建议以活动页和结算页展示为准，或转人工客服进一步核实。"
