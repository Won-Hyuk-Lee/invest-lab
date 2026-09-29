from copy import deepcopy

import pytest

from invest_lab.application.research import synthetic_research_dataset
from invest_lab.domain.research import (
    bars_at,
    decision_time,
    financial_features,
    macro_features,
    validate_dataset,
)
from invest_lab.domain.strategies import (
    StrategyRules,
    evaluate_strategy,
    replay,
    size_position,
    technical_features,
)


def test_uptrend_is_a_hard_filter_even_when_rebound_score_is_high():
    bars = [
        {
            "session": f"{i:04}",
            "open": p,
            "high": p + 1,
            "low": p - 1,
            "close": p,
            "volume": 100,
            "turnover": p * 100,
        }
        for i, p in enumerate(list(range(300, 171, -1)) + [173])
    ]
    technical = technical_features(bars)
    signal = evaluate_strategy(technical, {"kind": "kospi"}, StrategyRules())
    assert signal["eligible"] is False
    assert "uptrend_required" in signal["reasons"]


def test_etf_requires_explicit_product_metadata():
    bars = [
        {
            "session": str(i),
            "open": 100 + i,
            "high": 101 + i,
            "low": 99 + i,
            "close": 100 + i,
            "volume": 10000,
            "turnover": 1000000,
        }
        for i in range(140)
    ]
    technical = technical_features(bars)
    rules = StrategyRules(strategy_type="etf_trend")
    assert not evaluate_strategy(technical, {"kind": "etf_kr"}, rules)["eligible"]
    assert evaluate_strategy(
        technical,
        {"kind": "etf_kr"},
        rules,
        etf={"leverage": 1, "inverse": False, "benchmark_id": "KOSPI"},
    )["eligible"]
    assert not evaluate_strategy(
        technical,
        {"kind": "etf_kr"},
        rules,
        etf={"leverage": 2, "inverse": False, "benchmark_id": "KOSPI"},
    )["eligible"]


def test_sizing_includes_cost_and_allocation_caps():
    rules = StrategyRules(max_position_pct=10, max_total_pct=50, max_sector_pct=30)
    qty = size_position(
        equity=1000000,
        cash=1000000,
        price=10000,
        stop_distance=1000,
        total_value=490000,
        volume20=100000,
        rules=rules,
    )
    assert qty == 0  # One share plus its fee would exceed remaining exposure.
    assert (
        size_position(equity=1000000, cash=1000000, price=10000, stop_distance=0, rules=rules) == 0
    )


def test_atr_is_wilder_and_price_split_adjustment_is_as_of():
    data = synthetic_research_dataset(150)
    day = data["sessions"][100]
    data["actions"] = [
        {
            "event_id": "split",
            "symbol": "005930",
            "type": "split",
            "ratio": 2,
            "effective_date": day,
            "available_at": day + "T00:00:00+00:00",
            "ingested_at": day + "T00:00:00+00:00",
        }
    ]
    before = bars_at(data, "005930", data["sessions"][99], decision_time(data["sessions"][99]))
    after = bars_at(data, "005930", day, decision_time(day))
    assert after[0]["close"] == before[0]["close"] / 2
    assert after[0]["volume"] == before[0]["volume"] * 2


