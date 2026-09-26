"""Independent daily rules, explainable filters, sizing, and cost-aware replay."""

import math
from collections import defaultdict
from dataclasses import asdict, dataclass, fields
from statistics import mean, pstdev

from invest_lab.domain.indicators import rsi_wilder
from invest_lab.domain.research import (
    bars_at,
    decision_time,
    financial_features,
    finite,
    latest_visible,
    macro_features,
    timestamp,
    universe_at,
)


@dataclass(frozen=True)
class StrategyRules:
    strategy_type: str = "kospi_pullback"
    version: str = "research-v1"
    min_history: int = 125
    min_turnover: float = 0
    rsi_low: float = 35
    rsi_high: float = 50
    pullback_low: float = -1.5
    pullback_high: float = -0.25
    stop_atr: float = 2
    max_holding_sessions: int = 10
    max_positions: int = 5
    max_position_pct: float = 10
    max_total_pct: float = 50
    risk_per_trade_pct: float = 0.25
    max_sector_pct: float = 30
    max_index_pct: float = 20
    daily_loss_limit_pct: float = 1
    participation_pct: float = 1
    max_gap_pct: float = 3
    commission_bps: float = 1.5
    sell_tax_bps: float = 0
    slippage_bps: float = 5
    intraday_stop: bool = False
    use_quality: bool = False
    use_macro: bool = False
    max_vix: float = 35

    def __post_init__(self):
        if self.strategy_type not in {"kospi_pullback", "etf_trend"}:
            raise ValueError("지원 전략은 kospi_pullback/etf_trend입니다.")
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name in {"strategy_type", "version"}:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("전략 유형과 버전 문자열이 필요합니다.")
            elif field.name in {"intraday_stop", "use_quality", "use_macro"}:
                if type(value) is not bool:
                    raise ValueError(f"{field.name}은 bool이어야 합니다.")
            else:
                if isinstance(value, bool) or not isinstance(value, (float, int)):
                    raise ValueError(f"{field.name}은 숫자여야 합니다.")
                finite(value)
        for name in ("min_history", "max_holding_sessions", "max_positions"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name}은 양의 정수여야 합니다.")
        if self.min_history < 125 or not 0 <= self.rsi_low < self.rsi_high <= 100:
            raise ValueError("최소 이력/RSI 기준을 확인하세요.")
        if self.pullback_low >= self.pullback_high or self.stop_atr <= 0:
            raise ValueError("눌림/손절 기준을 확인하세요.")
        for name in (
            "max_position_pct",
            "max_total_pct",
            "risk_per_trade_pct",
            "max_sector_pct",
            "max_index_pct",
            "daily_loss_limit_pct",
            "participation_pct",
        ):
            if not 0 < getattr(self, name) <= 100:
                raise ValueError(f"{name}은 (0,100] 범위입니다.")
        for name in (
            "commission_bps",
            "sell_tax_bps",
            "slippage_bps",
            "max_gap_pct",
            "min_turnover",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name}은 음수가 될 수 없습니다.")

    @classmethod
    def from_dict(cls, values: dict):
        allowed = {f.name for f in fields(cls)}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError("알 수 없는 전략 설정: " + ", ".join(sorted(unknown)))
        return cls(**values)


