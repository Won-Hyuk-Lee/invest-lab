"""Multi-asset replay with a single capital budget and independent model states."""

import copy
import math
from dataclasses import asdict, dataclass
from datetime import date
from statistics import mean

from invest_lab.domain.auto_models import AutoModel, decide, features
from invest_lab.domain.etf_research import (
    EtfReplayConfig,
    VisibleSeries,
    instant,
    metrics,
    validate_etf_dataset,
)


@dataclass(frozen=True)
class AutoConfig(EtfReplayConfig):
    cash_currency: str = "USD"
    extra_delay: int = 0
    rebalance_band: float = 0.03

    def __post_init__(self):
        super().__post_init__()
        if type(self.extra_delay) is not int or not 0 <= self.extra_delay <= 5:
            raise ValueError("Extra execution delay must be 0..5 trading sessions")
        if not math.isfinite(self.rebalance_band) or not 0 <= self.rebalance_band < 1:
            raise ValueError("Invalid rebalance band")
        if self.use_treasury_cash_rate:
            raise ValueError(
                "This independent engine uses only explicit fixed cash-rate assumptions"
            )


def prepare(datasets, symbols):
    selected = {s: validate_etf_dataset(datasets[s]) for s in symbols}
    earliest = max(d["bars"][0]["session"] for d in selected.values())
    latest = min(d["bars"][-1]["session"] for d in selected.values())
    bars, signals = {}, {}
    for symbol, dataset in selected.items():
        full = dataset["bars"]
        computed = features(full)
        indices = [i for i, b in enumerate(full) if earliest <= b["session"] <= latest]
        bars[symbol] = [copy.deepcopy(full[i]) for i in indices]
        signals[symbol] = [computed[i] for i in indices]
    calendars = {tuple(b["session"] for b in rows) for rows in bars.values()}
    if len(calendars) != 1:
        raise ValueError("Missing or mismatched multi-asset trading sessions")
    sessions = list(next(iter(calendars)))
    anchor = bars[symbols[0]]
    for symbol, rows in bars.items():
        if any(
            (b["fx_open"], b["fx_close"], b["open_at"], b["close_at"], b["month_end"])
            != (a["fx_open"], a["fx_close"], a["open_at"], a["close_at"], a["month_end"])
            for a, b in zip(anchor, rows)
        ):
            raise ValueError(f"Inconsistent clock, FX or month-end across assets: {symbol}")
    return {
        "bars": bars,
        "features": signals,
        "sessions": sessions,
        "source_kind": "real"
        if all(d["source_kind"] == "real" for d in selected.values())
        else "synthetic",
        "datasets": selected,
    }


