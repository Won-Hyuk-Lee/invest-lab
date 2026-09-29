"""Public price/FX/VIX snapshots for independently evaluated automatic models."""

import csv
import io
import json
from datetime import date, datetime, time, timedelta, timezone

from invest_lab.adapters.etf_sources import NY, RawCache, fx_on, observation, parse_fx, stamp
from invest_lab.domain.etf_research import validate_etf_dataset

UNIVERSE = ("SPY", "QQQ", "IEF", "GLD")


class AutoModelSource:
    universe = UNIVERSE

    def __init__(self, raw_directory):
        self.cache = RawCache(raw_directory)

    def collect(self, symbols, start, end):
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        if first >= last or last >= datetime.now(NY).date():
            raise ValueError("Use a completed historical date range")
        if not symbols or set(symbols) - set(self.universe):
            raise ValueError("Unsupported automatic-model universe")
        sources = []

        def read(url):
            body, meta = self.cache.get(url)
            sources.append(meta)
            return body

        fx = parse_fx(
            read(
                "https://www.federalreserve.gov/datadownload/Output.aspx?rel=H10&"
                "series=a6a0113179fdeb9c6fbcdc18575ec09c&from="
                f"01/01/2000&to={last:%m/%d/%Y}"
                "&filetype=csv&label=include&layout=seriescolumn&type=package"
            )
        )
        fx_days = sorted(fx)
        raw = read("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv")
        vix = [
            observation(
                datetime.strptime(r["DATE"], "%m/%d/%Y").date(),
                r["CLOSE"],
                lag=0,
                hour=16,
                minute=15,
            )
            for r in csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
            if datetime.strptime(r["DATE"], "%m/%d/%Y").date() <= last
        ]
        result = {}
        for symbol in sorted(set(symbols)):
            p1 = int(datetime.combine(first, time(), timezone.utc).timestamp())
            p2 = int(datetime.combine(last + timedelta(days=1), time(), timezone.utc).timestamp())
            body = read(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?"
                f"period1={p1}&period2={p2}&interval=1d&events=div%2Csplits"
            )
            chart = json.loads(body)["chart"]["result"][0]
            quote, adjusted = chart["indicators"]["quote"][0], chart["indicators"]["adjclose"][0]
            bars = []
            for i, ts in enumerate(chart["timestamp"]):
                day = datetime.fromtimestamp(ts, NY).date()
                if not first <= day <= last:
                    continue
                opening, close, adj = quote["open"][i], quote["close"][i], adjusted["adjclose"][i]
                if any(x is None or x <= 0 for x in (opening, close, adj)):
                    raise ValueError(f"Missing price: {symbol}/{day}")
                text_day = str(day)
                bars.append(
                    {
                        "session": text_day,
                        "open_at": stamp(day, 9, 30),
                        "close_at": stamp(day, 16, 0),
                        "open": opening * adj / close,
                        "close": adj,
                        "raw_close": close,
                        "month_end": False,
                        "fx_open": fx_on(fx, fx_days, text_day, before=True),
                        "fx_close": fx_on(fx, fx_days, text_day),
                    }
                )
            for a, b in zip(bars, bars[1:]):
                a["month_end"] = a["session"][:7] != b["session"][:7]
            result[symbol] = validate_etf_dataset(
                {
                    "format_version": 1,
                    "symbol": symbol,
                    "source_kind": "real",
                    "price_basis": "dividend_split_adjusted_research_units",
                    "bars": bars,
                    "macro": {"VIX": vix},
                    "provenance": {
                        "point_in_time_verified": False,
                        "sources": [],
                        "requested_start": start,
                        "requested_end": end,
                        "limitations": [
                            "Adjusted fractional total-return units, "
                            "not actual shares/dividend cashflows.",
                            "Current revised data, no historical vintages; "
                            "estimated VIX cutoff 16:15 NY.",
                            "FX execution proxy is prior Fed fixing; "
                            "valuation uses current/prior fixing.",
                            "No taxes, integer lots or terminal liquidation; "
                            "costs are explicit assumptions.",
                            "Close-triggered exits execute at a later open; "
                            "gaps can exceed stop thresholds.",
                            "Chosen surviving ETFs, not an unbiased historical all-fund universe.",
                        ],
                    },
                }
            )
        for dataset in result.values():
            dataset["provenance"]["sources"] = sorted(sources, key=lambda s: s["url"])
        return result
