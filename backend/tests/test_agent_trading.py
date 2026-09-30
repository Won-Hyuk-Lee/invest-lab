from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from invest_lab.adapters.http import create_app
from invest_lab.adapters.runtime import Runtime, build_runtime
from invest_lab.adapters.runtime_store import RuntimeStore
from invest_lab.adapters.simulated_broker import SimulatedBroker
from invest_lab.domain.errors import TradingBlocked, VersionConflict
from invest_lab.worker import Worker


@pytest.fixture
def local(workspace, tmp_path):
    store = RuntimeStore(tmp_path / "runtime.db")
    broker = SimulatedBroker(store, workspace.market, workspace.clock)
    return Runtime(workspace, store, broker, execution_only=True)


def configure(local, **changes):
    return local.agent.configure(
        {
            "expected_version": local.agent.status()["control"]["version"],
            "enabled": True,
            "allowed_symbols": ["005930"],
            "max_order_value": "1000000",
            "max_position_pct": 20,
            "max_invested_pct": 50,
            **changes,
        }
    )


def signal(local, **changes):
    return {
        "client_request_id": str(uuid4()),
        "symbol": "005930",
        "side": "buy",
        "quantity": 1,
        "order_type": "market",
        "limit_price": None,
        "source": "fixture-agent",
        "reason": "판단 근거",
        "expires_at": (local.workspace.clock.now() + timedelta(minutes=2)).isoformat(),
        **changes,
    }


def test_default_workspace_is_execution_only_and_external_orders_are_disabled(
    workspace, tmp_path, monkeypatch
):
    monkeypatch.delenv("INVEST_WORKSPACE", raising=False)
    monkeypatch.setenv("INVEST_STATE_DIR", str(tmp_path))
    runtime = build_runtime(workspace, root=tmp_path, mode="local_simulation")
    assert runtime.execution_only
    assert runtime.store.document("screen") is None
    assert not runtime.agent.status()["control"]["enabled"]
    assert runtime.auto_trade()["submitted"] == 0


def test_ai_signal_preview_buy_fill_sell_and_durable_retries(local):
    configure(local)
    buy = signal(local)
    assert not local.agent.preview(buy)["submitted"]
    assert local.store.all_orders() == []
    order = local.agent.submit(buy)
    assert order["state"] == "registered"
    for _ in range(3):
        assert local.agent.submit(buy)["id"] == order["id"]
    assert len([j for j in local.store.jobs() if j["kind"] == "submit_order"]) == 1
    Worker(local).run(once=True)
    assert local.agent.find(buy["client_request_id"])["state"] == "filled"
    sell = signal(local, side="sell")
    local.agent.submit(sell)
    Worker(local).run(once=True)
    assert local.agent.find(sell["client_request_id"])["state"] == "filled"
    assert local.broker.account()["positions"] == []
    assert len(local.store.all_orders()) == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "069500"},
        {"quantity": 100000},
        {"expires_at": "2026-09-25T00:00:00+00:00"},
        {"expires_at": "2026-09-25T00:06:00+00:00"},
    ],
)
def test_disallowed_symbol_budget_expiry_and_future_expiry_never_queue(local, changes):
    configure(local)
    with pytest.raises(TradingBlocked):
        local.agent.submit(signal(local, **changes))
    assert local.store.all_orders() == []
    assert local.store.jobs() == []


def test_default_stop_and_paused_preview(local):
    with pytest.raises(TradingBlocked):
        local.agent.submit(signal(local))
    configure(local, enabled=False)
    assert not local.agent.preview(signal(local))["execution_enabled"]
    assert local.store.all_orders() == []


@pytest.mark.parametrize("bound", [None, "2000000"])
def test_external_market_order_uses_upper_limit_for_notional_cap(local, monkeypatch, bound):
    configure(local)
    monkeypatch.setattr(local.store, "mode", "kis_live")
    quote = local.broker.quote("005930")
    monkeypatch.setattr(local.execution, "_quote", lambda symbol: {**quote, "upper_limit": bound})
    with pytest.raises(TradingBlocked):
        local.agent.submit(signal(local))
    assert local.store.all_orders() == []
    assert local.store.jobs() == []


