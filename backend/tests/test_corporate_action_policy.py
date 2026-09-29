from copy import deepcopy

import pytest

from invest_lab.application.research import synthetic_research_dataset
from invest_lab.domain.corporate_actions import quotation_price, split_position
from invest_lab.domain.replay_ledger_audit import audit_ledger
from invest_lab.domain.research import bars_at, decision_time, validate_dataset
from invest_lab.domain.strategies import StrategyRules, replay


def event():
    return {
        "event_id": "reverse-split",
        "symbol": "005930",
        "type": "split",
        "ratio": 0.5,
        "effective_date": "2026-09-05",
        "price_basis_date": "2026-09-22",
        "fractional_policy": "cash_at_close",
        "fractional_price_date": "2026-09-22",
        "payment_date": None,
        "available_at": "2026-06-24T00:00:00Z",
        "ingested_at": "2026-09-29T00:00:00Z",
    }


def test_fractional_right_and_stop_units_survive_halt_without_early_cash():
    position = {
        "quantity": 179,
        "cost_per_share": 938,
        "entry_price": 937,
        "stop_price": 800,
        "high_watermark": 960,
        "initial_stop_distance": 137,
    }
    adjusted, right = split_position(position, event())
    assert adjusted["quantity"] == 89
    assert adjusted["stop_price"] == 1600
    assert adjusted["high_watermark"] == 1920
    assert adjusted["initial_stop_distance"] == 274
    assert right["quantity"] == 0.5 and right["amount"] is None
    assert right["payment_date"] is None
    assert adjusted["quantity"] * adjusted["cost_per_share"] + right["cost_basis"] == 179 * 938
    old_mark = quotation_price(913, "005930", "2026-09-07", [event()])
    assert (adjusted["quantity"] + right["quantity"]) * old_mark == 179 * 913
    assert quotation_price(1855, "005930", "2026-09-22", [event()]) == 1855


def test_quote_adjustment_starts_on_price_basis_date_not_legal_date():
    data = {
        "actions": [event()],
        "bars": [
            {
                "symbol": "005930",
                "session": "2026-09-02",
                "price_basis": "raw",
                "available_at": "2026-09-02T09:00:00Z",
                "ingested_at": "2026-09-29T00:00:00Z",
                "open": 900,
                "high": 930,
                "low": 890,
                "close": 913,
                "volume": 100,
            }
        ],
    }
    assert bars_at(data, "005930", "2026-09-07", decision_time("2026-09-07"))[0]["close"] == 913
    assert bars_at(data, "005930", "2026-09-22", decision_time("2026-09-22"))[0]["close"] == 1826


def test_unverified_fractional_terms_fail_instead_of_disappearing():
    action = event()
    action.pop("fractional_policy")
    with pytest.raises(ValueError, match="단주"):
        split_position({"quantity": 1}, action)