def technical_features(bars: list[dict], benchmark=None) -> dict:
    if not bars:
        return {"history_count": 0, "missing_reasons": ["no_history"]}
    closes = [finite(b["close"], positive=True) for b in bars]
    for bar in bars:
        if finite(bar["volume"]) < 0:
            raise ValueError("거래량이 음수입니다.")
    values = {
        "history_count": len(bars),
        "close": closes[-1],
        "session": bars[-1]["session"],
        "returns": {},
        "sma": {},
        "missing_reasons": [],
    }
    for n in (1, 7, 30, 63, 90):
        values["returns"][str(n)] = (
            100 * (closes[-1] / closes[-1 - n] - 1) if len(closes) > n else None
        )
    for n in (20, 60, 90, 120, 200):
        values["sma"][str(n)] = mean(closes[-n:]) if len(closes) >= n else None
    values["sma120_slope5"] = (
        values["sma"]["120"] - mean(closes[-125:-5]) if len(closes) >= 125 else None
    )
    values["rsi14"] = rsi_wilder(closes)
    trs = [
        max(float(b["high"]) - float(b["low"]), abs(float(b["high"]) - c), abs(float(b["low"]) - c))
        for b, c in zip(bars[1:], closes)
    ]
    atr = None
    if len(trs) >= 14:
        atr = mean(trs[:14])
        for tr in trs[14:]:
            atr = (13 * atr + tr) / 14
    values["atr14"] = atr
    values["atr_pct"] = 100 * atr / closes[-1] if atr is not None else None
    values["pullback_atr"] = (
        (closes[-1] - values["sma"]["20"]) / atr if atr and values["sma"]["20"] else None
    )
    prev_volume = mean(float(b["volume"]) for b in bars[-21:-1]) if len(bars) >= 21 else 0
    values["volume_ratio"] = float(bars[-1]["volume"]) / prev_volume if prev_volume else None
    values["turnover20"] = (
        mean(float(b.get("turnover", 0)) for b in bars[-20:]) if len(bars) >= 20 else None
    )
    values["volume20"] = mean(float(b["volume"]) for b in bars[-20:]) if len(bars) >= 20 else None
    changes = (
        [math.log(b / a) for a, b in zip(closes[-21:], closes[-20:])] if len(closes) >= 21 else []
    )
    values["volatility20"] = pstdev(changes) * math.sqrt(252) if changes else None
    values["down_streak"] = 0
    for prev, current in zip(reversed(closes[:-1]), reversed(closes[1:])):
        if current >= prev:
            break
        values["down_streak"] += 1
    values["relative63"] = None
    if benchmark and len(closes) >= 64:
        bm = {b["session"]: float(b["close"]) for b in benchmark}
        first, last = bars[-64]["session"], bars[-1]["session"]
        if first in bm and last in bm:
            values["relative63"] = values["returns"]["63"] - 100 * (bm[last] / bm[first] - 1)
    if len(closes) < 125:
        values["missing_reasons"].append("indicator_warmup")
    return values


def evaluate_strategy(
    technical: dict,
    instrument: dict,
    rules: StrategyRules,
    *,
    financial=None,
    macro=None,
    etf=None,
    position=None,
) -> dict:
    reasons = []
    result = {
        "strategy_type": rules.strategy_type,
        "version": rules.version,
        "eligible": False,
        "action": "hold",
        "rank": None,
        "stop_distance": None,
        "reasons": reasons,
    }
    if technical.get("history_count", 0) < rules.min_history:
        reasons.append("insufficient_history")
        return result
    close, ma, atr = technical["close"], technical["sma"], technical["atr14"]
    if position is not None:
        exits = []
        if close < ma["120"]:
            exits.append("trend_exit")
        if close <= position.get("stop_price", -1):
            exits.append("close_stop")
        if rules.strategy_type == "kospi_pullback" and close >= ma["20"]:
            exits.append("pullback_recovered")
        if position.get("holding_sessions", 0) >= rules.max_holding_sessions:
            exits.append("holding_expiry")
        result["action"] = "sell" if exits else "hold"
        reasons.extend(exits)
        return result
    if instrument.get("tradable", True) is not True:
        reasons.append("not_tradable")
    if rules.strategy_type == "kospi_pullback":
        if instrument["kind"] != "kospi" or instrument.get("share_class", "common") != "common":
            reasons.append("not_kospi_common")
    elif not instrument["kind"].startswith("etf"):
        reasons.append("not_etf")
    elif (
        not etf
        or etf.get("leverage") != 1
        or etf.get("inverse") is not False
        or not etf.get("benchmark_id")
    ):
        reasons.append("etf_metadata_or_eligibility")
    if not (close > ma["120"] and technical["sma120_slope5"] > 0):
        reasons.append("uptrend_required")
    if atr is None or atr <= 0:
        reasons.append("invalid_atr")
    if (technical.get("turnover20") or 0) < rules.min_turnover:
        reasons.append("insufficient_liquidity")
    if rules.strategy_type == "kospi_pullback":
        if ma["60"] <= ma["120"]:
            reasons.append("medium_trend_required")
        distance = technical.get("pullback_atr")
        if distance is None or not rules.pullback_low <= distance <= rules.pullback_high:
            reasons.append("outside_pullback_band")
        if not rules.rsi_low <= technical["rsi14"] <= rules.rsi_high:
            reasons.append("outside_rsi_band")
        if technical["returns"]["1"] <= 0:
            reasons.append("recovery_not_confirmed")
    if rules.use_quality:
        f = (financial or {}).get("values", {})
        if instrument.get("sector") == "financial":
            reasons.append("financial_sector_quality_requires_separate_policy")
        elif not all(f.get(k, -math.inf) > 0 for k in ("roe", "earnings_yield", "cashflow_yield")):
            reasons.append("quality_missing_or_rejected")
    if rules.use_macro:
        vix = (macro or {}).get("values", {}).get("VIXCLS")
        if vix is None:
            reasons.append("macro_missing")
        elif vix > rules.max_vix:
            reasons.append("macro_risk_off")
    result["eligible"] = not reasons
    result["action"] = "buy" if not reasons else "excluded"
    result["stop_distance"] = rules.stop_atr * atr if atr else None
    if not reasons:
        # A transparent ranking, explicitly not a probability.
        result["rank"] = (technical.get("relative63") or 0) + technical[
            "sma120_slope5"
        ] / close * 100
    return result


