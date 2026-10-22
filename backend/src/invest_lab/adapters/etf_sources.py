"""Read-only public ETF research sources, with retained raw bytes and provenance."""

import bisect
import csv
import hashlib
import io
import json
import math
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from datetime import time as day_time
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import xlrd

from invest_lab.domain.etf_research import validate_etf_dataset

NY = ZoneInfo("America/New_York")


def stamp(day, hour=23, minute=59):
    return datetime.combine(day, day_time(hour, minute), NY).isoformat()


def observation(day, value, lag=1, hour=0, minute=0):
    return {
        "date": day.isoformat(),
        "value": float(value),
        "available_at": stamp(day + timedelta(days=lag), hour, minute),
    }


class RawCache:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, url):
        key = hashlib.sha256(url.encode()).hexdigest()
        meta_path = self.root / f"{key}.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            data = (self.root / meta["sha256"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != meta["sha256"]:
                raise ValueError("ETF raw cache integrity failure")
            if data:
                return data, meta
        error = None
        for attempt in range(3):
            try:
                response = httpx.get(
                    url,
                    timeout=60,
                    follow_redirects=True,
                    headers={"User-Agent": "invest-lab-research/0.1"},
                )
                response.raise_for_status()
                data = response.content
                if not data:
                    raise httpx.DecodingError("Empty ETF source response")
                digest = hashlib.sha256(data).hexdigest()
                meta = {
                    "url": url,
                    "sha256": digest,
                    "bytes": len(data),
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "content_type": response.headers.get("content-type", ""),
                }
                (self.root / digest).write_bytes(data)
                meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
                return data, meta
            except httpx.HTTPError as exc:
                error = exc
                time.sleep(attempt + 1)
        raise RuntimeError(f"ETF source failed: {url}") from error


def parse_treasury(data, real=False):
    fields = (
        {"TC_10YEAR": "REAL10"}
        if real
        else {"BC_3MONTH": "RATE3M", "BC_2YEAR": "RATE2", "BC_10YEAR": "RATE10"}
    )
    result = {v: [] for v in fields.values()}
    for node in ET.fromstring(data).iter():
        if node.tag.split("}")[-1] != "properties":
            continue
        values = {child.tag.split("}")[-1]: child.text for child in node}
        day = date.fromisoformat(values["NEW_DATE"][:10])
        for source, target in fields.items():
            if values.get(source) is not None:
                result[target].append(observation(day, values[source]))
    if not any(result.values()):
        raise ValueError("Empty Treasury response")
    return result


def parse_fx(data):
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    header_index = next(i for i, r in enumerate(rows) if r and r[0] == "Time Period")
    column = rows[header_index].index("RXI_N.B.KO")
    result = {}
    for row in rows[header_index + 1 :]:
        if len(row) > column and row[column] not in {"ND", "", "NA"}:
            value = float(row[column])
            if value <= 0 or not math.isfinite(value):
                raise ValueError("Invalid Fed KRW/USD fixing")
            result[row[0]] = value
    return result


def parse_oil(data):
    book = xlrd.open_workbook(file_contents=data)
    sheet = book.sheet_by_name("Data 1")
    result = []
    for index in range(3, sheet.nrows):
        day, value = sheet.row_values(index)[:2]
        if isinstance(day, (int, float)) and isinstance(value, (int, float)):
            day = xlrd.xldate_as_datetime(day, book.datemode).date()
            result.append(observation(day, value, lag=7, hour=23, minute=59))
    return result


def fx_on(fixings, days, day, before=False):
    index = (bisect.bisect_left if before else bisect.bisect_right)(days, day) - 1
    if index < 0 or (date.fromisoformat(day) - date.fromisoformat(days[index])).days > 7:
        raise ValueError(f"Missing/stale ETF valuation FX on {day}")
    return fixings[days[index]]