def replay(tape, model: AutoModel, config: AutoConfig, *, decide_fn=decide):
    symbols = model.symbols
    if set(symbols) - set(tape["bars"]):
        raise ValueError("Model assets are absent from this cohort")
    if config.strict_pit and any(
        not d.get("provenance", {}).get("point_in_time_verified") for d in tape["datasets"].values()
    ):
        raise ValueError("Strict PIT cannot use current revised snapshots")
    days = tape["sessions"]
    active = [i for i, d in enumerate(days) if config.start <= d <= config.end]
    if len(active) < 2 or active[0] == 0:
        raise ValueError("Replay needs preceding warmup and at least two evaluation sessions")
    first, last = active[0], active[-1]
    if date.fromisoformat(config.end) > date.fromisoformat(days[-1]):
        raise ValueError("Requested end exceeds observed history")
    anchor = tape["bars"][symbols[0]]
    needs_fear = model.kind in {"panic", "panic_recovery"}
    vix = VisibleSeries(tape["datasets"][symbols[0]].get("macro", {}).get("VIX", []))
    cash_krw, cash_usd = config.initial_krw, 0.0
    units, basis = {s: 0.0 for s in symbols}, {s: 0.0 for s in symbols}
    positions, state = {}, {}
    trades, events, decisions, daily = [], [], [], []
    pending, total_cost = None, 0.0
    fee, slip, fx_fee = [x / 10000 for x in (config.fee_bps, config.slippage_bps, config.fx_bps)]
    for i in range(first - 1, last + 1):
        bar, day = anchor[i], days[i]
        if i >= first:
            fx = bar["fx_open"]
            if i == first and config.cash_currency == "USD":
                cash_usd = cash_krw / (fx * (1 + fx_fee))
                cost = cash_krw - cash_usd * fx
                events.append(
                    {
                        "kind": "initial_fx",
                        "session": day,
                        "krw": cash_krw,
                        "usd": cash_usd,
                        "fx": fx,
                        "cost_krw": cost,
                    }
                )
                total_cost += cost
                cash_krw = 0.0
            elapsed = (
                (date.fromisoformat(day) - date.fromisoformat(days[i - 1])).days if i > first else 0
            )
            rate = config.usd_cash_rate if config.cash_currency == "USD" else config.krw_cash_rate
            balance = cash_usd if config.cash_currency == "USD" else cash_krw
            interest = balance * rate * elapsed / 365
            if interest:
                events.append(
                    {
                        "kind": "interest",
                        "session": day,
                        "currency": config.cash_currency,
                        "amount": interest,
                        "rate": rate,
                        "days": elapsed,
                    }
                )
                if config.cash_currency == "USD":
                    cash_usd += interest
                else:
                    cash_krw += interest
            if pending and pending["due"] == i:
                target = pending["weights"]
                if instant(pending["signal_at"]) >= instant(bar["open_at"]):
                    raise ValueError("Signals cannot execute before they exist")
                if (
                    set(target) - set(symbols)
                    or any(not math.isfinite(w) or w < 0 for w in target.values())
                    or sum(target.values()) > 1 + 1e-10
                ):
                    raise ValueError("Targets must fit one unleveraged capital budget")
                prices = {s: tape["bars"][s][i]["open"] for s in symbols}
                nav_usd = cash_krw / fx + cash_usd + sum(units[s] * prices[s] for s in symbols)
                desired = {}
                for s in symbols:
                    weight = target.get(s, 0)
                    current_weight = units[s] * prices[s] / nav_usd
                    if (
                        weight == 0
                        or abs(weight - current_weight) >= config.rebalance_band
                        or not daily
                    ):
                        desired[s] = weight * nav_usd / prices[s]
                # Release sale proceeds first; all buy quantities share the remaining cash.
                orders = [
                    (s, "sell", units[s] - q) for s, q in desired.items() if q < units[s] - 1e-10
                ]
                buy_orders = [
                    (s, "buy", q - units[s]) for s, q in desired.items() if q > units[s] + 1e-10
                ]
                for phase in (orders, buy_orders):
                    needed = sum(q * prices[s] * (1 + slip) * (1 + fee) for s, _, q in phase)
                    available = (
                        cash_usd
                        if config.cash_currency == "USD"
                        else cash_krw / (fx * (1 + fx_fee))
                    )
                    scale = (
                        min(1.0, max(0.0, available / needed))
                        if phase is buy_orders and needed
                        else 1.0
                    )
                    for s, side, quantity in phase:
                        quantity *= scale
                        if quantity <= 1e-10:
                            continue
                        direction = 1 if side == "buy" else -1
                        execution = prices[s] * (1 + direction * slip)
                        gross = quantity * execution
                        commission = gross * fee
                        cash_delta_usd = -direction * gross - commission
                        fx_cost = (
                            abs(cash_delta_usd) * fx * fx_fee
                            if config.cash_currency == "KRW"
                            else 0
                        )
                        previous_units = units[s]
                        cost = (commission + quantity * prices[s] * slip) * fx + fx_cost
                        total_cost += cost
                        if config.cash_currency == "USD":
                            cash_usd += cash_delta_usd
                        else:
                            cash_krw += cash_delta_usd * fx - fx_cost
                        if side == "buy":
                            basis[s] += -cash_delta_usd * fx + fx_cost
                            if previous_units <= 1e-10:
                                positions[s] = {
                                    "entry_index": i,
                                    "entry_price": execution,
                                    "peak": execution,
                                }
                            realized = None
                        else:
                            released_basis = basis[s] * quantity / previous_units
                            realized = cash_delta_usd * fx - fx_cost - released_basis
                            basis[s] -= released_basis
                        units[s] += direction * quantity
                        if units[s] < 1e-9:
                            units[s], basis[s] = 0.0, 0.0
                            positions.pop(s, None)
                        trade = {
                            "session": day,
                            "symbol": s,
                            "side": side,
                            "units": quantity,
                            "at": bar["open_at"],
                            "signal_at": pending["signal_at"],
                            "signal_session": pending["session"],
                            "price": execution,
                            "reference_open": prices[s],
                            "fx": fx,
                            "fee_usd": commission,
                            "fx_cost_krw": fx_cost,
                            "cost_krw": cost,
                            "realized_krw": realized,
                            "reason": pending["reason"],
                        }
                        trades.append(trade)
                        events.append({"kind": "trade", **trade})
                pending = None
            if min(cash_krw, cash_usd, *units.values()) < -1e-6:
                raise ValueError("Negative cash or a short position")
            cash_krw, cash_usd = max(0.0, cash_krw), max(0.0, cash_usd)
            prices = {s: tape["bars"][s][i]["close"] for s in symbols}
            invested = sum(units[s] * prices[s] for s in symbols) * bar["fx_close"]
            equity = cash_krw + cash_usd * bar["fx_close"] + invested
            daily.append(
                {
                    "session": day,
                    "equity_krw": equity,
                    "equity_usd": equity / bar["fx_close"],
                    "cash_krw": cash_krw,
                    "cash_usd": cash_usd,
                    "units": dict(units),
                    "prices": prices,
                    "fx": bar["fx_close"],
                    "exposure": invested / equity,
                }
            )
            for s in positions:
                positions[s]["peak"] = max(positions[s]["peak"], prices[s])
        if pending is None:
            visible = vix.at(bar["close_at"]) if needs_fear else None
            current = {s: tape["features"][s][i] for s in symbols}
            weights, reason = decide_fn(
                model, current, positions, state, i, bar["month_end"], i == first - 1, visible
            )
            if weights is not None:
                pending = {
                    "weights": weights,
                    "signal_at": bar["close_at"],
                    "session": day,
                    "due": i + 1 + config.extra_delay,
                    "reason": reason,
                }
                decisions.append({**pending, "indicators": current})
    result = {
        "policy": asdict(model),
        "symbol": "+".join(symbols),
        "config": asdict(config),
        "start": days[first],
        "end": days[last],
        "source_kind": tape["source_kind"],
        "price_basis": "dividend_split_adjusted_research_units",
        "initial_fx": anchor[first]["fx_open"],
        "daily": daily,
        "trades": trades,
        "events": events,
        "decisions": decisions,
        "pending": pending,
        "cost_krw": total_cost,
        "performance_validated": False,
    }
    result["policy"]["symbols"] = list(symbols)
    result["metrics"] = performance(result)
    result["audit"] = audit(result)
    return result


