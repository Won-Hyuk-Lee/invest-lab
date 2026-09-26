import pytest

from invest_lab.domain.order_chain import reconcile_order_chain


def order(
    identifier="001",
    parent="000",
    quantity=10,
    filled=0,
    cancelled=0,
    rejected=0,
    remaining=None,
    **kwargs,
):
    return {
        "broker_order_id": identifier,
        "original_order_id": parent,
        "branch_id": "012",
        "symbol": "0048K0",
        "side": "buy",
        "trading_date": "2026-09-25",
        "quantity": quantity,
        "filled_quantity": filled,
        "filled_amount": str(filled * 100),
        "cancelled_quantity": cancelled,
        "rejected_quantity": rejected,
        "remaining_quantity": quantity - filled - cancelled - rejected
        if remaining is None
        else remaining,
        **kwargs,
    }


def receipt(identifier):
    return {"broker_order_id": identifier, "branch_id": "012"}


def test_single_order_partial_fill_full_fill_and_cancel():
    partial = reconcile_order_chain([order(filled=3)], "001")
    assert partial["status"] == "partially_filled"
    assert partial["current_active_target"]["broker_order_id"] == "001"
    assert partial["filled_amount"] == "300"
    assert reconcile_order_chain([order(filled=10)], "001")["status"] == "filled"
    cancelled = reconcile_order_chain([order(filled=3, cancelled=7)], "001")
    assert cancelled["status"] == "cancelled"
    assert cancelled["current_active_target"] is None


def test_two_amendments_do_not_double_count_intermediate_transfer_as_cancellation():
    rows = [
        order(filled=2, cancelled=8),
        order("002", "001", quantity=8, filled=1, cancelled=7),
        order("003", "002", quantity=7, filled=2),
    ]
    result = reconcile_order_chain(
        rows,
        "001",
        confirmed_amend_receipts=[receipt("002"), receipt("003")],
        semantics_verified=True,
    )
    assert result["status"] == "partially_filled"
    assert result["filled_quantity"] == 5
    assert result["filled_amount"] == "500"
    assert result["cancelled_quantity"] == 0
    assert result["remaining_quantity"] == 5
    assert result["current_active_target"]["broker_order_id"] == "003"
    assert [row["replacement_transfer"] for row in result["trace"]] == [8, 7, 0]


def test_final_cancel_counts_only_untransferred_quantities():
    rows = [
        order(filled=2, cancelled=8),
        order("002", "001", quantity=7, filled=1, cancelled=6),
        order("003", "002", quantity=6, filled=2, cancelled=4),
    ]
    result = reconcile_order_chain(
        rows,
        "001",
        confirmed_amend_receipts=[receipt("002"), receipt("003")],
        semantics_verified=True,
    )
    assert result["status"] == "cancelled"
    assert result["filled_quantity"] == 5 and result["cancelled_quantity"] == 5
    assert result["remaining_quantity"] == 0


def test_actual_kis_accounting_is_gated_until_session_evidence_exists():
    rows = [order(cancelled=10), order("002", "001", quantity=10)]
    result = reconcile_order_chain(rows, "001", confirmed_amend_receipts=[receipt("002")])
    assert result["status"] == "unknown"
    assert result["reasons"] == ["AMENDMENT_ACCOUNTING_NOT_VERIFIED"]
    assert result["filled_quantity"] is None and result["current_active_target"] is None


def test_unknown_cancel_or_amend_does_not_become_terminal_from_original_row():
    for command in ("cancel", "amend"):
        result = reconcile_order_chain(
            [order(cancelled=10)],
            "001",
            unresolved_commands=[{"kind": command, "state": "unknown"}],
        )
        assert result["status"] == "unknown"
        assert result["remaining_quantity"] is None
        assert "UNRESOLVED_ORDER_COMMAND" in result["reasons"]


@pytest.mark.parametrize(
    "rows,receipts,reason",
    [
        ([order(cancelled=10), order("002", "001")], [], "UNCONFIRMED_ORDER_CHILD"),
        ([order()], [receipt("002")], "CONFIRMED_REPLACEMENT_NOT_IN_GRAPH"),
        ([order(), order(filled=1)], [], "CONFLICTING_ORDER_VERSIONS"),
        ([order(parent="002"), order("002", "001")], [], "ORDER_GRAPH_CYCLE"),
        ([order(quantity=10, remaining=9)], [], "NODE_QUANTITIES_DO_NOT_BALANCE"),
        ([order(filled_amount="NaN")], [], "INVALID_NODE_FILLED_AMOUNT"),
        ([order(quantity=True)], [], "INVALID_NODE_QUANTITY"),
        (
            [order(cancelled=5), order("002", "001", quantity=5)],
            [receipt("002")],
            "MULTIPLE_ACTIVE_CHAIN_TARGETS",
        ),
        (
            [order(filled=10), order("002", "001", quantity=10)],
            [receipt("002")],
            "REPLACEMENT_TRANSFER_NOT_IN_PARENT_CANCEL_TOTAL",
        ),
        (
            [order(cancelled=10), order("002", "001", symbol="005930")],
            [receipt("002")],
            "CHAIN_SCOPE_MISMATCH",
        ),
    ],
)
def test_uncertain_graphs_or_quantity_models_fail_closed(rows, receipts, reason):
    result = reconcile_order_chain(
        rows, "001", confirmed_amend_receipts=receipts, semantics_verified=True
    )
    assert result["status"] == "unknown"
    assert reason in result["reasons"]
    assert result["current_active_target"] is None


def test_unrelated_external_order_does_not_join_chain_or_change_totals():
    result = reconcile_order_chain([order(filled=2), order("777", "000", quantity=200)], "001")
    assert result["chain_order_ids"] == ["001"]
    assert result["filled_quantity"] == 2


def test_duplicate_identical_page_record_is_idempotent():
    row = order(filled=2)
    assert reconcile_order_chain([row, dict(row)], "001")["filled_quantity"] == 2
