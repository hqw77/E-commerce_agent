from tools.planning import (
    build_route_plan,
    classify_guard_intent,
    extract_order_id,
)


def test_extract_order_id_from_chinese_text():
    assert extract_order_id("帮我查一下 SO20260602103000009-a1000009 的物流") == "SO20260602103000009-a1000009"
    assert extract_order_id("没有订单号") is None


def test_guard_catches_explicit_refund_and_return():
    assert classify_guard_intent("我要退款") == "refund_request"
    assert classify_guard_intent("我想申请退货") == "return_request"
    assert classify_guard_intent("今天天气怎么样") is None


def test_route_plan_blocks_order_tool_without_order_id():
    plan = build_route_plan(
        intent="order_query",
        user_message="帮我查一下物流",
        order_id=None,
        model_used=False,
    )
    assert plan.fallback_policy == "ask_order_id"
    assert plan.required_tools == []
    assert plan.needs_business_tools is True


def test_route_plan_allows_order_tool_with_order_id():
    plan = build_route_plan(
        intent="order_query",
        user_message="帮我查一下物流",
        order_id="SO20260602103000009-a1000009",
        model_used=True,
    )
    assert plan.required_tools == ["get_order_logistics"]
    assert plan.source == "llm_with_policy_constraints"