def performance(result):
    daily, config = result["daily"], result["config"]
    values = [config["initial_krw"]] + [d["equity_krw"] for d in daily]
    output = metrics(daily, config["initial_krw"], result["initial_fx"])
    for window in (252, 756):
        returns = [values[i] / values[i - window] - 1 for i in range(window, len(values))]
        output[f"rolling_{window}"] = {
            "count": len(returns),
            "positive_fraction": mean(r > 0 for r in returns) if returns else None,
            "worst": min(returns) if returns else None,
            "best": max(returns) if returns else None,
        }
    realized = [t["realized_krw"] for t in result["trades"] if t["realized_krw"] is not None]
    losses, wins = [v for v in realized if v < 0], [v for v in realized if v > 0]
    output["realized_sales"] = {
        "count": len(realized),
        "win_rate": len(wins) / len(realized) if realized else None,
        "profit_factor": sum(wins) / -sum(losses) if losses else None,
        "mean_gain_krw": mean(wins) if wins else None,
        "mean_loss_krw": mean(losses) if losses else None,
    }
    nav = {d["session"]: d["equity_krw"] for d in daily}
    output["turnover_sum"] = sum(
        t["units"] * t["reference_open"] * t["fx"] / nav[t["session"]] for t in result["trades"]
    )
    output["trade_count"] = len(result["trades"])
    return output