@pytest.mark.parametrize("intraday_stop", [False, True])
def test_halted_day_uses_published_close_for_valuation_without_fill_or_signal(
    monkeypatch, intraday_stop
):
    data = synthetic_research_dataset(130)
    symbol = "005930"
    halted_session = data["sessions"][126]
    published_close = next(
        row["close"]
        for row in data["bars"]
        if row["symbol"] == symbol and row["session"] == halted_session
    )
    for row in data["bars"]:
        if row["symbol"] == symbol and row["session"] == halted_session:
            row.update(
                open=0,
                high=0,
                low=0,
                volume=0,
                turnover=0,
                tradable=False,
                valuation_only=True,
            )
    next(row for row in data["universe"] if row["session"] == halted_session)["symbols"].remove(
        symbol
    )
    validate_dataset(data)
    seen_histories = []

    def fixed_signal(technical, instrument, rules, *, position=None, **kwargs):
        if instrument["symbol"] == symbol and technical["session"] == data["sessions"][127]:
            seen_histories.append(technical["history_count"])
        eligible = position is None and (
            (instrument["symbol"] == symbol and technical["session"] == data["sessions"][124])
            or (instrument["symbol"] == "105560" and technical["session"] == data["sessions"][125])
        )
        return {
            "action": "hold",
            "eligible": eligible,
            "rank": 1,
            "stop_distance": 1000,
            "reasons": [],
        }

    monkeypatch.setattr("invest_lab.domain.strategies.evaluate_strategy", fixed_signal)
    result = replay(data, StrategyRules(intraday_stop=intraday_stop))
    assert any(t["symbol"] == symbol and t["side"] == "buy" for t in result["trades"])
    assert not any(
        t["session"] == halted_session and t["symbol"] == symbol for t in result["trades"]
    )
    assert any(
        t["session"] == halted_session and t["symbol"] == "105560" and t["side"] == "buy"
        for t in result["trades"]
    )
    halt_equity = next(r for r in result["equity_curve"] if r["session"] == halted_session)
    position = result["open_positions"][symbol]
    other_close = next(
        float(row["close"])
        for row in data["bars"]
        if row["symbol"] == "105560" and row["session"] == halted_session
    )
    assert halt_equity["holdings_value"] == pytest.approx(
        position["quantity"] * float(published_close)
        + next(
            trade["quantity"]
            for trade in result["trades"]
            if trade["symbol"] == "105560"
            and trade["session"] == halted_session
            and trade["side"] == "buy"
        )
        * other_close
    )
    assert seen_histories == [127]


def test_valuation_only_row_rejects_executable_price():
    data = synthetic_research_dataset(5)
    row = data["bars"][0]
    row.update(open=0, high=0, low=0, volume=0, turnover=0, tradable=False, valuation_only=True)
    validate_dataset(data)
    row["open"] = 1
    with pytest.raises(ValueError, match="평가 전용"):
        validate_dataset(data)


def test_financial_ttm_uses_compatible_quarters_and_revisions_available_then():
    facts = []
    periods = [
        ("2022-01-01", "2022-03-31"),
        ("2022-04-01", "2022-06-30"),
        ("2022-07-01", "2022-09-30"),
        ("2022-10-01", "2022-12-31"),
    ]
    for start, end in periods:
        facts.append(
            {
                "symbol": "A",
                "account": "revenue",
                "scope": "CFS",
                "currency": "KRW",
                "attribution": "total",
                "period_type": "quarter",
                "period_start": start,
                "period_end": end,
                "value": "10",
                "available_at": "2023-02-01T00:00:00+00:00",
                "ingested_at": "2023-02-01T00:00:00+00:00",
            }
        )
    revision = {
        **facts[-1],
        "value": "100",
        "available_at": "2023-04-01T00:00:00+00:00",
        "ingested_at": "2023-04-01T00:00:00+00:00",
    }
    facts.append(revision)
    assert (
        financial_features(facts, "A", decision_time("2023-03-01"))["values"]["revenue_ttm"] == 40
    )
    assert (
        financial_features(facts, "A", decision_time("2023-05-01"))["values"]["revenue_ttm"] == 130
    )
    assert financial_features(facts[1:], "A", decision_time("2023-03-01"))["values"] == {}


def test_ytd_is_differenced_before_ttm_not_summed_as_quarters():
    facts = [
        {
            "symbol": "A",
            "account": "revenue",
            "scope": "CFS",
            "currency": "KRW",
            "attribution": "total",
            "period_type": "ytd",
            "period_start": "2022-01-01",
            "period_end": end,
            "value": value,
            "available_at": "2023-03-01T00:00:00+00:00",
            "ingested_at": "2023-03-01T00:00:00+00:00",
        }
        for end, value in (
            ("2022-03-31", 10),
            ("2022-06-30", 30),
            ("2022-09-30", 60),
            ("2022-12-31", 100),
        )
    ]
    assert (
        financial_features(facts, "A", decision_time("2023-04-01"))["values"]["revenue_ttm"] == 100
    )


