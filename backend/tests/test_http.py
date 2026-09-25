from dataclasses import asdict
from uuid import uuid4

from fastapi.testclient import TestClient

from invest_lab.adapters.http import create_app


def test_read_api_and_explicit_sample_mode(workspace):
    with TestClient(create_app(workspace)) as client:
        health = client.get("/api/health").json()
        assert health["source"] == "synthetic_demo"
        assert health["live_orders_enabled"] is False
        assert health["broker_connected"] is False
        screen = client.get("/api/screener").json()
        assert screen["period_basis"] == "trading_sessions"
        assert len(screen["rows"]) == 10
        assert {row["kind"] for row in screen["rows"]} == {"kospi", "etf_kr", "etf_us"}
        assert client.get("/api/instruments/005930").status_code == 200
        assert client.get("/api/instruments/999999").status_code == 404
        assert client.post("/api/live/orders").status_code == 404


def test_local_security_boundary_and_input_validation(workspace):
    body = {"symbol": "005930", "quantity": 1, "request_id": str(uuid4())}
    with TestClient(create_app(workspace)) as client:
        assert (
            client.post(
                "/api/paper/buys", json=body, headers={"Origin": "https://unrelated.example"}
            ).status_code
            == 403
        )
        assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 400
        for invalid in (0, -1, 1.5, True, "1"):
            assert (
                client.post("/api/paper/buys", json={**body, "quantity": invalid}).status_code
                == 422
            )
        response = client.post(
            "/api/paper/buys", json=body, headers={"Origin": "http://127.0.0.1:5173"}
        )
        assert response.status_code == 201
        assert response.headers["cache-control"] == "no-store"


def test_strategy_conflict_is_409(workspace):
    body = asdict(workspace.strategies.latest())
    body["expected_version"] = body.pop("version")
    with TestClient(create_app(workspace)) as client:
        assert client.post("/api/strategies", json=body).status_code == 201
        assert client.post("/api/strategies", json=body).status_code == 409
