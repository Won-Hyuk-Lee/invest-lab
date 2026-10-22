"""Point-in-time research contracts and calculations; no IO or ML frameworks."""

import hashlib
import json
import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from statistics import mean, pstdev

DATASET_TABLES = (
    "instruments",
    "bars",
    "universe",
    "actions",
    "financial_facts",
    "macro",
    "benchmarks",
    "etf_metadata",
)


def timestamp(value: str | datetime) -> datetime:
    result = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )
    if result.tzinfo is None:
        raise ValueError("시각에는 timezone이 필요합니다.")
    return result.astimezone(timezone.utc)


def finite(value, *, positive=False, integer=False) -> float:
    if isinstance(value, bool):
        raise ValueError("bool은 숫자 입력이 아닙니다.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("유효한 숫자가 필요합니다.") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError("유한한 숫자와 올바른 부호가 필요합니다.")
    if integer and (not number.is_integer() or number < 0):
        raise ValueError("0 이상의 정수가 필요합니다.")
    return number


def canonical(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def content_hash(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def decision_time(session: str) -> datetime:
    # 18:00 KST. Dataset calendar/session metadata remains the authority for real jobs.
    return datetime.combine(date.fromisoformat(session), time(9), timezone.utc)


def visible(row: dict, at: datetime, *, observed_by: datetime | None = None) -> bool:
    if timestamp(row["available_at"]) > at:
        return False
    return observed_by is None or timestamp(row["ingested_at"]) <= observed_by


def latest_visible(rows: list[dict], at: datetime, keys: tuple[str, ...], *, observed_by=None):
    selected = {}
    for row in rows:
        if not visible(row, at, observed_by=observed_by):
            continue
        key = tuple(row.get(k) for k in keys)
        previous = selected.get(key)
        if previous is None or (timestamp(row["available_at"]), timestamp(row["ingested_at"])) > (
            timestamp(previous["available_at"]),
            timestamp(previous["ingested_at"]),
        ):
            selected[key] = row
    return list(selected.values())


def validate_dataset(payload: dict) -> dict:
    """Reject malformed data; incomplete coverage is reported, never fabricated."""
    data = json.loads(canonical(payload))
    if data.get("source_kind") not in {"synthetic", "imported", "provider"}:
        raise ValueError("source_kind는 synthetic/imported/provider여야 합니다.")
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        raise ValueError("데이터셋 이름이 필요합니다.")
    if not isinstance(data.get("provenance"), dict) or not data["provenance"].get("source"):
        raise ValueError("데이터 출처 provenance.source가 필요합니다.")
    for table in DATASET_TABLES:
        data.setdefault(table, [])
        if not isinstance(data[table], list):
            raise ValueError(f"{table}은 배열이어야 합니다.")
    sessions = data.get("sessions", [])
    if not sessions or sessions != sorted(set(sessions)):
        raise ValueError("거래 세션은 중복 없는 오름차순 목록이어야 합니다.")
    for session in sessions:
        date.fromisoformat(session)
    instruments = {}
    for item in data["instruments"]:
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or not symbol or len(symbol) > 32:
            raise ValueError("종목 식별자는 선행 0을 보존한 문자열이어야 합니다.")
        if symbol in instruments:
            raise ValueError("중복 종목 식별자입니다.")
        if item.get("kind") not in {"kospi", "etf_kr", "etf_us", "etf_other", "benchmark"}:
            raise ValueError("지원하지 않는 상품 유형입니다.")
        instruments[symbol] = item
    if not instruments:
        raise ValueError("종목 목록이 필요합니다.")
    for table in DATASET_TABLES[1:]:
        seen = set()
        for row in data[table]:
            if not isinstance(row, dict):
                raise ValueError(f"{table} 행은 객체여야 합니다.")
            for key in ("available_at", "ingested_at"):
                timestamp(row[key])
            if "symbol" in row and row["symbol"] not in instruments:
                raise ValueError(f"마스터에 없는 종목: {row['symbol']}")
            if "session" in row and row["session"] not in sessions:
                raise ValueError("데이터 날짜가 거래 세션 목록에 없습니다.")
            # Exact duplicate revisions usually mean overlapping page ingestion.
            fingerprint = content_hash(row)
            if fingerprint in seen:
                raise ValueError(f"{table}에 동일한 중복 행이 있습니다.")
            seen.add(fingerprint)
    benchmark_row_ids = {id(row) for row in data["benchmarks"]}
    for row in data["bars"] + data["benchmarks"]:
        if row.get("valuation_only") is True:
            if (
                id(row) in benchmark_row_ids
                or row.get("tradable") is not False
                or finite(row["volume"], integer=True) != 0
                or finite(row.get("turnover", 0)) != 0
                or any(finite(row[k]) != 0 for k in ("open", "high", "low"))
            ):
                raise ValueError("평가 전용 봉은 거래 없는 종가 원문이어야 합니다.")
            finite(row["close"], positive=True)
        else:
            values = [finite(row[k], positive=True) for k in ("open", "high", "low", "close")]
            if values[2] > min(values[0], values[3]) or values[1] < max(values[0], values[3]):
                raise ValueError("OHLC 범위가 맞지 않습니다.")
        finite(row["volume"], integer=True)
        if finite(row.get("turnover", 0)) < 0:
            raise ValueError("거래대금은 음수가 될 수 없습니다.")
        if row.get("price_basis") not in {"raw", "adjusted", "total_return"}:
            raise ValueError("price_basis를 명시해야 합니다.")
        if row.get("final") is not True:
            raise ValueError("확정되지 않은 일봉은 연구 원장에 넣을 수 없습니다.")
        if row.get("venue") != "KRX" or row.get("session_type") != "regular":
            raise ValueError("현재 연구는 KRX 정규장 데이터만 허용합니다.")
    for row in data["universe"]:
        if not isinstance(row.get("symbols"), list) or len(row["symbols"]) != len(
            set(row["symbols"])
        ):
            raise ValueError("유니버스 종목 목록이 올바르지 않습니다.")
        if set(row["symbols"]) - set(instruments):
            raise ValueError("유니버스에 마스터 미등록 종목이 있습니다.")
        if "listed_symbols" in row:
            listed = row["listed_symbols"]
            if (
                not isinstance(listed, list)
                or len(listed) != len(set(listed))
                or set(listed) - set(instruments)
                or set(row["symbols"]) - set(listed)
            ):
                raise ValueError("독립 상장 명단과 매매 유니버스가 맞지 않습니다.")
    for action in data["actions"]:
        if not isinstance(action.get("event_id"), str) or not action["event_id"]:
            raise ValueError("기업행동 event_id가 필요합니다.")
        if action.get("type") not in {"split", "dividend", "distribution", "delisting"}:
            raise ValueError("지원하지 않는 기업행동입니다.")
        date.fromisoformat(action["effective_date"])
        if action["type"] == "split":
            finite(action["ratio"], positive=True)
            basis = action.get("price_basis_date", action["effective_date"])
            date.fromisoformat(basis)
            if basis < action["effective_date"]:
                raise ValueError("가격 기준일이 주식 권리 효력일보다 빠릅니다.")
            if action.get("fractional_policy") is not None:
                if action["fractional_policy"] not in {
                    "cash_at_close",
                    "sale_proceeds_unconfirmed",
                    "cancel_without_compensation",
                }:
                    raise ValueError("지원하지 않는 단주 처리 정책입니다.")
                if action["fractional_policy"] == "cash_at_close":
                    date.fromisoformat(action["fractional_price_date"])
                    if action["fractional_price_date"] < basis:
                        raise ValueError("단주 가격 결정일이 가격 기준일보다 빠릅니다.")
                if action.get("payment_date"):
                    date.fromisoformat(action["payment_date"])
                    if (
                        not action.get("fractional_price_date")
                        or action["payment_date"] < action["fractional_price_date"]
                    ):
                        raise ValueError("단주 지급일이 가격 결정일보다 빠릅니다.")
        elif action["type"] in {"dividend", "distribution"}:
            if finite(action["amount"]) < 0:
                raise ValueError("배당금은 음수가 될 수 없습니다.")
            date.fromisoformat(action["payment_date"])
            if action["payment_date"] < action["effective_date"]:
                raise ValueError("지급일이 권리락일보다 빠릅니다.")
        elif "settlement_price" in action and finite(action["settlement_price"]) < 0:
            raise ValueError("상폐 정산가는 0 이상이어야 합니다.")
    for row in data["financial_facts"]:
        finite(row["value"])
        date.fromisoformat(row["period_end"])
        if row.get("scope") not in {"CFS", "OFS"}:
            raise ValueError("재무 연결/별도 범위가 필요합니다.")
        if row.get("period_type") not in {"quarter", "ytd", "instant"}:
            raise ValueError("재무 기간 유형이 필요합니다.")
        if row.get("period_type") != "instant":
            date.fromisoformat(row["period_start"])
    for row in data["macro"]:
        if row.get("value") is not None:
            finite(row["value"])
        date.fromisoformat(row["observation_date"])
        date.fromisoformat(row["vintage_date"])
    data["schema_version"] = 1
    return data


def coverage(data: dict) -> dict:
    sessions = data["sessions"]
    issues = []
    universe = {row["session"]: row for row in data["universe"]}
    raw = {(row["symbol"], row["session"]) for row in data["bars"] if row["price_basis"] == "raw"}
    expected = set()
    missing_universe = []
    for session in sessions:
        item = universe.get(session)
        if item is None:
            missing_universe.append(session)
        else:
            # The denominator must survive missing/invalid OHLC normalization.
            expected.update(
                (symbol, session) for symbol in item.get("listed_symbols", item["symbols"])
            )
    missing_bars = sorted(expected - raw)
    if missing_universe:
        issues.append("missing_historical_universe")
    if missing_bars:
        issues.append("missing_raw_bars")
    provenance = data["provenance"]
    issues.extend(provenance.get("pipeline_issues", []))
    for key in ("historical_universe_verified", "corporate_actions_verified", "calendar_verified"):
        if provenance.get(key) is not True:
            issues.append(key + "_not_verified")
    if provenance.get("revised_only"):
        issues.append("revised_only")
    if provenance.get("survivorship_limited"):
        issues.append("survivorship_limited")
    if data["source_kind"] == "synthetic":
        issues.append("synthetic_data")
    if not data["benchmarks"]:
        issues.append("missing_benchmark")
    # Submitted attestations are metadata, not independent verification evidence.
    if not provenance.get("verification_report"):
        issues.append("missing_verification_report")
    visible_universe_sessions = {
        r["session"]
        for r in data["universe"]
        if r["symbols"] and timestamp(r["available_at"]) <= decision_time(r["session"])
    }
    unavailable_universe = sorted(set(sessions) - visible_universe_sessions)
    if unavailable_universe:
        issues.append("universe_not_available_at_decision")
    # Later corrections are allowed only if an original was available for that session.
    available_bars = {
        (r["symbol"], r["session"])
        for r in data["bars"]
        if r["price_basis"] == "raw" and timestamp(r["available_at"]) <= decision_time(r["session"])
    }
    unavailable_bars = sorted(expected - available_bars)
    if unavailable_bars:
        issues.append("bars_not_available_at_decision")
    if any(a["type"] == "delisting" and "settlement_price" not in a for a in data["actions"]):
        issues.append("missing_delisting_settlement")
    pit_ready = not issues
    return {
        "sessions": len(sessions),
        "instruments": len(data["instruments"]),
        "expected_bars": len(expected),
        "denominator": "independent_listed_symbols"
        if all("listed_symbols" in row for row in universe.values())
        else "declared_universe_only",
        "raw_bars": len(raw),
        "missing_bar_count": len(missing_bars),
        "missing_bars": missing_bars[:100],
        "missing_universe_sessions": missing_universe[:100],
        "issues": issues,
        "pit_ready": pit_ready,
        "pit_evidence": "submitted_provenance_not_independent_audit",
        "research_validation_ready": pit_ready,
        "real_validation_complete": pit_ready,
        "broker_validation_complete": False,
        "unavailable_universe_sessions": unavailable_universe[:100],
        "unavailable_bar_count": len(unavailable_bars),
        "financial_facts": len(data["financial_facts"]),
        "macro_observations": len(data["macro"]),
        "corporate_actions": len(data["actions"]),
        "etf_metadata": len(data["etf_metadata"]),
    }


def universe_at(data: dict, session: str, at: datetime, *, observed_by=None) -> list[str]:
    rows = [r for r in data["universe"] if r["session"] == session]
    rows = latest_visible(rows, at, ("session",), observed_by=observed_by)
    return sorted(rows[0]["symbols"]) if rows else []


def bars_at(data: dict, symbol: str, session: str, at: datetime, *, observed_by=None) -> list[dict]:
    rows = [
        r
        for r in data["bars"]
        if r["symbol"] == symbol and r["session"] <= session and r["price_basis"] == "raw"
    ]
    rows = latest_visible(rows, at, ("symbol", "session", "price_basis"), observed_by=observed_by)
    rows.sort(key=lambda r: r["session"])
    # Split adjustment is derived only from events effective and known at this decision.
    actions = latest_visible(data["actions"], at, ("event_id",), observed_by=observed_by)
    adjusted = []
    for bar in rows:
        factor = math.prod(
            float(a["ratio"])
            for a in actions
            if a["symbol"] == symbol
            and a["type"] == "split"
            and bar["session"] < a.get("price_basis_date", a["effective_date"]) <= session
        )
        adjusted.append(
            {
                **bar,
                **{k: float(bar[k]) / factor for k in ("open", "high", "low", "close")},
                "volume": float(bar["volume"]) * factor,
            }
        )
    return adjusted


def financial_features(
    facts: list[dict], symbol: str, at: datetime, market_cap=None, *, scope="CFS", observed_by=None
) -> dict:
    rows = [
        r
        for r in facts
        if r["symbol"] == symbol
        and r["scope"] == scope
        and date.fromisoformat(r["period_end"]) <= at.date()
    ]
    rows = latest_visible(
        rows,
        at,
        (
            "symbol",
            "account",
            "period_start",
            "period_end",
            "period_type",
            "scope",
            "currency",
            "attribution",
        ),
        observed_by=observed_by,
    )
    groups = defaultdict(list)
    for row in rows:
        groups[
            (row["account"], row.get("currency", "KRW"), row.get("attribution", "total"))
        ].append(row)
    result = {"scope": scope, "values": {}, "missing_reasons": {}, "available_at": None}
    used = []
    for (account, currency, attribution), entries in groups.items():
        if currency != "KRW" or attribution != "total":
            continue
        entries.sort(key=lambda r: r["period_end"])
        instants = [r for r in entries if r["period_type"] == "instant"]
        if instants:
            row = instants[-1]
            result["values"][account] = float(row["value"])
            if account == "equity" and len(instants) >= 2:
                # ROE denominator must use opening/closing balances approximately one year apart.
                end = date.fromisoformat(row["period_end"])
                start_rows = [
                    r
                    for r in instants[:-1]
                    if 330 <= (end - date.fromisoformat(r["period_end"])).days <= 400
                ]
                if start_rows:
                    result["values"]["average_equity"] = (
                        float(start_rows[-1]["value"]) + float(row["value"])
                    ) / 2
            used.append(row)
            continue
        quarters = {}
        for row in entries:
            start, end = (
                date.fromisoformat(row["period_start"]),
                date.fromisoformat(row["period_end"]),
            )
            if row["period_type"] == "quarter" or 70 <= (end - start).days <= 100:
                quarters[row["period_end"]] = (start, float(row["value"]), row)
            elif row["period_type"] == "ytd":
                previous = [
                    r
                    for r in entries
                    if r["period_type"] == "ytd"
                    and r["period_start"] == row["period_start"]
                    and r["period_end"] < row["period_end"]
                ]
                if previous:
                    prev = previous[-1]
                    quarter_start = date.fromisoformat(prev["period_end"]) + timedelta(days=1)
                    if 70 <= (end - quarter_start).days <= 100:
                        quarters[row["period_end"]] = (
                            quarter_start,
                            float(row["value"]) - float(prev["value"]),
                            row,
                        )
        periods = sorted(quarters)[-4:]
        if len(periods) != 4 or any(
            quarters[b][0] != date.fromisoformat(a) + timedelta(days=1)
            for a, b in zip(periods, periods[1:])
        ):
            result["missing_reasons"][account + "_ttm"] = "four_compatible_quarters_required"
        else:
            result["values"][account + "_ttm"] = sum(quarters[p][1] for p in periods)
            used.extend(quarters[p][2] for p in periods)
    values = result["values"]

    def ratio(name, numerator, denominator, scale=1):
        if numerator in values and denominator in values and values[denominator] > 0:
            values[name] = scale * values[numerator] / values[denominator]
        else:
            result["missing_reasons"][name] = "missing_or_nonpositive_denominator"

    ratio("operating_margin", "operating_income_ttm", "revenue_ttm")
    ratio("roe", "net_income_ttm", "average_equity")
    ratio("debt_to_equity", "liabilities", "equity")
    ratio("cash_conversion", "operating_cash_flow_ttm", "net_income_ttm")
    if market_cap is not None and finite(market_cap, positive=True):
        for name, key in (
            ("earnings_yield", "net_income_ttm"),
            ("book_yield", "equity"),
            ("cashflow_yield", "operating_cash_flow_ttm"),
        ):
            if key in values:
                values[name] = values[key] / float(market_cap)
    if used:
        result["available_at"] = max((r["available_at"] for r in used), key=timestamp)
    return result


def macro_features(rows: list[dict], at: datetime, *, max_age_days=10, observed_by=None) -> dict:
    selected = latest_visible(rows, at, ("series", "observation_date"), observed_by=observed_by)
    grouped = defaultdict(list)
    for row in selected:
        if row["value"] is not None and date.fromisoformat(row["observation_date"]) <= at.date():
            grouped[row["series"]].append(row)
    result = {"values": {}, "missing_reasons": {}, "observations": {}}
    for series, observations in grouped.items():
        observations.sort(key=lambda r: r["observation_date"])
        row = observations[-1]
        if (at.date() - date.fromisoformat(row["observation_date"])).days > max_age_days:
            result["missing_reasons"][series] = "stale_observation"
            continue
        values = [float(r["value"]) for r in observations[-20:]]
        result["values"][series] = values[-1]
        if len(values) >= 2:
            result["values"][series + "_change"] = values[-1] - values[-2]
        if len(values) == 20 and pstdev(values) > 0:
            result["values"][series + "_z20"] = (values[-1] - mean(values)) / pstdev(values)
        result["observations"][series] = {
            k: row[k] for k in ("observation_date", "vintage_date", "available_at")
        }
    if {"DGS10", "DGS2"} <= result["values"].keys():
        result["values"]["yield_curve"] = result["values"]["DGS10"] - result["values"]["DGS2"]
    return result


def etf_metrics(
    prices: list[float], benchmark: list[float], *, distributions=None, nav=None, same_basis=False
) -> dict:
    result = {
        "premium_pct": None,
        "price_return": None,
        "total_return": None,
        "tracking_difference": None,
        "tracking_error": None,
        "missing_reasons": [],
    }
    if len(prices) < 2:
        result["missing_reasons"].append("insufficient_history")
        return result
    if nav is not None and nav > 0:
        result["premium_pct"] = 100 * (prices[-1] / nav - 1)
    result["price_return"] = prices[-1] / prices[0] - 1
    distributions = distributions or [0.0] * len(prices)
    if len(distributions) != len(prices):
        raise ValueError("분배금과 가격의 날짜 배열을 맞춰야 합니다.")
    returns = [(prices[i] + distributions[i]) / prices[i - 1] - 1 for i in range(1, len(prices))]
    result["total_return"] = math.prod(1 + r for r in returns) - 1
    if same_basis and len(benchmark) == len(prices):
        br = [b / a - 1 for a, b in zip(benchmark, benchmark[1:])]
        result["tracking_difference"] = result["total_return"] - (benchmark[-1] / benchmark[0] - 1)
        result["tracking_error"] = pstdev(a - b for a, b in zip(returns, br)) * math.sqrt(252)
    else:
        result["missing_reasons"].append("benchmark_basis_or_sessions_mismatch")
    return result
