from copy import deepcopy

import pytest

from invest_lab.application.research import synthetic_research_dataset
from invest_lab.domain.research import coverage, validate_dataset
from invest_lab.domain.strategies import StrategyRules, position_size_details, replay


@pytest.fixture
def scenario(monkeypatch):
    data = synthetic_research_dataset(142)
    symbol = "005930"
    day = data["sessions"][124]

    def signal(technical, instrument, rules, *, position=None, **kwargs):
        eligible = not position and instrument["symbol"] == symbol and technical["session"] == day
        return {
            "eligible": eligible,
            "action": "buy" if eligible else "hold",
            "rank": 1,
            "stop_distance": 1000,
            "reasons": [],
        }

    monkeypatch.setattr("invest_lab.domain.strategies.evaluate_strategy", signal)
    return data, symbol


def run(data, **kwargs):
    return replay(
        data,
        StrategyRules(max_gap_pct=10, **kwargs),
        start_session=data["sessions"][124],
        record_decisions=True,
    )


def bar(data, symbol, index):
    return next(
        r for r in data["bars"] if r["symbol"] == symbol and r["session"] == data["sessions"][index]
    )


def halt(data, symbol, index):
    bar(data, symbol, index).update(
        open=0, high=0, low=0, volume=0, turnover=0, valuation_only=True, tradable=False
    )


def test_future_revision_cannot_rewrite_past_fills_or_marks(scenario):
    data, symbol = scenario
    expected = run(data)
    revised = deepcopy(bar(data, symbol, 125))
    for key in ("open", "high", "low", "close"):
        revised[key] = float(revised[key]) * 2
    revised.update(
        available_at="2030-01-01T00:00:00+00:00", ingested_at="2030-01-01T00:00:00+00:00"
    )
    data["bars"].append(revised)
    assert run(data) == expected


def test_feature_cache_preserves_results_and_rejects_changed_dataset(scenario):
    data, symbol = scenario
    cache = {}
    expected = run(data)
    kwargs = {"start_session": data["sessions"][124], "record_decisions": True}
    rules = StrategyRules(max_gap_pct=10)
    assert replay(data, rules, feature_cache=cache, **kwargs) == expected
    assert replay(data, rules, feature_cache=cache, **kwargs) == expected
    bar(data, symbol, 126)["volume"] += 1
    with pytest.raises(ValueError, match="캐시"):
        replay(data, rules, feature_cache=cache, **kwargs)


@pytest.mark.parametrize("candidate_remains", [True, False])
def test_rotation_waits_for_sale_then_next_close_recheck(monkeypatch, candidate_remains):
    data = synthetic_research_dataset(140)
    days = data["sessions"]

    def signal(technical, instrument, rules, *, position=None, **kwargs):
        symbol, day = instrument["symbol"], technical["session"]
        eligible = not position and (
            (symbol == "005930" and day == days[124])
            or (symbol == "105560" and day >= days[126] and (candidate_remains or day == days[126]))
        )
        return {
            "eligible": eligible,
            "action": "buy" if eligible else "hold",
            "rank": 100 if symbol == "105560" else 0,
            "stop_distance": 1000,
            "reasons": [],
            "holding_score": {"005930": 0, "105560": 100}.get(symbol),
        }

    monkeypatch.setattr("invest_lab.domain.strategies.evaluate_strategy", signal)
    rules = StrategyRules(
        max_positions=1,
        max_holding_sessions=50,
        rotation_enabled=True,
        rotation_min_holding=1,
        max_gap_pct=10,
    )
    report = replay(data, rules, start_session=days[124])
    sells = [t for t in report["trades"] if t["side"] == "sell"]
    assert sells[0]["session"] == days[127]
    assert sells[0]["reason"] == ["opportunity_rotation"]
    buys = [t for t in report["trades"] if t["side"] == "buy" and t["symbol"] == "105560"]
    if candidate_remains:
        assert buys[0]["session"] == days[128]
        assert report["rotations"][0]["status"] == "completed"
    else:
        assert not buys
        assert report["rotations"][0]["status"] == "entry_failed"


def test_evening_universe_cannot_cancel_same_morning_order(scenario):
    data, symbol = scenario
    expected = run(data)["trades"][0]
    next(u for u in data["universe"] if u["session"] == data["sessions"][125])["symbols"].remove(
        symbol
    )
    assert run(data)["trades"][0] == expected


