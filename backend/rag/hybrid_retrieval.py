"""
混合检索 RAG 的【检索引擎层】（v1 的核心）。

【这个文件是干什么的】
RAG 模块分三层：documents.py(数据层) → 本文件(引擎层) → knowledge.py(编排层)。
本文件只负责「检索算法」：输入一个用户问题 + 意图，输出命中的知识引用(citation)列表。
它不生成最终回答（那是 models/answer_client 的事），也不做业务判断（那是 knowledge.py 的事）。

【怎么跑起来】
本文件不是入口，不会被直接运行。真正的调用链是：
    POST /chat → customer_service_agent.chat()
      → knowledge.py 的 promotion_policy_result() / invoice_faq_result()
        → 本文件的 retrieve_knowledge()   ← 唯一对外的入口函数
启动整个服务：cd backend && python main.py

【在线 / 离线两种 embedding 模式】
由环境变量 AGENT_OFFLINE_RAG 切换：
  - 未设置（在线）：用真实语义 embedding（硅基流动 OpenAIEmbeddings）
  - =1（离线）：用本地确定性向量 LocalTokenEmbeddings（不联网、可复现，仅演示）

【7 步流水线】
retrieve_knowledge() 内部是一条流水线：
  ① 查询改写  normalize_query + build_retrieval_plan
  ② 取索引    get_knowledge_index（切块 + 向量化 + 指纹版本，懒加载缓存）
  ③ 查缓存    cache_key = 版本|意图|改写问题
  ④ 向量召回  similarity_search_with_score（语义相似）
  ⑤ 关键词召回 _keyword_score（精确词）
  ⑥ 合并重排  max(向量,关键词) + 双路命中 +0.08
  ⑦ 出引用    load_knowledge_citation → Citation + 写缓存
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_text_splitters import RecursiveCharacterTextSplitter

from api.schemas import Citation, Intent
from config.settings import api_key_is_missing, embedding_model_name, load_course_env, openai_base_url
from rag.documents import load_knowledge_citation


# ============ 模块级常量 ============

# 知识文档目录：backend/knowledge/（本文件上一级的 knowledge 目录）
KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "knowledge"

# 参与检索的 4 个知识 md（注意：received_return_policy.md 不在这里，它是知识库里单独固定的引用）
KNOWLEDGE_FILES = (
    "after_sale_policy.md",       # 未发货退款 → policy_id: refund_before_shipping
    "payment_invoice_policy.md",  # 发票 FAQ → policy_id: invoice_issue
    "promotion_policy.md",        # 618 大促 → policy_id: promotion_618_stack_rule
    "member_coupon_policy.md",    # 会员券 → policy_id: member_coupon_gold_rule
)

# 检索结果缓存：key(哈希) -> HybridRetrievalResult。同一问题第二次直接复用，不重复检索
RAG_RETRIEVAL_CACHE: dict[str, "HybridRetrievalResult"] = {}

# 知识索引的全局单例：None = 还没构建，首次检索时才懒加载构建一次
_KNOWLEDGE_INDEX: "KnowledgeIndex | None" = None


# ============ 数据结构 ============

@dataclass
class KnowledgeIndex:
    """知识索引：构建一次、多次复用。"""
    version: str                     # 版本指纹（内容变了就变，用于缓存一致性）
    vector_store: InMemoryVectorStore  # 向量库（存 chunk 的 embedding）
    documents: list[Document]        # 切块后的文档列表（关键词召回遍历它）
    embedding_mode: str              # 用的哪种 embedding（在线/离线）


@dataclass
class HybridRetrievalResult:
    """retrieve_knowledge 的返回结果。"""
    citations: list[Citation]        # 命中的知识引用（最终交付物）
    debug: dict[str, Any]            # 调试信息（版本/召回明细/缓存命中等）


# ============ embedding ============

class LocalTokenEmbeddings(Embeddings):
    """
    离线替身 embedding：把文本拆成单字+双字，哈希映射到 256 维向量。
    确定性、可复现、不联网，但只是字符级匹配，不冒充真语义。
    仅当 AGENT_OFFLINE_RAG=1 时使用（见 build_course_embeddings）。
    """

    dimensions = 256

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        normalized = normalize_query(text)
        # tokens = 所有单字 + 所有相邻双字
        tokens = [*normalized, *[normalized[index : index + 2] for index in range(max(0, len(normalized) - 1))]]
        # 每个 token 哈希到一个 256 维的桶里，计数 +1（bag-of-tokens 的哈希版）
        vector = [0.0] * self.dimensions
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            vector[int.from_bytes(digest[:2], "big") % self.dimensions] += 1.0
        # L2 归一化：让向量只保留「方向」，方便算余弦相似度
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


def build_course_embeddings() -> tuple[Embeddings, str]:
    """
    按环境变量选择 embedding 实现，返回 (embedding 实例, 模式标识)。
    在线默认用真实语义 embedding；离线(AGENT_OFFLINE_RAG=1)用本地替身。
    """
    load_course_env()
    if os.getenv("AGENT_OFFLINE_RAG") == "1":
        return LocalTokenEmbeddings(), "local_token_embedding_for_explicit_offline_course"

    api_key = os.getenv("AGENT_OPENAI_API_KEY")
    if api_key_is_missing(api_key):
        raise RuntimeError(
            "RAG 需要 AGENT_OPENAI_API_KEY；仅离线测试可显式设置 AGENT_OFFLINE_RAG=1"
        )
    from langchain_openai import OpenAIEmbeddings

    model_name = embedding_model_name()
    return (
        OpenAIEmbeddings(
            model=model_name,
            api_key=api_key,
            base_url=openai_base_url(),
            request_timeout=30,
            max_retries=0,
            check_embedding_ctx_length=False,
        ),
        f"openai_compatible_embedding:{model_name}",
    )


def normalize_query(text: str) -> str:
    """查询改写：把口语别名换成知识文档里的标准词，提高检索命中率。"""
    return (
        re.sub(r"\s+", "", text.lower())     # 去所有空格 + 转小写
        .replace("叠券", "叠加会员券")
        .replace("优惠卷", "优惠券")          # 纠正错别字
        .replace("开票", "电子发票")
        .replace("退钱", "退款")
        .replace("那个", "")                  # 去掉无意义指代词
        .replace("这个", "")
    )


def build_retrieval_plan(query: str, intent: Intent) -> dict[str, Any]:
    """检索计划：改写查询 + 按意图限定知识域（缩小检索范围）。"""
    rewritten = normalize_query(query)
    domains = {
        "promotion_query": ["promotion", "member_coupon"],
        "faq_query": ["invoice"],
        "refund_request": ["after_sale"],
    }.get(intent, [])   # 其它意图返回空列表 = 不限制，全放开
    return {
        "original_query": query,
        "rewritten_query": rewritten,
        "intent": intent,
        "knowledge_domains": domains,
        "reason": "口语归一后按 RoutePlan 知识域执行向量与关键词双路召回。",
    }


def get_knowledge_index() -> tuple[KnowledgeIndex, bool]:
    """
    构建知识索引（懒加载 + 全局缓存），返回 (KnowledgeIndex, 是否缓存命中)。
    首次调用构建，之后直接复用。流程：读 md → 切块 → 向量化 → 算指纹版本 → 缓存。
    """
    global _KNOWLEDGE_INDEX
    if _KNOWLEDGE_INDEX is not None:
        return _KNOWLEDGE_INDEX, True   # 已构建过，直接返回（缓存命中）

    source_documents: list[Document] = []
    fingerprint_parts: list[str] = []
    for filename in KNOWLEDGE_FILES:
        path = KNOWLEDGE_DIR / filename
        text = path.read_text(encoding="utf-8")
        citation = load_knowledge_citation(filename)
        fingerprint_parts.append(f"{filename}:{text}")   # 内容进指纹：内容变了版本就变
        source_documents.append(
            Document(
                page_content=text.split("---", 2)[-1].strip(),  # 去掉 YAML 头，只留正文
                metadata={
                    "filename": filename,
                    "source": citation.source,
                    "title": citation.title,
                    "policy_id": (citation.metadata or {}).get("policy_id"),
                    "scene_key": (citation.metadata or {}).get("scene_key"),
                    "base_score": citation.score,
                },
            )
        )

    # 切块：220 字符一块，块间重叠 30，避免一句话被从中间切断
    splitter = RecursiveCharacterTextSplitter(chunk_size=220, chunk_overlap=30)
    chunks = splitter.split_documents(source_documents)
    for index, chunk in enumerate(chunks):
        chunk.metadata["chunk_id"] = f"{chunk.metadata['policy_id']}-chunk-{index + 1}"

    # 向量化 + 存进内存向量库
    embeddings, embedding_mode = build_course_embeddings()
    vector_store = InMemoryVectorStore(embeddings)
    vector_store.add_documents(chunks)

    # 指纹版本：内容 + embedding 模式一起哈希，任一变了版本号就变
    fingerprint_parts.append(f"embedding:{embedding_mode}")
    fingerprint = hashlib.sha256("\n".join(fingerprint_parts).encode("utf-8")).hexdigest()[:12]
    _KNOWLEDGE_INDEX = KnowledgeIndex(
        version=f"idx-{fingerprint}",
        vector_store=vector_store,
        documents=chunks,
        embedding_mode=embedding_mode,
    )
    return _KNOWLEDGE_INDEX, False


def retrieve_knowledge(query: str, intent: Intent, *, top_k: int = 4) -> HybridRetrievalResult:
    """
    【对外唯一入口】执行混合检索，返回命中的知识引用。

    参数:
        query:  用户问题（原始口语）
        intent: 意图（决定去哪些知识域检索）
        top_k:  最多返回几条引用（默认 4）

    返回:
        HybridRetrievalResult：citations（命中引用）+ debug（检索明细）

    内部是 7 步流水线（见文件头注释）：
      ① 查询改写  ② 取索引  ③ 查缓存  ④ 向量召回  ⑤ 关键词召回  ⑥ 合并重排  ⑦ 出引用+写缓存
    """
    # ① 查询改写 + 定知识域
    plan = build_retrieval_plan(query, intent)

    # ② 取索引（懒加载，首次构建）
    index, index_cache_hit = get_knowledge_index()

    # ③ 查缓存：key = 版本|意图|改写问题，命中直接返回
    cache_key = hashlib.sha256(f"{index.version}|{intent}|{plan['rewritten_query']}".encode("utf-8")).hexdigest()[:16]
    if cache_key in RAG_RETRIEVAL_CACHE:
        cached = RAG_RETRIEVAL_CACHE[cache_key]
        return HybridRetrievalResult(
            citations=list(cached.citations),
            debug={**cached.debug, "retrieval_cache_hit": True, "index_cache_hit": True},
        )

    domains = set(plan["knowledge_domains"])

    # ④ 向量召回：语义相似度（InMemoryVectorStore 返回的就是余弦相似度，越大越像）
    vector_pairs = index.vector_store.similarity_search_with_score(plan["rewritten_query"], k=top_k)
    vector_scores = {
        str(document.metadata["policy_id"]): float(score)
        for document, score in vector_pairs
        if _domain_allowed(str(document.metadata.get("scene_key")), domains)
    }

    # ⑤ 关键词召回：精确词匹配（抓向量容易漏掉的长尾词，如「618」「满减」）
    keyword_scores: dict[str, float] = {}
    for document in index.documents:
        if not _domain_allowed(str(document.metadata.get("scene_key")), domains):
            continue
        score = _keyword_score(plan["rewritten_query"], document.page_content)
        if score > 0:
            policy_id = str(document.metadata["policy_id"])
            # 同一个 policy 有多个 chunk 命中时，取最高分
            keyword_scores[policy_id] = max(keyword_scores.get(policy_id, 0.0), score)

    # ⑥ 合并重排：两路结果取并集，max(向量,关键词) + 双路命中 +0.08
    merged_ids = set(vector_scores) | set(keyword_scores)
    ranked: list[tuple[str, float, list[str]]] = []
    for policy_id in merged_ids:
        vector_score = vector_scores.get(policy_id, 0.0)
        keyword_score = keyword_scores.get(policy_id, 0.0)
        # 双路都命中加 0.08：两个独立证据互相印证，比单路更可信
        score = max(vector_score, keyword_score) + (0.08 if vector_score and keyword_score else 0.0)
        reasons = []
        if vector_score:
            reasons.append("vector召回")
        if keyword_score:
            reasons.append("keyword召回")
        ranked.append((policy_id, round(min(1.0, score), 3), reasons))
    ranked.sort(key=lambda item: item[1], reverse=True)   # 按最终分降序

    # ⑦ 出引用 + 写缓存
    filename_by_policy = {
        str(document.metadata["policy_id"]): str(document.metadata["filename"])
        for document in index.documents
    }
    citations: list[Citation] = []
    source_scores: dict[str, Any] = {}
    for policy_id, score, reasons in ranked[:top_k]:
        citation = load_knowledge_citation(filename_by_policy[policy_id])  # 读完整知识 → Citation
        citations.append(citation.model_copy(update={"score": score}))
        source_scores[policy_id] = {
            "vector": round(vector_scores.get(policy_id, 0.0), 3),
            "keyword": round(keyword_scores.get(policy_id, 0.0), 3),
            "final": score,
            "sources": reasons,
        }
    result = HybridRetrievalResult(
        citations=citations,
        debug={
            "mode": "langchain_inmemory_hybrid",
            "embedding": index.embedding_mode,
            "plan": plan,
            "index_version": index.version,
            "index_chunk_count": len(index.documents),
            "index_cache_hit": index_cache_hit,
            "retrieval_cache_hit": False,
            "cache_key": cache_key,
            "vector_policy_ids": list(vector_scores),
            "keyword_policy_ids": list(keyword_scores),
            "source_scores": source_scores,
        },
    )
    RAG_RETRIEVAL_CACHE[cache_key] = result   # 写缓存，下次同样问题直接复用
    return result


def reset_hybrid_index_and_cache() -> None:
    """重置索引和检索缓存（测试 / 手动重建用）。"""
    global _KNOWLEDGE_INDEX
    _KNOWLEDGE_INDEX = None
    RAG_RETRIEVAL_CACHE.clear()


def _domain_allowed(scene_key: str, domains: set[str]) -> bool:
    """知识域过滤：判断 scene_key 是否在允许的 domains 里。domains 为空则不限制。"""
    if not domains:
        return True
    # 把 scene_key 归一成 domain 名（payment_invoice → invoice，因为发票域统一用 invoice）
    mapping = {
        "promotion": "promotion",
        "member_coupon": "member_coupon",
        "invoice": "invoice",
        "payment_invoice": "invoice",
        "after_sale": "after_sale",
    }
    return mapping.get(scene_key, scene_key) in domains


def _keyword_score(query: str, text: str) -> float:
    """关键词打分：查询里的词有多少比例出现在文本里（至少命中一个词再 +0.15）。"""
    normalized_text = normalize_query(text)
    # 业务术语：查询里出现过的业务词
    business_terms = {
        term
        for term in (
            "618",
            "大促",
            "满减",
            "300减40",
            "会员券",
            "金卡",
            "叠加",
            "电子发票",
            "发票",
            "24小时",
            "退款",
            "未发货",
        )
        if term in query
    }
    # 中文双字：所有相邻的中文两字组合（抓「满减」这种没进术语库的词）
    chinese_bigrams = {
        query[index : index + 2]
        for index in range(max(0, len(query) - 1))
        if re.fullmatch(r"[\u4e00-\u9fff]{2}", query[index : index + 2])
    }
    # 英文/数字 token（抓「618」「24小时」里的数字）
    terms = business_terms | chinese_bigrams | set(re.findall(r"[a-z0-9]+", query))
    # 匹配比例：命中的词 / 总词数，命中越多分越高
    matched = [term for term in terms if term in normalized_text]
    return round(min(1.0, len(matched) / max(1, len(terms)) + (0.15 if matched else 0.0)), 3)
