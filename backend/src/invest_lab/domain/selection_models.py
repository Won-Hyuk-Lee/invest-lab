"""Predeclared universe selection and risk allocation; no market-data or broker dependencies."""

import math
from dataclasses import asdict, dataclass
from datetime import date
from statistics import mean
from types import MappingProxyType

import numpy as np

from invest_lab.domain.auto_models import decide as original_decide
from invest_lab.domain.auto_replay import prepare

SECTORS = ("XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY")
GLOBAL = ("SPY", "QQQ", "IWM", "EFA", "EEM", "IYR", "IEF", "TLT", "GLD")
STYLES = ("QUAL", "MTUM", "IWD", "IWF", "USMV")
EQUITIES = frozenset((*SECTORS, *STYLES, "SPY", "QQQ", "IWM", "EFA", "EEM", "IYR"))


@dataclass(frozen=True)
class SelectionModel:
    id: str
    label: str
    candidates: tuple[str, ...]
    top_k: int = 3
    trend: bool = True
    weighting: str = "equal"
    target_vol: float | None = None
    max_weight: float = 1.0
    equity_cap: float = 1.0
    buffer_rank: int = 0
    weekly_risk: bool = True
    defensive: str | None = None
    select_all: bool = False
    kind: str = "selection"

    @property
    def symbols(self):
        return tuple(sorted(set(self.candidates) | ({self.defensive} if self.defensive else set())))

    def __post_init__(self):
        if not self.candidates or len(set(self.candidates)) != len(self.candidates):
            raise ValueError("Candidates must be nonempty and unique")
        if type(self.top_k) is not int or not 1 <= self.top_k <= len(self.candidates):
            raise ValueError("Invalid number of selection slots")
        if self.weighting not in {"equal", "inverse_vol"} or self.kind != "selection":
            raise ValueError("Invalid selection policy")
        if self.defensive in self.candidates:
            raise ValueError("Defensive asset cannot also be a ranked candidate")
        if self.buffer_rank and not self.top_k <= self.buffer_rank <= len(self.candidates):
            raise ValueError("Retention buffer must contain at least the top selection")
        for value in (self.max_weight, self.equity_cap):
            if not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError("Invalid portfolio cap")
        if self.target_vol is not None and (
            not math.isfinite(self.target_vol) or not 0 < self.target_vol <= 1
        ):
            raise ValueError("Invalid volatility target")


def _models():
    output = []
    for prefix, candidates, slots, label in (
        ("sector", SECTORS, 3, "미국 업종"),
        ("global", GLOBAL, 3, "세계 자산"),
        ("style", STYLES, 2, "투자 스타일"),
    ):
        output.extend(
            [
                SelectionModel(
                    f"{prefix}_equal",
                    f"{label} 동일 비중",
                    candidates,
                    len(candidates),
                    trend=False,
                    select_all=True,
                ),
                SelectionModel(
                    f"{prefix}_rank",
                    f"{label} 상대 강도",
                    candidates,
                    slots,
                    trend=False,
                ),
                SelectionModel(f"{prefix}_trend", f"{label} 상대 강도+추세", candidates, slots),
                SelectionModel(
                    f"{prefix}_risk",
                    f"{label} 추세+위험 배분",
                    candidates,
                    slots,
                    weighting="inverse_vol",
                    target_vol=0.12,
                    max_weight=0.6 if prefix == "style" else 0.4,
                ),
            ]
        )
    output.extend(
        [
            SelectionModel("sector_buffer", "업종 순위 완충", SECTORS, buffer_rank=5),
            SelectionModel(
                "global_group",
                "세계 자산 주식그룹 60% 제한",
                GLOBAL,
                weighting="inverse_vol",
                target_vol=0.12,
                max_weight=0.4,
                equity_cap=0.6,
            ),
            SelectionModel(
                "global_shy",
                "세계 자산 위험 배분+단기국채",
                GLOBAL,
                weighting="inverse_vol",
                target_vol=0.12,
                max_weight=0.4,
                defensive="SHY",
            ),
            SelectionModel(
                "qqq_risk",
                "QQQ 추세+목표 변동성",
                ("QQQ",),
                1,
                target_vol=0.12,
            ),
            SelectionModel(
                "quality_momentum",
                "QUAL·MTUM 동일 비중",
                ("QUAL", "MTUM"),
                2,
                trend=False,
                select_all=True,
            ),
        ]
    )
    return {m.id: m for m in output}


SELECTION_MODELS = MappingProxyType(_models())


