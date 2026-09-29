"""Fixed, independent hypotheses for long-horizon and tactical ETF research."""

import math
from dataclasses import asdict, dataclass
from statistics import mean, pstdev
from types import MappingProxyType


@dataclass(frozen=True)
class AutoModel:
    id: str
    label: str
    kind: str
    symbols: tuple[str, ...] = ("QQQ",)
    rsi_entry: float = 10
    z_entry: float = -2
    fear_level: float = 30
    panic_drop: float = 0.10
    recovery_fraction: float = 0.20
    stop_fraction: float = 0.08
    max_hold: int = 10
    trend_floor: float = 0.5

    def __post_init__(self):
        if self.kind not in {
            "hold",
            "cash",
            "equal",
            "constant",
            "trend",
            "dual",
            "rotation",
            "breakout",
            "rsi",
            "zscore",
            "panic",
            "panic_recovery",
        }:
            raise ValueError("Unknown automatic model family")
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("Model symbols must be unique")
        for v in asdict(self).values():
            if isinstance(v, (int, float)) and not math.isfinite(v):
                raise ValueError("Model parameters must be finite")
        if type(self.max_hold) is not int or self.max_hold < 1:
            raise ValueError("Maximum holding sessions must be a positive integer")
        if not 0 < self.rsi_entry < 70 or not -5 <= self.z_entry < 0:
            raise ValueError("Invalid oversold threshold")
        if not 0 < self.stop_fraction < 1 or not 0 <= self.trend_floor <= 1:
            raise ValueError("Invalid risk fraction")
        if (
            self.fear_level <= 0
            or not 0 < self.panic_drop < 1
            or not 0 < self.recovery_fraction < 1
        ):
            raise ValueError("Invalid fear parameters")


MODELS = MappingProxyType(
    {
        m.id: m
        for m in (
            AutoModel("hold_spy", "SPY 장기 보유", "hold", ("SPY",)),
            AutoModel("hold_qqq", "QQQ 장기 보유", "hold"),
            AutoModel("equal_equity", "SPY·QQQ 50:50", "equal", ("SPY", "QQQ")),
            AutoModel("cash", "현금 기준선", "cash"),
            AutoModel("fixed_half", "QQQ 50% 고정", "constant"),
            AutoModel("long_trend", "QQQ 월별 추세 100/50", "trend"),
            AutoModel("dual_momentum", "주식 상대·절대 모멘텀", "dual", ("SPY", "QQQ")),
            AutoModel("breakout", "QQQ 55일 돌파", "breakout", stop_fraction=0.10),
            AutoModel("pullback_rsi", "상승 추세 RSI 과매도", "rsi"),
            AutoModel("pullback_z", "상승 추세 통계적 눌림", "zscore", max_hold=15),
            AutoModel(
                "panic_immediate", "극단 공포 즉시 진입", "panic", stop_fraction=0.12, max_hold=20
            ),
            AutoModel(
                "panic_recovery",
                "극단 공포 회복 확인",
                "panic_recovery",
                stop_fraction=0.12,
                max_hold=20,
            ),
            AutoModel(
                "asset_rotation", "주식·금·채권 모멘텀", "rotation", ("SPY", "QQQ", "IEF", "GLD")
            ),
        )
    }
)
BLENDS = MappingProxyType(
    {
        "core_pullback": (("hold_qqq", 0.7), ("pullback_rsi", 0.3)),
        "core_panic": (("hold_qqq", 0.7), ("panic_recovery", 0.3)),
        "momentum_pullback": (("dual_momentum", 0.7), ("pullback_rsi", 0.3)),
    }
)


def catalog():
    return {
        "models": [asdict(m) for m in MODELS.values()],
        "blends": {k: list(v) for k, v in BLENDS.items()},
    }


def features(bars):
    """Each row uses only its own close and preceding observations, including RSI seeds."""
    closes, months, result = [], [], []
    gains = {2: 0.0, 14: 0.0}
    losses = {2: 0.0, 14: 0.0}
    for i, bar in enumerate(bars):
        close = bar["close"]
        previous = closes[-1] if closes else close
        closes.append(close)
        if bar["month_end"]:
            months.append(close)
        rsis = {}
        for n in (2, 14):
            gain, loss = max(close - previous, 0), max(previous - close, 0)
            if 0 < i <= n:
                gains[n] += gain / n
                losses[n] += loss / n
            elif i > n:
                gains[n] = (gains[n] * (n - 1) + gain) / n
                losses[n] = (losses[n] * (n - 1) + loss) / n
            rsis[n] = (
                (100 * gains[n] / (gains[n] + losses[n]) if gains[n] + losses[n] else 50)
                if i >= n
                else None
            )
        row = {"close": close, "rsi2": rsis[2], "rsi14": rsis[14], "ready": i >= 252}
        if i >= 252:
            sd = pstdev(closes[-20:])
            row.update(
                {
                    "sma5": mean(closes[-5:]),
                    "sma20": mean(closes[-20:]),
                    "sma200": mean(closes[-200:]),
                    "prior_sma200": mean(closes[-220:-20]),
                    "high55": max(closes[-56:-1]),
                    "low20": min(closes[-21:-1]),
                    "high63": max(closes[-63:]),
                    "high3": max(closes[-4:-1]),
                    "z20": (close - mean(closes[-20:])) / sd if sd else 0,
                    "month10": mean(months[-10:]) if len(months) >= 10 else None,
                    **{f"ret{n}": close / closes[-n - 1] - 1 for n in (63, 126, 252)},
                }
            )
        result.append(row)
    return result


