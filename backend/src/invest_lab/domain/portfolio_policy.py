"""Shared position lifecycle, exits and bounded portfolio replacement decisions."""

import math
from uuid import NAMESPACE_URL, uuid5


def holding_sessions(opened: str, as_of: str, sessions) -> int:
    return sum(opened <= session <= as_of for session in set(sessions))


def apply_managed_fill(allocation, *, side, quantity, price, session, order_id, context):
    """Only a new holding cycle initializes stops. Selling never resets them."""
    old = dict(allocation or {})
    held = old.get("managed_quantity", 0)
    if quantity <= 0 or price < 0:
        raise ValueError("체결 수량/가격이 올바르지 않습니다.")
    if side == "buy":
        if held == 0:
            distance = context.get("initial_stop_distance")
            if distance is None:
                distance = max(0, price - float(context.get("stop_price", price)))
            old = {
                "position_id": str(uuid5(NAMESPACE_URL, "position:" + order_id)),
                "opened_session": session,
                "managed_quantity": 0,
                "entry_price": price,
                "cost_basis_gross": 0.0,
                "initial_stop_distance": float(distance),
                "stop_price": price - float(distance),
                "high_watermark": price,
                "recovery_taken": False,
                "trailing_active": False,
                "status": "open",
            }
        old["managed_quantity"] = held + quantity
        if "cost_basis_gross" in old:
            old["cost_basis_gross"] += quantity * price
            old["entry_price"] = old["cost_basis_gross"] / old["managed_quantity"]
        else:
            old["ledger_incomplete"] = True
        old["high_watermark"] = max(old.get("high_watermark", price), price)
    elif side == "sell":
        if quantity > held:
            raise ValueError("관리 수량을 초과한 매도 체결입니다.")
        old["managed_quantity"] = held - quantity
        if "cost_basis_gross" in old:
            old["cost_basis_gross"] *= (held - quantity) / held
        if context.get("exit_kind") == "partial_recovery":
            old["recovery_taken"] = True
        if old["managed_quantity"] == 0:
            old.update(status="closed", closed_session=session)
    else:
        raise ValueError("알 수 없는 체결 방향입니다.")
    old["strategy_version"] = context.get("deployment_version")
    return old


def holding_score(technical):
    """Existing transparent signal, independently evaluated for held and unheld."""
    relative, slope, close = (
        technical.get("relative63"),
        technical.get("sma120_slope5"),
        technical.get("close"),
    )
    if relative is None or slope is None or not close:
        return None
    value = relative + slope / close * 100
    return value if math.isfinite(value) else None


def score_percentiles(scores):
    valid = {s: v for s, v in scores.items() if v is not None and math.isfinite(v)}
    ordered = sorted(valid.values())
    if len(ordered) < 2:
        return {s: 50.0 for s in valid}
    from bisect import bisect_left, bisect_right

    return {
        s: (bisect_left(ordered, v) + bisect_right(ordered, v) - 1) / 2 / (len(ordered) - 1) * 100
        for s, v in valid.items()
    }


def exit_decision(technical, position, rules, *, age):
    """Use the previous protection line; close-derived updates apply next session."""
    qty = position.get("quantity", position.get("managed_quantity", 1))
    close, ma = technical["close"], technical["sma"]
    old_stop = position.get("stop_price", -1)
    state = {
        "high_watermark": max(position.get("high_watermark", close), close),
        "trailing_active": position.get("trailing_active", False),
    }
    recovered = rules.strategy_type == "kospi_pullback" and close >= ma["20"]
    if rules.exit_policy != "recovery" and recovered:
        state["trailing_active"] = True
    atr = technical.get("atr14")
    if state["trailing_active"] and atr and atr > 0:
        state["stop_price"] = max(old_stop, state["high_watermark"] - rules.trailing_atr * atr)
    reasons = []
    if close <= old_stop:
        reasons.append("trailing_stop" if position.get("trailing_active") else "close_stop")
    if close < ma["120"]:
        reasons.append("trend_exit")
    limit = rules.max_holding_sessions + (
        rules.holding_extension_sessions if state["trailing_active"] else 0
    )
    if age >= limit:
        reasons.append("holding_expiry")
    distance = position.get("initial_stop_distance")
    entry = position.get("entry_price")
    if (
        rules.stagnation_sessions > 0
        and age >= rules.stagnation_sessions
        and entry is not None
        and distance
        and (state["high_watermark"] - entry) / distance < rules.min_progress_r
        and technical.get("relative63") is not None
        and technical["relative63"] < 0
    ):
        reasons.append("stagnation_exit")
    if recovered and rules.exit_policy == "recovery":
        reasons.append("pullback_recovered")
    if reasons:
        return {"quantity": qty, "reasons": reasons, "updates": state, "kind": "full"}
    if recovered and rules.exit_policy == "partial_trail" and not position.get("recovery_taken"):
        amount = max(1, math.floor(qty * rules.recovery_sell_pct / 100))
        return {
            "quantity": min(qty, amount),
            "reasons": ["partial_recovery"],
            "updates": state,
            "kind": "partial_recovery",
        }
    return {"quantity": 0, "reasons": [], "updates": state, "kind": "hold"}