def test_replay_odd_lot_receivable_is_not_spendable_until_known_payment(monkeypatch):
    data = synthetic_research_dataset(140)
    sessions = data["sessions"]
    symbol = "005930"
    bars = {b["session"]: b for b in data["bars"] if b["symbol"] == symbol}
    for row in bars.values():
        row.update(open=100, high=100, low=100, close=100, volume=10000)
    for day in sessions[127:]:
        bars[day].update(open=200, high=200, low=200, close=200)
    bars[sessions[126]].update(
        open=0, high=0, low=0, volume=0, turnover=0, valuation_only=True, tradable=False
    )
    action = {
        **event(),
        "effective_date": sessions[126],
        "price_basis_date": sessions[127],
        "fractional_price_date": sessions[127],
        "available_at": sessions[123] + "T00:00:00Z",
    }
    data["actions"] = [action]

    def signal(technical, instrument, rules, *, position=None, **kwargs):
        eligible = (
            not position
            and instrument["symbol"] == symbol
            and technical["session"] == sessions[124]
        )
        return {
            "eligible": eligible,
            "action": "buy" if eligible else "hold",
            "rank": 1,
            "stop_distance": 10,
            "reasons": [],
        }

    monkeypatch.setattr("invest_lab.domain.strategies.evaluate_strategy", signal)
    rules = StrategyRules(
        max_gap_pct=0,
        commission_bps=0,
        slippage_bps=0,
        max_position_pct=90,
        max_total_pct=100,
        max_sector_pct=100,
        max_index_pct=100,
        risk_per_trade_pct=100,
        max_holding_sessions=50,
    )
    result = replay(data, rules, initial_cash=1000, start_session=sessions[124])
    assert result["trades"][0]["quantity"] == 9
    assert result["open_positions"][symbol]["quantity"] == 4
    assert all(row["equity"] == 1000 for row in result["equity_curve"])
    assert result["equity_curve"][-1]["cash"] == 100
    assert result["equity_curve"][-1]["receivable"] == 100
    assert audit_ledger(data, result)["ledger_verified"]
    paid = deepcopy(data)
    paid["actions"][0]["payment_date"] = sessions[130]
    settled = replay(paid, rules, initial_cash=1000, start_session=sessions[124])
    assert settled["equity_curve"][-1]["cash"] == 200
    assert settled["equity_curve"][-1]["receivable"] == 0
    assert audit_ledger(paid, settled)["ledger_verified"]
    validate_dataset(paid)

    # A correction after legal conversion postpones quotation and odd-lot pricing.
    # Shares must not be halved twice, and frozen old-unit quotes must stay bridged.
    postponed = deepcopy(data)
    old_event = postponed["actions"][0]
    old_event.update(price_basis_date=sessions[128], fractional_price_date=sessions[128])
    postponed["actions"].append(
        {
            **old_event,
            "price_basis_date": sessions[130],
            "fractional_price_date": sessions[130],
            "available_at": sessions[127] + "T00:00:00Z",
        }
    )
    for row in postponed["bars"]:
        if row["symbol"] == symbol and sessions[126] <= row["session"] < sessions[130]:
            row.update(
                open=0,
                high=0,
                low=0,
                close=100,
                volume=0,
                turnover=0,
                valuation_only=True,
                tradable=False,
            )
    before = deepcopy(postponed)
    rescheduled = replay(postponed, rules, initial_cash=1000, start_session=sessions[124])
    assert postponed == before  # Applied event revisions cannot mutate the input dataset.
    assert rescheduled["open_positions"][symbol]["quantity"] == 4
    assert all(row["equity"] == 1000 for row in rescheduled["equity_curve"])
    assert rescheduled["corporate_action_entitlements"][0]["priced_session"] == sessions[130]
    assert audit_ledger(postponed, rescheduled)["ledger_verified"]
    postponed["actions"][1]["ratio"] = 0.25
    with pytest.raises(ValueError, match="별도 원장 대사"):
        replay(postponed, rules, initial_cash=1000, start_session=sessions[124])


def test_postponed_legal_date_without_any_portfolio_effect_can_be_rescheduled(monkeypatch):
    data = synthetic_research_dataset(140)
    days = data["sessions"]
    initial = {
        **event(),
        "effective_date": days[124],
        "price_basis_date": days[130],
        "fractional_price_date": days[130],
        "available_at": days[123] + "T00:00:00Z",
    }
    data["actions"] = [
        initial,
        {**initial, "effective_date": days[128], "available_at": days[125] + "T00:00:00Z"},
    ]
    monkeypatch.setattr(
        "invest_lab.domain.strategies.evaluate_strategy",
        lambda *args, **kwargs: {
            "eligible": False,
            "action": "hold",
            "rank": 0,
            "stop_distance": 1,
            "reasons": [],
        },
    )
    result = replay(data, StrategyRules(), start_session=days[124])
    assert not result["trades"]
    assert all(point["equity"] == result["initial_cash"] for point in result["equity_curve"])
    assert audit_ledger(data, result)["ledger_verified"]
