"""Next-open ETF replay in a separate, unleveraged research account.

Prices represent explicitly labelled split/dividend-adjusted research units.
They are never executable share quantities or orders for a brokerage account.
"""

import bisect
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime
from statistics import mean

from invest_lab.domain.etf_strategies import EtfPolicy, allocation


def instant(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("ETF timestamps must include a timezone")
    return value


@dataclass(frozen=True)
class EtfReplayConfig:
    start: str
    end: str
    initial_krw: float = 10_000_000
    cash_currency: str = "KRW"
    fee_bps: float = 2.5
    slippage_bps: float = 5
    fx_bps: float = 10
    krw_cash_rate: float = 0.0
    usd_cash_rate: float = 0.0
    use_treasury_cash_rate: bool = False
    strict_pit: bool = False

    def __post_init__(self):
        if date.fromisoformat(self.start) >= date.fromisoformat(self.end):
            raise ValueError("Start must precede end")
        if self.cash_currency not in {"KRW", "USD"}:
            raise ValueError("Cash currency must be KRW or USD")
        for key in (
            "initial_krw",
            "fee_bps",
            "slippage_bps",
            "fx_bps",
            "krw_cash_rate",
            "usd_cash_rate",
        ):
            if not math.isfinite(getattr(self, key)):
                raise ValueError("ETF replay values must be finite")
        if self.initial_krw <= 0 or min(self.fee_bps, self.slippage_bps, self.fx_bps) < 0:
            raise ValueError("Invalid capital or transaction costs")
        if max(self.fee_bps, self.slippage_bps, self.fx_bps) >= 1000:
            raise ValueError("Invalid basis-point cost")
        if not 0 <= self.krw_cash_rate <= 1 or not 0 <= self.usd_cash_rate <= 1:
            raise ValueError("Invalid annual cash interest assumption")
        if self.use_treasury_cash_rate and self.cash_currency != "USD":
            raise ValueError("Treasury cash-rate proxy requires USD cash")


class VisibleSeries:
    """Revision-aware observations. A later vintage never replaces an earlier input early."""

    def __init__(self, rows):
        self.rows = sorted(rows, key=lambda r: (instant(r["available_at"]), r["date"]))
        self.position, self.days, self.values, self.last_at = 0, [], {}, None

    def at(self, at, maximum_age=14):
        at = instant(at) if isinstance(at, str) else at
        if self.last_at is not None and at < self.last_at:
            raise ValueError("Macro replay time must be monotonic")
        self.last_at = at
        while (
            self.position < len(self.rows)
            and instant(self.rows[self.position]["available_at"]) <= at
        ):
            row = self.rows[self.position]
            self.position += 1
            if date.fromisoformat(row["date"]) > at.date():
                raise ValueError("A macro observation cannot precede its observation date")
            value = row["value"]
            if value is None or not math.isfinite(value):
                raise ValueError("Missing macro values cannot become zero")
            if row["date"] not in self.values:
                bisect.insort(self.days, row["date"])
            self.values[row["date"]] = value
        if not self.days or (at.date() - date.fromisoformat(self.days[-1])).days > maximum_age:
            raise ValueError("Missing or stale visible macro history")
        return [self.values[d] for d in self.days[-300:]]


def validate_etf_dataset(dataset):
    if dataset.get("format_version") != 1 or dataset.get("source_kind") not in {
        "real",
        "synthetic",
    }:
        raise ValueError("Invalid ETF dataset format")
    if dataset.get("price_basis") != "dividend_split_adjusted_research_units":
        raise ValueError("Explicit adjusted-unit price basis is required")
    bars = dataset.get("bars", [])
    if len(bars) < 2 or not dataset.get("symbol"):
        raise ValueError("ETF data needs an instrument and trading sessions")
    days = [b["session"] for b in bars]
    if days != sorted(set(days)):
        raise ValueError("ETF sessions must be unique and sorted")
    for index, bar in enumerate(bars):
        day = date.fromisoformat(bar["session"])
        opening, closing = instant(bar["open_at"]), instant(bar["close_at"])
        if not opening < closing or opening.date() != day or closing.date() != day:
            raise ValueError("Invalid ETF trading timestamps")
        if index and instant(bars[index - 1]["close_at"]) >= opening:
            raise ValueError("ETF sessions overlap")
        for key in ("open", "close", "fx_open", "fx_close"):
            if not math.isfinite(bar[key]) or bar[key] <= 0:
                raise ValueError(f"Invalid ETF price/FX: {key}")
        if index + 1 < len(bars):
            expected = bars[index + 1]["session"][:7] != bar["session"][:7]
            if bool(bar["month_end"]) != expected:
                raise ValueError("Month-end marker disagrees with the supplied trading calendar")
    for series, rows in dataset.get("macro", {}).items():
        seen = set()
        for row in rows:
            key = (row["date"], instant(row["available_at"]))
            if key in seen or date.fromisoformat(row["date"]) > key[1].date():
                raise ValueError(f"Invalid duplicate/date in macro series: {series}")
            seen.add(key)
            if not isinstance(row["value"], (int, float)) or not math.isfinite(row["value"]):
                raise ValueError(f"Invalid macro value: {series}")
    return dataset


def replay_etf(dataset: dict, policy: EtfPolicy, config: EtfReplayConfig):
    validate_etf_dataset(dataset)
    if config.strict_pit and not dataset.get("provenance", {}).get("point_in_time_verified"):
        raise ValueError(
            "Strict PIT requested but this dataset has unverified historical availability"
        )
    bars = dataset["bars"]
    active = [i for i, b in enumerate(bars) if config.start <= b["session"] <= config.end]
    if len(active) < 2 or active[0] == 0:
        raise ValueError("Evaluation requires at least two sessions and preceding warmup")
    first, last = active[0], active[-1]
    required = set(policy.required_series)
    if config.use_treasury_cash_rate and config.cash_currency == "USD":
        required.add("RATE3M")
    macro = {name: VisibleSeries(dataset.get("macro", {}).get(name, [])) for name in required}
    cash_krw, cash_usd, units = config.initial_krw, 0.0, 0.0
    pending, daily, trades, events, decisions = None, [], [], [], []
    closes, month_closes = [], []
    previous_target = policy.defensive_weight if policy.family == "vix_recovery" else 0.0
    fee, slip, fx_fee = (v / 10_000 for v in (config.fee_bps, config.slippage_bps, config.fx_bps))
    total_cost_krw, interest_krw = 0.0, 0.0
    prev_day = bars[first]["session"]
    for i, bar in enumerate(bars[: last + 1]):
        day, opening = bar["session"], instant(bar["open_at"])
        if i >= first:
            elapsed = (date.fromisoformat(day) - date.fromisoformat(prev_day)).days
            if i == first and config.cash_currency == "USD":
                amount = cash_krw / (bar["fx_open"] * (1 + fx_fee))
                cost = cash_krw - amount * bar["fx_open"]
                events.append(
                    {
                        "session": day,
                        "kind": "initial_fx",
                        "krw": cash_krw,
                        "usd": amount,
                        "fx": bar["fx_open"],
                        "cost_krw": cost,
                    }
                )
                cash_usd, cash_krw = amount, 0.0
                total_cost_krw += cost
            rate = config.krw_cash_rate if config.cash_currency == "KRW" else config.usd_cash_rate
            if config.use_treasury_cash_rate and config.cash_currency == "USD":
                # Use information visible at the PREVIOUS session close for the entire gap.
                rate = max(0, macro["RATE3M"].at(bars[i - 1]["close_at"])[-1] / 100)
            balance = cash_krw if config.cash_currency == "KRW" else cash_usd
            interest = balance * rate * elapsed / 365
            if interest:
                if config.cash_currency == "KRW":
                    cash_krw += interest
                else:
                    cash_usd += interest
                interest_krw += interest * (1 if config.cash_currency == "KRW" else bar["fx_open"])
                events.append(
                    {
                        "session": day,
                        "kind": "interest",
                        "currency": config.cash_currency,
                        "amount": interest,
                        "rate": rate,
                        "days": elapsed,
                    }
                )
            if pending:
                if instant(pending["at"]) >= opening:
                    raise ValueError("Decision cannot execute at or before its signal time")
                fx, price = bar["fx_open"], bar["open"]
                equity_usd = cash_krw / fx + cash_usd + units * price
                current_weight = units * price / equity_usd
                target = pending["target"]
                desired = target * equity_usd / price
                if abs(current_weight - target) >= policy.rebalance_band or not daily:
                    side = "buy" if desired > units else "sell"
                    execution = price * (1 + slip if side == "buy" else 1 - slip)
                    if side == "buy":
                        available = (
                            cash_usd
                            if config.cash_currency == "USD"
                            else cash_krw / (fx * (1 + fx_fee))
                        )
                        qty = max(0.0, min(desired - units, available / (execution * (1 + fee))))
                    else:
                        qty = min(units, max(0.0, units - desired))
                    if qty > 1e-10:
                        notional, commission = qty * execution, qty * execution * fee
                        cash_amount = (
                            notional + commission if side == "buy" else notional - commission
                        )
                        fx_cost = cash_amount * fx * fx_fee if config.cash_currency == "KRW" else 0
                        if side == "buy":
                            if config.cash_currency == "KRW":
                                cash_krw -= cash_amount * fx + fx_cost
                            else:
                                cash_usd -= cash_amount
                            units += qty
                        else:
                            if config.cash_currency == "KRW":
                                cash_krw += cash_amount * fx - fx_cost
                            else:
                                cash_usd += cash_amount
                            units -= qty
                        cost = (commission + qty * price * slip) * fx + fx_cost
                        trade = {
                            "session": day,
                            "at": bar["open_at"],
                            "signal_at": pending["at"],
                            "side": side,
                            "units": qty,
                            "price": execution,
                            "reference_open": price,
                            "fx": fx,
                            "fee_usd": commission,
                            "fx_cost_krw": fx_cost,
                            "cost_krw": cost,
                            "target": target,
                            "reasons": pending["reasons"],
                        }
                        trades.append(trade)
                        events.append({"kind": "trade", **trade})
                        total_cost_krw += cost
                pending = None
            if min(cash_krw, cash_usd, units) < -1e-6:
                raise ValueError("ETF replay overspent cash or became short")
            cash_krw, cash_usd, units = max(0, cash_krw), max(0, cash_usd), max(0, units)
            equity = cash_krw + (cash_usd + units * bar["close"]) * bar["fx_close"]
            daily.append(
                {
                    "session": day,
                    "equity_krw": equity,
                    "equity_usd": equity / bar["fx_close"],
                    "cash_krw": cash_krw,
                    "cash_usd": cash_usd,
                    "units": units,
                    "close": bar["close"],
                    "fx": bar["fx_close"],
                    "exposure": units * bar["close"] * bar["fx_close"] / equity,
                }
            )
            prev_day = day
        closes.append(bar["close"])
        if bar["month_end"]:
            month_closes.append(bar["close"])
        initial_decision = i == first - 1
        scheduled = bar["month_end"] or policy.family == "vix_recovery"
        if i >= first - 1 and i < last and (initial_decision or scheduled):
            if policy.family == "buy_hold" and not initial_decision:
                continue
            visible = {}
            for name in policy.required_series:
                try:
                    visible[name] = macro[name].at(bar["close_at"], 21 if name == "NFCI" else 14)
                except ValueError as exc:
                    raise ValueError(f"{policy.id}/{name}/{day}: {exc}") from exc
            target, reasons = allocation(policy, closes, month_closes, visible, previous_target)
            pending = {"at": bar["close_at"], "target": target, "reasons": reasons}
            decisions.append({"session": day, **pending})
            previous_target = target
    result = {
        "symbol": dataset["symbol"],
        "policy": asdict(policy),
        "config": asdict(config),
        "start": daily[0]["session"],
        "end": daily[-1]["session"],
        "price_basis": dataset["price_basis"],
        "source_kind": dataset["source_kind"],
        "daily": daily,
        "trades": trades,
        "events": events,
        "decisions": decisions,
        "performance_validated": False,
        "limitations": dataset.get("provenance", {}).get("limitations", []),
        "metrics": metrics(daily, config.initial_krw, bars[first]["fx_open"]),
        "cost_krw": total_cost_krw,
        "interest_krw": interest_krw,
    }
    result["audit"] = audit_etf(result)
    return result


def metrics(daily, initial_krw, initial_fx):
    peak, drawdown, peak_day, longest = initial_krw, 0.0, daily[0]["session"], 0
    usd_peak, usd_drawdown = initial_krw / initial_fx, 0.0
    years, previous = {}, initial_krw
    for row in daily:
        value, day = row["equity_krw"], row["session"]
        if previous < peak:
            longest = max(longest, (date.fromisoformat(day) - date.fromisoformat(peak_day)).days)
        if value >= peak:
            peak, peak_day = value, day
        else:
            longest = max(longest, (date.fromisoformat(day) - date.fromisoformat(peak_day)).days)
        drawdown = min(drawdown, value / peak - 1)
        usd_peak = max(usd_peak, row["equity_usd"])
        usd_drawdown = min(usd_drawdown, row["equity_usd"] / usd_peak - 1)
        year = day[:4]
        if year not in years:
            years[year] = {"start_equity": previous, "return": 0.0}
        years[year]["return"] = value / years[year]["start_equity"] - 1
        previous = value
    elapsed = (
        date.fromisoformat(daily[-1]["session"]) - date.fromisoformat(daily[0]["session"])
    ).days
    return {
        "return_krw": daily[-1]["equity_krw"] / initial_krw - 1,
        "return_usd": daily[-1]["equity_usd"] / (initial_krw / initial_fx) - 1,
        "cagr_krw": (daily[-1]["equity_krw"] / initial_krw) ** (365.25 / elapsed) - 1,
        "max_drawdown_krw": drawdown,
        "max_drawdown_usd": usd_drawdown,
        "longest_underwater_days": longest,
        "unrecovered_at_end": daily[-1]["equity_krw"] < peak,
        "mean_exposure": mean(r["exposure"] for r in daily),
        "years": years,
        "end_equity_krw": daily[-1]["equity_krw"],
    }


def audit_etf(result):
    """Reconstruct cash/units/equity independently from trades and interest events."""
    cash_krw, cash_usd, units = result["config"]["initial_krw"], 0.0, 0.0
    index, maximum_error = 0, 0.0
    events = result["events"]
    if [e["session"] for e in events] != sorted(e["session"] for e in events):
        raise ValueError("ETF events must be chronological")
    ledger_trades = [
        {k: v for k, v in e.items() if k != "kind"} for e in events if e["kind"] == "trade"
    ]
    if ledger_trades != result["trades"]:
        raise ValueError("ETF trade and event ledgers disagree")
    total_cost = 0.0
    for row in result["daily"]:
        while index < len(events) and events[index]["session"] <= row["session"]:
            event = events[index]
            index += 1
            if event["kind"] == "initial_fx":
                expected = event["krw"] / (event["fx"] * (1 + result["config"]["fx_bps"] / 10000))
                if not math.isclose(expected, event["usd"], abs_tol=1e-8):
                    raise ValueError("Initial FX ledger mismatch")
                cash_krw -= event["krw"]
                cash_usd += event["usd"]
                total_cost += event["krw"] - event["usd"] * event["fx"]
            elif event["kind"] == "interest":
                balance = cash_krw if event["currency"] == "KRW" else cash_usd
                amount = balance * event["rate"] * event["days"] / 365
                if not math.isclose(amount, event["amount"], abs_tol=1e-8):
                    raise ValueError("Cash interest ledger mismatch")
                if event["currency"] == "KRW":
                    cash_krw += amount
                else:
                    cash_usd += amount
            else:
                if event["kind"] != "trade" or event["side"] not in {"buy", "sell"}:
                    raise ValueError("Invalid ETF ledger event")
                if instant(event["signal_at"]) >= instant(event["at"]):
                    raise ValueError("ETF ledger contains a lookahead execution")
                direction = 1 if event["side"] == "buy" else -1
                expected_price = event["reference_open"] * (
                    1 + direction * result["config"]["slippage_bps"] / 10000
                )
                if not math.isclose(expected_price, event["price"], abs_tol=1e-8):
                    raise ValueError("Slippage ledger mismatch")
                units += direction * event["units"]
                gross = event["units"] * event["price"]
                expected_fee = gross * result["config"]["fee_bps"] / 10000
                if not math.isclose(expected_fee, event["fee_usd"], abs_tol=1e-8):
                    raise ValueError("Trade fee ledger mismatch")
                delta_usd = -direction * gross - expected_fee
                if result["config"]["cash_currency"] == "USD":
                    cash_usd += delta_usd
                    expected_fx = 0
                else:
                    expected_fx = abs(delta_usd) * event["fx"] * result["config"]["fx_bps"] / 10000
                    if not math.isclose(expected_fx, event["fx_cost_krw"], abs_tol=1e-6):
                        raise ValueError("FX cost ledger mismatch")
                    cash_krw += delta_usd * event["fx"] - expected_fx
                total_cost += (
                    expected_fee + abs(event["price"] - event["reference_open"]) * event["units"]
                ) * event["fx"] + expected_fx
        equity = cash_krw + (cash_usd + units * row["close"]) * row["fx"]
        errors = [
            abs(equity - row["equity_krw"]),
            abs(cash_krw - row["cash_krw"]),
            abs((cash_usd - row["cash_usd"]) * row["fx"]),
            abs((units - row["units"]) * row["close"] * row["fx"]),
        ]
        maximum_error = max(maximum_error, *errors)
    if maximum_error > 0.01:
        raise ValueError("ETF cash/units/equity audit failed")
    if index != len(events) or min(cash_krw, cash_usd, units) < -1e-6:
        raise ValueError("ETF ledger has unprocessed events or negative balances")
    if not math.isclose(total_cost, result["cost_krw"], abs_tol=0.01):
        raise ValueError("ETF total cost mismatch")
    return {
        "ledger_verified": True,
        "maximum_error_krw": maximum_error,
        "performance_validated": False,
    }
