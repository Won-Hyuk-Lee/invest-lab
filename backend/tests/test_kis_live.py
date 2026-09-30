import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from invest_lab.adapters.http import create_app
from invest_lab.adapters.kis import AmbiguousOrder, KisLiveClient, KisPaperClient
from invest_lab.adapters.runtime import Runtime, build_runtime
from invest_lab.adapters.runtime_store import RuntimeStore
from invest_lab.adapters.settings import LIVE_BASE_URL, KisSettings, SettingsError

NOW = datetime(2026, 9, 30, 1, tzinfo=timezone.utc)


def settings(tmp_path, **changes):
    return KisSettings.from_mapping(
        {
            "KIS_LIVE_APP_KEY": "live-fixture-key",
            "KIS_LIVE_APP_SECRET": "live-fixture-secret",
            "KIS_LIVE_ACCOUNT_NUMBER": "01234567",
            "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
            "KIS_LIVE_ORDERS_ENABLED": "true",
            **changes,
        },
        root=tmp_path,
        mode="kis_live",
    )


def live_client(tmp_path, handler, **changes):
    config = settings(tmp_path, **changes)
    calls = []

    def transport(request):
        assert str(request.url).startswith(LIVE_BASE_URL + "/")
        calls.append(request)
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(200, json={"access_token": "fixture-token", "expires_in": 86400})
        return handler(request)

    client = KisLiveClient(
        config, transport=httpx.MockTransport(transport), now=lambda: NOW, sleep=lambda _: None
    )
    # No waiting or actual network during the live endpoint contract checks.
    client.coordinator.wait = lambda _: None
    return client, calls


def receipt(request):
    return httpx.Response(
        200,
        json={
            "rt_cd": "0",
            "output": {
                "ODNO": "0000123",
                "KRX_FWDG_ORD_ORGNO": "00000",
                "ORD_TMD": "100000",
            },
        },
    )


def test_live_credentials_never_fall_back_to_paper_and_default_orders_are_off(tmp_path):
    config = KisSettings.from_mapping(
        {
            "KIS_PAPER_APP_KEY": "paper-key",
            "KIS_PAPER_APP_SECRET": "paper-secret",
            "KIS_PAPER_ORDERS_ENABLED": "true",
        },
        root=tmp_path,
        mode="kis_live",
    )
    assert config.base_url == LIVE_BASE_URL
    assert not config.readiness()["configured"]
    assert not config.orders_enabled
    assert "KIS_LIVE_APP_KEY" in config.readiness()["missing"]
    with pytest.raises(SettingsError):
        config.require(account=True, orders=True)
    with pytest.raises(ValueError):
        KisPaperClient(settings(tmp_path))
    assert "live-fixture-secret" not in repr(settings(tmp_path))


@pytest.mark.parametrize("side,tr_id", [("buy", "TTTC0012U"), ("sell", "TTTC0011U")])
def test_live_buy_sell_use_official_host_transaction_and_krx_cash_payload(tmp_path, side, tr_id):
    client, calls = live_client(tmp_path, receipt)
    result = client.place_order(
        {
            "symbol": "005930",
            "side": side,
            "quantity": 1,
            "order_type": "limit",
            "limit_price": "70000",
        }
    )
    request = calls[-1]
    assert request.headers["tr_id"] == tr_id
    assert request.url.path.endswith("/order-cash")
    body = json.loads(request.content)
    assert body["EXCG_ID_DVSN_CD"] == "KRX"
    assert body["ORD_QTY"] == "1" and body["ORD_UNPR"] == "70000"
    assert result["source"] == "kis_live"
    assert len([r for r in calls if r.url.path.endswith("/order-cash")]) == 1


@pytest.mark.parametrize("price,kind", [(None, "02"), ("70500", "01")])
def test_live_cancel_amend_use_official_transaction(tmp_path, price, kind):
    client, calls = live_client(tmp_path, receipt)
    client.cancel_order(
        {
            "broker_order_id": "0000123",
            "branch_id": "00000",
            "remaining_quantity": 1,
            "order_type": "limit",
        },
        1,
        price,
    )
    assert calls[-1].headers["tr_id"] == "TTTC0013U"
    assert json.loads(calls[-1].content)["RVSE_CNCL_DVSN_CD"] == kind


def test_live_orders_disabled_block_before_auth_or_any_network(tmp_path):
    client, calls = live_client(tmp_path, receipt, KIS_LIVE_ORDERS_ENABLED="false")
    with pytest.raises(SettingsError):
        client.place_order(
            {"symbol": "005930", "side": "buy", "quantity": 1, "order_type": "market"}
        )
    assert calls == []


