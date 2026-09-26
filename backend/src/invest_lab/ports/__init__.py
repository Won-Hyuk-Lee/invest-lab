from datetime import datetime
from typing import Protocol

from invest_lab.domain.models import Bar, Instrument, Quote, Strategy


class MarketData(Protocol):
    source: str

    def instruments(self) -> list[Instrument]: ...

    def history(self, symbol: str) -> list[Bar]: ...

    def quote(self, symbol: str) -> Quote: ...


class StrategyRepository(Protocol):
    def latest(self) -> Strategy: ...

    def versions(self) -> list[Strategy]: ...

    def save(self, strategy: Strategy, expected_version: int) -> Strategy: ...


class PortfolioRepository(Protocol):
    def snapshot(self) -> dict: ...

    def order_result(self, request_id: str) -> dict | None: ...

    def buy(
        self,
        symbol: str,
        quantity: int,
        price: str,
        request_id: str,
        max_position_pct: float,
        at: datetime,
        source: str,
        marks: dict[str, str],
    ) -> dict: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