class PublicEtfSource:
    def __init__(self, raw_directory):
        self.cache = RawCache(raw_directory)

    def collect(self, symbols, start, end):
        start_day, end_day = date.fromisoformat(start), date.fromisoformat(end)
        if start_day >= end_day or end_day >= datetime.now(NY).date():
            raise ValueError("Collect only completed historical days, with start before end")
        if not symbols or any(s not in {"SPY", "QQQ"} for s in symbols):
            raise ValueError("Initial ETF research universe is SPY and QQQ")
        sources = []

        def read(url):
            data, meta = self.cache.get(url)
            sources.append(meta)
            return data

        fx_url = (
            "https://www.federalreserve.gov/datadownload/Output.aspx?rel=H10&"
            "series=a6a0113179fdeb9c6fbcdc18575ec09c&from="
            f"01/01/{min(2000, start_day.year - 1)}&to={end_day:%m/%d/%Y}"
            "&filetype=csv&label=include&layout=seriescolumn&type=package"
        )
        fx = parse_fx(read(fx_url))
        fx_days = sorted(fx)
        macro = {
            "FX_SIGNAL": [
                observation(date.fromisoformat(d), fx[d], lag=8, hour=23, minute=59)
                for d in fx_days
            ]
        }
        for symbol in ("VIX", "VXN"):
            raw = read(
                f"https://cdn.cboe.com/api/global/us_indices/daily_prices/{symbol}_History.csv"
            )
            rows = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
            macro[symbol] = [
                observation(
                    datetime.strptime(r["DATE"], "%m/%d/%Y").date(),
                    r["CLOSE"],
                    lag=0,
                    hour=16,
                    minute=15,
                )
                for r in rows
            ]
        raw = read("https://www.chicagofed.org/-/media/publications/nfci/nfci-data-series-csv.csv")
        macro["NFCI"] = [
            observation(
                datetime.strptime(r["Friday_of_Week"], "%m/%d/%Y").date(),
                r["NFCI"],
                lag=7,
                hour=23,
                minute=59,
            )
            for r in csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        ]
        macro["WTI"] = parse_oil(read("https://www.eia.gov/dnav/pet/hist_xls/RWTCd.xls"))
        urls = [
            (
                real,
                "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                f"pages/xml?data=daily_treasury_{'real_' if real else ''}yield_curve&"
                f"field_tdr_date_value={year}",
            )
            for year in range(max(2003, start_day.year), end_day.year + 1)
            for real in (False, True)
        ]

        def treasury(item):
            real, url = item
            data, meta = self.cache.get(url)
            return parse_treasury(data, real), meta

        with ThreadPoolExecutor(max_workers=4) as pool:
            for parsed, meta in pool.map(treasury, urls):
                sources.append(meta)
                for key, rows in parsed.items():
                    macro.setdefault(key, []).extend(rows)
        macro = {
            key: sorted(
                (r for r in rows if start <= r["date"] <= end),
                key=lambda r: (r["date"], r["available_at"]),
            )
            for key, rows in macro.items()
        }
        datasets = {}
        for symbol in sorted(set(symbols)):
            p1 = int(datetime.combine(start_day, day_time(), timezone.utc).timestamp())
            p2 = int(
                datetime.combine(end_day + timedelta(days=1), day_time(), timezone.utc).timestamp()
            )
            raw = read(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?"
                f"period1={p1}&period2={p2}&interval=1d&events=div%2Csplits"
            )
            chart = json.loads(raw)["chart"]["result"][0]
            quote, adjusted = chart["indicators"]["quote"][0], chart["indicators"]["adjclose"][0]
            bars = []
            for i, timestamp in enumerate(chart["timestamp"]):
                day = datetime.fromtimestamp(timestamp, NY).date()
                if not start_day <= day <= end_day:
                    continue
                close, opening, adj = quote["close"][i], quote["open"][i], adjusted["adjclose"][i]
                if any(v is None or v <= 0 for v in (close, opening, adj)):
                    raise ValueError(f"Missing adjusted ETF prices: {symbol}/{day}")
                text_day = day.isoformat()
                bars.append(
                    {
                        "session": text_day,
                        "open_at": stamp(day, 9, 30),
                        "close_at": stamp(day, 16, 0),
                        "open": opening * adj / close,
                        "close": adj,
                        "raw_close": close,
                        "fx_open": fx_on(fx, fx_days, text_day, before=True),
                        "fx_close": fx_on(fx, fx_days, text_day),
                        "month_end": False,
                    }
                )
            for previous, current in zip(bars, bars[1:]):
                previous["month_end"] = previous["session"][:7] != current["session"][:7]
            dataset = {
                "format_version": 1,
                "symbol": symbol,
                "source_kind": "real",
                "price_basis": "dividend_split_adjusted_research_units",
                "bars": bars,
                "macro": macro,
                "provenance": {
                    "sources": [],
                    "point_in_time_verified": False,
                    "requested_start": start,
                    "requested_end": end,
                    "availability_policy": {
                        "rates": "observation date + 1 calendar day at 00:00 New York",
                        "VIX_VXN": "observation date at 16:15 New York",
                        "NFCI_WTI": "observation date + 7 days at 23:59 New York",
                        "FX_SIGNAL": "observation date + 8 days at 23:59 New York",
                    },
                    "limitations": [
                        "Current revised snapshots; estimated release lags do not establish PIT.",
                        "Adjusted fractional research units; no dividend payment dates or taxes.",
                        "Prior Fed fixing proxies opening FX; same-day marks closing FX. "
                        "These are not executable quotes.",
                        "16:00 New York cutoff includes early-close sessions; VIX is lagged.",
                        "NFCI measures financial conditions, not high-yield credit spreads. "
                        "VIX covers S&P 500 volatility, not Nasdaq VXN.",
                        "Costs and cash interest are assumptions, not actual account terms.",
                        "Ending holdings are marked, not liquidated; no terminal sale costs.",
                    ],
                },
            }
            datasets[symbol] = validate_etf_dataset(dataset)
        calendars = {tuple(b["session"] for b in d["bars"]) for d in datasets.values()}
        if len(calendars) != 1:
            raise ValueError("ETF trading calendars differ; investigate missing source sessions")
        for dataset in datasets.values():
            dataset["provenance"]["sources"] = sorted(sources, key=lambda s: s["url"])
        return datasets