def size_position(
    *,
    equity,
    cash,
    price,
    stop_distance,
    existing_value=0,
    total_value=0,
    sector_value=0,
    index_value=0,
    volume20=None,
    rules: StrategyRules,
) -> int:
    for value in (
        equity,
        cash,
        price,
        stop_distance,
        existing_value,
        total_value,
        sector_value,
        index_value,
    ):
        finite(value)
    if equity <= 0 or cash <= 0 or price <= 0 or stop_distance <= 0:
        return 0
    unit_cost = price * (1 + rules.commission_bps / 10000)
    exit_cost = price * (rules.commission_bps + rules.sell_tax_bps + rules.slippage_bps) / 10000
    bounds = [
        equity * rules.risk_per_trade_pct / 100 / (stop_distance + exit_cost),
        cash / unit_cost,
        (equity * rules.max_position_pct / 100 - existing_value) / unit_cost,
        (equity * rules.max_total_pct / 100 - total_value) / unit_cost,
        (equity * rules.max_sector_pct / 100 - sector_value) / unit_cost,
        (equity * rules.max_index_pct / 100 - index_value) / unit_cost,
    ]
    if volume20 is not None:
        bounds.append(volume20 * rules.participation_pct / 100)
    return max(0, math.floor(min(bounds)))


def replay(
    data: dict,
    rules: StrategyRules,
    *,
    initial_cash=10_000_000,
    start_session=None,
    end_session=None,
    benchmark_symbol=None,
    model_scores=None,
    model_threshold=None,
) -> dict:
    """Daily event replay. Orders decided at t close execute no earlier than t+1 open."""
    finite(initial_cash, positive=True)
    sessions = [
        s
        for s in data["sessions"]
        if (not start_session or s >= start_session) and (not end_session or s <= end_session)
    ]
    if len(sessions) < 2:
        raise ValueError("백테스트에는 최소 두 세션이 필요합니다.")
    instruments = {i["symbol"]: i for i in data["instruments"]}
    bars_by_symbol, actions_by_symbol, facts_by_symbol = (defaultdict(list) for _ in range(3))
    for row in data["bars"]:
        bars_by_symbol[row["symbol"]].append(row)
    for row in data["actions"]:
        actions_by_symbol[row["symbol"]].append(row)
    for row in data["financial_facts"]:
        facts_by_symbol[row["symbol"]].append(row)
    raw = {}
    for row in sorted(
        data["bars"], key=lambda r: (timestamp(r["available_at"]), timestamp(r["ingested_at"]))
    ):
        if row["price_basis"] == "raw":
            raw[(row["symbol"], row["session"])] = row
    positions, pending, entitlements, trades, curve, excluded = {}, [], [], [], [], []
    cash, peak = float(initial_cash), float(initial_cash)
    paid_events, applied_events = set(), set()
    total_cost = 0.0
    metadata = data["etf_metadata"]
    for index, session in enumerate(sessions):
        at = decision_time(session)
        day_start_equity = curve[-1]["equity"] if curve else float(initial_cash)
        actions = latest_visible(data["actions"], at, ("event_id",))
        for event in actions:
            if event["event_id"] in applied_events or event["effective_date"] > session:
                continue
            if event["effective_date"] < session:
                # Late corporate actions cannot silently mutate a past ledger.
                if event["effective_date"] >= sessions[0]:
                    raise ValueError(
                        "기업행동이 효력 세션 이후 알려졌습니다. 원문/효력 계약을 확인하세요."
                    )
                applied_events.add(event["event_id"])
                continue
            applied_events.add(event["event_id"])
            symbol = event["symbol"]
            if event["type"] == "split":
                ratio = float(event["ratio"])
                for order in pending:
                    if order["symbol"] == symbol and order["side"] == "buy":
                        order["reference_price"] /= ratio
                        order["stop_distance"] /= ratio
                        order["volume20"] *= ratio
            position = positions.get(symbol)
            if not position:
                continue
            if event["type"] == "split":
                qty = position["quantity"] * float(event["ratio"])
                if not qty.is_integer():
                    raise ValueError("분할 단주 정산 자료가 필요합니다.")
                position["quantity"] = int(qty)
                position["cost_per_share"] /= float(event["ratio"])
                position["stop_price"] /= float(event["ratio"])
            elif event["type"] in {"dividend", "distribution"}:
                entitlements.append(
                    {
                        "event_id": event["event_id"],
                        "payment_date": event["payment_date"],
                        "amount": position["quantity"] * float(event["amount"]),
                    }
                )
            elif event["type"] == "delisting":
                if "settlement_price" not in event:
                    raise ValueError("상장폐지 정산가격이 없어 포트폴리오를 재생할 수 없습니다.")
                settlement = finite(event["settlement_price"])
                cash += position["quantity"] * settlement
                trades.append(
                    {
                        "session": session,
                        "signal_session": session,
                        "symbol": symbol,
                        "side": "sell",
                        "quantity": position["quantity"],
                        "price": settlement,
                        "fee": 0,
                        "profit": position["quantity"] * (settlement - position["cost_per_share"]),
                        "reason": "delisting_supplied_net_settlement",
                    }
                )
                del positions[symbol]
        for item in entitlements:
            if item["payment_date"] <= session and item["event_id"] not in paid_events:
                cash += item["amount"]
                paid_events.add(item["event_id"])
        tradable_universe = universe_at(data, session, at)
        if not tradable_universe:
            raise ValueError(f"{session}: 당시 유니버스가 없습니다.")
        for symbol in positions:
            if (symbol, session) not in raw:
                raise ValueError(
                    f"{session} {symbol}: 보유 평가 봉 누락. 전일 값으로 채우지 않습니다."
                )
        orders, pending = pending, []
        for order in sorted(
            orders, key=lambda o: (o["side"] != "sell", -o.get("rank", 0), o["symbol"])
        ):
            symbol = order["symbol"]
            bar = raw.get((symbol, session))
            if bar is None or not bar.get("tradable", True) or int(bar["volume"]) == 0:
                excluded.append(
                    {
                        "session": session,
                        "symbol": symbol,
                        "reason": "unfilled_or_missing_execution_bar",
                    }
                )
                continue
            price = float(bar["open"]) * (
                1 + (1 if order["side"] == "buy" else -1) * rules.slippage_bps / 10000
            )
            if order["side"] == "sell":
                pos = positions.get(symbol)
                if not pos:
                    continue
                qty = pos["quantity"]
                fee = price * qty * (rules.commission_bps + rules.sell_tax_bps) / 10000
                cash += price * qty - fee
                trades.append(
                    {
                        "session": session,
                        "signal_session": order["signal_session"],
                        "symbol": symbol,
                        "side": "sell",
                        "quantity": qty,
                        "price": price,
                        "fee": fee,
                        "profit": qty * (price - pos["cost_per_share"]) - fee,
                        "reason": order["reason"],
                    }
                )
                total_cost += fee
                del positions[symbol]
            elif (
                symbol not in positions
                and len(positions) < rules.max_positions
                and symbol in tradable_universe
            ):
                if abs(price / order["reference_price"] - 1) * 100 > rules.max_gap_pct:
                    excluded.append({"session": session, "symbol": symbol, "reason": "gap_limit"})
                    continue
                marks = {
                    s: p["quantity"] * float(raw[(s, session)]["open"])
                    for s, p in positions.items()
                }
                total_value = sum(marks.values())
                equity = cash + total_value
                if equity < day_start_equity * (1 - rules.daily_loss_limit_pct / 100):
                    excluded.append(
                        {"session": session, "symbol": symbol, "reason": "daily_loss_entry_halt"}
                    )
                    continue
                sector = instruments[symbol].get("sector")
                index_group = order.get("index_group")
                qty = size_position(
                    equity=equity,
                    cash=cash,
                    price=price,
                    stop_distance=order["stop_distance"],
                    total_value=total_value,
                    sector_value=sum(
                        v
                        for s, v in marks.items()
                        if sector and instruments[s].get("sector") == sector
                    ),
                    index_value=sum(
                        v
                        for s, v in marks.items()
                        if index_group and positions[s].get("index_group") == index_group
                    ),
                    volume20=order["volume20"],
                    rules=rules,
                )
                if qty <= 0:
                    excluded.append(
                        {"session": session, "symbol": symbol, "reason": "risk_size_zero"}
                    )
                    continue
                fee = price * qty * rules.commission_bps / 10000
                cash -= price * qty + fee
                positions[symbol] = {
                    "quantity": qty,
                    "cost_per_share": price + fee / qty,
                    "stop_price": price - order["stop_distance"],
                    "entry_index": index,
                    "index_group": index_group,
                }
                trades.append(
                    {
                        "session": session,
                        "signal_session": order["signal_session"],
                        "symbol": symbol,
                        "side": "buy",
                        "quantity": qty,
                        "price": price,
                        "fee": fee,
                        "reason": "entry",
                    }
                )
                total_cost += fee
        if rules.intraday_stop:
            for symbol, pos in list(positions.items()):
                bar = raw[(symbol, session)]
                if float(bar["low"]) <= pos["stop_price"]:
                    price = min(float(bar["open"]), pos["stop_price"]) * (
                        1 - rules.slippage_bps / 10000
                    )
                    fee = (
                        price
                        * pos["quantity"]
                        * (rules.commission_bps + rules.sell_tax_bps)
                        / 10000
                    )
                    cash += price * pos["quantity"] - fee
                    trades.append(
                        {
                            "session": session,
                            "signal_session": session,
                            "symbol": symbol,
                            "side": "sell",
                            "quantity": pos["quantity"],
                            "price": price,
                            "fee": fee,
                            "profit": pos["quantity"] * (price - pos["cost_per_share"]) - fee,
                            "reason": "intraday_stop_conservative",
                        }
                    )
                    total_cost += fee
                    del positions[symbol]
        macro = macro_features(data["macro"], at)
        benchmark_rows = latest_visible(
            data["benchmarks"], at, ("symbol", "session", "price_basis")
        )
        etfs = {r["symbol"]: r for r in latest_visible(metadata, at, ("symbol",))}
        universe_history = {
            r["session"]: set(r["symbols"])
            for r in latest_visible(data["universe"], at, ("session",))
        }
        candidates = []
        for symbol in sorted(set(tradable_universe) | set(positions)):
            local_data = {"bars": bars_by_symbol[symbol], "actions": actions_by_symbol[symbol]}
            history = bars_at(local_data, symbol, session, at)
            if not history or history[-1]["session"] != session:
                excluded.append(
                    {"session": session, "symbol": symbol, "reason": "missing_final_bar"}
                )
                continue
            expected = {
                s
                for s in data["sessions"]
                if history[0]["session"] <= s <= session
                and symbol in universe_history.get(s, set())
            }
            if expected - {r["session"] for r in history}:
                excluded.append(
                    {"session": session, "symbol": symbol, "reason": "missing_trading_session"}
                )
                continue
            benchmark_id = etfs.get(symbol, {}).get(
                "benchmark_id", instruments[symbol].get("benchmark_id", "KOSPI")
            )
            benchmark = [
                r
                for r in benchmark_rows
                if r["symbol"] == benchmark_id and r["price_basis"] == "raw"
            ]
            technical = technical_features(history, benchmark)
            financial = financial_features(
                facts_by_symbol[symbol],
                symbol,
                at,
                history[-1].get("market_cap"),
            )
            pos = positions.get(symbol)
            result = evaluate_strategy(
                technical,
                instruments[symbol],
                rules,
                financial=financial,
                macro=macro,
                etf=etfs.get(symbol),
                position={**pos, "holding_sessions": index - pos["entry_index"]} if pos else None,
            )
            if result["action"] == "sell":
                pending.append(
                    {
                        "symbol": symbol,
                        "side": "sell",
                        "signal_session": session,
                        "reason": result["reasons"],
                    }
                )
            elif result["eligible"] and symbol not in positions:
                rank = result["rank"]
                if model_scores is not None:
                    rank = model_scores.get((session, symbol))
                    if rank is None or (model_threshold is not None and rank <= model_threshold):
                        excluded.append(
                            {
                                "session": session,
                                "symbol": symbol,
                                "reason": "model_unavailable_or_below_threshold",
                            }
                        )
                        continue
                candidates.append(
                    {
                        "symbol": symbol,
                        "side": "buy",
                        "signal_session": session,
                        "rank": rank,
                        "reference_price": float(history[-1]["close"]),
                        "stop_distance": result["stop_distance"],
                        "volume20": technical["volume20"],
                        "index_group": etfs.get(symbol, {}).get("benchmark_id"),
                    }
                )
        pending.extend(sorted(candidates, key=lambda o: (-o["rank"], o["symbol"]))[:20])
        holdings_value = sum(
            pos["quantity"] * float(raw[(symbol, session)]["close"])
            for symbol, pos in positions.items()
        )
        receivable = sum(e["amount"] for e in entitlements if e["event_id"] not in paid_events)
        equity = cash + holdings_value + receivable
        peak = max(peak, equity)
        curve.append(
            {
                "session": session,
                "cash": cash,
                "holdings_value": holdings_value,
                "receivable": receivable,
                "equity": equity,
                "drawdown": equity / peak - 1,
                "exposure": holdings_value / equity if equity else 0,
            }
        )
    exits = [t for t in trades if t["side"] == "sell"]
    benchmark_symbol = benchmark_symbol or "KOSPI"
    bm = [
        r
        for r in latest_visible(
            data["benchmarks"], decision_time(sessions[-1]), ("symbol", "session", "price_basis")
        )
        if r["session"] in sessions
        and r["symbol"] == benchmark_symbol
        and r["price_basis"] == "raw"
    ]
    bm = sorted(bm, key=lambda r: r["session"])
    return {
        "strategy": asdict(rules),
        "initial_cash": initial_cash,
        "equity_curve": curve,
        "trades": trades,
        "unfilled_or_excluded": excluded,
        "open_positions": positions,
        "benchmark": {
            "symbol": benchmark_symbol,
            "return_basis": "price",
            "comparison": "reference_index_not_a_currency_hedged_investable_portfolio",
        },
        "metrics": {
            "return": curve[-1]["equity"] / initial_cash - 1,
            "max_drawdown": min(p["drawdown"] for p in curve),
            "closed_trades": len(exits),
            "win_rate": mean(t["profit"] > 0 for t in exits) if exits else None,
            "net_realized_profit": sum(t["profit"] for t in exits),
            "fees_and_taxes": total_cost,
            "mean_exposure": mean(p["exposure"] for p in curve),
            "benchmark_return": float(bm[-1]["close"]) / float(bm[0]["close"]) - 1
            if len(bm) >= 2
            else None,
        },
        "assumptions": {
            "entry": "next_session_open",
            "intraday_stop": rules.intraday_stop,
            "tax_rate": "explicit_research_assumption_not_broker_tariff",
            "etf_fund_fee": "already_in_price_not_double_deducted",
            "end_positions": "marked_not_forced_liquidation",
            "same_day_stop_priority": "stop_first",
        },
    }