def test_live_account_quote_and_capacity_use_real_transactions(tmp_path):
    def reads(request):
        if request.url.path.endswith("/inquire-price"):
            return httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "output": {
                        "stck_prpr": "70000",
                        "aspr_unit": "100",
                        "stck_mxpr": "90000",
                        "stck_llam": "50000",
                    },
                },
            )
        if request.url.path.endswith("/inquire-psbl-order"):
            assert request.headers["tr_id"] == "TTTC8908R"
            return httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "output": {
                        "nrcvb_buy_amt": "800000",
                        "nrcvb_buy_qty": "8",
                    },
                },
            )
        assert request.headers["tr_id"] == "TTTC8434R"
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output1": [],
                "output2": [
                    {
                        "dnca_tot_amt": "1000000",
                        "tot_evlu_amt": "1000000",
                        "scts_evlu_amt": "0",
                        "nxdy_excc_amt": "1000000",
                        "prvs_rcdl_excc_amt": "1000000",
                    }
                ],
            },
        )

    client, calls = live_client(tmp_path, reads)
    assert client.quote("005930")["source"] == "kis_live"
    account = client.account()
    assert account["mode"] == "kis_live"
    assert account["cash"] == "1000000" and account["buying_power"] == "800000"
    assert client.capacity("005930", "70000")["quantity"] == 8
    assert all(r.method == "GET" or r.url.path == "/oauth2/tokenP" for r in calls)


def test_live_history_splits_recent_and_old_using_official_transactions(tmp_path):
    def history(request):
        return httpx.Response(200, json={"rt_cd": "0", "output1": []})

    client, calls = live_client(tmp_path, history)
    assert client.orders("2026-01-01", "2026-09-30") == []
    assert [r.headers["tr_id"] for r in calls if r.url.path.endswith("inquire-daily-ccld")] == [
        "CTSC9215R",
        "TTTC0081R",
    ]


def test_live_transport_timeout_never_retries_order_post(tmp_path):
    def lost(request):
        raise httpx.ReadTimeout("fixture response loss")

    client, calls = live_client(tmp_path, lost)
    with pytest.raises(AmbiguousOrder):
        client.place_order(
            {"symbol": "005930", "side": "buy", "quantity": 1, "order_type": "market"}
        )
    assert len([r for r in calls if r.url.path.endswith("/order-cash")]) == 1


def test_paper_live_runtime_namespaces_and_token_scopes_are_separate(tmp_path):
    live, _ = live_client(tmp_path, receipt)
    paper_config = KisSettings.from_mapping(
        {"KIS_PAPER_APP_KEY": "live-fixture-key", "KIS_PAPER_ACCOUNT_NUMBER": "01234567"},
        root=tmp_path,
    )
    paper = KisPaperClient(paper_config)
    assert paper._token_scope != live._token_scope
    assert paper._rate_scope != live._rate_scope
    paper_store = RuntimeStore(tmp_path / "same.db", "kis_paper", "same-account")
    live_store = RuntimeStore(tmp_path / "same.db", "kis_live", "same-account")
    paper_store.put_document("control", {"entries_enabled": True})
    assert live_store.document("control") is None
    paper.close()


def test_live_runtime_build_is_read_only_without_keys_and_disallows_research(
    workspace, tmp_path, monkeypatch
):
    monkeypatch.setenv("INVEST_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("INVEST_WORKSPACE", "execution")
    runtime = build_runtime(workspace, root=tmp_path, mode="kis_live")
    assert isinstance(runtime.broker, KisLiveClient)
    assert runtime.store.path.name == "runtime-kis_live.db"
    assert not runtime.context()["live_orders_enabled"]
    assert not runtime.agent.status()["control"]["enabled"]
    assert runtime.execution.session_check is not None
    assert runtime.execution.preflight is not None
    monkeypatch.setenv("INVEST_WORKSPACE", "research")
    with pytest.raises(ValueError):
        build_runtime(workspace, root=tmp_path, mode="kis_live")


def test_live_mode_and_enabled_flag_are_not_reported_as_simulation(workspace, tmp_path):
    broker, _ = live_client(tmp_path, receipt)
    runtime = Runtime(
        workspace,
        RuntimeStore(tmp_path / "runtime.db", "kis_live"),
        broker,
        settings=broker.settings,
        execution_only=True,
    )
    client = TestClient(create_app(workspace, runtime))
    for path in ("/api/context", "/api/health"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json()["mode"] == "kis_live"
        assert response.json()["live_orders_enabled"] is True
        assert response.json()["account_alias"] == "한투 실전계좌"
