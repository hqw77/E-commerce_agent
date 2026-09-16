"""
真实模型路由客户端 —— 决策层的「模型判断」部分。

【这个文件是干什么的】
决策层分两半：planning.py 用「规则」判断意图，本文件用「真实大模型」判断意图。
它回答：用户这句话该走哪条受控路径（RAG / Tool / Workflow / 拦截）。
被 customer_service_agent.chat() 调用，配合 planning.py 的规则分类做「双保险」。

【核心流程】
  ① can_call_model  检查能不能调模型（env 开关 / API key / 依赖是否装好）
  ② plan_intent     主入口：让模型判断意图，失败就回退到规则分类的结果
  ③ _extract_intent 从模型输出里解析 intent（JSON 解析 + 白名单校验）
  ④ _invoke_chain   用 LangChain RunnableSequence 跑「prompt → 模型 → 文本」

【关键设计：模型 + 规则双保险】
模型判断更准，但可能失败（没 key / 超时 / 输出不可信）；规则分类（planning.py）永远有兜底。
所以 plan_intent 的结果里带 fallback_reason，说明这次到底走了模型还是回退了规则。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from pydantic import BaseModel, Field

from api.schemas import Intent
from config.settings import api_key_is_missing, load_course_env, openai_base_url, openai_model_name
from prompts.loader import PromptManager, prompt_manager


class ModelRouteResult(BaseModel):
    """模型路由结果。模型不可用或输出不可信时，intent 用规则分类的结果。"""
    intent: Intent                              # 最终意图（模型判断 或 规则兜底）
    used_model: bool = False                    # 是否真的用了模型（False = 回退规则）
    model_name: str | None = None               # 用的模型名
    fallback_reason: str | None = None          # 回退原因（没走模型时，说明为什么）
    framework: str | None = None                # 用的框架（langchain_runnable_sequence）
    prompt_fragments: list[dict[str, Any]] = Field(default_factory=list)  # 选中的 prompt 片段


class RouteModelClient:
    """用真实大模型判断本轮应该进入哪条受控路径。"""

    def __init__(self, manager: PromptManager = prompt_manager) -> None:
        self.prompt_manager = manager

    def can_call_model(self) -> bool:
        """检查当前环境能否真实调用聊天模型（env 开关 / key / 依赖）。"""
        load_course_env()
        if os.getenv("AGENT_DISABLE_LLM") == "1":
            return False
        if api_key_is_missing(os.getenv("AGENT_OPENAI_API_KEY")):
            return False
        try:
            self._chat_model_class()
        except ImportError:
            return False
        return True

    def plan_intent(self, user_message: str, *, fallback_intent: Intent) -> ModelRouteResult:
        """
        主入口：优先用真实模型判断意图，失败时回退到规则分类（fallback_intent）。
        fallback_intent 来自 planning.classify_intent，是规则兜底的结果。
        """
        if not self.can_call_model():
            return ModelRouteResult(intent=fallback_intent, fallback_reason="model_config_missing")
        try:
            model = self._create_chat_model(temperature=0)
            fragments = self.prompt_manager.select_fragments({"phase": "route"})
            system_prompt = self.prompt_manager.render_system_prompt(fragments)
            content = self._invoke_chain(model, user_message, system_prompt)
            prompt_fragments = self.prompt_manager.selection_summary(fragments, phase="route")
            intent = self._extract_intent(content)
            if intent is None:
                # 模型输出不可信（解析不出合法 intent）→ 回退规则
                return ModelRouteResult(
                    intent=fallback_intent,
                    model_name=openai_model_name(),
                    fallback_reason="invalid_model_route",
                    prompt_fragments=prompt_fragments,
                )
            return ModelRouteResult(
                intent=intent,
                used_model=True,
                model_name=openai_model_name(),
                framework="langchain_runnable_sequence",
                prompt_fragments=prompt_fragments,
            )
        except Exception as exc:
            # 模型调用异常（超时/网络）→ 回退规则
            return ModelRouteResult(
                intent=fallback_intent,
                model_name=openai_model_name(),
                fallback_reason=exc.__class__.__name__,
            )

    @staticmethod
    def _extract_intent(content: str) -> Intent | None:
        """
        从模型输出里解析 intent。两道防护：
        1. 容错解析：模型可能输出裸 JSON 或带前后缀，先试直接 parse，再试正则抠 {…}
        2. 白名单校验：解析出的 intent 必须在 allowed 里，否则返回 None（触发回退）
        """
        allowed = {
            "general_chat",
            "order_query",
            "refund_status_query",
            "refund_request",
            "return_request",
            "faq_query",
            "promotion_query",
            "product_query",
            "low_confidence_query",
            "degradation_request",
            "security_request",
            "unknown",
        }
        text = content.strip()
        try:
            payload = json.loads(text)   # 先试直接解析
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, flags=re.S)  # 再试从文本里抠出 {…}
            if match is None:
                return None
            try:
                payload = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        intent = payload.get("intent")
        return intent if intent in allowed else None  # 白名单校验

    @staticmethod
    def _chat_model_class() -> Any:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI

    def _create_chat_model(self, *, temperature: float) -> Any:
        load_course_env()
        chat_model_class = self._chat_model_class()
        return chat_model_class(
            model=openai_model_name(),
            api_key=os.getenv("AGENT_OPENAI_API_KEY"),
            base_url=openai_base_url(),
            temperature=temperature,
            timeout=30,
            max_retries=0,
        )

    @staticmethod
    def _invoke_chain(model: Any, user_message: str, system_prompt: str) -> str:
        """用 LangChain RunnableSequence 串起「prompt → 模型 → 文本解析」。"""
        from langchain_core.messages import SystemMessage
        from langchain_core.output_parsers import StrOutputParser
        from langchain_core.prompts import ChatPromptTemplate

        prompt = ChatPromptTemplate.from_messages(
            [
                SystemMessage(content=system_prompt),
                ("human", "{user_message}"),
            ]
        )
        chain = prompt | model | StrOutputParser()
        return str(chain.invoke({"user_message": user_message}))
