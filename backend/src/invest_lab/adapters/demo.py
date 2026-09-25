import math
import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from invest_lab.domain.models import Bar, Instrument, Quote

# An explicit starter watchlist, not a complete or historical KOSPI universe.
INSTRUMENTS = [
    Instrument("005930", "삼성전자", "kospi", "반도체"),
    Instrument("000660", "SK하이닉스", "kospi", "반도체"),
    Instrument("005380", "현대차", "kospi", "자동차"),
    Instrument("035420", "NAVER", "kospi", "인터넷"),
    Instrument("105560", "KB금융", "kospi", "금융"),
    Instrument("068270", "셀트리온", "kospi", "바이오"),
    Instrument("069500", "KODEX 200", "etf_kr", "국내 지수"),
    Instrument("102110", "TIGER 200", "etf_kr", "국내 지수"),
    Instrument("360750", "TIGER 미국S&P500", "etf_us", "미국 지수"),
    Instrument("133690", "TIGER 미국나스닥100", "etf_us", "미국 지수"),
]


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class DemoMarket:
    source = "synthetic_demo"

    def __init__(self, clock: SystemClock):
        self.clock = clock
        self._bars: dict[str, list[Bar]] = {}
        # Weekdays in this fixture are synthetic sessions, not a KRX holiday calendar.
        dates = []
        day = date(2026, 9, 24)
        while len(dates) < 260:
            if day.weekday() < 5:
                dates.append(day)
            day -= timedelta(days=1)
        dates.reverse()
        for index, instrument in enumerate(INSTRUMENTS):
            rng = random.Random(instrument.symbol)
            price = [62000, 170000, 225000, 190000, 78000, 175000, 35000, 35500, 17000, 105000][
                index
            ]
            bars = []
            for step, session in enumerate(dates):
                change = 0.001 + math.sin(step / 12 + index) * 0.007 + rng.uniform(-0.009, 0.009)
                if step > 245:
                    if index in (0, 3, 5):
                        change = -0.009 - rng.random() * 0.01
                    elif index in (1, 4, 8):
                        change = 0.006 + rng.random() * 0.009
                if step == 259 and index == 3:
                    change = 0.025
                opening = round(price)
                price = max(100, round(price * (1 + change)))
                volume = rng.randint(300000, 1200000)
                if step == 259 and index == 3:
                    volume = 2400000
                bars.append(
                    Bar(
                        session,
                        opening,
                        max(opening, price) * 1.004,
                        min(opening, price) * 0.996,
                        price,
                        volume,
                    )
                )
            self._bars[instrument.symbol] = bars

    def instruments(self) -> list[Instrument]:
        return list(INSTRUMENTS)

    def history(self, symbol: str) -> list[Bar]:
        return list(self._bars[symbol])

    def quote(self, symbol: str) -> Quote:
        return Quote(
            symbol, Decimal(str(self._bars[symbol][-1].close)), self.clock.now(), self.source
        )
