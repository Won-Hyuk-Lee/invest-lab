from copy import deepcopy

import pytest

from invest_lab.domain.portfolio_policy import (
    apply_managed_fill,
    choose_rotations,
    exit_decision,
    holding_score,
    holding_sessions,
    pending_order_risk,
    portfolio_risk,
    score_percentiles,
)
from invest_lab.domain.strategies import StrategyRules


def features(close=110, **kwargs):
    return {"close": close, "sma": {"20": 105, "120": 80}, "atr14": 5, "relative63": 2, **kwargs}


def position(**kwargs):
    return {
        "quantity": 5,
        "entry_price": 100,
        "initial_stop_distance": 10,
        "stop_price": 90,
        "high_watermark": 100,
        **kwargs,
    }


def test_partial_sale_preserves_remaining_protection_and_basis():
    p = {"managed_quantity": 10, "stop_price": 80, "cost_basis_gross": 1000}
    sold = apply_managed_fill(
        p,
        side="sell",
        quantity=3,
        price=110,
        session="2026-09-25",
        order_id="s",
        context={"stop_price": 99},
    )
    assert sold["managed_quantity"] == 7
    assert sold["cost_basis_gross"] == 700
    assert sold["stop_price"] == 80
    assert p["managed_quantity"] == 10


def test_new_cycle_resets_age_and_protection_after_flat():
    closed = {"managed_quantity": 0, "opened_session": "2026-01-01", "stop_price": 80}
    new = apply_managed_fill(
        closed,
        side="buy",
        quantity=2,
        price=100,
        session="2026-09-25",
        order_id="new",
        context={"initial_stop_distance": 5},
    )
    assert new["opened_session"] == "2026-09-25"
    assert new["stop_price"] == 95 and new["entry_price"] == 100
    assert new["position_id"]


def test_partial_buy_keeps_first_fill_age_and_weighted_cost():
    p = apply_managed_fill(
        None,
        side="buy",
        quantity=2,
        price=100,
        session="2026-09-24",
        order_id="b",
        context={"initial_stop_distance": 10},
    )
    q = apply_managed_fill(
        p,
        side="buy",
        quantity=2,
        price=110,
        session="2026-09-25",
        order_id="b",
        context={"initial_stop_distance": 10},
    )
    assert q["opened_session"] == "2026-09-24"
    assert q["position_id"] == p["position_id"]
    assert q["entry_price"] == 105 and q["cost_basis_gross"] == 420
    assert q["stop_price"] == 90


def test_age_counts_entry_close_once_and_only_known_sessions():
    sessions = ["2026-09-24", "2026-09-25", "2026-09-28"]
    assert holding_sessions("2026-09-25", "2026-09-24", sessions) == 0
    assert holding_sessions("2026-09-25", "2026-09-25", sessions) == 1
    assert holding_sessions("2026-09-25", "2026-09-28", sessions) == 2


def test_recovery_switches_to_trailing_without_forced_sale():
    decision = exit_decision(features(), position(), StrategyRules(exit_policy="trail"), age=2)
    assert decision["quantity"] == 0
    assert decision["updates"]["trailing_active"]
    assert decision["updates"]["stop_price"] == 100


def test_trailing_never_loosens_when_atr_increases():
    p = position(stop_price=105, high_watermark=120, trailing_active=True)
    decision = exit_decision(features(110, atr14=50), p, StrategyRules(exit_policy="trail"), age=2)
    assert decision["updates"]["stop_price"] == 105
    assert p["stop_price"] == 105


def test_partial_recovery_rounding_and_one_share_case():
    rule = StrategyRules(exit_policy="partial_trail")
    assert exit_decision(features(), position(), rule, age=2)["quantity"] == 2
    assert exit_decision(features(), position(quantity=1), rule, age=2)["quantity"] == 1
    assert exit_decision(features(), position(recovery_taken=True), rule, age=2)["quantity"] == 0


def test_protective_exit_overrides_partial_recovery():
    p = position(stop_price=115)
    decision = exit_decision(features(), p, StrategyRules(exit_policy="partial_trail"), age=1)
    assert decision["quantity"] == 5 and "close_stop" in decision["reasons"]


