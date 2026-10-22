"""Collect a limited KIS history pilot and replay the existing stock-buy rule.

This is a research diagnostic, not a PIT-certified backtest. The current KIS
master is survivor-biased and KIS history does not provide historical release
timestamps or a complete corporate-action ledger. Only read-only quote/index
requests are made; no order or account endpoint is called.
"""

import argparse
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path

from invest_lab.adapters.kis import KisError, KisPaperClient
from invest_lab.adapters.market_data import MarketStore
from invest_lab.adapters.research import build_research_service
from invest_lab.adapters.settings import KisSettings
from invest_lab.domain.research import content_hash, decision_time, validate_dataset
from invest_lab.domain.strategies import StrategyRules, replay

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / ".local" / "backtest-20260929"
INDEX_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
INDEX_TR = "FHKUP03500100"


def save_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def selected_instruments(store: MarketStore, count: int, start: str):
    all_rows = store.instruments()
    eligible = [
        row
        for row in all_rows
        if row["kind"] == "kospi"
        and row.get("share_class") == "common"
        and row.get("tradable") is True
        and row.get("spac") is not True
        and row.get("listed_on")
        and row["listed_on"] <= start
    ]
    # Fixed hash sample, chosen without looking at historical returns.
    eligible.sort(key=lambda row: hashlib.sha256(row["symbol"].encode()).hexdigest())
    if count < 1 or count > len(eligible):
        raise ValueError(f"sample count must be 1..{len(eligible)}")
    return eligible[:count], {
        "master_rows": len(all_rows),
        "eligible_rows": len(eligible),
    }


def collect_index(client: KisPaperClient, start: str, end: str):
    cursor, last = date.fromisoformat(start), date.fromisoformat(end)
    rows = {}
    while cursor <= last:
        # The endpoint returns at most 50 rows; a 40-calendar-day window is smaller.
        finish = min(cursor + timedelta(days=39), last)
        body, _ = client._request(
            INDEX_PATH,
            INDEX_TR,
            {
                "FID_COND_MRKT_DIV_CODE": "U",
                "FID_INPUT_ISCD": "0001",
                "FID_INPUT_DATE_1": cursor.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": finish.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D",
            },
        )
        page = body.get("output2")
        if not isinstance(page, list) or len(page) >= 50:
            raise ValueError("KOSPI index page is missing or may be truncated")
        for row in page:
            raw_date = row["stck_bsop_date"]
            session = date.fromisoformat(
                f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
            ).isoformat()
            if not cursor.isoformat() <= session <= finish.isoformat():
                raise ValueError("KOSPI index returned an out-of-window session")
            item = {
                "session": session,
                "open": row["bstp_nmix_oprc"],
                "high": row["bstp_nmix_hgpr"],
                "low": row["bstp_nmix_lwpr"],
                "close": row["bstp_nmix_prpr"],
                "volume": int(row["acml_vol"]),
                "turnover": row["acml_tr_pbmn"],
            }
            if session in rows and rows[session] != item:
                raise ValueError("conflicting KOSPI index session")
            rows[session] = item
        cursor = finish + timedelta(days=1)
    if not rows:
        raise ValueError("no KOSPI index rows")
    return [rows[session] for session in sorted(rows)]


def collect_calendar(args):
    directory = Path(args.directory)
    index_path = directory / "index.json"
    if index_path.exists():
        saved = read_json(index_path)
        sessions = [row["session"] for row in saved["rows"]]
        if not sessions or sessions[0] > args.start or sessions[-1] < args.end:
            raise ValueError("saved index calendar does not cover the requested range")
        return
    settings = KisSettings.from_env_file(ROOT / ".env")
    with KisPaperClient(settings) as client:
        rows = collect_index(client, args.start, args.end)
    save_json(
        index_path,
        {"source": "KIS FHKUP03500100", "collected_at": now(), "rows": rows},
    )
    print(
        json.dumps(
            {
                "sessions": len(rows),
                "start": rows[0]["session"],
                "end": rows[-1]["session"],
            }
        )
    )


