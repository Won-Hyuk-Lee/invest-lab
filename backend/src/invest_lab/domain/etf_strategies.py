"""Independent ETF policy registry. No broker, database or KOSPI strategy dependency."""

import math
from dataclasses import asdict, dataclass
from statistics import mean, stdev
from types import MappingProxyType


@dataclass(frozen=True)
class EtfPolicy:
    id: str
    label: str
    family: str
    defensive_weight: float = 0.5
    trend_months: int = 10
    target_volatility: float = 0.12
    volatility_sessions: int = 60
    implied_series: str = "VIX"
    fear_level: float = 30.0
    recovery_fraction: float = 0.2
    real_rate_increase_pp: float = 0.5
    short_rate_increase_pp: float = 0.75
    oil_increase: float = 0.2
    fx_premium: float = 0.05
    entry_step: float = 0.25
    rebalance_band: float = 0.03

    def __post_init__(self):
        if self.family not in FAMILIES:
            raise ValueError("Unknown ETF policy family")
        numbers = [v for v in asdict(self).values() if isinstance(v, (float, int))]
        if not all(math.isfinite(v) for v in numbers):
            raise ValueError("Policy parameters must be finite")
        if not 0 <= self.defensive_weight <= 1 or not 0 <= self.rebalance_band < 1:
            raise ValueError("ETF policy weights must be unleveraged")
        if self.trend_months < 2 or self.volatility_sessions < 2:
            raise ValueError("ETF lookbacks must be at least two observations")
        if type(self.trend_months) is not int or type(self.volatility_sessions) is not int:
            raise ValueError("ETF lookbacks must be integers")
        if self.implied_series not in {"VIX", "VXN"}:
            raise ValueError("Only VIX and VXN implied-volatility series are supported")
        if not 0 < self.target_volatility <= 1 or not 0 < self.entry_step <= 1:
            raise ValueError("Invalid volatility target or entry step")
        if not 0 < self.recovery_fraction < 1 or self.fear_level <= 0:
            raise ValueError("Invalid implied volatility parameters")
        if min(self.real_rate_increase_pp, self.short_rate_increase_pp) <= 0:
            raise ValueError("Rate thresholds must be positive percentage points")
        if self.fx_premium < 0 or self.oil_increase < 0:
            raise ValueError("Invalid premium threshold")

    @property
    def required_series(self):
        return {
            "vix_recovery": (self.implied_series,),
            "rates": ("REAL10", "RATE2"),
            "credit": ("NFCI",),
            "oil": ("WTI",),
            "curve": ("RATE10", "RATE3M"),
            "fx_entry": ("FX_SIGNAL",),
        }.get(self.family, ())


FAMILIES = frozenset(
    {
        "buy_hold",
        "fixed",
        "trend",
        "volatility",
        "vix_recovery",
        "rates",
        "credit",
        "oil",
        "curve",
        "fx_entry",
    }
)
POLICIES = MappingProxyType(
    {
        p.id: p
        for p in (
            EtfPolicy("buy_hold", "장기 보유", "buy_hold"),
            EtfPolicy("cash_only", "현금 100% 기준선", "fixed", defensive_weight=0),
            EtfPolicy("fixed_50", "주식 50% 고정", "fixed"),
            EtfPolicy("trend_monthly", "월별 추세 100/50", "trend"),
            EtfPolicy("trend_volatility", "추세 + 변동성 비중 조절", "volatility"),
            EtfPolicy("vix_recovery", "VIX 진정 후 진입", "vix_recovery"),
            EtfPolicy("trend_rates", "추세 + 금리 상승 제한", "rates"),
            EtfPolicy("trend_credit", "추세 + 금융 스트레스 제한", "credit"),
            EtfPolicy("trend_oil", "추세 + 유가 급등 제한", "oil"),
            EtfPolicy("trend_curve", "추세 + 금리차 제한", "curve"),
            EtfPolicy("trend_fx_entry", "추세 + 환율 분할 진입", "fx_entry"),
        )
    }
)


def catalog():
    return [{**asdict(p), "required_series": list(p.required_series)} for p in POLICIES.values()]


