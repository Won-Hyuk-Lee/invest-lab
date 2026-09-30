from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from invest_lab.adapters.http import create_app
from invest_lab.adapters.runtime import build_runtime
from invest_lab.worker import Worker


@pytest.fixture
def runtime(workspace, tmp_path, monkeypatch):
    monkeypatch.delenv("INVEST_STATE_DIR", raising=False)
    # These tests exercise the archived research workspace, not the new default gateway.
    monkeypatch.setenv("INVEST_WORKSPACE", "research")
    return build_runtime(workspace, root=tmp_path, mode="local_simulation")


def drain(runtime):
    worker = Worker(runtime)
    for _ in range(30):
        if not worker.tick():
            return
    pytest.fail("job queue did not drain")


def test_full_http_order_lifecycle_with_durable_worker(runtime):
    with TestClient(create_app(runtime.workspace, runtime)) as client:
        for route in (
            "context",
            "screener",
            "portfolio",
            "operations",
            "watchlist",
            "reports",
            "research",
            "deployment",
        ):
            response = client.get("/api/" + route)
            assert response.status_code == 200, (route, response.text)
        payload = {
            "client_request_id": str(uuid4()),
            "symbol": "005930",
            "side": "buy",
            "quantity": 1,
            "order_type": "market",
        }
        created = client.post("/api/order-intents", json=payload)
        assert created.status_code == 201, created.text
        order = created.json()
        queued = client.post(f"/api/order-intents/{order['id']}/submit").json()
        assert queued["state"] == "registered"
        assert queued["pending_commands"][0]["state"] == "queued"
        drain(runtime)
        final = client.get(f"/api/orders/{order['id']}").json()
        assert final["state"] == "filled"
        assert client.get("/api/portfolio").json()["positions"][0]["quantity"] == 1
        repeated = client.post("/api/order-intents", json=payload).json()
        assert repeated["id"] == order["id"]
        assert client.post("/api/order-intents", json={**payload, "quantity": 2}).status_code == 409
        assert client.get("/api/orders").json()["total"] == 1


def test_durable_cancel_queue_recovery_and_public_schema(runtime):
    with TestClient(create_app(runtime.workspace, runtime)) as client:
        order = client.post(
            "/api/order-intents",
            json={
                "client_request_id": str(uuid4()),
                "symbol": "005930",
                "side": "buy",
                "quantity": 3,
                "order_type": "limit",
                "limit_price": "10000",
            },
        ).json()
        client.post(f"/api/order-intents/{order['id']}/submit")
        drain(runtime)
        command = {"client_request_id": str(uuid4()), "quantity": 3}
        queued = client.post(f"/api/orders/{order['id']}/cancel", json=command)
        assert queued.status_code == 200, queued.text
        assert any(
            c["id"] == command["client_request_id"] for c in queued.json()["pending_commands"]
        )
        assert client.post(f"/api/orders/{order['id']}/cancel", json=command).status_code == 200
        drain(runtime)
        assert client.get(f"/api/orders/{order['id']}").json()["state"] == "cancelled"
        schema = client.get("/openapi.json").json()
        for route in (
            "/api/screener",
            "/api/portfolio",
            "/api/orders",
            "/api/context",
            "/api/research",
        ):
            assert schema["paths"][route]["get"]["responses"]["200"]["content"]["application/json"][
                "schema"
            ]


def test_deployment_version_preserved_and_disabled_by_default(runtime):
    with TestClient(create_app(runtime.workspace, runtime)) as client:
        deployment = client.get("/api/deployment").json()
        assert deployment["enabled"] is False
        payload = {
            "expected_version": deployment["version"],
            "enabled": False,
            "strategy_type": "etf_trend",
            "rules": deployment["rules"],
            "model_id": None,
        }
        assert client.post("/api/deployment", json=payload).status_code == 200
        assert client.post("/api/deployment", json=payload).status_code == 409
        assert runtime.run_job({"kind": "auto_trade", "payload": {}})["submitted"] == 0


def test_source_keys_missing_fail_without_network(runtime):
    with pytest.raises(ValueError, match="DART_API_KEY"):
        runtime.collect_source({"provider": "dart", "operation": "companies"})


def test_interrupted_jobs_are_not_blindly_reexecuted(runtime):
    job = runtime.store.enqueue("submit_order", {"order_id": str(uuid4())})
    claimed = runtime.store.claim_job("dead-worker")
    assert claimed["id"] == job["id"]
    runtime.store.recover_jobs()
    assert runtime.store.jobs()[0]["state"] == "interrupted"
    assert runtime.store.claim_job("new-worker") is None


def test_account_external_mutation_causes_reconciliation_mismatch(runtime):
    runtime.execution.reconcile()
    runtime.broker.place_order(
        {
            "symbol": "005930",
            "side": "buy",
            "quantity": 1,
            "order_type": "market",
            "limit_price": None,
        }
    )
    result = runtime.execution.reconcile()
    assert result["status"] == "mismatch"
    assert len(result["external_orders"]) == 1
    assert runtime.store.document("allocation", "005930") is None