def test_quantity_is_committed_before_open_and_negative_gap_is_filled(scenario):
    data, symbol = scenario
    expected = run(data)
    opening = bar(data, symbol, 125)
    opening["open"] = float(opening["open"]) * 0.6
    opening["low"] = float(opening["low"]) * 0.6
    actual = run(data)
    assert actual["trades"][0]["quantity"] == expected["trades"][0]["quantity"]
    assert actual["trades"][0]["price"] < expected["trades"][0]["price"]
    assert actual["diagnostics"]["decisions"][0] == expected["diagnostics"]["decisions"][0]
    plan = next(d for d in actual["diagnostics"]["decisions"] if d["stage"] == "sizing")
    assert plan["sizing"]["quantity"] == actual["trades"][0]["quantity"]
    assert plan["sizing"]["binding_constraints"]


def test_upper_limit_order_expires_after_one_open_attempt(scenario):
    data, symbol = scenario
    opening = bar(data, symbol, 125)
    opening["open"] = float(opening["open"]) * 2
    opening["high"] = float(opening["high"]) * 2
    report = run(data)
    assert not report["trades"]
    assert any(x["reason"] == "opening_limit_not_filled" for x in report["unfilled_or_excluded"])


@pytest.mark.parametrize("holding", [1, 2, 10])
def test_N_close_holding_exits_after_N_open_intervals(scenario, holding):
    data, _ = scenario
    trades = run(data, max_holding_sessions=holding)["trades"]
    assert len(trades) == 2
    assert (
        data["sessions"].index(trades[1]["session"]) - data["sessions"].index(trades[0]["session"])
        == holding
    )


def test_sell_intent_survives_halt_and_fills_on_first_reopening(scenario):
    data, symbol = scenario
    halt(data, symbol, 127)
    halt(data, symbol, 128)
    report = run(data, max_holding_sessions=2)
    sell = next(t for t in report["trades"] if t["side"] == "sell")
    assert sell["signal_session"] == data["sessions"][126]
    assert sell["session"] == data["sessions"][129]
    assert (
        len([x for x in report["unfilled_or_excluded"] if x["reason"] == "sell_unfilled_retry"])
        == 2
    )
    assert not report["pending_orders"]["sells"]


def test_expiry_is_created_during_halt_and_frozen_value_is_reported(scenario):
    data, symbol = scenario
    for i in (126, 127, 128):
        halt(data, symbol, i)
    report = run(data, max_holding_sessions=2)
    sell = next(t for t in report["trades"] if t["side"] == "sell")
    assert sell["signal_session"] == data["sessions"][126]
    assert sell["session"] == data["sessions"][129]
    frozen = next(x for x in report["equity_curve"] if x["session"] == data["sessions"][127])
    assert frozen["untradable_mark_value"] == frozen["holdings_value"] > 0
    assert frozen["tradable_mark_value"] == 0


def test_evening_halt_mark_cannot_change_other_morning_order(monkeypatch):
    data = synthetic_research_dataset(132)

    def signal(technical, instrument, rules, *, position=None, **kwargs):
        eligible = not position and (instrument["symbol"], technical["session"]) in {
            ("005930", data["sessions"][124]),
            ("105560", data["sessions"][125]),
        }
        return {
            "eligible": eligible,
            "action": "hold",
            "rank": 1,
            "stop_distance": 1000,
            "reasons": [],
        }

    monkeypatch.setattr("invest_lab.domain.strategies.evaluate_strategy", signal)
    halt(data, "005930", 126)
    first = run(data)
    bar(data, "005930", 126)["close"] = float(bar(data, "005930", 126)["close"]) * 100
    second = run(data)
    assert [t for t in first["trades"] if t["side"] == "buy"] == [
        t for t in second["trades"] if t["side"] == "buy"
    ]


def test_missing_held_bar_is_not_filled_from_future_revision(scenario):
    data, symbol = scenario
    bar(data, symbol, 126)["available_at"] = "2030-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="보유 평가 봉"):
        run(data)


def test_late_corporate_action_cannot_change_opening_ledger(scenario):
    data, symbol = scenario
    day = data["sessions"][126]
    data["actions"].append(
        {
            "event_id": "late",
            "symbol": symbol,
            "type": "split",
            "ratio": 2,
            "effective_date": day,
            "available_at": day + "T09:00:00+00:00",
            "ingested_at": day + "T09:00:00+00:00",
        }
    )
    with pytest.raises(ValueError, match="기업행동"):
        run(data)