def audit(result):
    """Rebuild one portfolio without consulting the model's positions or target logic."""
    config = result["config"]
    krw, usd, units = config["initial_krw"], 0.0, {}
    allocations = {c["id"]: c["weight"] for c in result["policy"].get("components", [])}
    books = {
        key: {"krw": config["initial_krw"] * weight, "usd": 0.0, "units": {}, "basis": {}}
        for key, weight in (allocations or {None: 1}).items()
    }
    events = result["events"]
    if [e["session"] for e in events] != sorted(e["session"] for e in events):
        raise ValueError("Unsorted ledger")
    if [
        {k: v for k, v in e.items() if k != "kind"} for e in events if e["kind"] == "trade"
    ] != result["trades"]:
        raise ValueError("Trade and event ledgers disagree")
    cursor, maximum, cost = 0, 0.0, 0.0
    for row in result["daily"]:
        while cursor < len(events) and events[cursor]["session"] <= row["session"]:
            e = events[cursor]
            cursor += 1
            book = books[e.get("partition")]
            if e["kind"] == "initial_fx":
                expected = e["krw"] / (e["fx"] * (1 + config["fx_bps"] / 10000))
                if not math.isclose(e["usd"], expected, abs_tol=1e-8):
                    raise ValueError("Initial conversion mismatch")
                krw -= e["krw"]
                usd += e["usd"]
                book["krw"] -= e["krw"]
                book["usd"] += e["usd"]
                cost += e["krw"] - e["usd"] * e["fx"]
            elif e["kind"] == "interest":
                expected = book[e["currency"].lower()] * e["rate"] * e["days"] / 365
                if not math.isclose(expected, e["amount"], abs_tol=1e-7):
                    raise ValueError("Interest mismatch")
                book[e["currency"].lower()] += e["amount"]
                if e["currency"] == "USD":
                    usd += e["amount"]
                else:
                    krw += e["amount"]
            elif e["kind"] == "trade":
                s, q = e["symbol"], e["units"]
                direction = 1 if e["side"] == "buy" else -1
                if e["side"] not in {"buy", "sell"} or q <= 0:
                    raise ValueError("Invalid side/quantity")
                if instant(e["signal_at"]) >= instant(e["at"]):
                    raise ValueError("Lookahead fill in ledger")
                expected_price = e["reference_open"] * (
                    1 + direction * config["slippage_bps"] / 10000
                )
                fee = q * expected_price * config["fee_bps"] / 10000
                delta = -direction * q * expected_price - fee
                spread = (
                    abs(delta) * e["fx"] * config["fx_bps"] / 10000
                    if config["cash_currency"] == "KRW"
                    else 0
                )
                if not all(
                    math.isclose(a, b, abs_tol=1e-7)
                    for a, b in [
                        (expected_price, e["price"]),
                        (fee, e["fee_usd"]),
                        (spread, e["fx_cost_krw"]),
                    ]
                ):
                    raise ValueError("Execution cost mismatch")
                if config["cash_currency"] == "USD":
                    usd += delta
                    book["usd"] += delta
                else:
                    krw += delta * e["fx"] - spread
                    book["krw"] += delta * e["fx"] - spread
                if direction == 1:
                    book["basis"][s] = book["basis"].get(s, 0) - delta * e["fx"] + spread
                    if e["realized_krw"] is not None:
                        raise ValueError("Buy cannot realize a gain")
                else:
                    released = book["basis"][s] * q / book["units"][s]
                    expected_gain = delta * e["fx"] - spread - released
                    if not math.isclose(expected_gain, e["realized_krw"], abs_tol=0.01):
                        raise ValueError("Realized gain mismatch")
                    book["basis"][s] -= released
                book["units"][s] = book["units"].get(s, 0) + direction * q
                units[s] = units.get(s, 0) + direction * q
                cost += (fee + abs(expected_price - e["reference_open"]) * q) * e["fx"] + spread
            else:
                raise ValueError("Unknown ledger event")
            if min(krw, usd, *units.values()) < -1e-5:
                raise ValueError("Ledger overspent or became short")
            if min(book["krw"], book["usd"], *book["units"].values()) < -1e-5:
                raise ValueError("Capital partition overspent or became short")
        nav = krw + (usd + sum(q * row["prices"][s] for s, q in units.items())) * row["fx"]
        errors = [
            abs(nav - row["equity_krw"]),
            abs(krw - row["cash_krw"]),
            abs(usd - row["cash_usd"]) * row["fx"],
        ]
        errors += [
            abs(units.get(s, 0) - q) * row["prices"][s] * row["fx"] for s, q in row["units"].items()
        ]
        maximum = max(maximum, *errors)
    if (
        cursor != len(events)
        or maximum > 0.01
        or not math.isclose(cost, result["cost_krw"], abs_tol=0.01)
    ):
        raise ValueError("Portfolio cash/units/cost reconciliation failed")
    return {"ledger_verified": True, "maximum_error_krw": maximum, "performance_validated": False}