def test_macro_does_not_use_future_revision_or_backfill_future_observation():
    rows = [
        {
            "series": "VIXCLS",
            "observation_date": "2023-01-02",
            "vintage_date": "2023-01-03",
            "value": 20,
            "available_at": "2023-01-03T00:00:00+00:00",
            "ingested_at": "2023-01-03T00:00:00+00:00",
        }
    ]
    rows.append(
        {
            **rows[0],
            "value": 99,
            "vintage_date": "2023-01-10",
            "available_at": "2023-01-10T00:00:00+00:00",
        }
    )
    assert macro_features(rows, decision_time("2023-01-04"))["values"]["VIXCLS"] == 20
    assert (
        macro_features(rows, decision_time("2023-02-04"))["missing_reasons"]["VIXCLS"]
        == "stale_observation"
    )


@pytest.mark.parametrize("volume", [True, -1, float("inf"), float("nan"), 1.5])
def test_dataset_rejects_invalid_volume(volume):
    data = synthetic_research_dataset(5)
    data["bars"][0]["volume"] = volume
    with pytest.raises(ValueError):
        validate_dataset(data)


def test_replay_executes_after_signal_and_is_deterministic():
    data = validate_dataset(synthetic_research_dataset(170))
    rules = StrategyRules(strategy_type="etf_trend", max_gap_pct=10)
    first = replay(data, rules, start_session=data["sessions"][124])
    second = replay(data, rules, start_session=data["sessions"][124])
    assert first == second
    assert first["trades"]
    assert all(t["session"] > t["signal_session"] for t in first["trades"])
    assert first["metrics"]["fees_and_taxes"] > 0


def test_replay_missing_universe_is_a_failure_not_current_list_fallback():
    data = synthetic_research_dataset(130)
    data["universe"] = data["universe"][:-1]
    with pytest.raises(ValueError, match="유니버스"):
        replay(data, StrategyRules(), start_session=data["sessions"][-3])


def test_future_bars_do_not_change_past_features():
    data = synthetic_research_dataset(150)
    session = data["sessions"][130]
    before = technical_features(bars_at(data, "005930", session, decision_time(session)))
    changed = deepcopy(data)
    changed["bars"][-4]["close"] = "999999"
    assert technical_features(bars_at(changed, "005930", session, decision_time(session))) == before


def test_distribution_is_receivable_on_ex_date_and_cash_only_on_payment_date():
    data = synthetic_research_dataset(170)
    rules = StrategyRules(strategy_type="etf_trend", max_gap_pct=10)
    baseline = replay(data, rules, start_session=data["sessions"][124])
    buy = next(t for t in baseline["trades"] if t["side"] == "buy")
    index = data["sessions"].index(buy["session"])
    ex_day, pay_day = data["sessions"][index + 1], data["sessions"][index + 3]
    data["actions"].append(
        {
            "event_id": "distribution",
            "symbol": buy["symbol"],
            "type": "distribution",
            "effective_date": ex_day,
            "payment_date": pay_day,
            "amount": "100",
            "available_at": buy["session"] + "T08:00:00+00:00",
            "ingested_at": buy["session"] + "T08:00:00+00:00",
        }
    )
    report = replay(data, rules, start_session=data["sessions"][124])
    ex = next(p for p in report["equity_curve"] if p["session"] == ex_day)
    paid = next(p for p in report["equity_curve"] if p["session"] == pay_day)
    assert ex["receivable"] == buy["quantity"] * 100
    assert ex["equity"] == pytest.approx(ex["cash"] + ex["holdings_value"] + ex["receivable"])
    assert paid["receivable"] == 0


def test_model_scores_only_change_future_entries_and_preserve_same_cost_contract():
    data = synthetic_research_dataset(150)
    rules = StrategyRules(strategy_type="etf_trend")
    baseline = replay(data, rules, start_session=data["sessions"][124])
    blocked = replay(data, rules, start_session=data["sessions"][124], model_scores={})
    assert baseline["trades"]
    assert not blocked["trades"]
    assert blocked["metrics"]["return"] == 0
    assert baseline["strategy"] == blocked["strategy"]
    assert any(
        r["reason"] == "model_unavailable_or_below_threshold"
        for r in blocked["unfilled_or_excluded"]
    )


@pytest.mark.parametrize("value", [True, "10", float("nan"), float("inf")])
def test_strategy_numeric_contract_rejects_bool_strings_and_nonfinite(value):
    with pytest.raises(ValueError):
        StrategyRules(max_total_pct=value)
