"""Durable, versioned imports and network-free market reads.

Calendar rows are supplied from a reviewed source. Missing dates are unknown, never
inferred to be open from a weekday. Price revisions retain their availability time.
"""

import csv
import hashlib
import io
import json
import os
import sqlite3
import zipfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from invest_lab.adapters.kis import decimal_string, integer, symbol_code
from invest_lab.domain.models import Bar, Instrument, Quote

KST = ZoneInfo("Asia/Seoul")
KIS_MASTER_WIDTHS = (
    2,
    1,
    4,
    4,
    4,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    9,
    5,
    5,
    1,
    1,
    1,
    2,
    1,
    1,
    1,
    2,
    2,
    2,
    3,
    1,
    3,
    12,
    12,
    8,
    15,
    21,
    2,
    7,
    1,
    1,
    1,
    1,
    1,
    9,
    9,
    9,
    5,
    9,
    8,
    9,
    3,
    1,
    1,
    1,
)


class MarketDataError(ValueError):
    pass


def timestamp(value) -> datetime:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise MarketDataError("An explicit timezone is required for data timestamps.") from None


def session_date(value) -> date:
    try:
        if isinstance(value, datetime):
            raise ValueError
        return value if isinstance(value, date) else date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise MarketDataError("A valid trading date is required.") from None


def _json(row):
    try:
        return json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise MarketDataError("Imported metadata must contain finite JSON values.") from None


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


class MarketStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS market_instruments (
                    id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, available_at TEXT NOT NULL,
                    source TEXT NOT NULL, checksum TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(symbol,available_at,source,checksum)
                );
                CREATE INDEX IF NOT EXISTS market_instrument_time
                    ON market_instruments(symbol,available_at);
                CREATE TABLE IF NOT EXISTS market_calendar (
                    id INTEGER PRIMARY KEY, session TEXT NOT NULL, available_at TEXT NOT NULL,
                    source TEXT NOT NULL, checksum TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(session,available_at,source,checksum)
                );
                CREATE INDEX IF NOT EXISTS market_calendar_time
                    ON market_calendar(session,available_at);
                CREATE TABLE IF NOT EXISTS market_bars (
                    id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, session TEXT NOT NULL,
                    adjusted INTEGER NOT NULL, available_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    revision TEXT NOT NULL, source TEXT NOT NULL, checksum TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(symbol,session,adjusted,available_at,source,checksum)
                );
                CREATE INDEX IF NOT EXISTS market_bar_time
                    ON market_bars(symbol,adjusted,session,available_at);
                CREATE TABLE IF NOT EXISTS market_quotes (
                    symbol TEXT PRIMARY KEY, observed_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_checkpoints (
                    key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_source_artifacts (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, retrieved_at TEXT NOT NULL,
                    filename TEXT NOT NULL, payload TEXT NOT NULL
                );
            """)

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _source(source):
        if not isinstance(source, str) or not source.strip() or len(source) > 120:
            raise MarketDataError("An explicit data source is required.")
        return source.strip()

    def import_instruments(self, rows, *, source, as_of):
        source, available = self._source(source), timestamp(as_of).isoformat()
        validated, seen = [], set()
        for item in rows:
            row = dict(item)
            symbol = symbol_code(row.get("symbol"))
            if symbol in seen:
                raise MarketDataError("Duplicate instrument in an import.")
            seen.add(symbol)
            if row.get("kind") not in {"kospi", "etf_kr", "etf_us"}:
                raise MarketDataError("Instrument classification must be explicit.")
            if not isinstance(row.get("name"), str) or not row["name"].strip():
                raise MarketDataError("Instrument name is missing.")
            if row.get("market", "KOSPI") not in {"KOSPI", "ETF"}:
                raise MarketDataError("Only KOSPI stocks and Korea-listed ETFs are supported.")
            row.setdefault("sector", "미분류")
            if not isinstance(row["sector"], str):
                raise MarketDataError("Instrument sector must be text.")
            for field in ("listed_on", "delisted_on", "valid_from", "valid_to"):
                if row.get(field) is not None:
                    row[field] = session_date(row[field]).isoformat()
            if row.get("listed_on") and row.get("delisted_on"):
                if row["listed_on"] > row["delisted_on"]:
                    raise MarketDataError("Listing dates are reversed.")
            row.update(source=source, available_at=available)
            payload = _json(row)
            validated.append((symbol, available, source, _hash(payload), payload))
        with self.connection() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO market_instruments "
                "(symbol,available_at,source,checksum,payload) VALUES (?,?,?,?,?)",
                validated,
            )
            return conn.total_changes - before

    def instruments(self, *, as_of=None, active_on=None):
        cutoff = timestamp(as_of or datetime.now(timezone.utc)).isoformat()
        on = session_date(active_on).isoformat() if active_on is not None else None
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT payload FROM (SELECT payload,ROW_NUMBER() OVER "
                "(PARTITION BY symbol ORDER BY available_at DESC,id DESC) AS n "
                "FROM market_instruments WHERE available_at<=?) WHERE n=1",
                (cutoff,),
            ).fetchall()
        result = []
        for value in rows:
            row = json.loads(value[0])
            if on and (
                (row.get("listed_on") or on) > on or (row.get("delisted_on") or "9999") < on
            ):
                continue
            if on and ((row.get("valid_from") or on) > on or (row.get("valid_to") or "9999") < on):
                continue
            result.append(row)
        return sorted(result, key=lambda row: row["symbol"])

    def import_calendar(self, rows, *, source, as_of):
        source, available = self._source(source), timestamp(as_of).isoformat()
        values, seen = [], set()
        for item in rows:
            row = dict(item)
            session = session_date(row.get("session")).isoformat()
            if session in seen or type(row.get("is_open")) is not bool:
                raise MarketDataError("Calendar dates must be unique with an explicit open flag.")
            seen.add(session)
            if row["is_open"]:
                opened, closed = timestamp(row.get("open_at")), timestamp(row.get("close_at"))
                if opened >= closed or opened.astimezone(KST).date().isoformat() != session:
                    raise MarketDataError("Invalid exchange session window.")
                if closed.astimezone(KST).date().isoformat() != session:
                    raise MarketDataError("The close must be on the declared KRX session.")
                row.update(open_at=opened.isoformat(), close_at=closed.isoformat())
            else:
                if row.get("open_at") is not None or row.get("close_at") is not None:
                    raise MarketDataError("Closed sessions cannot have trading hours.")
                row.update(open_at=None, close_at=None)
            row.update(session=session, source=source, available_at=available)
            payload = _json(row)
            values.append((session, available, source, _hash(payload), payload))
        with self.connection() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO market_calendar "
                "(session,available_at,source,checksum,payload) VALUES (?,?,?,?,?)",
                values,
            )
            return conn.total_changes - before

    def calendar(self, session, *, as_of=None):
        session = session_date(session).isoformat()
        cutoff = timestamp(as_of or datetime.now(timezone.utc)).isoformat()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT payload FROM market_calendar WHERE session=? AND available_at<=? "
                "ORDER BY available_at DESC,id DESC LIMIT 1",
                (session, cutoff),
            ).fetchone()
        if row is None:
            raise MarketDataError("Calendar coverage is missing; trading is blocked.")
        return json.loads(row[0])

    def is_trading_time(self, at):
        at = timestamp(at)
        row = self.calendar(at.astimezone(KST).date(), as_of=at)
        return bool(row["is_open"] and timestamp(row["open_at"]) <= at < timestamp(row["close_at"]))

    is_open = is_trading_time

    def sessions(self, start, end, *, as_of=None):
        start, end = session_date(start), session_date(end)
        if start > end:
            raise MarketDataError("Session dates are reversed.")
        result = []
        while start <= end:
            row = self.calendar(start, as_of=as_of)
            if row["is_open"]:
                result.append(row)
            start += timedelta(days=1)
        return result

    def next_session(self, after, *, as_of=None):
        current = session_date(after)
        for _ in range(370):
            current += timedelta(days=1)
            row = self.calendar(current, as_of=as_of)
            if row["is_open"]:
                return row
        raise MarketDataError("No subsequent trading session is available.")

    def import_bars(
        self,
        symbol,
        rows,
        *,
        adjusted=False,
        source,
        available_at,
        recorded_at=None,
        revision=None,
        require_calendar=True,
    ):
        symbol = symbol_code(symbol)
        source = self._source(source)
        if type(adjusted) is not bool:
            raise MarketDataError("Adjustment basis must be explicit.")
        available = timestamp(available_at)
        recorded = timestamp(recorded_at or datetime.now(timezone.utc))
        if available > recorded:
            raise MarketDataError("A bar cannot be available after its collection time.")
        catalog = {row["symbol"]: row for row in self.instruments(as_of=recorded)}
        if symbol not in catalog:
            raise MarketDataError("Import the instrument master before its prices.")
        values, seen = [], set()
        for item in rows:
            row = dict(item)
            session = session_date(row.get("session"))
            if session in seen:
                raise MarketDataError("Duplicate bar session in an import.")
            seen.add(session)
            if row.get("symbol", symbol) != symbol or row.get("adjusted", adjusted) != adjusted:
                raise MarketDataError("Mixed instrument or adjustment basis.")
            if row.get("venue", "KRX") != "KRX":
                raise MarketDataError("Only KRX session bars are accepted.")
            prices = {
                field: Decimal(decimal_string(row.get(field)))
                for field in ("open", "high", "low", "close")
            }
            if any(value <= 0 for value in prices.values()):
                raise MarketDataError("OHLC prices must be positive.")
            if not (
                prices["low"]
                <= min(prices["open"], prices["close"])
                <= max(prices["open"], prices["close"])
                <= prices["high"]
            ):
                raise MarketDataError("OHLC bounds are inconsistent.")
            if session > available.astimezone(KST).date():
                raise MarketDataError("Future session data is not allowed.")
            if require_calendar:
                calendar = self.calendar(session, as_of=recorded)
                if not calendar["is_open"] or available < timestamp(calendar["close_at"]):
                    raise MarketDataError(
                        "A final bar requires a confirmed closed trading session."
                    )
            listed = catalog[symbol].get("listed_on")
            delisted = catalog[symbol].get("delisted_on")
            if (listed and session.isoformat() < listed) or (
                delisted and session.isoformat() > delisted
            ):
                raise MarketDataError("Bar lies outside the instrument listing interval.")
            row.update({field: format(value, "f") for field, value in prices.items()})
            row.update(
                symbol=symbol,
                session=session.isoformat(),
                adjusted=adjusted,
                volume=integer(row.get("volume")),
                source=source,
                venue="KRX",
                available_at=available.isoformat(),
                finalized=require_calendar,
            )
            row["turnover"] = (
                decimal_string(row["turnover"]) if row.get("turnover") is not None else None
            )
            payload = _json(row)
            values.append((session.isoformat(), _hash(payload), payload))
        version = str(revision or _hash(_json(values)))
        with self.connection() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO market_bars "
                "(symbol,session,adjusted,available_at,recorded_at,revision,source,"
                "checksum,payload) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        symbol,
                        session,
                        int(adjusted),
                        available.isoformat(),
                        recorded.isoformat(),
                        version,
                        source,
                        checksum,
                        payload,
                    )
                    for session, checksum, payload in values
                ],
            )
            return conn.total_changes - before

    def history(
        self,
        symbol,
        *,
        adjusted=True,
        as_of=None,
        start=None,
        end=None,
        replay_ingestion=False,
        revision=None,
        finalized_only=True,
    ):
        symbol_code(symbol)
        cutoff = timestamp(as_of or datetime.now(timezone.utc)).isoformat()
        clauses, params = (
            ["symbol=?", "adjusted=?", "available_at<=?"],
            [symbol, int(adjusted), cutoff],
        )
        if replay_ingestion:
            clauses.append("recorded_at<=?")
            params.append(cutoff)
        if revision is not None:
            clauses.append("revision=?")
            params.append(revision)
        for field, value, op in (("session", start, ">="), ("session", end, "<=")):
            if value is not None:
                clauses.append(field + op + "?")
                params.append(session_date(value).isoformat())
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT payload,revision,checksum,recorded_at FROM (SELECT *,ROW_NUMBER() OVER "
                "(PARTITION BY session ORDER BY available_at DESC,id DESC) AS n "
                "FROM market_bars WHERE " + " AND ".join(clauses) + ") WHERE n=1 ORDER BY session",
                params,
            ).fetchall()
        result = []
        for item in rows:
            row = json.loads(item["payload"])
            if finalized_only and not row["finalized"]:
                continue
            row.update(
                revision=item["revision"],
                checksum=item["checksum"],
                recorded_at=item["recorded_at"],
            )
            result.append(row)
        return result

    def put_quote(self, row):
        value = dict(row)
        symbol = symbol_code(value.get("symbol"))
        observed = timestamp(value.get("observed_at")).isoformat()
        price = decimal_string(value.get("price"))
        if Decimal(price) <= 0:
            raise MarketDataError("Quote price must be positive.")
        self._source(value.get("source"))
        value.update(price=price, observed_at=observed)
        if value.get("timestamp_basis") == "exchange":
            value["source_timestamp"] = timestamp(value.get("source_timestamp")).isoformat()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT payload FROM market_quotes WHERE symbol=?", (symbol,)
            ).fetchone()
            if previous is not None:
                old = json.loads(previous[0])
                if (
                    old.get("timestamp_basis") == "exchange"
                    and value.get("timestamp_basis") == "exchange"
                    and old["source_timestamp"] > value["source_timestamp"]
                ):
                    return old
            conn.execute(
                "INSERT INTO market_quotes VALUES (?,?,?) ON CONFLICT(symbol) DO UPDATE SET "
                "observed_at=excluded.observed_at,payload=excluded.payload "
                "WHERE excluded.observed_at>=market_quotes.observed_at",
                (symbol, observed, _json(value)),
            )
        return value

    def bar_rows(self, symbol, **kwargs):
        return self.history(symbol, **kwargs)

    def quote(self, symbol):
        symbol_code(symbol)
        with self.connection() as conn:
            row = conn.execute(
                "SELECT payload FROM market_quotes WHERE symbol=?", (symbol,)
            ).fetchone()
        if row is None:
            raise MarketDataError("No observed quote is cached for this instrument.")
        return json.loads(row[0])

    def coverage(self, symbol, start, end, *, adjusted=True, as_of=None):
        start, end = session_date(start), session_date(end)
        if start > end:
            raise MarketDataError("Coverage dates are reversed.")
        available = {
            row["session"]
            for row in self.history(symbol, start=start, end=end, adjusted=adjusted, as_of=as_of)
        }
        catalog = {row["symbol"]: row for row in self.instruments(as_of=as_of)}
        instrument = catalog.get(symbol, {})
        missing, unknown, outside = [], [], []
        expected = 0
        current = start
        while current <= end:
            day = current.isoformat()
            current += timedelta(days=1)
            if (instrument.get("listed_on") and day < instrument["listed_on"]) or (
                instrument.get("delisted_on") and day > instrument["delisted_on"]
            ):
                outside.append(day)
                continue
            try:
                calendar = self.calendar(day, as_of=as_of)
            except MarketDataError:
                unknown.append(day)
                continue
            if calendar["is_open"]:
                expected += 1
                if day not in available:
                    missing.append(day)
        return {
            "symbol": symbol,
            "expected": expected,
            "present": expected - len(missing),
            "missing_sessions": missing,
            "unknown_calendar": unknown,
            "outside_listing": outside,
            "complete": not missing and not unknown,
        }

    def checkpoint(self, key, value=None):
        with self.connection() as conn:
            if value is not None:
                conn.execute(
                    "INSERT INTO market_checkpoints VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
                    "payload=excluded.payload,updated_at=excluded.updated_at",
                    (key, _json(value), datetime.now(timezone.utc).isoformat()),
                )
            row = conn.execute(
                "SELECT payload FROM market_checkpoints WHERE key=?", (key,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def overview(self, *, as_of=None):
        with self.connection() as conn:
            counts = {
                name: conn.execute(f"SELECT COUNT(*) FROM market_{name}").fetchone()[0]
                for name in ("instruments", "calendar", "bars", "quotes")
            }
            checkpoints = [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT payload FROM market_checkpoints ORDER BY key"
                ).fetchall()
            ]
        return {
            "source": "kis_paper_cache",
            "version_rows": counts,
            "instruments": len(self.instruments(as_of=as_of)),
            "checkpoints": checkpoints,
        }


class CachedMarket:
    source = "kis_paper_cache"

    def __init__(self, store: MarketStore, clock, *, adjusted=True):
        self.store, self.clock, self.adjusted = store, clock, adjusted

    def instruments(self):
        at = self.clock.now()
        return [
            Instrument(row["symbol"], row["name"], row["kind"], row["sector"])
            for row in self.store.instruments(as_of=at, active_on=at.astimezone(KST).date())
        ]

    def history(self, symbol):
        rows = self.store.history(symbol, adjusted=self.adjusted, as_of=self.clock.now())
        return [
            Bar(
                session_date(row["session"]),
                *[float(row[field]) for field in ("open", "high", "low", "close")],
                row["volume"],
            )
            for row in rows
        ]

    def quote(self, symbol):
        row = self.store.quote(symbol)
        # Never re-stamp an old price with the UI request time or a synthetic close.
        return Quote(symbol, Decimal(row["price"]), timestamp(row["observed_at"]), row["source"])


class MarketIngestion:
    """Explicit background operations; construct/read CachedMarket without network IO."""

    def __init__(self, client, store: MarketStore, clock):
        self.client, self.store, self.clock = client, store, clock

    def refresh_quotes(self, symbols):
        results = []
        for symbol in dict.fromkeys(symbols):
            results.append(self.store.put_quote(self.client.quote(symbol)))
        return results

    def collect(self, symbols, start, end, *, adjusted=True, resume=True):
        results = {}
        for symbol in dict.fromkeys(symbols):
            results[symbol] = self.refresh_history(
                symbol, start, end, adjusted=adjusted, resume=resume
            )
        return results

    def overview(self):
        return self.store.overview(as_of=self.clock.now())

    def refresh_history(self, symbol, start, end, *, adjusted=True, resume=True):
        start, end = session_date(start), session_date(end)
        if start > end:
            raise MarketDataError("Ingestion dates are reversed.")
        key = f"bars:{symbol}:{int(adjusted)}:{start}:{end}"
        previous = self.store.checkpoint(key) if resume else None
        cursor = session_date(previous["next_date"]) if previous else start
        count = 0
        while cursor <= end:
            finish = min(end, cursor + timedelta(days=98))
            rows = self.client.bars(symbol, cursor, finish, adjusted=adjusted)
            at = self.clock.now()
            count += self.store.import_bars(
                symbol,
                rows,
                adjusted=adjusted,
                source=self.client.source,
                available_at=at,
                recorded_at=at,
            )
            coverage = self.store.coverage(symbol, cursor, finish, adjusted=adjusted, as_of=at)
            if not coverage["complete"]:
                # Preserve successfully received bars, but do not advance across a data gap.
                self.store.checkpoint(
                    key,
                    {"next_date": cursor.isoformat(), "status": "incomplete", "coverage": coverage},
                )
                return {"inserted": count, "status": "incomplete", "coverage": coverage}
            cursor = finish + timedelta(days=1)
            self.store.checkpoint(key, {"next_date": cursor.isoformat(), "status": "running"})
        self.store.checkpoint(key, {"next_date": cursor.isoformat(), "status": "complete"})
        return {"inserted": count, "status": "complete"}


def parse_krx_master_csv(text: str, *, classifications=None):
    """Reviewed KRX export import. ETF geography must come from explicit metadata."""
    classifications = classifications or {}
    result = []
    for row in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))):
        symbol = row.get("ISU_SRT_CD") or row.get("단축코드") or row.get("symbol")
        market = row.get("MKT_NM") or row.get("시장구분") or row.get("market")
        kind = classifications.get(symbol) or row.get("kind")
        if market == "KOSDAQ" and kind not in {"etf_kr", "etf_us"}:
            continue
        if kind is None and market in {"KOSPI", "유가증권"}:
            kind = "kospi"
        if kind not in {"kospi", "etf_kr", "etf_us"}:
            raise MarketDataError("ETF classification requires reviewed product metadata.")
        item = {
            "symbol": symbol_code(symbol),
            "name": row.get("ISU_ABBRV") or row.get("한글종목약명") or row.get("name"),
            "kind": kind,
            "sector": row.get("sector", "미분류"),
            "market": "KOSPI" if kind == "kospi" else "ETF",
            "isin": row.get("ISU_CD"),
            "original": row,
        }
        listed = row.get("LIST_DD") or row.get("상장일") or row.get("listed_on")
        if listed:
            item["listed_on"] = session_date(listed.replace("/", "-")).isoformat()
        result.append(item)
    return result


def parse_kis_master(data: bytes, *, classifications=None, allow_unclassified=False):
    """Parse the official KOSPI fixed-width prefix; retain the full source record.

    The KIS suffix is 227 characters plus a newline in the published contract. UTF-8
    offsets must never be applied to the Korean CP949 name. ETF class/region requires
    a supplied classification, as a name substring does not establish exposure.
    """
    classifications = classifications or {}
    try:
        lines = data.decode("cp949").splitlines()
    except UnicodeError:
        raise MarketDataError("The KIS master must be valid CP949.") from None
    results = []
    for line in lines:
        if not line:
            continue
        if len(line) <= 227 + 21:
            raise MarketDataError("Invalid KIS master record width.")
        prefix, suffix = line[:-227], line[-227:]
        symbol, isin, name = prefix[:9].strip(), prefix[9:21].strip(), prefix[21:].strip()
        # First suffix field is the 2-character security group code (ST, EF, ...).
        group = suffix[:2]
        if group not in {"ST", "EF"}:
            continue
        kind = classifications.get(symbol) or ("kospi" if group == "ST" else None)
        if kind not in {"kospi", "etf_kr", "etf_us"} and not (
            allow_unclassified and group == "EF" and kind is None
        ):
            raise MarketDataError("ETF classification requires reviewed product metadata.")
        fields, offset = [], 0
        for width in KIS_MASTER_WIDTHS:
            fields.append(suffix[offset : offset + width].strip())
            offset += width
        metadata = {
            "halted": {"Y": True, "N": False}.get(fields[34]),
            "liquidation": {"Y": True, "N": False}.get(fields[35]),
            "under_management": {"Y": True, "N": False}.get(fields[36]),
            "spac": {"Y": True, "N": False}.get(fields[19]),
            "market_warning_code": fields[37],
            "preferred_share_code": fields[54],
            "industry_code": fields[2],
            "industry_medium_code": fields[3],
            "industry_small_code": fields[4],
            "share_class": "common" if fields[54] == "0" else "non_common",
        }
        metadata["tradable"] = (
            metadata["halted"] is False
            and metadata["liquidation"] is False
            and metadata["under_management"] is False
        )
        if fields[49] and fields[49] != "00000000":
            metadata["listed_on"] = session_date(fields[49]).isoformat()
        results.append(
            {
                **metadata,
                "symbol": symbol_code(symbol),
                "name": name,
                "isin": isin,
                "kind": kind,
                "sector": "미분류",
                "security_group": group,
                "market": "KOSPI" if kind == "kospi" else "ETF",
                "original": line,
            }
        )
    return results


class MarketMasterDownloader:
    """Download only the fixed official public archive, without brokerage credentials.

    The immutable source is retained even when ETF classification is incomplete.
    Unclassified ETFs are returned for review and never guessed from their names.
    """

    URL = "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip"
    MEMBER = "kospi_code.mst"
    MAX_ARCHIVE = 5 * 1024 * 1024
    MAX_EXPANDED = 20 * 1024 * 1024

    def __init__(self, store: MarketStore, *, directory=None, transport=None, now=None):
        self.store = store
        self.directory = Path(directory or store.path.parent / "market_sources")
        self.transport = transport
        self.now = now or (lambda: datetime.now(timezone.utc))

    def _download(self):
        try:
            with httpx.Client(
                transport=self.transport, timeout=30, trust_env=False, follow_redirects=False
            ) as client:
                with client.stream("GET", self.URL) as response:
                    if response.status_code != 200:
                        raise MarketDataError("The official master download failed.")
                    chunks, length = [], 0
                    for chunk in response.iter_bytes():
                        length += len(chunk)
                        if length > self.MAX_ARCHIVE:
                            raise MarketDataError("The official master archive exceeded its limit.")
                        chunks.append(chunk)
                    return b"".join(chunks)
        except httpx.HTTPError:
            raise MarketDataError("The official master download failed.") from None

    def _unpack(self, content):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                infos = archive.infolist()
                if len(infos) != 1 or infos[0].filename != self.MEMBER:
                    raise MarketDataError("Unexpected member in the official master archive.")
                info = infos[0]
                if (
                    info.is_dir()
                    or info.flag_bits & 1
                    or not 0 < info.file_size <= self.MAX_EXPANDED
                ):
                    raise MarketDataError("Invalid official master archive size or encryption.")
                if info.file_size > max(info.compress_size, 1) * 200:
                    raise MarketDataError("The master archive compression ratio is excessive.")
                # Read the exact member to memory; never extract paths supplied in a ZIP.
                with archive.open(info) as stream:
                    raw = stream.read(self.MAX_EXPANDED + 1)
                if len(raw) != info.file_size or len(raw) > self.MAX_EXPANDED:
                    raise MarketDataError("The master archive size is inconsistent.")
                return raw
        except (zipfile.BadZipFile, OSError, RuntimeError, ValueError) as error:
            if isinstance(error, MarketDataError):
                raise
            raise MarketDataError("The official master archive is invalid.") from None

    def download_kis_kospi(self, *, classifications=None):
        content = self._download()
        raw = self._unpack(content)
        retrieved = timestamp(self.now()).isoformat()
        checksum = hashlib.sha256(content).hexdigest()
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self.directory / (checksum + ".zip")
        temporary = self.directory / (checksum + "." + uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        metadata = {
            "url": self.URL,
            "checksum": checksum,
            "retrieved_at": retrieved,
            "compressed_size": len(content),
            "expanded_size": len(raw),
            "status": "received",
            "historical_universe_complete": False,
        }
        with self.store.connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO market_source_artifacts VALUES (?,?,?,?,?)",
                (checksum, "kis_kospi_master", retrieved, destination.name, _json(metadata)),
            )
        rows = parse_kis_master(raw, classifications=classifications, allow_unclassified=True)
        if not rows:
            raise MarketDataError("The official master contained no supported instruments.")
        included = [row for row in rows if row["kind"] is not None]
        unresolved = [
            {"symbol": row["symbol"], "name": row["name"], "isin": row["isin"]}
            for row in rows
            if row["kind"] is None
        ]
        imported = self.store.import_instruments(
            included, source="kis_kospi_master", as_of=retrieved
        )
        result = {
            **metadata,
            "artifact_id": checksum,
            "imported": imported,
            "supported_rows": len(included),
            "unclassified_etfs": unresolved,
            "status": "needs_classification" if unresolved else "complete",
        }
        with self.store.connection() as conn:
            conn.execute(
                "UPDATE market_source_artifacts SET payload=? WHERE id=?", (_json(result), checksum)
            )
        self.store.checkpoint("master:kis_kospi", result)
        return result
