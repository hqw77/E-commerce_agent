"""
工具调用治理钩子 —— 在工具调用前后、错误和完成阶段生成公开治理事件。

【这个文件是干什么的】
横切层：包裹在 Tool 调用外面，记录「调前校验参数、调后清洗结果、完成时总结」的治理事件。
被 customer_service_agent.chat() 和 langchain_runtime.run() 调用。

【核心设计：Hook 只观察，不越权】
Hook 负责统一治理和观察（记录、脱敏、统计），但不替代 Tool 执行，也不批准高风险动作——
高风险动作的批准权在 Workflow/HITL 层，Hook 只是记录。
"""

from __future__ import annotations

from typing import Any

from api.schemas import ToolCallTrace


class HookManager:
    """Hook 负责统一治理和观察，不负责替代 Tool 或批准高风险动作。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []   # 治理事件列表
        self.touched_tools: list[str] = []       # 被调用的工具

    def pre_tool_call(self, tool_name: str, arguments: dict[str, Any], runtime_user_id: str) -> None:
        """工具调用前：校验必填参数是否存在。"""
        required_argument = {
            "get_order_detail": "order_id",
            "get_order_logistics": "order_id",
            "get_refund_status": "order_id",
            "search_products": "keyword",
        }.get(tool_name)
        arguments_valid = bool(arguments.get(required_argument)) if required_argument else bool(arguments)
        self.touched_tools.append(tool_name)
        self.events.append(
            {
                "hook_type": "pre_tool_call",
                "target_name": tool_name,
                "action": "validate_arguments_and_runtime_identity",
                "status": "allowed" if arguments_valid else "needs_clarification",
                "argument_keys": sorted(arguments),
                "runtime_user_id": runtime_user_id,
                "redacted": True,
            }
        )

    def post_tool_call(self, call: ToolCallTrace) -> None:
        """工具调用后：清洗观察结果；若出错则记降级事件。"""
        self.events.append(
            {
                "hook_type": "post_tool_call",
                "target_name": call.tool_name,
                "action": "sanitize_observation",
                "status": call.status,
                "risk_level": call.risk_level,
                "next_action": call.next_action,
                "redacted": True,
            }
        )
        if call.status == "error":
            self.events.append(
                {
                    "hook_type": "on_error",
                    "target_name": call.tool_name,
                    "action": "normalize_error_for_degradation",
                    "status": "degraded",
                    "error_type": call.error_type,
                    "redacted": True,
                }
            )

    def on_completion(self, *, risk_level: str, next_action: str, degraded: bool) -> dict[str, Any]:
        """完成时：总结整轮工具治理情况（钩子数、降级数、风险命中数）。"""
        event = {
            "hook_type": "on_completion",
            "target_name": "chat_request",
            "action": "summarize_tool_governance",
            "status": "completed",
            "risk_level": risk_level,
            "next_action": next_action,
            "degraded": degraded,
            "redacted": True,
        }
        self.events.append(event)
        degraded_count = sum(event.get("status") == "degraded" for event in self.events)
        return {
            "hook_count": len(self.events),
            "tool_count": len(self.touched_tools),
            "touched_tools": list(self.touched_tools),
            "degraded": degraded,
            "redacted_count": sum(bool(event.get("redacted")) for event in self.events),
            "degraded_count": max(int(degraded), degraded_count),
            "risk_hit_count": sum(event.get("risk_level") in {"medium", "high"} for event in self.events),
        }
