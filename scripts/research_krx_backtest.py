"""Collect dated KRX market snapshots for a reproducible stock-rule backtest.

Only approved read-only KRX services are called. Raw responses and keys stay in
.local; this script never places the API key in a URL, log, or tracked artifact.
"""

import argparse
import gzip
import json
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path

from dotenv import dotenv_values
from invest_lab.adapters.market_data import MarketStore
from invest_lab.adapters.research_sources import KrxSource, ResearchSourceError
from invest_lab.domain.research import (
    content_hash,
    coverage,
    decision_time,
    finite,
    validate_dataset,
)
from invest_lab.domain.strategies import StrategyRules, replay

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / ".local" / "backtest-20260929" / "krx"
SERVICES = ("kospi_daily", "kospi_master", "kospi_index")


def save_gzip(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=6) as stream:
        json.dump(
            payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    temporary.replace(path)


def load_gzip(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def collect(args):
    index_path = ROOT / ".local" / "backtest-20260929" / "index.json"
    if not index_path.exists():
        raise ValueError("collect the KIS index calendar before KRX snapshots")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    sessions = [
        row["session"]
        for row in index["rows"]
        if args.start <= row["session"] <= args.end
    ]
    if not sessions:
        raise ValueError("no source sessions in requested period")
    directory = Path(args.directory)
    key = dotenv_values(ROOT / ".env").get("KRX_API_KEY")
    if not key:
        raise ValueError("KRX_API_KEY is not configured")
    source = KrxSource(key)
    try:
        for number, session in enumerate(sessions, start=1):
            counts = {}
            for service in SERVICES:
                path = directory / service / (session + ".json.gz")
                if path.exists():
                    saved = load_gzip(path)
                    if (
                        saved["session"] != session
                        or saved["service"] != service
                        or saved["hash"] != content_hash(saved["raw"])
                    ):
                        raise ValueError("cached KRX snapshot failed integrity check")
                    counts[service] = saved["rows"]
                    continue
                for attempt in range(3):
                    try:
                        result = source.daily(service, session)
                        break
                    except ResearchSourceError as error:
                        if "HTTP 401" in str(error) or attempt == 2:
                            raise
                        time.sleep(2**attempt)
                raw_rows = result["raw"].get("OutBlock_1")
                if not isinstance(raw_rows, list) or not raw_rows:
                    raise ValueError(
                        f"KRX {service} missing data for source session {session}"
                    )
                saved = {
                    "service": service,
                    "session": session,
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                    "raw": result["raw"],
                    "hash": result["hash"],
                    "rows": len(raw_rows),
                    "normalized_rows": len(result["records"]),
                    "omitted_rows": len(result["omitted"]),
                }
                save_gzip(path, saved)
                counts[service] = len(raw_rows)
                time.sleep(0.15)
            if number % 10 == 0 or number in (1, len(sessions)):
                print(
                    json.dumps(
                        {
                            "completed": number,
                            "total": len(sessions),
                            "last_session": session,
                            "last_counts": counts,
                        }
                    ),
                    flush=True,
                )
    finally:
        source.close()


def date_code(value):
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}"


def stock_bar(row, session, collected_at, source_hash):
    if (
        row.get("ACC_TRDVOL") == "0"
        and row.get("ACC_TRDVAL") == "0"
        and all(row.get(key) == "0" for key in ("TDD_OPNPRC", "TDD_HGPRC", "TDD_LWPRC"))
    ):
        try:
            finite(row["TDD_CLSPRC"], positive=True)
        except (ValueError, KeyError):
            return None
        return {
            "symbol": row["ISU_CD"],
            "session": session,
            "open": row["TDD_OPNPRC"],
            "high": row["TDD_HGPRC"],
            "low": row["TDD_LWPRC"],
            "close": row["TDD_CLSPRC"],
            "volume": 0,
            "turnover": 0,
            "price_basis": "raw",
            "venue": "KRX",
            "session_type": "regular",
            "final": True,
            "tradable": False,
            "valuation_only": True,
            "available_at": decision_time(session).isoformat(),
            "ingested_at": collected_at,
            "source": "krx:kospi_daily",
            "source_hash": source_hash,
            "availability_policy": "inferred_18_kst_not_historical_release_evidence",
        }
    try:
        prices = [
            finite(row[key], positive=True)
            for key in ("TDD_OPNPRC", "TDD_HGPRC", "TDD_LWPRC", "TDD_CLSPRC")
        ]
        volume = int(finite(row["ACC_TRDVOL"], integer=True))
        turnover = finite(row["ACC_TRDVAL"])
        if (
            turnover < 0
            or prices[2] > min(prices[0], prices[3])
            or prices[1] < max(prices[0], prices[3])
        ):
            return None
    except (ValueError, KeyError):
        return None
    result = {
        "symbol": row["ISU_CD"],
        "session": session,
        "open": row["TDD_OPNPRC"],
        "high": row["TDD_HGPRC"],
        "low": row["TDD_LWPRC"],
        "close": row["TDD_CLSPRC"],
        "volume": volume,
        "turnover": row["ACC_TRDVAL"],
        "price_basis": "raw",
        "venue": "KRX",
        "session_type": "regular",
        "final": True,
        "available_at": decision_time(session).isoformat(),
        "ingested_at": collected_at,
        "source": "krx:kospi_daily",
        "source_hash": source_hash,
        "availability_policy": "inferred_18_kst_not_historical_release_evidence",
    }
    if row.get("MKTCAP") not in (None, "", "-"):
        result["market_cap"] = row["MKTCAP"]
    return result


def benchmark_bar(row, session, collected_at, source_hash):
    prices = [
        finite(row[key], positive=True)
        for key in ("OPNPRC_IDX", "HGPRC_IDX", "LWPRC_IDX", "CLSPRC_IDX")
    ]
    if prices[2] > min(prices[0], prices[3]) or prices[1] < max(prices[0], prices[3]):
        raise ValueError(f"{session}: KOSPI index OHLC inconsistent")
    return {
        "symbol": "KOSPI",
        "session": session,
        "open": row["OPNPRC_IDX"],
        "high": row["HGPRC_IDX"],
        "low": row["LWPRC_IDX"],
        "close": row["CLSPRC_IDX"],
        "volume": int(finite(row["ACC_TRDVOL"], integer=True)),
        "turnover": row["ACC_TRDVAL"],
        "price_basis": "raw",
        "venue": "KRX",
        "session_type": "regular",
        "final": True,
        "available_at": decision_time(session).isoformat(),
        "ingested_at": collected_at,
        "source": "krx:kospi_index",
        "source_hash": source_hash,
        "availability_policy": "inferred_18_kst_not_historical_release_evidence",
    }


def common_share_pair(master_rows, kis_master):
    crosswalk = defaultdict(Counter)
    for row in master_rows:
        known = kis_master.get(row["ISU_SRT_CD"])
        if known:
            crosswalk[(row["SECUGRP_NM"], row["KIND_STKCERT_TP_NM"])][
                known["share_class"]
            ] += 1
    pair, counts = max(crosswalk.items(), key=lambda item: item[1]["common"])
    if counts["common"] < 100 or counts["non_common"]:
        raise ValueError("KRX/KIS common-share crosswalk is not unambiguous")
    return pair, {str(key): dict(value) for key, value in crosswalk.items()}


def build(args):
    directory = Path(args.directory)
    output_directory = Path(getattr(args, "output_directory", None) or directory)
    state = ROOT / ".local" / "backtest-20260929"
    source_index = json.loads((state / "index.json").read_text(encoding="utf-8"))
    kis_index = {
        row["session"]: row
        for row in source_index["rows"]
        if args.start <= row["session"] <= args.end
    }
    sessions = sorted(kis_index)
    if not sessions:
        raise ValueError("no dated source sessions")
    kis_master = {
        row["symbol"]: row for row in MarketStore(state / "market.db").instruments()
    }
    first_master = load_gzip(directory / "kospi_master" / (sessions[0] + ".json.gz"))[
        "raw"
    ]["OutBlock_1"]
    common_pair, crosswalk = common_share_pair(first_master, kis_master)
    instruments, bars, universe, benchmarks = {}, [], [], []
    source_manifest, counts, listed_by_session = {}, Counter(), {}
    isin_by_symbol = {}
    for number, session in enumerate(sessions, start=1):
        saved = {
            service: load_gzip(directory / service / (session + ".json.gz"))
            for service in SERVICES
        }
        for service, artifact in saved.items():
            if artifact["hash"] != content_hash(artifact["raw"]):
                raise ValueError(f"{service} {session}: source hash mismatch")
        source_manifest[session] = {
            service: artifact["hash"] for service, artifact in saved.items()
        }
        master_rows = saved["kospi_master"]["raw"]["OutBlock_1"]
        daily_rows = saved["kospi_daily"]["raw"]["OutBlock_1"]
        common = {}
        for row in master_rows:
            if (row["SECUGRP_NM"], row["KIND_STKCERT_TP_NM"]) != common_pair:
                continue
            symbol = row["ISU_SRT_CD"]
            isin = row["ISU_CD"]
            if symbol in isin_by_symbol and isin_by_symbol[symbol] != isin:
                raise ValueError(f"{symbol}: short code reused by another ISIN")
            isin_by_symbol[symbol] = isin
            common[symbol] = row
            if symbol not in instruments:
                kis = kis_master.get(symbol, {})
                instruments[symbol] = {
                    "symbol": symbol,
                    "isin": isin,
                    "name": row.get("ISU_ENG_NM") or symbol,
                    "kind": "kospi",
                    "share_class": "common",
                    "tradable": True,
                    "sector": "KIS-industry:"
                    + str(kis.get("industry_code") or "unknown"),
                    "listed_on": date_code(row["LIST_DD"]),
                    "source": "krx_dated_master_with_current_kis_sector_crosswalk",
                }
        listed_by_session[session] = set(common)
        valid = {}
        statuses = {symbol: "missing_source" for symbol in common}
        seen = set()
        for row in daily_rows:
            symbol = row["ISU_CD"]
            if symbol not in common:
                continue
            if symbol in seen:
                raise ValueError(f"{session}: duplicate daily symbol {symbol}")
            seen.add(symbol)
            normalized = stock_bar(
                row,
                session,
                saved["kospi_daily"]["collected_at"],
                saved["kospi_daily"]["hash"],
            )
            if normalized is None:
                counts["listed_common_without_valid_bar"] += 1
                statuses[symbol] = "invalid_ohlc"
                continue
            if symbol in valid:
                raise ValueError(f"{session}: duplicate daily symbol {symbol}")
            if normalized.get("valuation_only"):
                statuses[symbol] = "valuation_only"
                bars.append(normalized)
                counts["valuation_only_bars"] += 1
                continue
            valid[symbol] = normalized
            statuses[symbol] = "tradable"
        counts["listed_common_missing_source"] += sum(
            v == "missing_source" for v in statuses.values()
        )
        if not valid:
            raise ValueError(f"{session}: no valid common-share bars")
        bars.extend(valid.values())
        universe.append(
            {
                "session": session,
                "symbols": sorted(valid),
                "listed_symbols": sorted(common),
                "bar_status": statuses,
                "available_at": decision_time(session).isoformat(),
                "ingested_at": saved["kospi_master"]["collected_at"],
                "source": "krx_dated_master_intersect_valid_daily_bar",
                "source_hash": saved["kospi_master"]["hash"],
                "availability_policy": "inferred_18_kst_not_historical_release_evidence",
            }
        )
        index_rows = saved["kospi_index"]["raw"]["OutBlock_1"]
        matched = [
            row
            for row in index_rows
            if row.get("IDX_CLSS") == "KOSPI"
            and row.get("CLSPRC_IDX") not in (None, "", "-")
            and abs(float(row["CLSPRC_IDX"]) - float(kis_index[session]["close"]))
            < 0.005
        ]
        if len(matched) != 1:
            raise ValueError(
                f"{session}: KRX/KIS KOSPI close match count is {len(matched)}"
            )
        benchmarks.append(
            benchmark_bar(
                matched[0],
                session,
                saved["kospi_index"]["collected_at"],
                saved["kospi_index"]["hash"],
            )
        )
        counts["sessions"] += 1
        counts["bars"] += len(valid)
        counts["listed_common"] += len(common)
        if number % 50 == 0 or number == len(sessions):
            print(
                json.dumps(
                    {"stage": "normalize", "completed": number, "total": len(sessions)}
                ),
                flush=True,
            )
    source_manifest_path = output_directory / "source_manifest.json.gz"
    save_gzip(source_manifest_path, source_manifest)
    first_listed, last_listed = (
        listed_by_session[sessions[0]],
        listed_by_session[sessions[-1]],
    )
    bars_by_symbol = defaultdict(list)
    for bar in bars:
        if not bar.get("valuation_only"):
            bars_by_symbol[bar["symbol"]].append(bar)
    large_gaps = []
    for symbol, history in bars_by_symbol.items():
        for previous, current in pairwise(history):
            ratio = float(current["open"]) / float(previous["close"])
            if ratio < 0.6 or ratio > 1.4:
                large_gaps.append(
                    {"symbol": symbol, "session": current["session"], "ratio": ratio}
                )
    payload = {
        "name": "KRX dated KOSPI common-share history (PIT availability and actions unverified)",
        "source_kind": "provider",
        "sessions": sessions,
        "instruments": list(instruments.values())
        + [{"symbol": "KOSPI", "name": "KOSPI", "kind": "benchmark"}],
        "bars": bars,
        "universe": universe,
        "benchmarks": benchmarks,
        "actions": [],
        "financial_facts": [],
        "macro": [],
        "etf_metadata": [],
        "provenance": {
            "source": "KRX OPEN API dated KOSPI daily/master/index; KIS index calendar cross-check",
            "historical_universe_verified": True,
            "calendar_verified": False,
            "corporate_actions_verified": False,
            "verification_report": "data_quality.json; public dated snapshots, release clock and actions incomplete",
            "source_manifest_hash": content_hash(source_manifest),
            "common_share_token_crosswalk": crosswalk,
            "common_share_token_pair": list(common_pair),
            "pipeline_issues": [
                "historical_release_clock_inferred",
                "corporate_action_history_missing",
                "current_sector_mapping_not_pit",
            ],
        },
    }
    quality = {
        **dict(counts),
        "start": sessions[0],
        "end": sessions[-1],
        "instruments_ever": len(instruments),
        "historical_only_first_vs_last": len(first_listed - last_listed),
        "new_only_first_vs_last": len(last_listed - first_listed),
        "first_common": len(first_listed),
        "last_common": len(last_listed),
        "benchmark_krx_kis_close_matches": len(benchmarks),
        "large_overnight_gap_count": len(large_gaps),
        "large_overnight_gap_examples": large_gaps[:50],
        "raw_price_action_adjustment_unverified": True,
        "source_release_clock_unverified": True,
    }
    validated = validate_dataset(payload)
    quality["coverage"] = coverage(validated)
    save_gzip(output_directory / "dataset.json.gz", validated)
    save_gzip(output_directory / "data_quality.json.gz", quality)
    print(
        json.dumps(
            {
                "stage": "built",
                "quality": quality,
                "dataset_hash": content_hash(validated),
            }
        ),
        flush=True,
    )


def run(args):
    directory = Path(args.directory)
    data = load_gzip(directory / "dataset.json.gz")
    quality = load_gzip(directory / "data_quality.json.gz")
    if quality["end"] != args.end or quality["start"] != args.start:
        raise ValueError("built dataset period does not match requested replay period")
    if quality["coverage"]["missing_bar_count"]:
        raise ValueError("source data has missing expected bars")
    rules = StrategyRules(sell_tax_bps=20)
    output = {
        "dataset_hash": content_hash(data),
        "source_manifest_hash": data["provenance"]["source_manifest_hash"],
        "rules": asdict(rules),
        "cost_assumption": "20bps sell tax is hypothetical; broker tariff not verified",
        "quality": quality,
        "runs": {},
    }
    for name, start in (("full_2025_to_latest", "2025-01-02"),):
        print(json.dumps({"stage": "replay_start", "name": name}), flush=True)
        result = replay(
            data,
            rules,
            initial_cash=10_000_000,
            start_session=start,
            end_session=args.end,
        )
        output["runs"][name] = result
        save_gzip(directory / f"result_{name}.json.gz", result)
        print(
            json.dumps(
                {"stage": "replay_done", "name": name, "metrics": result["metrics"]}
            ),
            flush=True,
        )
    save_gzip(directory / "backtest_report_data.json.gz", output)


def audit(args):
    directory = Path(args.directory)
    data = load_gzip(directory / "dataset.json.gz")
    result = load_gzip(directory / "result_full_2025_to_latest.json.gz")
    run_record = load_gzip(directory / "backtest_report_data.json.gz")
    if run_record["dataset_hash"] != content_hash(data):
        raise ValueError("replay result does not match the current dataset")
    trades, curve = result["trades"], result["equity_curve"]
    histories = defaultdict(list)
    for bar in data["bars"]:
        if not bar.get("valuation_only"):
            histories[bar["symbol"]].append(bar)
    openings, intervals = {}, defaultdict(list)
    for trade in trades:
        symbol = trade["symbol"]
        if trade["side"] == "buy":
            openings[symbol] = trade["session"]
        elif symbol in openings:
            intervals[symbol].append((openings.pop(symbol), trade["session"]))
    for symbol, opened in openings.items():
        intervals[symbol].append((opened, curve[-1]["session"]))
    held_large_gaps = []
    for symbol, history in histories.items():
        for previous, current in pairwise(history):
            ratio = float(current["open"]) / float(previous["close"])
            if ratio < 0.6 or ratio > 1.4:
                for opened, closed in intervals[symbol]:
                    if opened < current["session"] <= closed:
                        held_large_gaps.append(
                            {
                                "symbol": symbol,
                                "session": current["session"],
                                "ratio": ratio,
                                "holding_since": opened,
                            }
                        )
    by_year = {}
    base_equity = float(result["initial_cash"])
    for year in sorted({row["session"][:4] for row in curve}):
        rows = [row for row in curve if row["session"].startswith(year)]
        by_year[year] = {
            "sessions": len(rows),
            "carried_portfolio_return": rows[-1]["equity"] / base_equity - 1,
            "buy_count": sum(
                trade["side"] == "buy" and trade["session"].startswith(year)
                for trade in trades
            ),
            "sell_count": sum(
                trade["side"] == "sell" and trade["session"].startswith(year)
                for trade in trades
            ),
        }
        base_equity = rows[-1]["equity"]
    exclusions = Counter(item["reason"] for item in result["unfilled_or_excluded"])
    report = {
        "dataset_hash": content_hash(data),
        "result_hash": content_hash(result),
        "metrics": result["metrics"],
        "by_year": by_year,
        "buy_count": sum(trade["side"] == "buy" for trade in trades),
        "sell_count": sum(trade["side"] == "sell" for trade in trades),
        "exclusion_reasons": dict(exclusions),
        "held_large_gap_count": len(held_large_gaps),
        "held_large_gaps": held_large_gaps,
        "final_open_positions": result["open_positions"],
    }
    save_gzip(directory / "backtest_audit.json.gz", report)
    print(json.dumps(report, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("collect", "build", "run", "audit"))
    parser.add_argument("--directory", default=str(DEFAULT_DIR))
    parser.add_argument(
        "--output-directory", help="Separate build outputs from preserved source files"
    )
    parser.add_argument("--start", default="2024-01-02")
    parser.add_argument("--end", default="2026-09-23")
    args = parser.parse_args()
    if args.command == "collect":
        collect(args)
    elif args.command == "build":
        build(args)
    elif args.command == "run":
        run(args)
    else:
        audit(args)


if __name__ == "__main__":
    main()
