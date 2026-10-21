"""路由守则:用确定性 Guard 覆盖模型误判."""

from __future__ import annotations

from models.router_client import ModelRouteResult
from tools.planning import classify_guard_intent


def apply_route_guard(*, user_message: str, route_result: ModelRouteResult) -> ModelRouteResult:
    """仅让明确边界覆盖模型;宽泛关键词只用于模型不可用时的 fallback."""
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
