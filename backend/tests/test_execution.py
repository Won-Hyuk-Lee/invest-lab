from dataclasses import asdict, replace
from decimal import Decimal
from uuid import uuid4

import pytest

from invest_lab.adapters.runtime_store import RuntimeStore
from invest_lab.adapters.simulated_broker import SimulatedBroker
from invest_lab.application.execution import ExecutionService
from invest_lab.domain.errors import TradingBlocked, VersionConflict
from invest_lab.domain.indicators import validate_bars
from invest_lab.domain.models import Strategy
from invest_lab.domain.orders import OrderRequest, valid_price


@pytest.fixture
def execution(workspace, tmp_path):
    store = RuntimeStore(tmp_path / "runtime.db")
    store.put_document("control", {"entries_enabled": True})
    broker = SimulatedBroker(store, workspace.market, workspace.clock)
    service = ExecutionService(
        store,
        broker,
        workspace.clock,
        instruments=workspace.market.instruments,
        strategy=workspace.strategies.latest,
    )
    return service, broker, store


def request(side="buy", quantity=1, price=None):
    return dict(
        client_request_id=str(uuid4()),
        symbol="005930",
        side=side,
        quantity=quantity,
        order_type="market" if price is None else "limit",
        limit_price=price,
    )


def test_buy_fill_sell_round_trip_uses_broker_not_receipt(execution):
    service, broker, store = execution
    buy = service.register(request())
    ack = service.submit(buy["id"])
    assert ack["state"] == "acknowledged"
    assert ack["filled_quantity"] == 0
    assert service.reconcile()["status"] == "matched"
    assert store.get_order(buy["id"])["state"] == "filled"
    sell = service.register(request("sell"))
    service.submit(sell["id"])
    service.reconcile()
    assert store.get_order(sell["id"])["state"] == "filled"
    assert broker.account()["positions"] == []
    assert Decimal(broker.account()["cash"]) < Decimal("10000000")


def test_response_loss_never_resubmits_and_recovers(execution):
    service, broker, store = execution
    intent = service.register(request())
    broker.timeout_after_accept = True
    assert service.submit(intent["id"])["state"] == "unknown"
    for _ in range(3):
        assert service.submit(intent["id"])["state"] == "unknown"
    assert len(broker.orders("20260925", "20260925")) == 1
    assert service.reconcile()["status"] == "matched"
    assert store.get_order(intent["id"])["filled_quantity"] == 1
    assert broker.account()["positions"][0]["quantity"] == 1


def test_intent_identity_survives_fresh_service_and_market_failure(execution):
    service, broker, store = execution
    payload = request()
    first = service.register(payload)
    service.submit(first["id"])
    broker.quote = lambda _: (_ for _ in ()).throw(RuntimeError("network"))
    restarted = ExecutionService(
        RuntimeStore(store.path), broker, service.clock, instruments=lambda: []
    )
    assert restarted.register(payload)["id"] == first["id"]
    assert restarted.submit(first["id"])["state"] == "acknowledged"
    with pytest.raises(VersionConflict):
        restarted.register({**payload, "quantity": 2})


def test_partial_fill_then_partial_cancel_reconciles_cumulative(execution):
    service, broker, store = execution
    intent = service.register(request(quantity=5, price="10000"))
    receipt = service.submit(intent["id"])
    broker.fill_order(receipt["broker_order_id"], 2)
    service.reconcile()
    order = store.get_order(intent["id"])
    assert (order["state"], order["filled_quantity"], order["remaining_quantity"]) == (
        "partially_filled",
        2,
        3,
    )
    service.cancel(intent["id"], str(uuid4()), 3)
    service.reconcile()
    order = store.get_order(intent["id"])
    assert (order["state"], order["filled_quantity"], order["cancelled_quantity"]) == (
        "cancelled",
        2,
        3,
    )
    before = broker.account()
    service.reconcile()
    assert broker.account() == before


def test_price_amendment_and_command_replay(execution):
    service, broker, store = execution
    intent = service.register(request(quantity=3, price="10000"))
    service.submit(intent["id"])
    command_id = str(uuid4())
    service.cancel(intent["id"], command_id, 3, "11000")
    assert service.cancel(intent["id"], command_id, 3, "11000")["submitted_price"] == "11000"
    with pytest.raises(TradingBlocked):
        service.cancel(intent["id"], command_id, 3, "12000")
    assert broker.orders("20260925", "20260925")[0]["limit_price"] == "11000"


def test_unknown_blocks_next_entry_and_external_identical_orders_stay_ambiguous(execution):
    service, broker, store = execution
    payload = request()
    intent = service.register(payload)
    broker.timeout_after_accept = True
    service.submit(intent["id"])
    broker.place_order(
        {k: payload[k] for k in ("symbol", "side", "quantity", "order_type", "limit_price")}
    )
    assert service.reconcile()["unresolved"] == [intent["id"]]
    next_order = service.register(request())
    with pytest.raises(TradingBlocked, match="미확정"):
        service.submit(next_order["id"])


def test_mode_scope_and_cursor_more_than_100_orders(workspace, tmp_path):
    path = tmp_path / "shared.db"
    sim, paper = RuntimeStore(path), RuntimeStore(path, "kis_paper", "masked")
    payload = request()
    sim.create_intent(payload, "2026-01-01T01:00:00+00:00", {})
    assert paper.find_intent(payload["client_request_id"]) is None
    for i in range(120):
        sim.create_intent(request(), "2026-01-01T01:00:00+00:00", {})
    pages, cursor = [], None
    while True:
        response = sim.list_orders(limit=25, cursor=cursor)
        assert response["total"] == 121
        pages.extend(o["id"] for o in response["items"])
        cursor = response["next_cursor"]
        if cursor is None:
            break
    assert len(pages) == len(set(pages)) == 121


