"""Conservative projection of an order and its confirmed replacement chain.

KIS's daily order schema does not define whether parent cancellation totals include
replacement transfers or whether fill totals are per node. The multi-node model below
must be enabled only after market-session evidence verifies both semantics. A unique
graph alone is not evidence of those accounting rules.
"""

from decimal import Decimal, InvalidOperation


def reconcile_order_chain(
    rows,
    root_order_id,
    *,
    confirmed_amend_receipts=(),
    unresolved_commands=(),
    semantics_verified=False,
):
    """Return a safe projection or ``status='unknown'`` with machine-readable reasons.

    Caller must scope rows to one broker/account/mode/trading date. Supported verified
    model: each node's quantities balance independently; fills/amounts are per node;
    a replacement's original quantity is included in its parent's cancellation total.
    The parent cancellation representing that transfer is subtracted exactly once.

    Receipts identify confirmed amendment order numbers. Unconfirmed commands remain
    unknown rather than being inferred successful from an unrelated cancellation row.
    Unknown results expose *no* aggregate quantities or executable current target.
    """
    reasons, trace, index = [], [], {}
    result = {
        "status": "unknown",
        "root_order_id": root_order_id,
        "current_active_target": None,
        "filled_quantity": None,
        "filled_amount": None,
        "fee": None,
        "cancelled_quantity": None,
        "rejected_quantity": None,
        "remaining_quantity": None,
        "chain_order_ids": [],
        "reasons": reasons,
        "trace": trace,
        "semantics": "per_order_with_transfer_cancel" if semantics_verified else "unverified",
    }

    def unknown(reason):
        if reason not in reasons:
            reasons.append(reason)
        return result

    if not isinstance(root_order_id, str) or not root_order_id:
        return unknown("INVALID_ROOT_IDENTIFIER")
    for value in rows:
        if not isinstance(value, dict):
            return unknown("INVALID_ORDER_RECORD")
        row = dict(value)
        identifier = row.get("broker_order_id")
        if not isinstance(identifier, str) or not identifier:
            return unknown("INVALID_ORDER_IDENTIFIER")
        if identifier in index and index[identifier] != row:
            return unknown("CONFLICTING_ORDER_VERSIONS")
        index[identifier] = row
    if root_order_id not in index:
        return unknown("ROOT_ORDER_NOT_FOUND")
    children = {}
    for identifier, row in index.items():
        parent = row.get("original_order_id")
        if parent and parent != identifier:
            children.setdefault(parent, set()).add(identifier)
    connected, visiting = set(), set()

    def visit(identifier):
        if identifier in visiting:
            return False
        if identifier in connected:
            return True
        visiting.add(identifier)
        for child in children.get(identifier, ()):
            if not visit(child):
                return False
        visiting.remove(identifier)
        connected.add(identifier)
        return True

    if not visit(root_order_id):
        return unknown("ORDER_GRAPH_CYCLE")
    result["chain_order_ids"] = sorted(connected)
    root = index[root_order_id]
    if root.get("original_order_id") in connected - {root_order_id}:
        return unknown("ORDER_GRAPH_CYCLE")
    for identifier in sorted(connected):
        row = index[identifier]
        if any(row.get(field) != root.get(field) for field in ("symbol", "side", "trading_date")):
            return unknown("CHAIN_SCOPE_MISMATCH")
        for field in ("quantity", "filled_quantity", "cancelled_quantity", "remaining_quantity"):
            if type(row.get(field)) is not int or row[field] < 0:
                return unknown("INVALID_NODE_QUANTITY")
        if type(row.get("rejected_quantity", 0)) is not int or row.get("rejected_quantity", 0) < 0:
            return unknown("INVALID_NODE_QUANTITY")
        if row["quantity"] == 0:
            return unknown("ZERO_NODE_QUANTITY")
        try:
            amount = Decimal(str(row["filled_amount"]))
            if (
                isinstance(row["filled_amount"], bool)
                or not amount.is_finite()
                or amount < 0
                or bool(row["filled_quantity"]) != bool(amount)
            ):
                return unknown("INVALID_NODE_FILLED_AMOUNT")
        except (KeyError, InvalidOperation, ValueError):
            return unknown("INVALID_NODE_FILLED_AMOUNT")
        parts = sum(
            row.get(field, 0)
            for field in (
                "filled_quantity",
                "cancelled_quantity",
                "remaining_quantity",
                "rejected_quantity",
            )
        )
        if parts != row["quantity"]:
            return unknown("NODE_QUANTITIES_DO_NOT_BALANCE")
        trace.append(
            {
                "order_id": identifier,
                "parent_id": row.get("original_order_id"),
                "quantity": row["quantity"],
                "filled": row["filled_quantity"],
                "cancelled_reported": row["cancelled_quantity"],
                "rejected": row.get("rejected_quantity", 0),
                "remaining": row["remaining_quantity"],
                "filled_amount": str(amount),
            }
        )
    if unresolved_commands:
        return unknown("UNRESOLVED_ORDER_COMMAND")
    receipts = {}
    for receipt in confirmed_amend_receipts:
        if not isinstance(receipt, dict) or not isinstance(receipt.get("broker_order_id"), str):
            return unknown("INVALID_AMENDMENT_RECEIPT")
        identifier = receipt["broker_order_id"]
        if identifier not in index or identifier not in connected:
            return unknown("CONFIRMED_REPLACEMENT_NOT_IN_GRAPH")
        if receipt.get("branch_id") and receipt["branch_id"] != index[identifier].get("branch_id"):
            return unknown("REPLACEMENT_BRANCH_MISMATCH")
        parent = receipt.get("original_order_id")
        if parent is not None and parent != index[identifier].get("original_order_id"):
            return unknown("REPLACEMENT_PARENT_MISMATCH")
        receipts[identifier] = receipt
    if connected - {root_order_id} - receipts.keys():
        return unknown("UNCONFIRMED_ORDER_CHILD")
    if (len(connected) > 1 or receipts) and not semantics_verified:
        return unknown("AMENDMENT_ACCOUNTING_NOT_VERIFIED")
    transfers = {}
    for identifier in connected:
        transfer = sum(index[child]["quantity"] for child in children.get(identifier, ()))
        if transfer > index[identifier]["cancelled_quantity"]:
            return unknown("REPLACEMENT_TRANSFER_NOT_IN_PARENT_CANCEL_TOTAL")
        transfers[identifier] = transfer
    filled = sum(index[key]["filled_quantity"] for key in connected)
    amount = sum((Decimal(str(index[key]["filled_amount"])) for key in connected), Decimal(0))
    cancelled = sum(index[key]["cancelled_quantity"] - transfers[key] for key in connected)
    rejected = sum(index[key].get("rejected_quantity", 0) for key in connected)
    remaining = sum(index[key]["remaining_quantity"] for key in connected)
    if filled + cancelled + rejected + remaining != root["quantity"]:
        return unknown("CHAIN_QUANTITIES_DO_NOT_BALANCE")
    active = [index[key] for key in connected if index[key]["remaining_quantity"] > 0]
    if len(active) > 1:
        return unknown("MULTIPLE_ACTIVE_CHAIN_TARGETS")
    for row in trace:
        row["replacement_transfer"] = transfers[row["order_id"]]
        row["true_cancelled"] = row["cancelled_reported"] - row["replacement_transfer"]
    if remaining:
        state = "partially_filled" if filled else "acknowledged"
    elif filled == root["quantity"]:
        state = "filled"
    elif cancelled:
        state = "cancelled"
    else:
        state = "rejected"
    result.update(
        status=state,
        filled_quantity=filled,
        filled_amount=format(amount, "f"),
        cancelled_quantity=cancelled,
        rejected_quantity=rejected,
        remaining_quantity=remaining,
        current_active_target=dict(active[0]) if active else None,
    )
    # Absence is unknown, not zero. Fees may only be added under the same verified
    # per-node semantics as fills; a single-order projection needs no chain assumption.
    if all(index[key].get("fee") is not None for key in connected):
        try:
            fees = [Decimal(str(index[key]["fee"])) for key in connected]
            if all(value.is_finite() and value >= 0 for value in fees):
                result["fee"] = format(sum(fees, Decimal(0)), "f")
        except InvalidOperation:
            pass
    return result
