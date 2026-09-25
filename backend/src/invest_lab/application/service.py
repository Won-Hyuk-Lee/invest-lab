from dataclasses import asdict
from decimal import Decimal

from invest_lab.domain.indicators import analyze
from invest_lab.domain.models import Strategy
from invest_lab.ports import Clock, MarketData, PortfolioRepository, StrategyRepository


class Workspace:
    def __init__(
        self,
        market: MarketData,
        strategies: StrategyRepository,
        portfolio: PortfolioRepository,
        clock: Clock,
    ):
        self.market = market
        self.strategies = strategies
        self.portfolio = portfolio
        self.clock = clock

    def screen(self) -> dict:
        strategy = self.strategies.latest()
        rows = []
        for instrument in self.market.instruments():
            analysis = analyze(instrument.symbol, self.market.history(instrument.symbol), strategy)
            quote = self.market.quote(instrument.symbol)
            rows.append({**asdict(instrument), **asdict(analysis), "quote": asdict(quote)})
        return {
            "rows": rows,
            "source": self.market.source,
            "strategy": asdict(strategy),
            "generated_at": self.clock.now(),
            "period_basis": "trading_sessions",
        }

    def detail(self, symbol: str) -> dict:
        instrument = next((i for i in self.market.instruments() if i.symbol == symbol), None)
        if instrument is None:
            raise LookupError("등록되지 않은 종목입니다.")
        bars = self.market.history(symbol)
        return {
            **asdict(instrument),
            "analysis": asdict(analyze(symbol, bars, self.strategies.latest())),
            "quote": asdict(self.market.quote(symbol)),
            "bars": [asdict(bar) for bar in bars],
            "source": self.market.source,
        }

    def account(self) -> dict:
        account = self.portfolio.snapshot()
        names = {i.symbol: i.name for i in self.market.instruments()}
        holdings = []
        value = Decimal(0)
        cost = Decimal(0)
        for position in account["positions"]:
            quote = self.market.quote(position["symbol"])
            amount = quote.price * position["quantity"]
            basis = Decimal(position["average_price"]) * position["quantity"]
            value += amount
            cost += basis
            holdings.append(
                {
                    **position,
                    "name": names.get(position["symbol"], position["symbol"]),
                    "price": str(quote.price),
                    "value": str(amount),
                    "profit": str(amount - basis),
                }
            )
        return {
            **account,
            "positions": holdings,
            "holdings_value": str(value),
            "equity": str(Decimal(account["cash"]) + value),
            "profit": str(value - cost),
            "mode": "local_simulation",
            "source": self.market.source,
        }

    def save_strategy(self, values: dict, expected_version: int) -> Strategy:
        return self.strategies.save(Strategy(**values), expected_version)

    def buy(self, symbol: str, quantity: int, request_id: str) -> dict:
        if symbol not in {i.symbol for i in self.market.instruments()}:
            raise LookupError("등록되지 않은 종목입니다.")
        if type(quantity) is not int or not 1 <= quantity <= 100000:
            raise ValueError("수량은 1~100000의 정수여야 합니다.")
        quote = self.market.quote(symbol)
        age = (self.clock.now() - quote.observed_at).total_seconds()
        if not 0 <= age <= 300:
            raise ValueError("시세가 오래되었습니다. 새로고침한 뒤 다시 시도하세요.")
        return self.portfolio.buy(
            symbol,
            quantity,
            str(quote.price),
            request_id,
            self.strategies.latest().max_position_pct,
            self.clock.now(),
            self.market.source,
            {i.symbol: str(self.market.quote(i.symbol).price) for i in self.market.instruments()},
        )