def portfolio_risk(positions, prices, *, reserved=0.0):
    if reserved is None:
        return None
    risk = float(reserved)
    for symbol, position in positions.items():
        if symbol not in prices or position.get("stop_price") is None:
            return None
        quantity = position.get("quantity", position.get("managed_quantity", 0))
        risk += quantity * max(0, prices[symbol] - position["stop_price"])
    return risk


def pending_order_risk(orders):
    reserved = 0.0
    for order in orders:
        context = order.get("context", {})
        distance = context.get("initial_stop_distance")
        if distance is None:
            price = order.get("submitted_price") or order.get("limit_price")
            stop = context.get("stop_price")
            if price is None or stop is None:
                return None
            distance = float(price) - float(stop)
        if not math.isfinite(float(distance)) or float(distance) < 0:
            return None
        reserved += order["remaining_quantity"] * float(distance)
    return reserved


def risk_reductions(positions, prices, instruments, rules, *, equity):
    """Trim explicit risk/exposure excess only; an entry halt is not liquidation."""
    if not rules.rebalance_risk or portfolio_risk(positions, prices) is None:
        return {}
    remaining = {s: dict(p) for s, p in positions.items()}
    quantities = {s: p.get("quantity", p.get("managed_quantity", 0)) for s, p in positions.items()}
    result = {}
    ordered = sorted(
        positions,
        key=lambda s: (-quantities[s] * max(0, prices[s] - positions[s]["stop_price"]), s),
    )
    for symbol in ordered:
        pos, price = remaining[symbol], prices[symbol]
        if price <= 0 or not pos.get("tradable", True):
            continue
        sector = instruments[symbol].get("sector")
        index = pos.get("index_group")
        values = {s: quantities[s] * prices[s] for s in remaining}
        risk_per_share = max(0, price - pos["stop_price"])
        current_risk = sum(
            quantities[s] * max(0, prices[s] - p["stop_price"]) for s, p in remaining.items()
        )
        overages = [
            (sum(values.values()) - equity * rules.max_total_pct / 100) / price,
            (values[symbol] - equity * rules.max_position_pct / 100) / price,
        ]
        if sector:
            overages.append(
                (
                    sum(v for s, v in values.items() if instruments[s].get("sector") == sector)
                    - equity * rules.max_sector_pct / 100
                )
                / price
            )
        if index:
            overages.append(
                (
                    sum(v for s, v in values.items() if remaining[s].get("index_group") == index)
                    - equity * rules.max_index_pct / 100
                )
                / price
            )
        if risk_per_share:
            overages.append(
                (current_risk - equity * rules.max_open_risk_pct / 100) / risk_per_share
            )
        quantity = min(quantities[symbol], max(0, math.ceil(max(overages) - 1e-10)))
        # Mixed manual holdings may contribute risk, but only managed shares can be sold.
        quantity = min(quantity, pos.get("managed_quantity", quantities[symbol]))
        if quantity:
            result[symbol] = quantity
            quantities[symbol] -= quantity
    return result


def choose_rotations(holdings, candidates, percentiles, rules, *, day_index, cooldowns):
    """Heuristic rank/cost gates, never score minus return/percentage arithmetic."""
    if not rules.rotation_enabled:
        return []
    cost_bps = 2 * rules.commission_bps + rules.sell_tax_bps + 2 * rules.slippage_bps
    if cost_bps > rules.rotation_max_cost_bps:
        return []
    losers = sorted(
        (
            p
            for p in holdings
            if p.get("tradable", False)
            and not p.get("pending")
            and p.get("fully_managed", True)
            and p["age"] >= rules.rotation_min_holding
            and p["symbol"] in percentiles
            and percentiles[p["symbol"]] <= rules.rotation_exit_percentile
        ),
        key=lambda p: (percentiles[p["symbol"]], p["symbol"]),
    )
    winners = sorted(
        (
            c
            for c in candidates
            if c.get("quantity", 0) > 0
            and c["symbol"] in percentiles
            and percentiles[c["symbol"]] >= rules.rotation_entry_percentile
            and day_index - cooldowns.get(c["symbol"], -100000) > rules.rotation_cooldown
        ),
        key=lambda c: (-percentiles[c["symbol"]], c["symbol"]),
    )
    pairs = []
    for old, new in zip(losers, winners):
        gap = percentiles[new["symbol"]] - percentiles[old["symbol"]]
        if gap < rules.rotation_min_gap:
            continue
        pairs.append(
            {
                "sell_symbol": old["symbol"],
                "buy_symbol": new["symbol"],
                "held_percentile": percentiles[old["symbol"]],
                "candidate_percentile": percentiles[new["symbol"]],
                "score_gap": gap,
                "estimated_cost_bps": cost_bps,
            }
        )
        if len(pairs) >= rules.rotation_max_per_day:
            break
    return pairs