def decide(model, current, positions, state, index, month_end, initial, vix):
    """Return target portfolio weights or None (hold); state belongs to one replay only."""
    monthly = model.kind in {"equal", "constant", "trend", "dual", "rotation"}
    if model.kind == "cash":
        return ({}, "cash_baseline") if initial else (None, "hold")
    if model.kind == "hold":
        return ({model.symbols[0]: 1}, "buy_hold") if initial else (None, "hold")
    if monthly and not (month_end or initial):
        return None, "wait_month_end"
    if model.kind == "equal":
        return {s: 1 / len(model.symbols) for s in model.symbols}, "fixed_equal"
    if model.kind == "constant":
        return {model.symbols[0]: model.trend_floor}, "fixed_exposure"
    if any(not current[s]["ready"] for s in model.symbols):
        raise ValueError("At least 253 sessions of model warmup are required")
    if model.kind in {"dual", "rotation"}:
        periods = (126, 252) if model.kind == "dual" else (63, 126, 252)
        ranked = sorted(
            model.symbols, key=lambda s: (-mean(current[s][f"ret{n}"] for n in periods), s)
        )
        slots = 1 if model.kind == "dual" else 2
        eligible = [
            s
            for s in ranked[:slots]
            if mean(current[s][f"ret{n}"] for n in periods) > 0
            and current[s]["close"] > current[s]["sma200"]
        ]
        return {s: 1 / slots for s in eligible}, "positive_momentum_rank"
    symbol = model.symbols[0]
    f, position = current[symbol], positions.get(symbol)
    if model.kind == "trend":
        if f["month10"] is None:
            raise ValueError("Ten completed monthly closes are required")
        return {symbol: 1 if f["close"] > f["month10"] else model.trend_floor}, "monthly_trend"
    if position:
        held = index - position["entry_index"] + 1
        stop = f["close"] <= position["entry_price"] * (1 - model.stop_fraction)
        if model.kind == "breakout":
            exit_now = f["close"] < f["low20"] or f["close"] <= position["peak"] * (
                1 - model.stop_fraction
            )
            why = "channel_or_trailing_exit"
        elif model.kind in {"rsi", "zscore"}:
            recovery = (
                (f["rsi2"] >= 70 or f["close"] >= f["sma5"])
                if model.kind == "rsi"
                else f["close"] >= f["sma20"]
            )
            exit_now = stop or held >= model.max_hold or recovery
            why = "stop" if stop else "time_exit" if held >= model.max_hold else "mean_recovered"
        else:
            profit = f["close"] >= position["entry_price"] * 1.10
            exit_now = stop or held >= model.max_hold or profit
            why = "stop" if stop else "time_exit" if held >= model.max_hold else "profit_target"
        return ({}, why) if exit_now else (None, "hold_position")
    uptrend = f["close"] > f["sma200"] and f["sma200"] > f["prior_sma200"]
    if model.kind == "breakout":
        enter = f["close"] > f["high55"] and f["close"] > f["sma200"]
    elif model.kind == "rsi":
        enter = uptrend and f["rsi2"] <= model.rsi_entry
    elif model.kind == "zscore":
        enter = uptrend and f["z20"] <= model.z_entry
    else:
        if vix is None or len(vix) < 20:
            raise ValueError("Fear models require available VIX observations")
        fear = (
            f["close"] / f["high63"] - 1 <= -model.panic_drop
            and f["rsi14"] <= 35
            and vix[-1] >= model.fear_level
        )
        if model.kind == "panic":
            enter = fear
        else:
            if fear:
                state["armed_until"] = index + 20
            enter = (
                index <= state.get("armed_until", -1)
                and f["close"] > f["high3"]
                and vix[-1] <= max(vix[-20:]) * (1 - model.recovery_fraction)
            )
            if enter:
                state["armed_until"] = -1
    return ({symbol: 1}, f"{model.kind}_entry") if enter else (None, "no_entry")
