from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

AssetKind = Literal["kospi", "etf_kr", "etf_us"]


class FrozenWeights(dict):
    """JSON/asdict-compatible immutable copy, including in-place dictionary operations."""

    def _deny(self, *args, **kwargs):
        raise TypeError("저장한 가중치는 변경할 수 없습니다. 새 전략 버전을 만드세요.")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _deny


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    kind: AssetKind
    sector: str


@dataclass(frozen=True)
class Bar:
    session: date
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: Decimal
    observed_at: datetime
    source: str


@dataclass(frozen=True)
class Strategy:
    version: int = 1
    name: str = "추세와 반등 탐색"
    weights: dict[str, int] = field(
        default_factory=lambda: {"oversold": 30, "pullback": 25, "trend": 25, "rebound": 20}
    )
    oversold_rsi: float = 30
    overbought_rsi: float = 70
    review_threshold: float = 65
    max_position_pct: float = 20

    def __post_init__(self) -> None:
        import math

        expected = {"oversold", "pullback", "trend", "rebound"}
        if set(self.weights) != expected:
            raise ValueError("가중치 항목이 올바르지 않습니다.")
        if any(type(v) is not int or not 0 <= v <= 100 for v in self.weights.values()):
            raise ValueError("가중치는 0~100의 정수여야 합니다.")
        if sum(self.weights.values()) != 100:
            raise ValueError("가중치 합계는 100이어야 합니다.")
        values = [
            self.oversold_rsi,
            self.overbought_rsi,
            self.review_threshold,
            self.max_position_pct,
        ]
        if not all(math.isfinite(value) for value in values):
            raise ValueError("설정값은 유한한 숫자여야 합니다.")
        if not 0 < self.oversold_rsi < self.overbought_rsi < 100:
            raise ValueError("RSI 기준은 0 < 과매도 < 과매수 < 100이어야 합니다.")
        if not 0 <= self.review_threshold <= 100 or not 0 < self.max_position_pct <= 100:
            raise ValueError("점수 또는 보유 비중 범위를 확인하세요.")
        if not self.name.strip() or len(self.name) > 60:
            raise ValueError("전략 이름은 1~60자여야 합니다.")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("전략 버전은 양의 정수여야 합니다.")
        object.__setattr__(self, "weights", FrozenWeights(self.weights))


@dataclass(frozen=True)
class Analysis:
    symbol: str
    as_of: date | None
    history_count: int
    close: float | None
    returns: dict[str, float | None]
    moving_averages: dict[str, float | None]
    distances: dict[str, float | None]
    rsi: float | None
    down_streak: int
    volume_ratio: float | None
    tags: list[str]
    score: float | None
    components: list[dict]
    strategy_version: int