def test_execution_lock_does_not_steal_on_old_worker_resume(execution):
    _, _, store = execution
    with store.execution_lock() as generation:
        with pytest.raises(TradingBlocked):
            with RuntimeStore(store.path).execution_lock():
                pytest.fail("stole live execution lock")
        store.assert_generation(generation)
    with store.execution_lock() as newer:
        assert newer > generation
        with pytest.raises(TradingBlocked):
            store.assert_generation(generation)


def test_backup_restores_intents_and_not_reexecution(execution, tmp_path):
    service, broker, store = execution
    intent = service.register(request())
    service.submit(intent["id"])
    dest = tmp_path / "backups" / "snapshot.db"
    store.backup(dest)
    restored = RuntimeStore(dest)
    assert restored.get_order(intent["id"])["state"] == "acknowledged"
    assert (
        ExecutionService(restored, broker, service.clock, instruments=lambda: []).submit(
            intent["id"]
        )["state"]
        == "acknowledged"
    )


def test_registered_order_session_and_entry_controls(execution):
    service, _, store = execution
    intent = service.register(request())
    store.put_document("control", {"entries_enabled": False})
    with pytest.raises(TradingBlocked, match="중지"):
        service.submit(intent["id"])
    store.put_document("control", {"entries_enabled": True})
    service.session_check = lambda at: False
    with pytest.raises(TradingBlocked, match="정규장"):
        service.submit(intent["id"])
    assert store.get_order(intent["id"])["state"] == "registered"


def test_a06_finite_integer_volume_and_immutable_weights(workspace):
    bars = workspace.market.history("005930")
    for bad in (True, 1.2, float("nan"), float("inf"), -1):
        with pytest.raises(ValueError):
            validate_bars([replace(bars[0], volume=bad)])
    weights = dict(Strategy().weights)
    strategy = Strategy(weights=weights)
    weights["trend"] = 99
    assert strategy.weights["trend"] == 25
    with pytest.raises(TypeError):
        strategy.weights["trend"] = 99
    assert sum(asdict(strategy)["weights"].values()) == 100


def test_a02_existing_result_requires_no_market_and_a04_no_unheld_quote(workspace):
    original = workspace.market.quote
    calls = []

    def quote(symbol):
        calls.append(symbol)
        if symbol != "005930":
            raise RuntimeError("unrelated provider failure")
        return original(symbol)

    workspace.market.quote = quote
    ident = str(uuid4())
    first = workspace.buy("005930", 1, ident)
    assert calls == ["005930"]
    workspace.market.quote = lambda _: (_ for _ in ()).throw(RuntimeError("down"))
    assert workspace.buy("005930", 1, ident) == first


def test_price_tick_limits_and_alphanumeric_symbol():
    assert valid_price("10003", "5", "7000", "13000", "buy") == Decimal("10000")
    assert valid_price("10003", "5", "7000", "13000", "sell") == Decimal("10005")
    with pytest.raises(TradingBlocked):
        valid_price("13001", "5", "7000", "13000", "sell")
    assert OrderRequest(**{**request(), "symbol": "0048K0"}).symbol == "0048K0"


def test_seoul_trading_date_across_utc_midnight(workspace, tmp_path):
    from datetime import UTC, datetime

    from invest_lab.adapters.demo import DemoMarket

    class NightClock:
        def now(self):
            return datetime(2026, 9, 26, 23, 30, tzinfo=UTC)

    clock = NightClock()
    store = RuntimeStore(tmp_path / "night.db")
    store.put_document("control", {"entries_enabled": True})
    market = DemoMarket(clock)
    broker = SimulatedBroker(store, market, clock)
    service = ExecutionService(store, broker, clock, instruments=market.instruments)
    order = service.register(request())
    service.submit(order["id"])
    assert service.reconcile()["status"] == "matched"
    assert store.get_order(order["id"])["trading_date"] == "2026-09-27"
    assert store.get_order(order["id"])["state"] == "filled"


def test_cancel_response_loss_stays_unknown_until_command_evidence(execution):
    service, broker, store = execution
    order = service.register(request(price="10000"))
    service.submit(order["id"])
    cancel = broker.cancel_order

    def lose(*args):
        cancel(*args)
        raise TimeoutError("lost receipt")

    broker.cancel_order = lose
    service.cancel(order["id"], str(uuid4()), 1)
    assert service.reconcile()["status"] == "pending"
    assert store.get_order(order["id"])["state"] == "unknown"


def test_account_staleness_and_registered_session_expiry_block(execution):
    service, broker, store = execution
    original = broker.account
    broker.account = lambda: {**original(), "observed_at": "2020-01-01T00:00:00+00:00"}
    order = service.register(request())
    with pytest.raises(TradingBlocked, match="계좌"):
        service.submit(order["id"])
    broker.account = original
    stale = {**order["context"], "valid_session": "2020-01-01"}
    store.transition(order["id"], {"registered"}, "registered", service.at(), context=stale)
    with pytest.raises(TradingBlocked, match="거래일"):
        service.submit(order["id"])