def test_independent_master_reveals_missing_bar_even_when_removed_from_tradable_universe():
    data = synthetic_research_dataset(130)
    for row in data["universe"]:
        row["listed_symbols"] = list(row["symbols"])
    last = data["sessions"][-1]
    data["bars"] = [b for b in data["bars"] if (b["symbol"], b["session"]) != ("005930", last)]
    data["universe"][-1]["symbols"].remove("005930")
    report = coverage(validate_dataset(data))
    assert report["denominator"] == "independent_listed_symbols"
    assert report["missing_bar_count"] == 1
    assert report["missing_bars"] == [("005930", last)]


def test_sizing_explains_binding_constraint_and_reserves_fee():
    result = position_size_details(
        equity=1_000_000,
        cash=1_000_000,
        price=100,
        stop_distance=10,
        volume20=1_000_000,
        rules=StrategyRules(),
    )
    assert result["binding_constraints"] == ["risk_budget"]
    assert result["quantity"] == 248
    assert result["reserved_cost"] > result["quantity"] * 100
    assert result["planned_stop_risk"] <= result["risk_budget"]


def test_screen_failures_and_slot_rejections_are_counted(monkeypatch):
    data = synthetic_research_dataset(130)

    def signal(technical, instrument, rules, *, position=None, **kwargs):
        return {
            "eligible": not position,
            "action": "hold",
            "rank": 1,
            "stop_distance": 1000,
            "reasons": [],
        }

    monkeypatch.setattr("invest_lab.domain.strategies.evaluate_strategy", signal)
    report = run(data, max_positions=1)
    assert report["diagnostics"]["funnel"]["order:position_slots_full"] > 0
    assert max(c["position_count"] for c in report["equity_curve"]) == 1
    assert all(c["cash"] >= 0 for c in report["equity_curve"])


def test_announced_split_adjusts_precommitted_quantity_and_limit(scenario):
    data, symbol = scenario
    original = run(data)["trades"][0]
    effective = data["sessions"][125]
    for row in data["bars"]:
        if row["symbol"] == symbol and row["session"] >= effective:
            for key in ("open", "high", "low", "close"):
                row[key] = float(row[key]) / 2
            row["volume"] *= 2
    data["actions"] = [
        {
            "event_id": "split",
            "symbol": symbol,
            "type": "split",
            "ratio": 2,
            "effective_date": effective,
            "available_at": data["sessions"][124] + "T09:00:00+00:00",
            "ingested_at": data["sessions"][124] + "T09:00:00+00:00",
        }
    ]
    result = run(data)
    trade = result["trades"][0]
    assert trade["quantity"] == original["quantity"] * 2
    assert trade["price"] == pytest.approx(original["price"] / 2)
    assert trade["limit_price"] == pytest.approx(original["limit_price"] / 2)


def test_known_delisting_cancels_precommitted_buy_even_if_price_row_exists(scenario):
    data, symbol = scenario
    effective = data["sessions"][125]
    data["actions"] = [
        {
            "event_id": "delisted",
            "symbol": symbol,
            "type": "delisting",
            "settlement_price": 0,
            "effective_date": effective,
            "available_at": effective + "T00:00:00+00:00",
            "ingested_at": effective + "T00:00:00+00:00",
        }
    ]
    result = run(data)
    assert not result["trades"]
    assert any(
        r["reason"] == "known_delisting_cancelled_entry" for r in result["unfilled_or_excluded"]
    )


def test_multiple_precommitted_orders_cannot_reserve_the_same_cash(monkeypatch):
    data = synthetic_research_dataset(130)

    def signal(technical, instrument, rules, *, position=None, **kwargs):
        return {
            "eligible": not position,
            "action": "hold",
            "rank": 1,
            "stop_distance": 1,
            "reasons": [],
        }

    monkeypatch.setattr("invest_lab.domain.strategies.evaluate_strategy", signal)
    result = replay(
        data,
        StrategyRules(
            max_position_pct=100,
            max_total_pct=100,
            max_sector_pct=100,
            max_index_pct=100,
            risk_per_trade_pct=100,
            participation_pct=100,
        ),
        initial_cash=1_000_000,
        start_session=data["sessions"][124],
        record_decisions=True,
    )
    first_plans = [
        d["sizing"]
        for d in result["diagnostics"]["decisions"]
        if d["session"] == data["sessions"][124] and d["stage"] == "sizing"
    ]
    assert len(first_plans) > 1
    assert sum(p.get("reserved_cost", 0) for p in first_plans) <= 1_000_000
    assert any(p["quantity"] == 0 for p in first_plans)
    assert min(p["cash"] for p in result["equity_curve"]) >= 0
