"""Monthly diagnostic of published fundamental sorts, explicitly not a tradable stock engine."""

import math
from statistics import mean, stdev

FACTOR_MODELS = {
    "market": (("market",), False),
    "profit_high": (("profit_high",), False),
    "profit_low": (("profit_low",), False),
    "value_high": (("value_high",), False),
    "value_low": (("value_low",), False),
    "profit_value": (("profit_high", "value_high"), False),
    "profit_trend": (("profit_high",), True),
    "profit_value_trend": (("profit_high", "value_high"), True),
    "market_trend": (("market",), True),
}


def panel(datasets):
    series = {
        name: {r["month"]: r["returns"] for r in datasets[name]["rows"]}
        for name in ("profitability", "value", "market")
    }
    months = sorted(set.intersection(*(set(r) for r in series.values())))
    for a, b in zip(months, months[1:]):
        ay, am = map(int, a.split("-"))
        by, bm = map(int, b.split("-"))
        if 12 * by + bm != 12 * ay + am + 1:
            raise ValueError("Missing fundamental portfolio month")
    output = []
    for m in months:
        op, value, market = (series[k][m] for k in ("profitability", "value", "market"))
        row = {
            "profit_high": op["Hi 30"],
            "profit_low": op["Lo 30"],
            "value_high": value["Hi 30"],
            "value_low": value["Lo 30"],
            "rf": market["RF"],
            "market_excess": market["Mkt-RF"],
        }
        if any(v is None or not math.isfinite(v) or v <= -1 for v in row.values()):
            raise ValueError("Missing or invalid selected fundamental return")
        row["market"] = row["rf"] + row.pop("market_excess")
        output.append({"month": m, "returns": row})
    if not output:
        raise ValueError("Empty fundamental return panel")
    return output


def curve_metrics(values):
    returns = [b / a - 1 for a, b in zip(values, values[1:])]
    peak, drawdown = values[0], 0.0
    for value in values:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1)
    return {
        "cagr": (values[-1] / values[0]) ** (12 / len(returns)) - 1,
        "drawdown_monthly": drawdown,
        "volatility": stdev(returns) * math.sqrt(12) if len(returns) > 1 else 0,
        "total_return": values[-1] / values[0] - 1,
    }


def replay_factors(rows, model_id, start, end, *, annual_drag=0.0, switch_bps=7.5, fx=None):
    """Signals lag a full month for aggregate-data release uncertainty; costs are diagnostics."""
    if model_id not in FACTOR_MODELS:
        raise ValueError("Unknown fundamental model")
    if not 0 <= annual_drag < 1 or not 0 <= switch_bps < 1000:
        raise ValueError("Invalid fundamental cost scenario")
    months = [r["month"] for r in rows]
    active = [i for i, m in enumerate(months) if start <= m <= end]
    if len(active) < 2 or active[0] < 11 or end > months[-1]:
        raise ValueError("Fundamental study needs lagged ten-month warmup and observed end")
    chosen, trend = FACTOR_MODELS[model_id]
    levels = {s: [1.0] for s in chosen}
    balances = {s: 0.0 for s in chosen}
    cash, curve, details = 1.0, [1.0], []
    first, last = active[0], active[-1]
    for i, row in enumerate(rows[: last + 1]):
        if i >= first:
            # levels[-1] is previous month end. Exclude it: trade from data through t-2.
            target = {
                s: 1 / len(chosen)
                for s in chosen
                if not trend or levels[s][-2] > mean(levels[s][-11:-1])
            }
            before = cash + sum(balances.values())
            turnover = sum(abs(target.get(s, 0) - balances[s] / before) for s in chosen)
            switch_cost = before * turnover * switch_bps / 10000
            drag = (before - switch_cost) * (1 - (1 - annual_drag) ** (1 / 12))
            usable = before - switch_cost - drag
            returns = row["returns"]
            balances = {s: usable * target.get(s, 0) * (1 + returns[s]) for s in chosen}
            cash = usable * (1 - sum(target.values())) * (1 + returns["rf"])
            curve.append(cash + sum(balances.values()))
            details.append(
                {
                    "month": row["month"],
                    "signal_through": months[i - 2] if trend else None,
                    "weights": target,
                    "opening_equity": before,
                    "turnover": turnover,
                    "switch_cost": switch_cost,
                    "annual_drag_cost": drag,
                    "cash": cash,
                    "balances": dict(balances),
                    "equity": curve[-1],
                }
            )
        for s in chosen:
            levels[s].append(levels[s][-1] * (1 + row["returns"][s]))
    metrics = {"usd": curve_metrics(curve)}
    if fx is not None:
        needed = [months[first - 1]] + months[first : last + 1]
        if any(m not in fx or not math.isfinite(fx[m]) or fx[m] <= 0 for m in needed):
            raise ValueError("Missing fundamental monthly FX")
        converted = [v * fx[m] / fx[needed[0]] for v, m in zip(curve, needed)]
        metrics["krw"] = curve_metrics(converted)
    result = {
        "model": model_id,
        "start": months[first],
        "end": months[last],
        "months": len(details),
        "annual_drag": annual_drag,
        "switch_bps": switch_bps,
        "metrics": metrics,
        "mean_exposure": mean(sum(d["weights"].values()) for d in details),
        "turnover_sum": sum(d["turnover"] for d in details),
        "monthly": details,
        "performance_validated": False,
        "executable_portfolio": False,
    }
    result["audit"] = audit_factors(result, rows)
    return result


def audit_factors(result, rows):
    source = {r["month"]: r["returns"] for r in rows}
    before, maximum = 1.0, 0.0
    prior_balances = {}
    for row in result["monthly"]:
        weights = row["weights"]
        if (
            any(w < 0 or not math.isfinite(w) for w in weights.values())
            or sum(weights.values()) > 1 + 1e-12
        ):
            raise ValueError("Invalid fundamental allocation")
        if row["signal_through"] is not None and row["signal_through"] >= row["month"]:
            raise ValueError("Fundamental lookahead")
        turnover = sum(
            abs(weights.get(s, 0) - prior_balances.get(s, 0) / before)
            for s in set(weights) | set(prior_balances)
        )
        cost = before * turnover * result["switch_bps"] / 10000
        drag = (before - cost) * (1 - (1 - result["annual_drag"]) ** (1 / 12))
        expected = (before - cost - drag) * (
            sum(w * (1 + source[row["month"]][s]) for s, w in weights.items())
            + (1 - sum(weights.values())) * (1 + source[row["month"]]["rf"])
        )
        maximum = max(
            maximum,
            abs(expected - row["equity"]),
            abs(before - row["opening_equity"]),
            abs(cost - row["switch_cost"]),
            abs(drag - row["annual_drag_cost"]),
        )
        before = expected
        prior_balances = row["balances"]
    if maximum > 1e-8:
        raise ValueError("Fundamental return arithmetic mismatch")
    return {"monthly_arithmetic_verified": True, "maximum_error_units": maximum}