def collect_stock_bars(client: KisPaperClient, symbol: str, start: str, end: str):
    """Bisect an EGW00316 500 window without discarding successful date windows."""
    recovered_windows = []

    def one_window(first: date, last: date):
        try:
            return client.bars(symbol, first, last, adjusted=False)
        except KisError as error:
            if error.code != "EGW00316" or first >= last:
                raise
            middle = first + timedelta(days=(last - first).days // 2)
            recovered_windows.append(
                {"start": str(first), "end": str(last), "error": error.code}
            )
            return one_window(first, middle) + one_window(
                middle + timedelta(days=1), last
            )

    cursor, final = date.fromisoformat(start), date.fromisoformat(end)
    by_session = {}
    while cursor <= final:
        finish = min(cursor + timedelta(days=98), final)
        for bar in one_window(cursor, finish):
            session = bar["session"]
            if session in by_session and by_session[session] != bar:
                raise ValueError("conflicting overlapping KIS stock bars")
            by_session[session] = bar
        cursor = finish + timedelta(days=1)
    return [by_session[key] for key in sorted(by_session)], recovered_windows


def collect(args):
    directory = Path(args.directory)
    store = MarketStore(directory / "market.db")
    selected, master_counts = selected_instruments(store, args.count, args.start)
    plan_path = directory / "selection.json"
    selection = {
        "start": args.start,
        "end": args.end,
        "symbols": [row["symbol"] for row in selected],
        "selection_rule": "sha256(symbol), first N of current common/tradable/non-SPAC listed by start",
        "master_counts": master_counts,
        "master_source": "current KIS KOSPI master, not historical membership",
    }
    if plan_path.exists() and read_json(plan_path) != selection:
        raise ValueError("existing selection differs; use another output directory")
    save_json(plan_path, selection)
    settings = KisSettings.from_env_file(ROOT / ".env")
    with KisPaperClient(settings) as client:
        index_path = directory / "index.json"
        if not index_path.exists():
            index = collect_index(client, args.start, args.end)
            save_json(
                index_path,
                {"source": "KIS FHKUP03500100", "collected_at": now(), "rows": index},
            )
            print(
                json.dumps(
                    {"kind": "index", "sessions": len(index)}, ensure_ascii=False
                ),
                flush=True,
            )
        for number, row in enumerate(selected, start=1):
            path = directory / "bars" / (row["symbol"] + ".json")
            if path.exists():
                continue
            bars, recovered_windows = collect_stock_bars(
                client, row["symbol"], args.start, args.end
            )
            save_json(
                path,
                {
                    "source": "KIS FHKST03010100 raw FID_ORG_ADJ_PRC=1",
                    "collected_at": now(),
                    "symbol": row["symbol"],
                    "rows": bars,
                    "recovered_windows": recovered_windows,
                },
            )
            print(
                json.dumps(
                    {
                        "kind": "symbol",
                        "completed": number,
                        "total": len(selected),
                        "symbol": row["symbol"],
                        "bars": len(bars),
                        "recovered_windows": len(recovered_windows),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )


def now():
    return datetime.now(timezone.utc).isoformat()


def research_row(row, *, symbol, source, ingested_at):
    session = row["session"]
    return {
        "symbol": symbol,
        "session": session,
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "volume": row["volume"],
        "turnover": row["turnover"],
        "venue": "KRX",
        "session_type": "regular",
        "final": True,
        "price_basis": "raw",
        "available_at": decision_time(session).isoformat(),
        "ingested_at": ingested_at,
        "source": source,
        "availability_policy": "inferred_18_kst_not_historical_source_evidence",
    }


def build_dataset(directory: Path):
    selection = read_json(directory / "selection.json")
    index = read_json(directory / "index.json")
    sessions = [row["session"] for row in index["rows"]]
    store = MarketStore(directory / "market.db")
    by_symbol = {row["symbol"]: row for row in store.instruments()}
    instruments, bars, missing_files, source_hashes = [], [], [], {}
    for symbol in selection["symbols"]:
        path = directory / "bars" / (symbol + ".json")
        if not path.exists():
            missing_files.append(symbol)
            continue
        saved = read_json(path)
        if saved["symbol"] != symbol:
            raise ValueError("cached symbol mismatch")
        source_hashes[symbol] = content_hash(saved)
        master = by_symbol[symbol]
        instruments.append(
            {
                "symbol": symbol,
                "name": master["name"],
                "kind": "kospi",
                "share_class": "common",
                "tradable": True,
                "sector": "KIS-industry:"
                + str(master.get("industry_code") or "unknown"),
                "listed_on": master["listed_on"],
                "source": "current_kis_master_survivorship_limited",
            }
        )
        bars.extend(
            research_row(
                row,
                symbol=symbol,
                source=saved["source"],
                ingested_at=saved["collected_at"],
            )
            for row in saved["rows"]
            if row["session"] in sessions
        )
    if missing_files:
        raise ValueError(f"missing cached files: {len(missing_files)}")
    bar_sessions = {symbol: set() for symbol in selection["symbols"]}
    for row in bars:
        bar_sessions[row["symbol"]].add(row["session"])
    continuous_symbols = [
        symbol
        for symbol in selection["symbols"]
        if bar_sessions[symbol] == set(sessions)
    ]
    if not continuous_symbols:
        raise ValueError("no continuously observed stocks in the fixed pilot sample")
    payload = {
        "name": "KIS current-master historical-price pilot (survivorship limited)",
        "source_kind": "provider",
        "sessions": sessions,
        "instruments": instruments
        + [{"symbol": "KOSPI", "name": "KOSPI", "kind": "benchmark"}],
        "bars": bars,
        "universe": [
            {
                "session": session,
                # A halted/missing stock would make portfolio marking impossible.
                # This retrospective completeness filter is disclosed as bias.
                "symbols": continuous_symbols,
                "available_at": decision_time(session).isoformat(),
                "ingested_at": index["collected_at"],
                "source": "current_kis_master_backfilled_for_pilot_only",
            }
            for session in sessions
        ],
        "benchmarks": [
            research_row(
                row,
                symbol="KOSPI",
                source=index["source"],
                ingested_at=index["collected_at"],
            )
            for row in index["rows"]
        ],
        "actions": [],
        "financial_facts": [],
        "macro": [],
        "etf_metadata": [],
        "provenance": {
            "source": "KIS paper read-only raw stock bars and KOSPI index; current KIS master",
            "historical_universe_verified": False,
            "calendar_verified": False,
            "corporate_actions_verified": False,
            "survivorship_limited": True,
            "verification_report": "pilot_data_quality.json; not a historical PIT attestation",
            "selection_hash": content_hash(selection),
            "index_hash": content_hash(index),
            "bar_file_hashes": source_hashes,
            "continuous_history_symbols": continuous_symbols,
            "excluded_for_missing_history": sorted(
                set(selection["symbols"]) - set(continuous_symbols)
            ),
            "pipeline_issues": [
                "historical_release_clock_inferred",
                "current_master_backfilled",
                "corporate_action_history_missing",
                "retrospective_continuous_history_filter",
            ],
        },
    }
    return validate_dataset(payload)


def quality_report(data):
    sessions = data["sessions"]
    by_symbol = {
        row["symbol"]: set() for row in data["instruments"] if row["kind"] == "kospi"
    }
    for row in data["bars"]:
        by_symbol[row["symbol"]].add(row["session"])
    missing = {
        symbol: sorted(set(sessions) - present) for symbol, present in by_symbol.items()
    }
    gaps = {symbol: dates[:20] for symbol, dates in missing.items() if dates}
    suspicious_gaps = []
    by_symbol_bars = {}
    for row in data["bars"]:
        by_symbol_bars.setdefault(row["symbol"], []).append(row)
    for symbol, rows in by_symbol_bars.items():
        rows.sort(key=lambda row: row["session"])
        for previous, current in pairwise(rows):
            ratio = float(current["open"]) / float(previous["close"])
            if not 0.6 <= ratio <= 1.4:
                suspicious_gaps.append(
                    {
                        "symbol": symbol,
                        "session": current["session"],
                        "open_to_previous_close": ratio,
                    }
                )
    return {
        "sessions": len(sessions),
        "start": sessions[0],
        "end": sessions[-1],
        "symbols": len(by_symbol),
        "bars": len(data["bars"]),
        "expected_bars": len(sessions) * len(by_symbol),
        "missing_bars": sum(map(len, missing.values())),
        "symbols_with_gaps": len(gaps),
        "gap_examples": dict(list(gaps.items())[:20]),
        "continuous_history_symbols": len(data["universe"][0]["symbols"]),
        "suspicious_large_overnight_gap_count": len(suspicious_gaps),
        "suspicious_large_overnight_gap_examples": suspicious_gaps[:20],
        "current_master_survivorship_bias": True,
        "historical_availability_unverified": True,
        "corporate_actions_unverified": True,
    }


def run(args):
    directory = Path(args.directory)
    data = build_dataset(directory)
    quality = quality_report(data)
    save_json(directory / "pilot_data_quality.json", quality)
    service = build_research_service(
        directory / "research.db", directory / "research_artifacts"
    )
    stored = service.import_dataset(data)
    print(
        json.dumps(
            {
                "kind": "dataset",
                "id": stored["id"],
                "coverage": stored["coverage"],
                "quality": quality,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    # Full-window rule is frozen before inspecting results; costs are research assumptions.
    rules = StrategyRules(sell_tax_bps=20)
    output = {
        "dataset_id": stored["id"],
        "dataset_hash": stored["hash"],
        "rules": rules.__dict__,
        "runs": {},
    }
    for name, start, end in (
        ("full", args.test_start, args.end),
        ("final_2026", max(args.test_start, "2026-01-02"), args.end),
    ):
        if start > end:
            continue
        result = replay(
            data, rules, initial_cash=10_000_000, start_session=start, end_session=end
        )
        output["runs"][name] = {
            "start": start,
            "end": end,
            "metrics": result["metrics"],
            "trades": result["trades"],
            "exclusions": result["unfilled_or_excluded"],
            "equity_curve": result["equity_curve"],
            "open_positions": result["open_positions"],
        }
        print(
            json.dumps(
                {"kind": "replay", "name": name, "metrics": result["metrics"]},
                ensure_ascii=False,
            ),
            flush=True,
        )
    save_json(directory / "pilot_result.json", output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("calendar", "collect", "run"))
    parser.add_argument("--directory", default=str(DEFAULT_DIR))
    parser.add_argument("--start", default="2024-01-02")
    parser.add_argument("--end", default="2026-09-25")
    parser.add_argument("--test-start", default="2025-01-02")
    parser.add_argument("--count", type=int, default=100)
    args = parser.parse_args()
    if args.command == "calendar":
        collect_calendar(args)
    elif args.command == "collect":
        collect(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
