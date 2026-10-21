from workflows.after_sale_graph import REFUND_APPROVAL_GRAPH


def test_unshipped_paid_order_is_eligible():
    order = {
        "orderNo": "SO20260601090000008-a1000008",
        "paymentStatus": "PAID",
        "fulfillmentStatus": "PAID_PENDING_SHIPMENT",
    }
    eligible, reason = REFUND_APPROVAL_GRAPH.assess_eligibility(order)
    assert eligible is True
    assert reason == "eligible"


def test_shipped_order_is_blocked():
    order = {
        "orderNo": "SO20260602103000009-a1000009",
        "paymentStatus": "PAID",
        "fulfillmentStatus": "SHIPPED",
        "logisticsStatus": "IN_TRANSIT",
    }
    eligible, reason = REFUND_APPROVAL_GRAPH.assess_eligibility(order)
    assert eligible is False
    assert reason == "order_already_shipped_or_not_eligible"


def test_missing_order_is_not_eligible():
    eligible, reason = REFUND_APPROVAL_GRAPH.assess_eligibility(None)
    assert eligible is False
    assert reason == "order_not_verified"