def test_trailing_extension_has_fixed_absolute_expiry():
    rule = StrategyRules(
        exit_policy="trail", max_holding_sessions=10, holding_extension_sessions=10
    )
    assert exit_decision(features(), position(trailing_active=True), rule, age=10)["quantity"] == 0
    assert exit_decision(features(), position(trailing_active=True), rule, age=20)["quantity"] == 5


def test_stagnation_requires_weak_relative_strength_and_little_progress():
    rule = StrategyRules(stagnation_sessions=5)
    t = features(100, relative63=-1)
    assert "stagnation_exit" in exit_decision(t, position(), rule, age=5)["reasons"]
    assert not exit_decision(t, position(high_watermark=110), rule, age=5)["quantity"]


def test_missing_scores_and_ties_do_not_create_spurious_extremes():
    assert holding_score(features()) is None  # No slope, not a made-up zero.
    assert score_percentiles({"a": 2, "b": 2, "missing": None}) == {"a": 50, "b": 50}


def test_rotation_requires_material_gap_age_cooldown_cost_and_tradability():
    rule = StrategyRules(rotation_enabled=True)
    old = [{"symbol": "a", "tradable": True, "age": 3}]
    new = [{"symbol": "b", "quantity": 1}]
    percentiles = {"a": 40, "b": 90}

    def call(h=old, c=new, p=percentiles, cooldowns=None):
        return choose_rotations(h, c, p, rule, day_index=10, cooldowns=cooldowns or {})

    assert call()[0]["sell_symbol"] == "a"
    assert not call(p={"a": 60, "b": 70})
    assert not call(cooldowns={"b": 8})
    assert not call(c=[{"symbol": "b", "quantity": 0}])
    for field, value in [
        ("tradable", False),
        ("fully_managed", False),
        ("pending", True),
        ("age", 2),
    ]:
        copy = deepcopy(old)
        copy[0][field] = value
        assert not call(h=copy)
    expensive = StrategyRules(rotation_enabled=True, sell_tax_bps=100)
    assert not choose_rotations(old, new, percentiles, expensive, day_index=10, cooldowns={})


def test_portfolio_risk_includes_reservations_and_missing_marks():
    assert portfolio_risk({"a": position()}, {"a": 100}, reserved=20) == 70
    assert portfolio_risk({"a": position()}, {}) is None


def test_pending_risk_is_unknown_without_a_stop_and_reserves_only_remaining_shares():
    order = {"remaining_quantity": 3, "submitted_price": "100", "context": {"stop_price": 90}}
    assert pending_order_risk([order]) == 30
    assert pending_order_risk([{**order, "context": {"initial_stop_distance": 5}}]) == 15
    assert pending_order_risk([{**order, "context": {}}]) is None
    assert portfolio_risk({}, {}, reserved=None) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"exit_policy": "typo"},
        {"rotation_max_per_day": 0},
        {"rotation_min_holding": -1},
        {"rotation_entry_percentile": 40},
        {"rotation_enabled": 1},
        {"max_open_risk_pct": 0},
    ],
)
def test_invalid_portfolio_parameters_are_rejected(kwargs):
    with pytest.raises(ValueError):
        StrategyRules(**kwargs)


def test_risk_reduction_trims_highest_contribution_without_selling_manual_shares():
    from invest_lab.domain.portfolio_policy import risk_reductions

    rules = StrategyRules(
        rebalance_risk=True,
        max_open_risk_pct=1,
        max_position_pct=100,
        max_total_pct=100,
        max_sector_pct=100,
    )
    positions = {
        "a": {"quantity": 10, "managed_quantity": 2, "stop_price": 80},
        "b": {"quantity": 10, "stop_price": 90},
    }
    reductions = risk_reductions(
        positions, {"a": 100, "b": 100}, {"a": {}, "b": {}}, rules, equity=10000
    )
    assert reductions == {"a": 2, "b": 10}
    positions["a"]["tradable"] = False
    assert risk_reductions(
        positions, {"a": 100, "b": 100}, {"a": {}, "b": {}}, rules, equity=10000
    ) == {"b": 10}