def prepare_selection(datasets):
    """Past-only covariance and week calendar; reuse the existing validated price tape."""
    tape = prepare(datasets, tuple(sorted(datasets)))
    symbols = tuple(sorted(tape["bars"]))
    prices = np.asarray([[b["close"] for b in tape["bars"][s]] for s in symbols]).T
    returns = prices[1:] / prices[:-1] - 1
    covariances = [None] * len(prices)
    for i in range(63, len(prices)):
        covariances[i] = np.atleast_2d(np.cov(returns[i - 63 : i], rowvar=False, ddof=1)) * 252
    days = tape["sessions"]
    weeks = [date.fromisoformat(d).isocalendar()[:2] for d in days]
    tape["risk_symbols"] = symbols
    tape["covariances"] = covariances
    tape["week_ends"] = [a != b for a, b in zip(weeks, weeks[1:])] + [False]
    return tape


def target_weights(model, chosen, covariance, symbols):
    """No leverage; unfilled selection slots and clipped budgets stay defensive/cash."""
    index = {s: i for i, s in enumerate(symbols)}
    vol = {s: math.sqrt(max(0.0, covariance[index[s], index[s]])) for s in chosen}
    slots = len(model.candidates) if model.select_all else model.top_k
    budget = len(chosen) / slots
    raw = {s: 1 / max(vol[s], 0.01) if model.weighting == "inverse_vol" else 1 for s in chosen}
    denominator = sum(raw.values())
    weights = {s: min(model.max_weight, budget * v / denominator) for s, v in raw.items()}
    equity = sum(w for s, w in weights.items() if s in EQUITIES)
    if equity > model.equity_cap:
        weights = {
            s: w * (model.equity_cap / equity if s in EQUITIES else 1) for s, w in weights.items()
        }
    vector = np.array([weights.get(s, 0) for s in symbols])
    estimated = math.sqrt(max(0.0, float(vector @ covariance @ vector)))
    scale = min(1.0, model.target_vol / estimated) if model.target_vol and estimated else 1.0
    weights = {s: w * scale for s, w in weights.items()}
    if model.defensive:
        weights[model.defensive] = max(0.0, 1 - sum(weights.values()))
    final = np.array([weights.get(s, 0) for s in symbols])
    after_defensive = math.sqrt(max(0.0, float(final @ covariance @ final)))
    final_scale = (
        min(1.0, model.target_vol / after_defensive)
        if model.target_vol and after_defensive
        else 1.0
    )
    weights = {s: w * final_scale for s, w in weights.items()}
    return weights, {
        "selected": list(chosen),
        "volatility_before_scale": estimated,
        "risk_scale": scale,
        "defensive_risk_scale": final_scale,
        "volatility_after_defensive": after_defensive * final_scale,
        "equity_weight": sum(w for s, w in weights.items() if s in EQUITIES),
    }


def decision_function(tape, diagnostics):
    def decide(model, current, positions, state, index, month_end, initial, vix):
        if not isinstance(model, SelectionModel):
            return original_decide(model, current, positions, state, index, month_end, initial, vix)
        select = initial or month_end
        if not select and not (
            model.target_vol is not None and model.weekly_risk and tape["week_ends"][index]
        ):
            return None, "wait_selection_or_risk_update"
        if any(not current[s]["ready"] for s in model.candidates):
            raise ValueError("Selection needs 253 observed warmup sessions")
        covariance = tape["covariances"][index]
        if covariance is None:
            raise ValueError("Risk allocation needs 63 observed daily returns")
        if select:
            score = {
                s: mean(current[s][f"ret{n}"] for n in (63, 126, 252)) for s in model.candidates
            }
            ranked = sorted(model.candidates, key=lambda s: (-score[s], s))
            if model.select_all:
                chosen = list(model.candidates)
            elif model.buffer_rank:
                retained = [
                    s for s in ranked[: model.buffer_rank] if s in state.get("selected", [])
                ]
                chosen = (retained + [s for s in ranked if s not in retained])[: model.top_k]
            else:
                chosen = ranked[: model.top_k]
            if model.trend:
                chosen = [
                    s for s in chosen if score[s] > 0 and current[s]["close"] > current[s]["sma200"]
                ]
            state["selected"] = chosen
        weights, detail = target_weights(model, state["selected"], covariance, tape["risk_symbols"])
        diagnostics.append({"session": tape["sessions"][index], "weights": weights, **detail})
        return weights, "monthly_selection" if select else "weekly_risk_update"

    return decide


def catalog():
    return [asdict(m) for m in SELECTION_MODELS.values()]