def test_stop_remains_available_if_symbol_is_removed_from_master(local, monkeypatch):
    configure(local)
    monkeypatch.setattr(local.execution, "instruments", lambda: [])
    assert configure(local, enabled=False)["enabled"] is False


def test_changed_reason_or_payload_cannot_reuse_identity(local):
    configure(local)
    value = signal(local)
    local.agent.submit(value)
    for change in ({"reason": "다른 근거"}, {"quantity": 2}, {"source": "another-agent"}):
        with pytest.raises(VersionConflict):
            local.agent.submit({**value, **change})
    assert len(local.store.all_orders()) == 1


@pytest.mark.parametrize("gate", ["stop", "change", "expiry", "entries", "budget", "risk"])
def test_queued_signal_rechecks_controls_and_account_before_broker_call(local, monkeypatch, gate):
    configure(local)
    order = local.agent.submit(signal(local))
    if gate in {"stop", "change"}:
        configure(local, enabled=gate != "stop")
    elif gate == "expiry":
        clock = local.workspace.clock
        original = clock.now()
        monkeypatch.setattr(clock, "now", lambda: original + timedelta(minutes=3))
    elif gate == "entries":
        local.control(False)
    elif gate == "budget":
        original = local.broker.quote
        monkeypatch.setattr(
            local.broker, "quote", lambda symbol: {**original(symbol), "price": "2000000"}
        )
    else:
        # Matching policy version with a strict risk limit must still reach normal risk checks.
        context = order["context"]
        context["risk_policy"]["max_position_pct"] = 0.000001
        local.store.transition(
            order["id"], {"registered"}, "registered", local.at(), context=context
        )
    with pytest.raises(TradingBlocked):
        local.execution.submit(order["id"])
    assert local.broker.orders("20260925", "20260925") == []


def test_response_loss_preserves_one_order_and_unknown_is_not_resent(local):
    configure(local)
    value = signal(local)
    order = local.agent.submit(value)
    local.broker.timeout_after_accept = True
    assert local.execution.submit(order["id"])["state"] == "unknown"
    assert local.agent.submit(value)["state"] == "unknown"
    assert local.execution.submit(order["id"])["state"] == "unknown"
    assert len(local.broker.orders("20260925", "20260925")) == 1
    local.execution.reconcile()
    assert local.agent.find(value["client_request_id"])["state"] == "filled"


def test_legacy_enabled_deployment_and_queued_research_cannot_trade(local):
    deployment = local.store.document("deployment")
    local.store.put_document("deployment", {**deployment, "enabled": True})
    Worker(local).schedule()
    assert all(j["kind"] == "reconcile" for j in local.store.jobs())
    with pytest.raises(TradingBlocked):
        local.run_job({"kind": "auto_trade", "payload": {}})
    assert local.store.all_orders() == []


def test_http_contract_validation_and_no_strategy_dependencies(local):
    configure(local)
    client = TestClient(create_app(local.workspace, local))
    assert client.get("/api/trading").json()["execution_only"]
    assert client.get("/api/trading/instruments").json()
    assert client.get("/api/trading/quotes/005930").status_code == 200
    value = signal(local)
    assert client.post("/api/trading/signals/preview", json=value).status_code == 200
    assert client.post("/api/trading/signals", json={**value, "quantity": True}).status_code == 422
    assert (
        client.post("/api/trading/signals", json={**value, "strategy_version": 1}).status_code
        == 422
    )
    assert client.post("/api/trading/signals", json=value).status_code == 202
    assert client.get(f"/api/trading/signals/{value['client_request_id']}").status_code == 200
    assert len(local.store.all_orders()) == 1