def allocation(policy, closes, month_closes, macro, previous_target):
    """Consume only observations visible at the decision instant; return a target, reasons.

    Rate values are percentage points, implied volatility is index points. Every
    policy has separate state in its replay. No fitted weights or cross-run cache.
    """
    if policy.family == "buy_hold":
        return 1.0, ["buy_and_hold"]
    if policy.family == "fixed":
        return policy.defensive_weight, ["fixed_allocation"]
    if len(month_closes) < policy.trend_months:
        raise ValueError("Insufficient monthly trend warmup")
    trend = closes[-1] > mean(month_closes[-policy.trend_months :])
    target = 1.0 if trend else policy.defensive_weight
    reasons = ["uptrend" if trend else "defensive_trend"]
    for series in policy.required_series:
        if not macro.get(series):
            raise ValueError(f"Missing visible macro observations: {series}")
    if policy.family == "volatility":
        if len(closes) <= policy.volatility_sessions:
            raise ValueError("Insufficient volatility warmup")
        values = closes[-policy.volatility_sessions - 1 :]
        returns = [b / a - 1 for a, b in zip(values, values[1:])]
        vol = stdev(returns) * math.sqrt(252)
        target = min(target, policy.target_volatility / vol if vol else 1.0)
        reasons.append("realized_volatility_budget")
    elif policy.family == "vix_recovery":
        values = macro[policy.implied_series]
        if len(values) < 20 or len(closes) < 21:
            raise ValueError("Insufficient implied volatility warmup")
        peak = max(values[-20:])
        above_price = closes[-1] > mean(closes[-20:]) and closes[-2] > mean(closes[-21:-1])
        recovery = peak >= policy.fear_level and values[-1] <= peak * (1 - policy.recovery_fraction)
        # Hold an established recovery position while the long trend persists.
        if not trend:
            target = policy.defensive_weight
        elif recovery and above_price:
            target = 1.0
        else:
            target = min(target, previous_target)
        reasons.append("fear_recovery" if recovery and above_price else "wait_or_hold")
    elif policy.family == "rates":
        if min(len(macro["REAL10"]), len(macro["RATE2"])) < 64:
            raise ValueError("Insufficient rates warmup")
        real_rise = macro["REAL10"][-1] - macro["REAL10"][-64]
        short_rise = macro["RATE2"][-1] - macro["RATE2"][-64]
        if real_rise >= policy.real_rate_increase_pp or short_rise >= policy.short_rate_increase_pp:
            target = min(target, policy.defensive_weight)
            reasons.append("rates_rising")
    elif policy.family == "credit":
        values = macro["NFCI"]
        if len(values) < 5:
            raise ValueError("Insufficient financial conditions warmup")
        if values[-1] > 0 and values[-1] > values[-5]:
            target = min(target, policy.defensive_weight)
            reasons.append("financial_conditions_tightening")
    elif policy.family == "oil":
        values = macro["WTI"]
        if len(values) < 64:
            raise ValueError("Insufficient oil warmup")
        # Negative WTI prices are real data. Never log/divide by a non-positive base.
        if values[-64] <= 0 or values[-1] <= 0:
            target = min(target, policy.defensive_weight)
            reasons.append("nonpositive_oil_price_regime")
        elif values[-1] / values[-64] - 1 >= policy.oil_increase:
            target = min(target, policy.defensive_weight)
            reasons.append("oil_shock")
    elif policy.family == "curve":
        if macro["RATE10"][-1] - macro["RATE3M"][-1] < 0:
            target = min(target, policy.defensive_weight)
            reasons.append("inverted_curve")
    elif policy.family == "fx_entry":
        values = macro["FX_SIGNAL"]
        if len(values) < 252:
            raise ValueError("Insufficient FX signal warmup")
        if target > previous_target and values[-1] > mean(values[-252:]) * (1 + policy.fx_premium):
            target = min(target, previous_target + policy.entry_step)
            reasons.append("stage_entry_at_high_usdkrw")
    return max(0.0, min(1.0, target)), reasons
