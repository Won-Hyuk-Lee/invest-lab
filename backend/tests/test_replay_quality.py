from copy import deepcopy

import pytest

from invest_lab.domain.replay_quality import audit_no_action_replay


def sample(*, held_before=True):
    sessions = ["2026-09-21", "2026-09-22"]
    data = {
        "sessions": sessions,
        "source_kind": "imported",
        "provenance": {},
        "instruments": [{"symbol": "002630"}],
        "actions": [],
        "benchmarks": [],
        "financial_facts": [],
        "macro": [],
        "etf_metadata": [],
        "universe": [
            {"session": s, "symbols": ["002630"], "available_at": s + "T18:00:00+09:00"}
            for s in sessions
        ],
        "bars": [
            {
                "symbol": "002630",
                "session": s,
                "price_basis": "raw",
                "open": price,
                "close": price,
                "volume": 100,
                "available_at": s + "T18:00:00+09:00",
                "ingested_at": s + "T18:00:00+09:00",
            }
            for s, price in zip(sessions, [100, 210], strict=True)
        ],
    }
    result = {
        "trades": [
            {
                "session": sessions[0 if held_before else 1],
                "symbol": "002630",
                "quantity": 179,
                "side": "buy",
            },
            {"session": sessions[1], "symbol": "002630", "quantity": 179, "side": "sell"},
        ],
        "equity_curve": [{"session": s} for s in sessions],
    }
    return data, result


def test_sold_at_open_still_flags_unadjusted_held_jump():
    report = audit_no_action_replay(*sample())
    assert report["status"] == "blocked"
    assert not report["performance_validated"]
    assert report["held_price_breaks"][0]["quantity"] == 179
    assert report["held_price_breaks"][0]["ratio"] == 2.1


def test_same_day_entry_does_not_claim_overnight_exposure():
    report = audit_no_action_replay(*sample(held_before=False))
    assert not report["held_price_breaks"]
    assert not report["performance_validated"]  # Missing PIT evidence still blocks.


def test_later_revision_cannot_hide_original_jump():
    data, result = sample()
    revised = deepcopy(data["bars"][-1])
    revised.update(open=100, close=100, available_at="2026-09-23T18:00:00+09:00")
    data["bars"].append(revised)
    assert audit_no_action_replay(data, result)["held_price_breaks"][0]["ratio"] == 2.1


def test_untradable_mark_jump_is_also_reviewed():
    data, result = sample()
    data["bars"][-1].update(open=0, volume=0, tradable=False, valuation_only=True)
    assert audit_no_action_replay(data, result)["held_price_breaks"][0]["price_kind"] == (
        "valuation_close"
    )


def test_missing_held_bar_cannot_pass():
    data, result = sample()
    data["bars"].pop()
    report = audit_no_action_replay(data, result)
    assert "missing_held_valuation" in report["issues"]


def test_action_ledger_needs_entitlement_aware_audit():
    data, result = sample()
    data["actions"].append({"type": "split"})
    with pytest.raises(ValueError, match="단주"):
        audit_no_action_replay(data, result)