def blend(identifier, components):
    """Initial capital partitions; no cross-partition transfers or duplicated purchasing power."""
    if any(w <= 0 or not math.isfinite(w) for _, w in components) or not math.isclose(
        sum(w for _, w in components), 1
    ):
        raise ValueError("Blend weights must be positive and sum to one")
    template = components[0][0]
    if any(
        r["config"] != template["config"]
        or [d["session"] for d in r["daily"]] != [d["session"] for d in template["daily"]]
        or r["initial_fx"] != template["initial_fx"]
        for r, _ in components
    ):
        raise ValueError("Blend components need identical capital, dates, currency and costs")
    result = {
        k: copy.deepcopy(v)
        for k, v in template.items()
        if k not in {"daily", "events", "trades", "decisions", "pending", "metrics", "audit"}
    }
    result["policy"] = {
        "id": identifier,
        "kind": "initial_capital_partitions",
        "components": [{"id": r["policy"]["id"], "weight": w} for r, w in components],
    }
    result["symbol"] = "+".join(sorted({s for r, _ in components for s in r["daily"][0]["units"]}))
    result["daily"], result["events"], result["trades"] = [], [], []
    for index, row in enumerate(template["daily"]):
        joined = {"session": row["session"], "fx": row["fx"], "prices": {}, "units": {}}
        for key in ("equity_krw", "equity_usd", "cash_krw", "cash_usd"):
            joined[key] = sum(r["daily"][index][key] * w for r, w in components)
        for r, w in components:
            d = r["daily"][index]
            if d["fx"] != row["fx"]:
                raise ValueError("Blend FX mismatch")
            for s, q in d["units"].items():
                if s in joined["prices"] and joined["prices"][s] != d["prices"][s]:
                    raise ValueError("Blend price basis mismatch")
                joined["prices"][s] = d["prices"][s]
                joined["units"][s] = joined["units"].get(s, 0) + q * w
        joined["exposure"] = (
            sum(joined["units"][s] * p for s, p in joined["prices"].items())
            * row["fx"]
            / joined["equity_krw"]
        )
        result["daily"].append(joined)
    scalable = {
        "units",
        "fee_usd",
        "fx_cost_krw",
        "cost_krw",
        "realized_krw",
        "krw",
        "usd",
        "amount",
    }
    for r, w in components:
        for e in r["events"]:
            new = {k: v * w if k in scalable and v is not None else v for k, v in e.items()}
            new["partition"] = r["policy"]["id"]
            result["events"].append(new)
    # Every partition's initial conversion precedes any trade in the aggregate ledger.
    result["events"].sort(
        key=lambda e: (e["session"], {"initial_fx": 0, "interest": 1, "trade": 2}[e["kind"]])
    )
    result["trades"] = [
        {k: v for k, v in e.items() if k != "kind"}
        for e in result["events"]
        if e["kind"] == "trade"
    ]
    result["cost_krw"] = sum(r["cost_krw"] * w for r, w in components)
    result["metrics"] = performance(result)
    result["component_audits"] = [audit(r) for r, _ in components]
    result["audit"] = audit(result)
    return result
