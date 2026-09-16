"""
Tool 路的执行层 —— 用 LangChain create_agent 驱动「模型选工具 + 调工具」的只读闭环。

【这个文件是干什么的】
主链路第 ⑥ 步分派到 Tool 路后，由本文件执行。它回答：给定 RoutePlan 需要的工具白名单，
怎么让模型自主选择工具、调用工具、拿到结果、最终生成回答。
被 customer_service_agent.chat() 调用，run() 是唯一对外入口。

【怎么跑起来】
调用链：
    customer_service_agent.chat()
      → LangChainToolRunner.run()   ← 本文件入口
        → create_agent（LangChain）让模型自主调工具
          → 4 个内部函数 → tools/tool_runtime.py → integrations/ecommerce_client.py → Java 后端

【核心流程：run() 的闭环】
  ① 入口检查：required_tools 为空 或 模型不可用 → 直接跳过（不调模型）
  ② 参数校验：模型给的 order_id/keyword 必须等于受控 RoutePlan 的 expected_xxx，否则阻止执行
  ③ 工具白名单：从 required_tools 里挑出允许的工具，模型不能「发明」新工具
  ④ create_agent：让模型自主决定调哪个工具、调几次，直到给出最终回答
  ⑤ 收集结果：把 tool_calls、order、products、最终 answer 打包返回

【关键设计：只读工具 + 参数受控】
4 个工具全是「只读」（查订单/物流/退款/商品），高风险动作（退款/退货）留给 Workflow。
模型虽然能自主选工具，但参数受控——order_id 和 keyword 必须和 RoutePlan 确认的一致，
防止模型「张冠李戴」查错订单。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from api.schemas import ChatRequest, Intent, ToolCallTrace
from config.settings import api_key_is_missing, load_course_env, openai_base_url, openai_model_name
from hooks.manager import HookManager
from integrations.ecommerce_client import product_query_keyword
from prompts.loader import PromptManager, prompt_manager
from tools.tool_runtime import get_order_detail, get_order_logistics, get_refund_status, search_products


class OrderIdInput(BaseModel):
    """工具入参：订单号。必须来自用户输入或受控上下文。"""
    order_id: str = Field(..., description="跨境电商订单号，必须来自用户输入或受控上下文")


class ProductQueryInput(BaseModel):
    """工具入参：商品关键词。"""
    keyword: str = Field(..., description="用户明确提到的商品关键词，例如降噪耳机")


@dataclass
class LangChainToolResult:
    """run() 的返回结果，打包给编排层用。"""
    executed: bool = False                      # 是否真的执行了工具
    answer: str | None = None                   # 模型最终回答
    order: dict[str, Any] | None = None         # 查到的订单（若有）
    products: list[dict[str, Any]] = field(default_factory=list)  # 查到的商品
    tool_calls: list[ToolCallTrace] = field(default_factory=list)  # 工具调用记录
    state: dict[str, Any] = field(default_factory=dict)  # 调试状态
    prompt_fragments: list[dict[str, Any]] = field(default_factory=list)  # prompt 片段
    model_calls: int = 0                        # 模型调用次数


class LangChainToolRunner:
    """让模型只在 RoutePlan 白名单内选择只读工具，高风险动作仍留给 Workflow。"""

    def __init__(self, manager: PromptManager = prompt_manager) -> None:
        self.prompt_manager = manager

    def can_call_model(self) -> bool:
        """检查能否调用模型（env 开关 / key）。"""
        load_course_env()
        return os.getenv("AGENT_DISABLE_LLM") != "1" and not api_key_is_missing(
            os.getenv("AGENT_OPENAI_API_KEY")
        )

    def run(
        self,
        *,
        request: ChatRequest,
        intent: Intent,
        required_tools: list[str],
        hooks: HookManager,
        expected_order_id: str | None = None,
        expected_product_keyword: str | None = None,
        model: Any | None = None,
    ) -> LangChainToolResult:
        """
        主入口：执行「模型选工具 → 调工具 → 拿结果」的只读闭环。
        required_tools 是 RoutePlan 白名单；expected_order_id/keyword 是受控参数，用于校验模型。
        """
        # ① 入口检查：没工具可调 或 模型不可用 → 跳过
        if not required_tools:
            return LangChainToolResult(state=self._skipped_state("no_executable_tool", required_tools))
        if model is None and not self.can_call_model():
            return LangChainToolResult(state=self._skipped_state("model_config_missing", required_tools))

        # 捕获变量：内部函数往里填，最后打包返回
        captured_calls: list[ToolCallTrace] = []
        captured_order: dict[str, Any] | None = None
        captured_products: list[dict[str, Any]] = []

        # ② 参数校验：订单号必须和 RoutePlan 确认的一致，否则阻止执行
        def route_plan_mismatch(tool_name: str, order_id: str) -> ToolCallTrace | None:
            if expected_order_id is None or order_id == expected_order_id:
                return None
            call = ToolCallTrace(
                tool_name=tool_name,
                arguments={"order_id": order_id},
                output_summary="模型工具参数与受控 RoutePlan 的订单号不一致，已阻止执行。",
                status="error",
                risk_level="high",
                next_action="ask_clarification",
                error_type="route_plan_argument_mismatch",
            )
            hooks.pre_tool_call(tool_name, {"order_id": order_id}, request.runtime_user_id)
            hooks.post_tool_call(call)
            captured_calls.append(call)
            return call

        # ② 参数校验：商品关键词同理
        def product_route_plan_mismatch(keyword: str) -> ToolCallTrace | None:
            if expected_product_keyword is None or product_query_keyword(keyword) == product_query_keyword(expected_product_keyword):
                return None
            call = ToolCallTrace(
                tool_name="search_products",
                arguments={"keyword": keyword, "expected_keyword": expected_product_keyword},
                output_summary="模型工具参数与受控 RoutePlan 的商品关键词不一致，已阻止执行。",
                status="error",
                risk_level="medium",
                next_action="ask_clarification",
                error_type="route_plan_argument_mismatch",
            )
            hooks.pre_tool_call("search_products", {"keyword": keyword}, request.runtime_user_id)
            hooks.post_tool_call(call)
            captured_calls.append(call)
            return call

        # ③ 4 个工具的实现函数（模式一致：校验参数 → pre hook → 调 tool_runtime → post hook → 捕获）
            # 查询订单详情
        def query_order_detail(order_id: str) -> str:    
            nonlocal captured_order
            mismatch = route_plan_mismatch("get_order_detail", order_id)
            if mismatch:
                return json.dumps({"status": "error", "summary": mismatch.output_summary}, ensure_ascii=False)
            hooks.pre_tool_call("get_order_detail", {"order_id": order_id}, request.runtime_user_id)
            order, call = get_order_detail(order_id, request.runtime_user_id, request.runtime_context)
            hooks.post_tool_call(call)
            captured_calls.append(call)
            captured_order = order
            return json.dumps(
                {"status": call.status, "summary": call.output_summary, "order_verified": order is not None},
                ensure_ascii=False,
            )

            # 查询订单物流
        def query_order_logistics(order_id: str) -> str:  
            nonlocal captured_order
            mismatch = route_plan_mismatch("get_order_logistics", order_id)
            if mismatch:
                return json.dumps({"status": "error", "summary": mismatch.output_summary}, ensure_ascii=False)
            # 查物流前先查订单（物流依赖订单存在）
            hooks.pre_tool_call("get_order_detail", {"order_id": order_id}, request.runtime_user_id)
            order, detail_call = get_order_detail(order_id, request.runtime_user_id, request.runtime_context)
            hooks.post_tool_call(detail_call)
            captured_calls.append(detail_call)
            captured_order = order
            if order is None:
                return json.dumps({"status": detail_call.status, "summary": detail_call.output_summary}, ensure_ascii=False)
            hooks.pre_tool_call("get_order_logistics", {"order_id": order_id}, request.runtime_user_id)
            logistics_call = get_order_logistics(order)
            hooks.post_tool_call(logistics_call)
            captured_calls.append(logistics_call)
            return json.dumps(
                {"status": logistics_call.status, "summary": logistics_call.output_summary},
                ensure_ascii=False,
            )

            # 查询退款状态
        def query_refund_status(order_id: str) -> str:
            nonlocal captured_order
            mismatch = route_plan_mismatch("get_refund_status", order_id)
            if mismatch:
                return json.dumps({"status": "error", "summary": mismatch.output_summary}, ensure_ascii=False)
            hooks.pre_tool_call("get_order_detail", {"order_id": order_id}, request.runtime_user_id)
            order, detail_call = get_order_detail(order_id, request.runtime_user_id, request.runtime_context)
            hooks.post_tool_call(detail_call)
            captured_calls.append(detail_call)
            captured_order = order
            if order is None:
                return json.dumps({"status": detail_call.status, "summary": detail_call.output_summary}, ensure_ascii=False)
            hooks.pre_tool_call("get_refund_status", {"order_id": order_id}, request.runtime_user_id)
            status_call = get_refund_status(order, request.runtime_user_id)
            hooks.post_tool_call(status_call)
            captured_calls.append(status_call)
            return json.dumps({"status": status_call.status, "summary": status_call.output_summary}, ensure_ascii=False)

            # 查询产品
        def query_products(keyword: str) -> str:
            nonlocal captured_products
            mismatch = product_route_plan_mismatch(keyword)
            if mismatch:
                return json.dumps({"status": "error", "summary": mismatch.output_summary}, ensure_ascii=False)
            hooks.pre_tool_call("search_products", {"keyword": keyword}, request.runtime_user_id)
            captured_products, call = search_products(keyword)
            hooks.post_tool_call(call)
            captured_calls.append(call)
            return json.dumps({"status": call.status, "summary": call.output_summary}, ensure_ascii=False)

        # ③ 工具白名单：把上面 4 个函数包装成 StructuredTool，模型只能从 required_tools 里选
        tool_catalog: dict[str, BaseTool] = {
            "get_order_detail": StructuredTool.from_function(
                func=query_order_detail,
                name="get_order_detail",
                description="只读查询当前登录用户的订单详情；不能退款、取消或修改订单。",
                args_schema=OrderIdInput,
            ),
            "get_order_logistics": StructuredTool.from_function(
                func=query_order_logistics,
                name="get_order_logistics",
                description="只读查询当前登录用户的订单与物流事实；不能修改物流状态。",
                args_schema=OrderIdInput,
            ),
            "get_refund_status": StructuredTool.from_function(
                func=query_refund_status,
                name="get_refund_status",
                description="只读查询当前登录用户订单已经存在的退款申请状态；不能创建退款。",
                args_schema=OrderIdInput,
            ),
            "search_products": StructuredTool.from_function(
                func=query_products,
                name="search_products",
                description="按商品关键词查询跨境电商公司实时价格、库存和商品活动事实。",
                args_schema=ProductQueryInput,
            ),
        }
        tools = [tool_catalog[name] for name in required_tools if name in tool_catalog]

        # 组装 prompt + 模型
        fragments = self.prompt_manager.select_fragments(
            {
                "phase": "final_answer",
                "needs_business_tools": True,
                "needs_rag": intent in {"refund_request", "product_query"},
                "high_risk_after_sale": intent == "refund_request",
            }
        )
        system_prompt = self.prompt_manager.render_system_prompt(fragments)
        prompt_fragments = self.prompt_manager.selection_summary(fragments, phase="tool_agent")
        tool_model = model or self._create_chat_model()

        # ④ 核心闭环：create_agent 让模型自主选工具、调工具，多轮直到给出最终回答
        try:
            agent = create_agent(model=tool_model, tools=tools, system_prompt=system_prompt)
            # 把受控参数显式写进用户消息，告诉模型「订单号/关键词已经确认好了」
            controlled_user_message = request.user_message
            if expected_order_id:
                controlled_user_message += f"\n\n受控 RoutePlan 已确认订单号：{expected_order_id}"
            if expected_product_keyword:
                controlled_user_message += f"\n\n受控 RoutePlan 已确认商品查询词：{expected_product_keyword}"
            result = agent.invoke(
                {"messages": [{"role": "user", "content": controlled_user_message}]},
                config={"recursion_limit": 4},
            )
        except Exception as exc:
            # 模型调用异常：返回已捕获的工具调用 + 跳过状态
            return LangChainToolResult(
                executed=bool(captured_calls),
                order=captured_order,
                products=captured_products,
                tool_calls=captured_calls,
                state={
                    **self._skipped_state(exc.__class__.__name__, required_tools),
                    "create_agent": True,
                    "error_after_tool_execution": bool(captured_calls),
                },
                prompt_fragments=prompt_fragments,
            )

        # ⑤ 收集结果：从消息流里提取选中的工具、最终回答、调用次数
        messages: list[BaseMessage] = list(result.get("messages", []))
        selected_tools = [
            str(tool_call.get("name"))
            for message in messages
            if isinstance(message, AIMessage)
            for tool_call in message.tool_calls
        ]
        final_answer = next(
            (str(message.content) for message in reversed(messages) if isinstance(message, AIMessage) and message.content),
            None,
        )
        model_calls = sum(1 for message in messages if isinstance(message, AIMessage))
        return LangChainToolResult(
            executed=bool(captured_calls),
            answer=final_answer,
            order=captured_order,
            products=captured_products,
            tool_calls=captured_calls,
            prompt_fragments=prompt_fragments,
            model_calls=model_calls,
            state={
                "create_agent": True,
                "model": self._model_label(tool_model),
                "available_tools": [tool.name for tool in tools],
                "selected_tools": selected_tools,
                "message_types": [message.__class__.__name__ for message in messages],
                "tool_message_count": sum(isinstance(message, ToolMessage) for message in messages),
                "fallback_used": not bool(captured_calls),
            },
        )

    def _create_chat_model(self) -> Any:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=openai_model_name(),
            api_key=os.getenv("AGENT_OPENAI_API_KEY"),
            base_url=openai_base_url(),
            temperature=0,
            timeout=30,
            max_retries=0,
        )

    @staticmethod
    def _model_label(model: Any) -> str:
        return str(getattr(model, "model_name", None) or getattr(model, "model", None) or model.__class__.__name__)

    @staticmethod
    def _skipped_state(reason: str, required_tools: list[str]) -> dict[str, Any]:
        """构建「跳过执行」时的调试状态（没走模型/没调工具）。"""
        return {
            "create_agent": False,
            "skip_reason": reason,
            "available_tools": required_tools,
            "selected_tools": [],
            "message_types": [],
            "tool_message_count": 0,
            "fallback_used": True,
        }
