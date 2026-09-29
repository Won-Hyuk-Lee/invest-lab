from copy import deepcopy

import pytest

from invest_lab.domain.replay_ledger_audit import audit_ledger


def ledger_case():
    days = [f"2026-09-{day:02d}" for day in (1, 2, 3, 4)]
    data = {
        "bars": [
            {
                "symbol": "A",
                "session": day,
                "close": 100 if index == 0 else 200,
                "price_basis": "raw",
                "available_at": day + "T09:00:00Z",
                "ingested_at": day + "T09:00:00Z",
            }
            for index, day in enumerate(days)
        ],
        "actions": [
            {
                "event_id": "consolidation",
                "symbol": "A",
                "type": "split",
                "ratio": 0.5,
                "effective_date": days[1],
                "price_basis_date": days[1],
                "fractional_policy": "cash_at_close",
                "fractional_price_date": days[1],
                "payment_date": days[3],
                "available_at": days[0] + "T00:00:00Z",
                "ingested_at": days[0] + "T00:00:00Z",
            }
        ],
    }
    result = {
        "initial_cash": 1000,
        "trades": [
            {
                "session": days[0],
                "symbol": "A",
                "side": "buy",
                "quantity": 9,
                "price": 100,
                "fee": 0,
            },
            {
                "session": days[2],
                "symbol": "A",
                "side": "sell",
                "quantity": 2,
                "price": 220,
                "fee": 2,
                "profit": 38,
            },
        ],
        "equity_curve": [
            {
                "session": days[0],
                "cash": 100,
                "holdings_value": 900,
                "receivable": 0,
                "equity": 1000,
            },
            {
                "session": days[1],
                "cash": 100,
                "holdings_value": 800,
                "receivable": 100,
                "equity": 1000,
            },
            {
                "session": days[2],
                "cash": 538,
                "holdings_value": 400,
                "receivable": 100,
                "equity": 1038,
            },
            {
                "session": days[3],
                "cash": 638,
                "holdings_value": 400,
                "receivable": 0,
                "equity": 1038,
            },
        ],
        "open_positions": {"A": {"quantity": 2}},
    }
    return data, result


def test_independent_audit_allocates_cost_and_tracks_receivable_payment():
    data, result = ledger_case()
    report = audit_ledger(data, result)
    assert report["ledger_verified"]
    assert not report["performance_validated"]
    assert set(report["maximum_errors"].values()) == {0}


@pytest.mark.parametrize("field", ["profit", "cash", "equity", "quantity"])
def test_independent_audit_rejects_tampered_report(field):
    data, result = ledger_case()
    if field == "profit":
        result["trades"][1][field] += 1
    elif field == "quantity":
        result["open_positions"]["A"][field] += 1
    else:
        result["equity_curve"][2][field] += 1
    with pytest.raises(AssertionError):
        audit_ledger(data, result)


def test_independent_audit_orders_timestamp_offsets_and_ignores_future_revisions():
    data, result = ledger_case()
    earlier = deepcopy(data["bars"][0])
    earlier.update(close=99, available_at="2026-09-01T17:30:00+09:00")
    future = deepcopy(data["bars"][0])
    future.update(close=101, available_at="2026-09-05T00:00:00Z")
    data["bars"] += [earlier, future]
    assert audit_ledger(data, result)["ledger_verified"]
