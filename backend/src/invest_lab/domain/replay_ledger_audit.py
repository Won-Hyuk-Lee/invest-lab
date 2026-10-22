"""Rebuild cash, shares and fractional rights independently from replay output."""

import math
from collections import defaultdict
from datetime import datetime, timezone

from invest_lab.domain.research import decision_time, timestamp


def audit_ledger(data, result):
    raw = {}
    for row in sorted(
        data["bars"], key=lambda r: (timestamp(r["available_at"]), timestamp(r["ingested_at"]))
    ):
        if row["price_basis"] == "raw" and timestamp(row["available_at"]) <= decision_time(
            row["session"]
        ):
            raw[row["symbol"], row["session"]] = row
    grouped = defaultdict(list)
    for trade in result["trades"]:
        grouped[trade["session"]].append(trade)
    holdings, rights, applied, splits = {}, {}, set(), []
    cash = float(result["initial_cash"])
    max_error = defaultdict(float)
    start = result["equity_curve"][0]["session"]

    def quote(symbol, day):
        denominator = math.prod(
            float(a["ratio"])
            for a in splits
            if a["symbol"] == symbol
            and a["effective_date"] <= day < a.get("price_basis_date", a["effective_date"])
        )
        return float(raw[symbol, day]["close"]) / denominator

    for point in result["equity_curve"]:
        day = point["session"]
        opened = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
        visible = {}
        for action in sorted(
            data["actions"],
            key=lambda a: (timestamp(a["available_at"]), timestamp(a["ingested_at"])),
        ):
            if timestamp(action["available_at"]) <= opened:
                visible[action["event_id"]] = action
        for event_id, action in visible.items():
            if event_id in applied:
                prior = next((a for a in splits if a["event_id"] == event_id), None)
                if prior is not None and prior != action:
                    assert prior["ratio"] == action["ratio"]
                    if action["effective_date"] > day:
                        assert action["symbol"] not in holdings and event_id not in rights
                        applied.remove(event_id)
                        splits.remove(prior)
                        continue
                    prior.update(action)
                    if event_id in rights:
                        right = rights[event_id]
                        if right["amount"] is None:
                            right["pricing"] = action.get("fractional_price_date")
                            right["payment"] = action.get("payment_date")
                continue
            if action["effective_date"] > day:
                continue
            applied.add(event_id)
            symbol = action["symbol"]
            if action["type"] == "split":
                splits.append(dict(action))
            if action["effective_date"] < start or symbol not in holdings:
                continue
            qty, basis = holdings[symbol]
            if action["type"] == "split":
                exact = qty * float(action["ratio"])
                whole = math.floor(exact + 1e-9)
                fraction = max(0, exact - whole)
                holdings[symbol] = (whole, basis * whole / exact)
                if not whole:
                    holdings.pop(symbol)
                if fraction:
                    policy = action.get("fractional_policy")
                    rights[event_id] = {
                        "symbol": symbol,
                        "fraction": fraction,
                        "pricing": action.get("fractional_price_date"),
                        "amount": 0.0 if policy == "cancel_without_compensation" else None,
                        "payment": action["effective_date"]
                        if policy == "cancel_without_compensation"
                        else action.get("payment_date"),
                    }
            elif action["type"] in {"dividend", "distribution"}:
                rights[event_id] = {
                    "symbol": symbol,
                    "amount": qty * float(action["amount"]),
                    "payment": action.get("payment_date"),
                }
        for right in rights.values():
            if (
                not right.get("paid")
                and right.get("payment")
                and right["payment"] <= day
                and right["amount"] is not None
            ):
                cash += right["amount"]
                right["paid"] = True
        for trade in grouped[day]:
            symbol, qty, price, fee = (trade[key] for key in ("symbol", "quantity", "price", "fee"))
            if trade["side"] == "buy":
                assert symbol not in holdings, "unexpected duplicate holding"
                holdings[symbol] = (qty, qty * price + fee)
                cash -= qty * price + fee
            else:
                held, basis = holdings[symbol]
                assert 0 < qty <= held, "sell exceeds independently tracked shares"
                allocated = basis * qty / held
                expected_profit = qty * price - fee - allocated
                max_error["profit"] = max(
                    max_error["profit"], abs(expected_profit - trade["profit"])
                )
                cash += qty * price - fee
                if held == qty:
                    del holdings[symbol]
                else:
                    holdings[symbol] = (held - qty, basis - allocated)
        receivable = 0.0
        for right in rights.values():
            if right.get("paid"):
                continue
            if right["amount"] is None:
                mark = right["fraction"] * quote(right["symbol"], day)
                if right.get("pricing") and right["pricing"] <= day:
                    right["amount"] = mark
                receivable += mark
            else:
                receivable += right["amount"]
        invested = sum(qty * quote(symbol, day) for symbol, (qty, _) in holdings.items())
        for key, value in {
            "cash": cash,
            "holdings_value": invested,
            "receivable": receivable,
            "equity": cash + invested + receivable,
        }.items():
            max_error[key] = max(max_error[key], abs(value - point[key]))
        assert cash >= -1e-6
    assert {s: p[0] for s, p in holdings.items()} == {
        s: p["quantity"] for s, p in result["open_positions"].items()
    }
    assert max(max_error.values(), default=0) < 1e-5, dict(max_error)
    return {
        "ledger_verified": True,
        "maximum_errors": dict(max_error),
        "performance_validated": False,
        "scope": "cash_share_cost_and_entitlement_reconstruction",
        "note": "Ledger agreement does not certify action completeness or PIT timestamps.",
    }
