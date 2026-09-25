from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from invest_lab.adapters.sqlite import VersionConflict


def test_purchase_updates_cash_cost_and_holdings_atomically(workspace):
    order = workspace.buy("005930", 1, str(uuid4()))
    account = workspace.account()
    assert len(account["orders"]) == 1
    assert account["positions"][0]["quantity"] == 1
    assert Decimal(account["cash"]) == (
        Decimal("10000000") - Decimal(order["price"]) - Decimal(order["fee"])
    )
    assert Decimal(account["equity"]) == Decimal("10000000") - Decimal(order["fee"])
    assert Decimal(account["profit"]) == -Decimal(order["fee"])


def test_concurrent_retry_cannot_double_charge(workspace):
    request_id = str(uuid4())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: workspace.buy("005930", 1, request_id), range(4)))
    assert all(result == results[0] for result in results)
    assert workspace.account()["positions"][0]["quantity"] == 1
    assert len(workspace.account()["orders"]) == 1
    with pytest.raises(ValueError, match="식별자"):
        workspace.buy("005930", 2, request_id)


def test_position_limit_rejection_rolls_back_everything(workspace):
    before = workspace.account()
    price = workspace.market.quote("005930").price
    quantity = int(Decimal("2100000") / price) + 1
    with pytest.raises(ValueError, match="비중"):
        workspace.buy("005930", quantity, str(uuid4()))
    assert workspace.account() == before


def test_insufficient_cash_rejection(workspace):
    with pytest.raises(ValueError, match="현금"):
        workspace.buy("005930", 100000, str(uuid4()))
    assert workspace.account()["cash"] == "10000000"


def test_stale_or_future_quote_rejected(workspace):
    quote = workspace.market.quote("005930")
    for offset in (-301, 1):
        workspace.market.quote = lambda _: replace(
            quote, observed_at=workspace.clock.now() + timedelta(seconds=offset)
        )
        with pytest.raises(ValueError, match="시세"):
            workspace.buy("005930", 1, str(uuid4()))
    assert not workspace.account()["orders"]


def test_strategy_versions_are_immutable_and_optimistic(workspace):
    original = workspace.strategies.latest()
    values = asdict(original)
    values["name"] = "새 가설"
    assert workspace.save_strategy(values, 1).version == 2
    assert workspace.strategies.versions()[1] == original
    with pytest.raises(VersionConflict):
        workspace.save_strategy(values, 1)
    assert len(workspace.strategies.versions()) == 2
    assert all(row["strategy_version"] == 2 for row in workspace.screen()["rows"])
