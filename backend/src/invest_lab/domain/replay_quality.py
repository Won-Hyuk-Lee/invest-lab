"""Independent post-run checks for exploratory replays with no action ledger."""

from collections import defaultdict

from invest_lab.domain.research import coverage, decision_time, timestamp


def audit_no_action_replay(data: dict, result: dict, *, gap_threshold: float = 0.4) -> dict:
    """Flag suspect held price changes; never infer a split or repair PnL from a gap.

    This audit deliberately does not certify performance. Action-bearing datasets
    need an entitlement-aware independent ledger audit instead of this one.
    """
    if data["actions"]:
        raise ValueError(
            "기업행동이 있는 결과는 권리·단주·정산을 포함한 별도 원장 검증이 필요합니다."
        )
    if not 0 < gap_threshold < 1:
        raise ValueError("가격 변화 점검 기준은 0과 1 사이여야 합니다.")
    raw = {}
    for row in data["bars"]:
        if row["price_basis"] != "raw":
            continue
        if timestamp(row["available_at"]) > decision_time(row["session"]):
            continue
        key = row["symbol"], row["session"]
        old = raw.get(key)
        version = timestamp(row["available_at"]), timestamp(row["ingested_at"])
        if old is None or version > (timestamp(old["available_at"]), timestamp(old["ingested_at"])):
            raw[key] = row

    trades_by_day = defaultdict(list)
    for trade in result["trades"]:
        trades_by_day[trade["session"]].append(trade)
    positions, previous_marks = {}, {}
    breaks, missing = [], []
    for point in result["equity_curve"]:
        session = point["session"]
        # Inspect positions carried INTO the session, including opening exits.
        for symbol, quantity in positions.items():
            row = raw.get((symbol, session))
            previous = previous_marks.get(symbol)
            if row is None or previous is None:
                missing.append({"symbol": symbol, "session": session})
                continue
            tradable = row.get("tradable", True) and row["volume"] > 0
            price = float(row["open"] if tradable else row["close"])
            ratio = price / previous["close"]
            if abs(ratio - 1) > gap_threshold:
                breaks.append(
                    {
                        "symbol": symbol,
                        "session": session,
                        "quantity": quantity,
                        "previous_session": previous["session"],
                        "previous_close": previous["close"],
                        "current_price": price,
                        "price_kind": "open" if tradable else "valuation_close",
                        "ratio": ratio,
                        "requires_source_review": True,
                    }
                )
        for trade in trades_by_day[session]:
            symbol = trade["symbol"]
            if trade["side"] == "buy":
                if symbol in positions:
                    raise ValueError("이 감사는 종목별 단일 진입 원장만 지원합니다.")
                positions[symbol] = trade["quantity"]
            elif trade["side"] == "sell":
                if positions.pop(symbol) != trade["quantity"]:
                    raise ValueError("이 감사는 부분 청산 원장을 지원하지 않습니다.")
            else:
                raise ValueError("알 수 없는 체결 방향입니다.")
        previous_marks = {}
        for symbol in positions:
            row = raw.get((symbol, session))
            if row is None:
                missing.append({"symbol": symbol, "session": session})
            else:
                previous_marks[symbol] = {"session": session, "close": float(row["close"])}

    cov = coverage(data)
    issues = list(cov["issues"])
    if breaks:
        issues.append("unreviewed_held_price_breaks")
    if missing:
        issues.append("missing_held_valuation")
    last = result["equity_curve"][-1]
    return {
        "scope": "post_run_no_action_price_and_coverage_audit",
        "status": "blocked" if issues else "requires_independent_validation",
        "performance_validated": False,
        "runtime_validation_complete": False,
        "issues": sorted(set(issues)),
        "coverage": cov,
        "gap_threshold": gap_threshold,
        "held_price_breaks": breaks,
        "missing_held_bars": missing,
        "terminal_untradable_mark": last.get("untradable_mark_value"),
        "interpretation": "가격 급변은 조사 대상이며 기업행동의 증명이나 자동 보정 비율이 아니다.",
    }
