"""业务后端集成层:通过 HTTP 获取订单、商品和售后事实.

生产路径只调用业务后端；只有显式开启 demo 模式时才回退到固定样例数据。
HTTP 失败返回 None/[],由上层决定降级或转人工，不编造业务事实。
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from config.settings import ecommerce_base_url, load_agent_env
from demo.seed_data import SEED_AFTER_SALE_MIRRORS, SEED_ORDER_MIRRORS, SEED_PRODUCT_MIRRORS


def seed_mirror_enabled() -> bool:
    """是否允许使用演示数据；生产环境通过启动校验禁止该开关."""
    return os.getenv("AGENT_DEMO_MODE") == "1"


def _with_fact_source(value: dict[str, Any], source: str) -> dict[str, Any]:
    """给数据打上来源标记,便于 Trace 和排障."""
    return {**value, "_fact_source": source}


def delegated_service_headers(current_user_id: str | None) -> dict[str, str]:
    """构造服务身份 + 用户身份头."""
    load_agent_env()
    user_id = str(current_user_id or "").strip()
    token = os.getenv(
        "AGENT_ECOMMERCE_SERVICE_TOKEN",
        os.getenv("AGENT_SERVICE_AUTH_TOKEN", "demo-agent-service"),
    ).strip()
    if not user_id or not token:
        return {}
    return {"X-Agent-Service-Token": token, "X-Agent-User-Id": user_id}


def ecommerce_get(
    path: str,
    *,
    delegated_user_id: str | None = None,
) -> dict[str, Any] | list[Any] | None:
    """封装业务后端 GET 调用,统一处理响应结构和错误边界."""
    # trust_env=False:不继承宿主机 HTTP 代理,避免本地请求被代理劫持
    with httpx.Client(timeout=5, trust_env=False) as client:
        response = client.get(
            f"{ecommerce_base_url()}{path}",
            headers=delegated_service_headers(delegated_user_id),
        )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return None
    return payload.get("data")


def order_fact_from_ecommerce(target_order_no: str, current_user_id: str) -> dict[str, Any] | None:
    """从业务后端读订单事实(含商品可退属性);demo 模式失败时回退样例数据."""
    try:
        order = ecommerce_get(f"/api/orders/{target_order_no}", delegated_user_id=current_user_id)
    except Exception:
        order = None
    if isinstance(order, dict):
        enriched = _with_fact_source(order, "business_api")
        items = enriched.get("items") or []
        product_facts: list[dict[str, Any]] = []
        for item in items:
            # 无 productId 的 item 直接保留;有 productId 的再去查商品的可退属性
            if not isinstance(item, dict) or item.get("productId") is None:
                product_facts.append(item)
                continue
            try:
                product = ecommerce_get(f"/api/products/{item['productId']}")
            except Exception:
                product = None
            product_facts.append({**item, "returnable": product.get("returnable")} if isinstance(product, dict) else item)
        if product_facts:
            enriched["items"] = product_facts
            # 汇总商品可退属性到订单级:全 True -> True;有 False -> False;否则 None
            returnability = [item.get("returnable") for item in product_facts if isinstance(item, dict)]
            if returnability and all(value is True for value in returnability):
                enriched["returnable"] = True
            elif any(value is False for value in returnability):
                enriched["returnable"] = False
            else:
                enriched["returnable"] = None
        return enriched
    if not seed_mirror_enabled():
        return None
    mirror = SEED_ORDER_MIRRORS.get(target_order_no)
    return _with_fact_source(mirror, "seed_mirror") if mirror else None


def products_from_ecommerce(keyword: str) -> list[dict[str, Any]]:
    """查商品实时事实;demo 模式失败时回退样例商品."""
    try:
        query_keyword = product_query_keyword(keyword)
        products = ecommerce_get(f"/api/products?{httpx.QueryParams({'keyword': query_keyword})}")
    except Exception:
        products = None
    if isinstance(products, list):
        return [_with_fact_source(item, "business_api") for item in products if isinstance(item, dict)]
    if not seed_mirror_enabled():
        return []
    normalized = keyword.replace(" ", "")
    return [
        _with_fact_source(item, "seed_mirror")
        for item in SEED_PRODUCT_MIRRORS
        if any(term in normalized for term in ("耳机", "降噪", "通勤")) and "耳机" in str(item.get("name"))
    ]


def product_query_keyword(user_message: str) -> str:
    """把自然语言商品咨询收窄成业务后端可搜索的关键词."""
    for term in ("降噪蓝牙耳机", "降噪耳机", "耳机", "音箱", "充电器", "投影仪", "键盘"):
        if term in user_message:
            return "降噪" if term in {"降噪蓝牙耳机", "降噪耳机"} else term
    return user_message.strip()


def after_sale_requests_from_ecommerce(order_id: str, current_user_id: str) -> list[dict[str, Any]] | None:
    """按订单号读售后进度;None=接口不可用,空列表=确认无记录."""
    try:
        order = order_fact_from_ecommerce(order_id, current_user_id)
        if not current_user_id:
            raise ValueError("missing_current_user_id")
        requests = ecommerce_get(
            f"/api/after-sale/requests?orderNo={order_id}",
            delegated_user_id=current_user_id,
        )
    except Exception:
        requests = None
    if isinstance(requests, list):
        return [_with_fact_source(item, "business_api") for item in requests if isinstance(item, dict)]
    if not seed_mirror_enabled():
        return None
    return [_with_fact_source(item, "seed_mirror") for item in SEED_AFTER_SALE_MIRRORS.get(order_id, [])]
