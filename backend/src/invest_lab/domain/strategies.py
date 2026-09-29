"""Independent daily rules, explainable filters, sizing, and cost-aware replay."""

import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import datetime, time, timezone
from statistics import mean, pstdev

from invest_lab.domain.corporate_actions import quotation_price, split_position
from invest_lab.domain.indicators import rsi_wilder
from invest_lab.domain.portfolio_policy import (
    choose_rotations,
    exit_decision,
    holding_score,
    portfolio_risk,
    risk_reductions,
    score_percentiles,
)
from invest_lab.domain.research import (
    bars_at,
    content_hash,
    decision_time,
    financial_features,
    finite,
    latest_visible,
    macro_features,
    timestamp,
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
    exit_policy: str = "recovery"
    trailing_atr: float = 2
    recovery_sell_pct: float = 50
    holding_extension_sessions: int = 0
    stagnation_sessions: int = 0
    min_progress_r: float = 0.25
    max_initial_stop_pct: float = 100
    max_open_risk_pct: float = 100
    rebalance_risk: bool = False
    candidate_limit: int = 20
    rotation_enabled: bool = False
    rotation_min_holding: int = 3
    rotation_cooldown: int = 3
    rotation_max_per_day: int = 1
    rotation_entry_percentile: float = 85
    rotation_exit_percentile: float = 60
    rotation_min_gap: float = 20
    rotation_max_cost_bps: float = 50

    def __post_init__(self):
        if self.strategy_type not in {"kospi_pullback", "etf_trend"}:
            raise ValueError("지원 전략은 kospi_pullback/etf_trend입니다.")
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name in {"strategy_type", "version", "exit_policy"}:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("전략 유형과 버전 문자열이 필요합니다.")
            elif field.name in {
                "intraday_stop",
                "use_quality",
                "use_macro",
                "rotation_enabled",
                "rebalance_risk",
            }:
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
        if self.exit_policy not in {"recovery", "trail", "partial_trail"}:
            raise ValueError("지원하지 않는 청산 정책입니다.")
        for name in ("candidate_limit", "rotation_min_holding", "rotation_max_per_day"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name}은 양의 정수여야 합니다.")
        for name in ("holding_extension_sessions", "stagnation_sessions", "rotation_cooldown"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name}은 0 이상의 정수여야 합니다.")
        if self.trailing_atr <= 0 or self.min_progress_r < 0 or self.rotation_max_cost_bps < 0:
            raise ValueError("청산·교체 비용 설정을 확인하세요.")
        if not 0 <= self.rotation_exit_percentile < self.rotation_entry_percentile <= 100:
            raise ValueError("교체 진입/유지 백분위를 확인하세요.")
        if not 0 < self.rotation_min_gap <= 100:
            raise ValueError("교체 점수 차이는 (0,100] 범위입니다.")
        for name in (
            "max_position_pct",
            "max_total_pct",
            "risk_per_trade_pct",
            "max_sector_pct",
            "max_index_pct",
            "daily_loss_limit_pct",
            "participation_pct",
            "recovery_sell_pct",
            "max_initial_stop_pct",
            "max_open_risk_pct",
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
        "holding_score": holding_score(technical),
    }
    if technical.get("history_count", 0) < rules.min_history:
        reasons.append("insufficient_history")
        return result
    close, ma, atr = technical["close"], technical["sma"], technical["atr14"]
    if position is not None:
        decision = exit_decision(
            technical, position, rules, age=position.get("holding_sessions", 0)
        )
        result["action"] = "sell" if decision["quantity"] else "hold"
        result["exit_quantity"] = decision["quantity"]
        result["position_updates"] = decision["updates"]
        result["exit_kind"] = decision["kind"]
        reasons.extend(decision["reasons"])
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
    elif rules.stop_atr * atr / close * 100 > rules.max_initial_stop_pct:
        reasons.append("initial_stop_too_wide")
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


def position_size_details(
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
    open_risk=0.0,
    rules: StrategyRules,
) -> dict:
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
        return {"quantity": 0, "quantity_caps": {}, "binding_constraints": ["invalid_budget"]}
    if stop_distance / price * 100 > rules.max_initial_stop_pct:
        return {
            "quantity": 0,
            "quantity_caps": {},
            "binding_constraints": ["initial_stop_too_wide"],
        }
    unit_cost = price * (1 + rules.commission_bps / 10000)
    exit_cost = price * (rules.commission_bps + rules.sell_tax_bps + rules.slippage_bps) / 10000
    bounds = {
        "risk_budget": equity * rules.risk_per_trade_pct / 100 / (stop_distance + exit_cost),
        "cash": cash / unit_cost,
        "position": (equity * rules.max_position_pct / 100 - existing_value) / unit_cost,
        "total_exposure": (equity * rules.max_total_pct / 100 - total_value) / unit_cost,
        "sector": (equity * rules.max_sector_pct / 100 - sector_value) / unit_cost,
        "index": (equity * rules.max_index_pct / 100 - index_value) / unit_cost,
    }
    if rules.max_open_risk_pct < 100:
        bounds["portfolio_risk"] = (
            (equity * rules.max_open_risk_pct / 100 - open_risk) / (stop_distance + exit_cost)
            if open_risk is not None
            else 0
        )
    if volume20 is not None:
        if finite(volume20) < 0:
            raise ValueError("평균 거래량은 음수일 수 없습니다.")
        bounds["participation"] = volume20 * rules.participation_pct / 100
    minimum = min(bounds.values())
    quantity = max(0, math.floor(minimum))
    return {
        "quantity": quantity,
        "quantity_caps": bounds,
        "binding_constraints": [k for k, v in bounds.items() if math.isclose(v, minimum)],
        "equity": equity,
        "cash": cash,
        "price": price,
        "stop_distance": stop_distance,
        "volume20": volume20,
        "risk_budget": equity * rules.risk_per_trade_pct / 100,
        "planned_stop_risk": quantity * stop_distance,
        "reserved_cost": quantity * unit_cost,
        "allocation_pct": quantity * price / equity * 100,
    }


def size_position(**kwargs) -> int:
    """Compatibility wrapper; research and runtime share the explained calculation."""
    return position_size_details(**kwargs)["quantity"]


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
    record_decisions=False,
    on_session=None,
    feature_cache=None,
) -> dict:
    """Precommit next-open limit orders at the close; daily bars describe fills only.

    Published-by-close raw versions are frozen for each execution/valuation day.
    They must never enter the preceding order decision. This is a daily research
    contract, not the runtime's quote-observed 30-minute execution window.
    """
    finite(initial_cash, positive=True)
    if feature_cache is not None:
        fingerprint = content_hash(data)
        if feature_cache and feature_cache.get("dataset_hash") != fingerprint:
            raise ValueError("다른 자료의 피처 캐시를 사용할 수 없습니다.")
        feature_cache["dataset_hash"] = fingerprint
        feature_cache.setdefault("values", {})
    sessions = [
        s
        for s in data["sessions"]
        if (not start_session or s >= start_session) and (not end_session or s <= end_session)
    ]
    if len(sessions) < 2:
        raise ValueError("백테스트에는 최소 두 세션이 필요합니다.")
    instruments = {i["symbol"]: i for i in data["instruments"]}
    facts = defaultdict(list)
    for row in data["financial_facts"]:
        facts[row["symbol"]].append(row)
    # No future correction can replace a historical execution or valuation fact.
    bar_events = sorted(
        (r for r in data["bars"] if r["price_basis"] == "raw"),
        key=lambda r: (timestamp(r["available_at"]), timestamp(r["ingested_at"])),
    )
    raw = {}
    for row in bar_events:
        if timestamp(row["available_at"]) <= decision_time(row["session"]):
            raw[(row["symbol"], row["session"])] = row
    visible_history, event_index = defaultdict(dict), 0
    positions, pending_buys, pending_sells = {}, [], {}
    entitlements, trades, curve, excluded, decisions, daily = [], [], [], [], [], []
    cash, peak, total_cost, slippage_cost = float(initial_cash), float(initial_cash), 0.0, 0.0
    paid_events, applied_events = set(), set()
    applied_splits = []
    share_effects = set()
    rotations, cooldowns = [], {}

    def rotation_state(pair_id, status, **details):
        pair = next((p for p in rotations if p["id"] == pair_id), None)
        if pair:
            pair.update(status=status, **details)

    funnel, sizing_counts = Counter(), Counter()

    def record(session, symbol, stage, outcome, **details):
        item = {"session": session, "symbol": symbol, "stage": stage, "outcome": outcome, **details}
        funnel[stage + ":" + outcome] += 1
        if record_decisions or stage != "screen":
            decisions.append(item)
        return item

    def reject(session, symbol, reason, **details):
        excluded.append({"session": session, "symbol": symbol, "reason": reason, **details})
        record(session, symbol, "order", reason, **details)

    def time_exit(symbol, pos, age, session):
        limit = rules.max_holding_sessions + (
            rules.holding_extension_sessions if pos and pos.get("trailing_active") else 0
        )
        if pos and age >= limit:
            previous = pending_sells.get(symbol, {})
            if previous.get("rotation_id"):
                rotation_state(
                    previous["rotation_id"], "cancelled", reason="protective_exit_priority"
                )
            pending_sells[symbol] = {
                "signal_session": previous.get("signal_session", session),
                "reason": sorted(set(previous.get("reason", [])) | {"holding_expiry"}),
                "quantity": pos["quantity"],
                "kind": "full",
            }

    def sell(
        symbol, session, price, signal_session, reason, reference_price, quantity=None, kind=None
    ):
        nonlocal cash, total_cost, slippage_cost
        pos = positions[symbol]
        qty = min(quantity, pos["quantity"]) if quantity is not None else pos["quantity"]
        pos["quantity"] -= qty
        if kind == "partial_recovery":
            pos["recovery_taken"] = True
        if not pos["quantity"]:
            del positions[symbol]
        fee = price * qty * (rules.commission_bps + rules.sell_tax_bps) / 10000
        slip = (reference_price - price) * qty
        cash += price * qty - fee
        total_cost += fee
        slippage_cost += slip
        trades.append(
            {
                "session": session,
                "signal_session": signal_session,
                "symbol": symbol,
                "side": "sell",
                "quantity": qty,
                "price": price,
                "fee": fee,
                "slippage_cost": slip,
                "profit": qty * (price - pos["cost_per_share"]) - fee,
                "reason": reason,
            }
        )
        pending_sells.pop(symbol, None)
        record(session, symbol, "execution", "sell_filled", reason=reason)

    for index, session in enumerate(sessions):
        at = decision_time(session)
        opened = datetime.combine(at.date(), time(0), timezone.utc)  # KRX 09:00 KST
        day_start_equity = curve[-1]["equity"] if curve else float(initial_cash)
        while (
            event_index < len(bar_events)
            and timestamp(bar_events[event_index]["available_at"]) <= at
        ):
            row = bar_events[event_index]
            visible_history[row["symbol"]][row["session"]] = row
            event_index += 1
        # Actions changing opening positions must be known by the opening event.
        for event in latest_visible(data["actions"], opened, ("event_id",)):
            event_id = event["event_id"]
            if event_id in applied_events:
                previous = next((a for a in applied_splits if a["event_id"] == event_id), None)
                if previous is not None and previous != event:
                    if event["effective_date"] > session and event["ratio"] == previous["ratio"]:
                        # A postponed plan that never touched this portfolio can wait
                        # until its revised legal date without rewriting any ledger entry.
                        touched = event_id in share_effects or any(
                            t["symbol"] == event["symbol"]
                            and t["session"] >= previous["effective_date"]
                            for t in trades
                        )
                        if not touched:
                            applied_events.remove(event_id)
                            applied_splits.remove(previous)
                            continue
                    if event["ratio"] != previous["ratio"] or event["effective_date"] > session:
                        raise ValueError(
                            f"{event_id}: 적용 후 기업행동 수량/효력 정정은 "
                            "별도 원장 대사가 필요합니다."
                        )
                    for right in entitlements:
                        if right["event_id"] != event_id + ":fraction":
                            continue
                        if event.get("fractional_policy") not in {
                            "cash_at_close",
                            "sale_proceeds_unconfirmed",
                            "cancel_without_compensation",
                        }:
                            raise ValueError("정정 공시의 단주 정산 자료가 필요합니다.")
                        revised = {
                            "pricing_session": event.get("fractional_price_date"),
                            "payment_date": event["effective_date"]
                            if event.get("fractional_policy") == "cancel_without_compensation"
                            else event.get("payment_date"),
                            "valuation_policy": event.get("fractional_policy"),
                        }
                        if right["amount"] is not None and any(
                            right.get(k) != v for k, v in revised.items()
                        ):
                            raise ValueError(
                                "확정된 단주 정산의 정정은 별도 원장 대사가 필요합니다."
                            )
                        right.update(revised)
                    # Relisting can be postponed after the legal share conversion.
                    # Refresh quotation/settlement dates without converting shares twice.
                    previous.clear()
                    previous.update(event)
                continue
            if event["effective_date"] > session:
                continue
            if event["effective_date"] < session:
                if index and sessions[index - 1] >= event["effective_date"] >= sessions[0]:
                    raise ValueError("기업행동이 효력 세션 이후 알려졌습니다.")
                if event["effective_date"] < sessions[0]:
                    applied_events.add(event_id)
                    if event["type"] == "split":
                        applied_splits.append(dict(event))
                    continue
            applied_events.add(event_id)
            symbol = event["symbol"]
            record(session, symbol, "corporate_action", event["type"], event_id=event_id)
            if event["type"] == "delisting":
                for order in pending_buys:
                    if order["symbol"] == symbol:
                        reject(session, symbol, "known_delisting_cancelled_entry")
                pending_buys = [o for o in pending_buys if o["symbol"] != symbol]
            if event["type"] == "split":
                applied_splits.append(dict(event))
                ratio = float(event["ratio"])
                for order in list(pending_buys):
                    if order["symbol"] == symbol:
                        share_effects.add(event_id)
                        quantity = order["quantity"] * ratio
                        if not quantity.is_integer():
                            pending_buys.remove(order)
                            reject(session, symbol, "fractional_split_entry_cancelled")
                            continue
                        order["quantity"] = int(quantity)
                        order["limit_price"] /= ratio
                        order["stop_distance"] /= ratio
            pos = positions.get(symbol)
            if not pos:
                continue
            if event["type"] == "split":
                share_effects.add(event_id)
                pos, fractional = split_position(pos, event)
                if fractional:
                    entitlements.append(fractional)
                if pos["quantity"]:
                    positions[symbol] = pos
                    if (
                        symbol in pending_sells
                        and pending_sells[symbol].get("quantity") is not None
                    ):
                        pending_sells[symbol]["quantity"] = min(
                            pos["quantity"],
                            max(1, math.floor(pending_sells[symbol]["quantity"] * ratio)),
                        )
                else:
                    del positions[symbol]
                    pending_sells.pop(symbol, None)
            elif event["type"] in {"dividend", "distribution"}:
                entitlements.append(
                    {
                        "event_id": event_id,
                        "payment_date": event["payment_date"],
                        "amount": pos["quantity"] * float(event["amount"]),
                    }
                )
            elif event["type"] == "delisting":
                if "settlement_price" not in event:
                    raise ValueError("상장폐지 정산가격이 필요합니다.")
                settlement = finite(event["settlement_price"])
                cash += pos["quantity"] * settlement
                trades.append(
                    {
                        "session": session,
                        "signal_session": session,
                        "symbol": symbol,
                        "side": "sell",
                        "quantity": pos["quantity"],
                        "price": settlement,
                        "fee": 0,
                        "slippage_cost": 0,
                        "profit": pos["quantity"] * (settlement - pos["cost_per_share"]),
                        "reason": "delisting_supplied_net_settlement",
                    }
                )
                del positions[symbol]
                pending_sells.pop(symbol, None)
                pending_buys = [o for o in pending_buys if o["symbol"] != symbol]
        for event in latest_visible(data["actions"], at, ("event_id",)):
            if (
                sessions[0] <= event["effective_date"] <= session
                and event["event_id"] not in applied_events
            ):
                raise ValueError(
                    "장 시작 후 공개된 기업행동을 시가 원장에 소급 적용할 수 없습니다."
                )
        for item in entitlements:
            if (
                item.get("payment_date")
                and item["payment_date"] <= session
                and item.get("amount") is not None
                and item["event_id"] not in paid_events
            ):
                cash += item["amount"]
                paid_events.add(item["event_id"])
        for symbol in positions:
            if (symbol, session) not in raw:
                raise ValueError(f"{session} {symbol}: 보유 평가 봉 누락 또는 당일 미공개")
        # Retry exit intent until filled. A suspension cannot revoke a prior decision.
        for symbol, order in list(sorted(pending_sells.items())):
            if symbol not in positions:
                del pending_sells[symbol]
                continue
            bar = raw.get((symbol, session))
            if bar is None or not bar.get("tradable", True) or not bar["volume"]:
                order["attempts"] = order.get("attempts", 0) + 1
                reject(
                    session, symbol, "sell_unfilled_retry", signal_session=order["signal_session"]
                )
                continue
            reference = float(bar["open"])
            sell(
                symbol,
                session,
                reference * (1 - rules.slippage_bps / 10000),
                order["signal_session"],
                order["reason"],
                reference,
                order.get("quantity"),
                order.get("kind"),
            )
            if order.get("rotation_id"):
                rotation_state(order["rotation_id"], "entry_recheck", exit_session=session)
                cooldowns[symbol] = index
        orders, pending_buys = pending_buys, []
        for order in orders:
            symbol = order["symbol"]
            if order.get("rotation_id"):
                # Until a fill is confirmed this pair remains a failed entry.
                rotation_state(order["rotation_id"], "entry_failed", entry_attempt=session)
            bar = raw.get((symbol, session))
            if bar is None or not bar.get("tradable", True) or not bar["volume"]:
                reject(
                    session, symbol, "buy_unfilled_expired", signal_session=order["signal_session"]
                )
                continue
            if symbol in positions or len(positions) >= rules.max_positions:
                reject(session, symbol, "execution_position_limit")
                continue
            reference = float(bar["open"])
            price = reference * (1 + rules.slippage_bps / 10000)
            # Conservative open-only limit execution: no later-in-day touch is assumed.
            if price > order["limit_price"]:
                reject(session, symbol, "opening_limit_not_filled")
                continue
            qty = order["quantity"]
            fee = price * qty * rules.commission_bps / 10000
            if price * qty + fee > cash + 1e-8:
                reject(session, symbol, "execution_cash_limit")
                continue
            cash -= price * qty + fee
            slip = (price - reference) * qty
            total_cost += fee
            slippage_cost += slip
            positions[symbol] = {
                "quantity": qty,
                "cost_per_share": price + fee / qty,
                "stop_price": price - order["stop_distance"],
                "entry_index": index,
                "entry_session": session,
                "entry_price": price,
                "initial_stop_distance": order["stop_distance"],
                "high_watermark": price,
                "recovery_taken": False,
                "trailing_active": False,
                "index_group": order.get("index_group"),
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
                    "slippage_cost": slip,
                    "reason": "entry",
                    "decision_id": order["decision_id"],
                    "limit_price": order["limit_price"],
                }
            )
            record(
                session,
                symbol,
                "execution",
                "buy_filled",
                quantity=qty,
                decision_id=order["decision_id"],
                price=price,
            )
            if order.get("rotation_id"):
                rotation_state(order["rotation_id"], "completed", entry_session=session)
        if rules.intraday_stop:
            for symbol, pos in list(positions.items()):
                bar = raw[(symbol, session)]
                if not bar.get("valuation_only") and float(bar["low"]) <= pos["stop_price"]:
                    reference = min(float(bar["open"]), pos["stop_price"])
                    sell(
                        symbol,
                        session,
                        reference * (1 - rules.slippage_bps / 10000),
                        session,
                        "intraday_stop_conservative",
                        reference,
                    )
        # All following decisions occur at the close and affect later sessions only.
        visible_universe = latest_visible(data["universe"], at, ("session",))
        universe_history = {r["session"]: set(r["symbols"]) for r in visible_universe}
        expected_universe = {
            r["session"]: set(r.get("listed_symbols", r["symbols"])) for r in visible_universe
        }
        if session not in universe_history:
            raise ValueError(f"{session}: 당시 유니버스가 없습니다.")
        tradable_universe = universe_history[session]
        prices = {
            s: quotation_price(raw[(s, session)]["close"], s, session, applied_splits)
            for s in positions
        }
        marks = {s: p["quantity"] * prices[s] for s, p in positions.items()}
        holdings_value = sum(marks.values())
        receivable = 0.0
        for item in entitlements:
            if item["event_id"] in paid_events:
                continue
            if item.get("type") == "fractional_cash" and item["amount"] is None:
                symbol = item["symbol"]
                mark = quotation_price(
                    raw[(symbol, session)]["close"], symbol, session, applied_splits
                )
                if item.get("pricing_session") and session >= item["pricing_session"]:
                    item["amount"] = item["quantity"] * mark
                    item["priced_session"] = session
                else:
                    receivable += item["quantity"] * mark
                    continue
            receivable += item["amount"]
        equity = cash + holdings_value + receivable
        loss_halt = equity < day_start_equity * (1 - rules.daily_loss_limit_pct / 100)
        macro = macro_features(data["macro"], at)
        benchmark_rows = latest_visible(
            data["benchmarks"], at, ("symbol", "session", "price_basis")
        )
        benchmark_by_id = defaultdict(list)
        for row in benchmark_rows:
            if row["price_basis"] == "raw" and row["session"] <= session:
                benchmark_by_id[row["symbol"]].append(row)
        etfs = {r["symbol"]: r for r in latest_visible(data["etf_metadata"], at, ("symbol",))}
        actions = latest_visible(data["actions"], at, ("event_id",))
        candidates, screen_counts, scores = [], Counter(), {}
        for symbol in sorted(set(tradable_universe) | set(positions)):
            pos = positions.get(symbol)
            # Entry session counts as one. N closes held -> exit at the next open.
            holding_sessions = index - pos["entry_index"] + 1 if pos else 0
            bar = raw.get((symbol, session))
            if not bar or bar.get("valuation_only"):
                time_exit(symbol, pos, holding_sessions, session)
                reason = "valuation_only_no_signal" if bar else "missing_final_bar"
                screen_counts[reason] += 1
                record(session, symbol, "screen", reason, holding_sessions=holding_sessions)
                continue
            cache_key = (session, symbol)
            cached = feature_cache["values"].get(cache_key) if feature_cache is not None else None
            if cached is not None:
                technical, financial = cached
            else:
                history = sorted(
                    (
                        r
                        for r in visible_history[symbol].values()
                        if r["session"] <= session and not r.get("valuation_only")
                    ),
                    key=lambda r: r["session"],
                )
                symbol_actions = [a for a in actions if a["symbol"] == symbol]
                if symbol_actions:
                    history = bars_at(
                        {"bars": history, "actions": symbol_actions}, symbol, session, at
                    )
                expected = {
                    s
                    for s in data["sessions"]
                    if history
                    and history[0]["session"] <= s <= session
                    and symbol in expected_universe.get(s, set())
                }
                # Official no-trade rows satisfy presence, but are excluded from indicators.
                observed = {s for s in visible_history[symbol] if s <= session}
                if not history or history[-1]["session"] != session or expected - observed:
                    time_exit(symbol, pos, holding_sessions, session)
                    reason = (
                        "missing_trading_session" if expected - observed else "missing_final_bar"
                    )
                    screen_counts[reason] += 1
                    record(session, symbol, "screen", reason)
                    continue
                benchmark_id = etfs.get(symbol, {}).get(
                    "benchmark_id", instruments[symbol].get("benchmark_id", "KOSPI")
                )
                technical = technical_features(history, benchmark_by_id[benchmark_id])
                financial = financial_features(
                    facts[symbol], symbol, at, history[-1].get("market_cap")
                )
                if feature_cache is not None:
                    feature_cache["values"][cache_key] = (technical, financial)
            result = evaluate_strategy(
                technical,
                instruments[symbol],
                rules,
                financial=financial,
                macro=macro,
                etf=etfs.get(symbol),
                position={**pos, "holding_sessions": holding_sessions} if pos else None,
            )
            if pos:
                pos.update(result.get("position_updates", {}))
            time_exit(symbol, pos, holding_sessions, session)
            kind = instruments[symbol].get("kind", "")
            if (rules.strategy_type == "kospi_pullback" and kind == "kospi") or (
                rules.strategy_type == "etf_trend" and kind.startswith("etf")
            ):
                scores[symbol] = result.get("holding_score")
            outcome = "eligible" if result["eligible"] else result["action"]
            screen_counts[outcome] += 1
            for reason in result["reasons"]:
                screen_counts["reason:" + reason] += 1
            record(
                session,
                symbol,
                "screen",
                outcome,
                reasons=result["reasons"],
                rank=result["rank"],
                atr_pct=technical.get("atr_pct"),
                turnover20=technical.get("turnover20"),
                relative63=technical.get("relative63"),
            )
            if result["action"] == "sell":
                pending_sells.setdefault(
                    symbol,
                    {
                        "signal_session": session,
                        "reason": result["reasons"],
                        "quantity": result.get("exit_quantity"),
                        "kind": result.get("exit_kind"),
                    },
                )
                if result.get("exit_kind") == "full":
                    if pending_sells[symbol].get("rotation_id"):
                        rotation_state(
                            pending_sells[symbol].pop("rotation_id"),
                            "cancelled",
                            reason="protective_exit_priority",
                        )
                    pending_sells[symbol].update(quantity=pos["quantity"], kind="full")
                if pending_sells[symbol]["signal_session"] == session:
                    pending_sells[symbol]["reason"] = sorted(
                        set(pending_sells[symbol]["reason"]) | set(result["reasons"])
                    )
            elif result["eligible"] and not pos:
                rank = result["rank"]
                if model_scores is not None:
                    rank = model_scores.get((session, symbol))
                    if rank is None or (model_threshold is not None and rank <= model_threshold):
                        reject(session, symbol, "model_unavailable_or_below_threshold")
                        continue
                candidates.append(
                    {
                        "symbol": symbol,
                        "side": "buy",
                        "signal_session": session,
                        "rank": rank,
                        "reference_price": technical["close"],
                        "stop_distance": result["stop_distance"],
                        "volume20": technical["volume20"],
                        "atr_pct": technical.get("atr_pct"),
                        "index_group": etfs.get(symbol, {}).get("benchmark_id"),
                    }
                )
        reductions = risk_reductions(
            {
                s: {**p, "tradable": not raw[(s, session)].get("valuation_only")}
                for s, p in positions.items()
            },
            prices,
            instruments,
            rules,
            equity=equity,
        )
        for symbol, quantity in reductions.items():
            previous = pending_sells.get(symbol)
            if previous and (previous.get("quantity") or positions[symbol]["quantity"]) >= quantity:
                continue
            pending_sells[symbol] = {
                "signal_session": session,
                "reason": ["portfolio_risk_reduction"],
                "quantity": quantity,
                "kind": "risk_reduction",
            }
        # A rotation purchase can first be planned AFTER the sale-day close.
        candidate_map = {o["symbol"]: o for o in candidates}
        for pair in rotations:
            if pair["status"] == "exit_submitted":
                order = pending_sells.get(pair["sell_symbol"], {})
                if pair["buy_symbol"] not in candidate_map or loss_halt or reductions:
                    if order.get("kind") == "rotation":
                        pending_sells.pop(pair["sell_symbol"], None)
                    pair.update(
                        status="cancelled",
                        reason="candidate_or_risk_recheck_failed",
                        cancelled_session=session,
                    )
                continue
            if pair["status"] != "entry_recheck":
                continue
            target = candidate_map.get(pair["buy_symbol"])
            if target is None or loss_halt or reductions:
                pair.update(
                    status="entry_failed",
                    entry_failure_session=session,
                    reason="candidate_or_risk_recheck_failed",
                )
            else:
                target["rotation_id"] = pair["id"]
                pair["status"] = "entry_planning"
        for symbol, order in pending_sells.items():
            if order["signal_session"] == session:
                record(session, symbol, "exit", "planned", reasons=order["reason"])
        reserved_cash, reserved_marks, reserved_risk = 0.0, {}, 0.0
        open_risk = portfolio_risk(positions, prices)
        # Exits may free slots, but their proceeds/exposure relief are never assumed.
        planned_slots = len(positions) - sum(
            order.get("quantity", positions[s]["quantity"]) in (None, positions[s]["quantity"])
            for s, order in pending_sells.items()
        )
        for rank_index, order in enumerate(
            sorted(
                candidates, key=lambda o: (not bool(o.get("rotation_id")), -o["rank"], o["symbol"])
            )
        ):
            symbol = order["symbol"]
            if (
                rules.rotation_enabled
                and index - cooldowns.get(symbol, -100000) <= rules.rotation_cooldown
            ):
                reject(session, symbol, "rotation_reentry_cooldown")
                continue
            if rank_index >= rules.candidate_limit:
                reject(session, symbol, "candidate_rank_limit")
                continue
            if loss_halt or reductions:
                reject(session, symbol, "daily_loss_entry_halt")
                continue
            if planned_slots >= rules.max_positions:
                reject(session, symbol, "position_slots_full")
                continue
            limit_price = order["reference_price"] * (1 + rules.max_gap_pct / 100)
            sector = instruments[symbol].get("sector")
            all_marks = {**marks, **reserved_marks}
            index_group = order["index_group"]
            details = position_size_details(
                equity=equity,
                cash=cash - reserved_cash,
                price=limit_price,
                stop_distance=order["stop_distance"],
                total_value=sum(all_marks.values()),
                sector_value=sum(
                    v
                    for s, v in all_marks.items()
                    if sector and instruments[s].get("sector") == sector
                ),
                index_value=sum(
                    v
                    for s, v in all_marks.items()
                    if index_group
                    and (
                        positions.get(s, {}).get("index_group")
                        or etfs.get(s, {}).get("benchmark_id")
                    )
                    == index_group
                ),
                volume20=order["volume20"],
                open_risk=None if open_risk is None else open_risk + reserved_risk,
                rules=rules,
            )
            for constraint in details["binding_constraints"]:
                sizing_counts[constraint] += 1
            decision_id = session + ":" + symbol + ":buy"
            record(
                session,
                symbol,
                "sizing",
                "planned" if details["quantity"] else "zero",
                decision_id=decision_id,
                sizing=details,
                rank=order["rank"],
                atr_pct=order["atr_pct"],
            )
            if not details["quantity"]:
                reject(session, symbol, "risk_size_zero")
                continue
            pending_buys.append(
                {
                    **order,
                    "quantity": details["quantity"],
                    "limit_price": limit_price,
                    "decision_id": decision_id,
                }
            )
            if order.get("rotation_id"):
                rotation_state(
                    order["rotation_id"], "entry_submitted", entry_signal_session=session
                )
            reserved_cash += details["reserved_cost"]
            reserved_risk += details["planned_stop_risk"]
            reserved_marks[symbol] = details["quantity"] * limit_price
            planned_slots += 1
        for pair in rotations:
            if pair["status"] == "entry_planning":
                pair.update(
                    status="entry_failed",
                    entry_failure_session=session,
                    reason="sizing_or_order_gate",
                )
        if (
            rules.rotation_enabled
            and not loss_halt
            and not reductions
            and not pending_sells
            and len(positions) >= rules.max_positions
            and not any(
                p["status"] in {"exit_submitted", "entry_recheck", "entry_submitted"}
                for p in rotations
            )
        ):
            ranked = sorted(candidates, key=lambda o: (-o["rank"], o["symbol"]))[
                : rules.candidate_limit
            ]
            percentiles = score_percentiles(scores)
            pairs = choose_rotations(
                [
                    {
                        "symbol": s,
                        "tradable": not raw[(s, session)].get("valuation_only"),
                        "age": index - p["entry_index"] + 1,
                    }
                    for s, p in positions.items()
                ],
                [{**o, "quantity": 1} for o in ranked],
                percentiles,
                rules,
                day_index=index,
                cooldowns=cooldowns,
            )
            for pair in pairs:
                old, new = pair["sell_symbol"], pair["buy_symbol"]
                candidate = candidate_map[new]
                hypothetical_marks = {s: v for s, v in marks.items() if s != old}
                sector = instruments[new].get("sector")
                group = candidate["index_group"]
                details = position_size_details(
                    equity=equity,
                    cash=cash
                    + marks[old]
                    * (
                        1 - (rules.commission_bps + rules.sell_tax_bps + rules.slippage_bps) / 10000
                    ),
                    price=candidate["reference_price"] * (1 + rules.max_gap_pct / 100),
                    stop_distance=candidate["stop_distance"],
                    total_value=sum(hypothetical_marks.values()),
                    sector_value=sum(
                        v
                        for s, v in hypothetical_marks.items()
                        if sector and instruments[s].get("sector") == sector
                    ),
                    index_value=sum(
                        v
                        for s, v in hypothetical_marks.items()
                        if group and positions[s].get("index_group") == group
                    ),
                    volume20=candidate["volume20"],
                    open_risk=portfolio_risk(
                        {s: p for s, p in positions.items() if s != old}, prices
                    ),
                    rules=rules,
                )
                if not details["quantity"]:
                    record(session, new, "rotation", "prospective_size_zero", sell_symbol=old)
                    continue
                pair.update(
                    id=f"{session}:{old}:{new}",
                    signal_session=session,
                    status="exit_submitted",
                    prospective_sizing=details,
                    execution_policy="sell_next_open_recheck_close_buy_following_open",
                )
                rotations.append(pair)
                pending_sells[old] = {
                    "signal_session": session,
                    "reason": ["opportunity_rotation"],
                    "kind": "rotation",
                    "quantity": positions[old]["quantity"],
                    "rotation_id": pair["id"],
                }
                record(session, old, "rotation", "planned", pair=pair.copy())
        peak = max(peak, equity)
        frozen_value = sum(v for s, v in marks.items() if raw[(s, session)].get("valuation_only"))
        curve.append(
            {
                "session": session,
                "cash": cash,
                "holdings_value": holdings_value,
                "receivable": receivable,
                "equity": equity,
                "drawdown": equity / peak - 1,
                "exposure": holdings_value / equity if equity else 0,
                "position_count": len(positions),
                "untradable_mark_value": frozen_value,
                "tradable_mark_value": holdings_value - frozen_value,
            }
        )
        daily.append(
            {
                "session": session,
                "screen_counts": dict(screen_counts),
                "candidate_count": len(candidates),
                "planned_buys": len(pending_buys),
                "pending_sells": len(pending_sells),
                "entry_halt": loss_halt,
            }
        )
        if on_session:
            on_session(session, index + 1, len(sessions))
    exits = [t for t in trades if t["side"] == "sell"]
    bm_id = benchmark_symbol or "KOSPI"
    bm = {}
    for row in sorted(
        data["benchmarks"],
        key=lambda r: (timestamp(r["available_at"]), timestamp(r["ingested_at"])),
    ):
        if (
            row["symbol"] == bm_id
            and row["price_basis"] == "raw"
            and row["session"] in sessions
            and timestamp(row["available_at"]) <= decision_time(row["session"])
        ):
            bm[row["session"]] = row
    bm = [bm[s] for s in sorted(bm)]
    profit, loss = sum(max(t["profit"], 0) for t in exits), -sum(min(t["profit"], 0) for t in exits)
    return {
        "engine_version": "daily-precommitted-v3",
        "strategy": asdict(rules),
        "initial_cash": initial_cash,
        "equity_curve": curve,
        "trades": trades,
        "unfilled_or_excluded": excluded,
        "open_positions": positions,
        "corporate_action_entitlements": entitlements,
        "paid_entitlement_ids": sorted(paid_events),
        "rotations": rotations,
        "pending_orders": {"buys": pending_buys, "sells": pending_sells},
        "diagnostics": {
            "decisions": decisions,
            "daily": daily,
            "funnel": dict(funnel),
            "sizing_constraints": dict(sizing_counts),
            "all_screen_decisions_recorded": record_decisions,
        },
        "benchmark": {
            "symbol": bm_id,
            "return_basis": "price",
            "comparison": "reference_index_not_a_currency_hedged_investable_portfolio",
        },
        "metrics": {
            "return": curve[-1]["equity"] / initial_cash - 1,
            "max_drawdown": min(p["drawdown"] for p in curve),
            "closed_trades": len(exits),
            "win_rate": mean(t["profit"] > 0 for t in exits) if exits else None,
            "net_realized_profit": sum(t["profit"] for t in exits),
            "profit_factor": profit / loss if loss else None,
            "fees_and_taxes": total_cost,
            "slippage_cost": slippage_cost,
            "mean_exposure": mean(p["exposure"] for p in curve),
            "benchmark_return": float(bm[-1]["close"]) / float(bm[0]["close"]) - 1
            if len(bm) >= 2
            else None,
        },
        "assumptions": {
            "entry": "prior_close_quantity_and_limit_next_open_only",
            "runtime_equivalent": False,
            "runtime_entry": "quote_observed_first_30_minutes_requires_intraday_validation",
            "execution_bar_vintage": "published_by_execution_day_18_kst",
            "gap_policy": "upper_buy_limit_only_negative_gaps_are_not_cancelled",
            "buy_expiry": "one_open_attempt",
            "sell_expiry": "retry_until_fill_or_delisting",
            "holding_sessions": "entry_counts_as_one_Nth_close_exit_next_open",
            "daily_loss_limit": "close_observed_halt_next_entry_plan_not_loss_guarantee",
            "allocation_limits": "decision_time_including_reservations_no_assumed_exit_proceeds",
            "intraday_stop": rules.intraday_stop,
            "tax_rate": "explicit_research_assumption_not_broker_tariff",
            "etf_fund_fee": "already_in_price_not_double_deducted",
            "end_positions": "marked_not_forced_liquidation",
            "same_day_stop_priority": "stop_first",
            "untradable_mark_value": "official_mark_not_liquidation_proceeds",
        },
    }
