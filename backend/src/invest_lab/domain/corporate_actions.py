"""Share entitlement and quotation units are distinct during a consolidation halt."""

import math


def price_basis_date(event):
    return event.get("price_basis_date", event["effective_date"])


def split_position(position, event):
    ratio = float(event["ratio"])
    exact = position["quantity"] * ratio
    whole = math.floor(exact + 1e-9)
    fraction = max(0.0, exact - whole)
    if fraction and event.get("fractional_policy") not in {
        "cash_at_close",
        "sale_proceeds_unconfirmed",
        "cancel_without_compensation",
    }:
        raise ValueError("분할 단주 정산 자료가 필요합니다.")
    result = dict(position, quantity=whole)
    for field in (
        "cost_per_share",
        "stop_price",
        "entry_price",
        "initial_stop_distance",
        "high_watermark",
    ):
        if field in result:
            result[field] /= ratio
    entitlement = None
    if fraction:
        entitlement = {
            "event_id": event["event_id"] + ":fraction",
            "symbol": event["symbol"],
            "type": "fractional_cash",
            "quantity": fraction,
            "pricing_session": event.get("fractional_price_date"),
            "payment_date": event.get("payment_date"),
            "amount": None,
            "cost_basis": fraction * result["cost_per_share"],
            "valuation_policy": event["fractional_policy"],
        }
        if event["fractional_policy"] == "cancel_without_compensation":
            entitlement.update(amount=0.0, payment_date=event["effective_date"])
    return result, entitlement


def quotation_price(price, symbol, session, applied_splits):
    """Value new legal shares using old-unit suspended quotes without a fake gain."""
    factor = math.prod(
        float(event["ratio"])
        for event in applied_splits
        if event["symbol"] == symbol
        and event["effective_date"] <= session < price_basis_date(event)
    )
    return float(price) / factor
