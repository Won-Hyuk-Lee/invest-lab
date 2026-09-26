import asyncio
import base64
import json
from datetime import datetime, timezone

import httpx
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from invest_lab.adapters.broker_stream import (
    NOTICE_CHANNEL,
    NOTICE_FIELDS,
    TRADE_CHANNEL,
    TRADE_FIELDS,
    KisPaperStream,
    KisStreamParser,
    StreamEvent,
    StreamProtocolError,
    SubscriptionManager,
)
from invest_lab.adapters.kis import (
    AmbiguousOrder,
    BrokerDataError,
    BrokerRejected,
    KisCoordinator,
    KisError,
    KisPaperClient,
)
from invest_lab.adapters.settings import PAPER_BASE_URL, KisSettings
from invest_lab.domain.errors import TradingBlocked

NOW = datetime(2026, 9, 27, 3, tzinfo=timezone.utc)


class MemoryCoordinator:
    def __init__(self):
        self.cached = None
        self.waits = 0

    def wait(self, scope):
        self.waits += 1

    def token(self, scope, issue):
        if self.cached is None:
            self.cached = issue()[0]
        return self.cached

    def invalidate(self, scope):
        self.cached = None


def settings(tmp_path, **kwargs):
    return KisSettings(
        app_key="fixture-key",
        app_secret="fixture-secret",
        account_number="01234567",
        account_product_code="01",
        hts_id="fixture-hts",
        orders_enabled=True,
        cache_path=tmp_path / "tokens.db",
        **kwargs,
    )


def client(tmp_path, handler, **kwargs):
    def transport(request):
        assert str(request.url).startswith(PAPER_BASE_URL + "/")
        if request.url.path == "/oauth2/tokenP":
            assert json.loads(request.content) == {
                "grant_type": "client_credentials",
                "appkey": "fixture-key",
                "appsecret": "fixture-secret",
            }
            return httpx.Response(200, json={"access_token": "fixture-token", "expires_in": 86400})
        return handler(request)

    return KisPaperClient(
        settings(tmp_path),
        transport=httpx.MockTransport(transport),
        coordinator=MemoryCoordinator(),
        now=lambda: NOW,
        sleep=lambda _: None,
        **kwargs,
    )


def ok(**values):
    return httpx.Response(200, json={"rt_cd": "0", **values})


def test_quote_alphanumeric_code_no_fabricated_exchange_timestamp(tmp_path):
    def handler(request):
        assert request.headers["tr_id"] == "FHKST01010100"
        assert request.url.params["FID_INPUT_ISCD"] == "0048K0"
        return ok(
            output={
                "stck_prpr": "10500",
                "aspr_unit": "5",
                "stck_mxpr": "13650",
                "stck_llam": "7350",
                "iscd_stat_cls_code": "00",
            }
        )

    with client(tmp_path, handler) as api:
        result = api.quote("0048K0")
        assert result["price"] == "10500"
        assert result["tick_size"] == "5"
        assert result["timestamp_basis"] == "received"
        assert result["source_timestamp"] is None


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1", "", None])
def test_invalid_broker_prices_never_reach_domain(tmp_path, bad):
    with client(tmp_path, lambda _: ok(output={"stck_prpr": bad})) as api:
        with pytest.raises(BrokerDataError):
            api.quote("005930")


@pytest.mark.parametrize("mode", ["timeout", "server-error", "missing-receipt"])
def test_ambiguous_post_is_sent_exactly_once(tmp_path, mode):
    calls = []

    def handler(request):
        calls.append(request)
        if mode == "timeout":
            raise httpx.ReadTimeout("fixture-secret-not-for-logs")
        if mode == "server-error":
            return httpx.Response(503, json={"msg1": "fixture-secret-not-for-logs"})
        return ok(output={})

    with client(tmp_path, handler) as api:
        with pytest.raises(AmbiguousOrder) as error:
            api.place_order(
                {"symbol": "005930", "side": "buy", "quantity": 1, "order_type": "market"}
            )
        assert len(calls) == 1
        assert "fixture-secret" not in str(error.value)
        assert error.value.ambiguous


def test_definite_business_rejection_is_distinct_from_unknown(tmp_path):
    with client(
        tmp_path,
        lambda _: httpx.Response(
            200, json={"rt_cd": "1", "msg_cd": "APBK0919", "msg1": "private broker detail"}
        ),
    ) as api:
        with pytest.raises(TradingBlocked) as error:
            api.place_order(
                {
                    "symbol": "005930",
                    "side": "buy",
                    "quantity": 1,
                    "order_type": "limit",
                    "limit_price": "100",
                }
            )
        assert isinstance(error.value, BrokerRejected)
        assert "private" not in str(error.value)


def test_paper_buy_sell_cancel_amend_contracts(tmp_path):
    requests = []

    def handler(request):
        requests.append((request.headers["tr_id"], json.loads(request.content)))
        return ok(output={"KRX_FWDG_ORD_ORGNO": "001", "ODNO": "0000001234", "ORD_TMD": "120000"})

    with client(tmp_path, handler) as api:
        buy = api.place_order(
            {"symbol": "0048K0", "side": "buy", "quantity": 2, "order_type": "market"}
        )
        api.place_order(
            {
                "symbol": "005930",
                "side": "sell",
                "quantity": 1,
                "order_type": "limit",
                "limit_price": "70000",
            }
        )
        api.cancel_order({**buy, "remaining_quantity": 2}, 2)
        api.cancel_order({**buy, "remaining_quantity": 2}, 1, price="10500")
    assert [row[0] for row in requests] == ["VTTC0012U", "VTTC0011U", "VTTC0013U", "VTTC0013U"]
    assert requests[0][1]["ORD_UNPR"] == "0"
    assert requests[0][1]["EXCG_ID_DVSN_CD"] == "KRX"
    assert requests[1][1]["SLL_TYPE"] == "01"
    assert requests[2][1]["ORGN_ODNO"] == "0000001234"
    assert requests[2][1]["RVSE_CNCL_DVSN_CD"] == "02"
    assert requests[3][1]["RVSE_CNCL_DVSN_CD"] == "01"
    assert buy["ordered_at"] == "2026-09-27T12:00:00+09:00"


def account_summary():
    return {
        "dnca_tot_amt": "10000",
        "tot_evlu_amt": "14000",
        "scts_evlu_amt": "4000",
        "nxdy_excc_amt": "8000",
        "prvs_rcdl_excc_amt": "6000",
    }


def position(symbol):
    return {
        "pdno": symbol,
        "prdt_name": "fixture",
        "hldg_qty": "2",
        "ord_psbl_qty": "1",
        "pchs_avg_pric": "900",
        "prpr": "1000",
        "evlu_amt": "2000",
        "evlu_pfls_amt": "200",
    }


def test_account_pages_do_not_add_summary_or_equate_cash_and_capacity(tmp_path):
    count = 0

    def handler(request):
        nonlocal count
        if request.headers["tr_id"] == "VTTC8908R":
            assert request.url.params["PDNO"] == ""
            return ok(output={"nrcvb_buy_amt": "6000", "max_buy_amt": "999999"})
        count += 1
        if count == 1:
            return httpx.Response(
                200,
                headers={"tr_cont": "M"},
                json={
                    "rt_cd": "0",
                    "output1": [position("005930")],
                    "output2": [account_summary()],
                    "ctx_area_fk100": " keep-spaces ",
                    "ctx_area_nk100": "next",
                },
            )
        assert request.headers["tr_cont"] == "N"
        assert request.url.params["CTX_AREA_FK100"] == " keep-spaces "
        return ok(output1=[position("0048K0")], output2=[account_summary()])

    with client(tmp_path, handler) as api:
        result = api.account()
    assert len(result["positions"]) == 2
    assert result["cash"] == "10000"
    assert result["buying_power"] == "6000"
    assert result["positions"][0]["sellable_quantity"] == 1


def test_repeated_page_fails_closed(tmp_path):
    def handler(_):
        return httpx.Response(
            200,
            headers={"tr_cont": "M"},
            json={
                "rt_cd": "0",
                "output1": [],
                "output2": [account_summary()],
                "ctx_area_fk100": "",
                "ctx_area_nk100": "",
            },
        )

    with client(tmp_path, handler) as api:
        with pytest.raises(BrokerDataError, match="REPEATED_PAGE_CURSOR"):
            api.account()


def test_get_transient_retry_is_bounded(tmp_path):
    count = 0

    def handler(_):
        nonlocal count
        count += 1
        return httpx.Response(200, json={"rt_cd": "1", "msg_cd": "EGW00215"})

    with client(tmp_path, handler) as api:
        with pytest.raises(KisError) as error:
            api.quote("005930")
    assert count == 3
    assert error.value.retryable


def test_history_splits_windows_and_preserves_raw_adjusted_basis(tmp_path):
    windows = []

    def handler(request):
        params = request.url.params
        windows.append((params["FID_INPUT_DATE_1"], params["FID_INPUT_DATE_2"]))
        assert params["FID_ORG_ADJ_PRC"] == "0"
        return ok(
            output2=[
                {
                    "stck_bsop_date": params["FID_INPUT_DATE_1"],
                    "stck_oprc": "100",
                    "stck_hgpr": "110",
                    "stck_lwpr": "90",
                    "stck_clpr": "101",
                    "acml_vol": "20",
                    "acml_tr_pbmn": "2000",
                }
            ]
        )

    with client(tmp_path, handler) as api:
        rows = api.bars("005930", "2026-01-01", "2026-09-01", adjusted=True)
    assert len(windows) == 3
    assert len(rows) == 3 and rows[0]["adjusted"]
    assert windows[1][0] == "20260410"


def test_order_history_recent_old_trs_and_normalized_recovery_fields(tmp_path):
    trs = []

    def handler(request):
        trs.append(request.headers["tr_id"])
        return ok(
            output1=[
                {
                    "ord_dt": request.url.params["INQR_STRT_DT"],
                    "ord_tmd": "091003",
                    "sll_buy_dvsn_cd": "02",
                    "tot_ccld_qty": "1",
                    "rmn_qty": "1",
                    "cncl_cfrm_qty": "0",
                    "rjct_qty": "0",
                    "ord_qty": "2",
                    "odno": "00000001",
                    "orgn_odno": "00000000",
                    "ord_gno_brno": "001",
                    "pdno": "0048K0",
                    "tot_ccld_amt": "1000",
                    "ord_unpr": "1000",
                    "ord_dvsn_cd": "00",
                }
            ]
        )

    with client(tmp_path, handler) as api:
        rows = api.orders("2026-01-01", "2026-09-27")
    assert trs == ["VTSC9215R", "VTTC0081R"]
    assert len(rows) == 2
    assert rows[0]["status"] == "partially_filled"
    assert rows[0]["limit_price"] == "1000"
    assert rows[0]["ordered_at"].endswith("+09:00")


def test_token_cache_and_rate_slots_shared_between_coordinators(tmp_path):
    class Clock:
        value = 1000.0

        def now(self):
            return self.value

        def sleep(self, amount):
            self.value += amount

    clock = Clock()
    first = KisCoordinator(tmp_path / "fixture.db", now=clock.now, sleep=clock.sleep)
    second = KisCoordinator(tmp_path / "fixture.db", now=clock.now, sleep=clock.sleep)
    first.wait("account")
    second.wait("account")
    assert clock.value == 1001.25
    assert first.token("app", lambda: ("fixture-token", 2000)) == "fixture-token"
    assert second.token("app", lambda: pytest.fail("Token should be shared")) == "fixture-token"
    clock.value = 1941
    assert second.token("app", lambda: ("next-token", 3000)) == "next-token"


def trade_record(symbol="005930"):
    row = dict.fromkeys(TRADE_FIELDS, "0")
    row.update(
        MKSC_SHRN_ISCD=symbol,
        STCK_CNTG_HOUR="091000",
        STCK_PRPR="70000",
        BSOP_DATE="20260925",
        CNTG_VOL="10",
        ACML_VOL="100",
        ACML_TR_PBMN="7000000",
    )
    return "^".join(row[key] for key in TRADE_FIELDS)


def test_stream_parses_every_record_and_rejects_schema_drift(tmp_path):
    parser = KisStreamParser(settings(tmp_path), now=lambda: NOW)
    packet = f"0|{TRADE_CHANNEL}|002|{trade_record()}^{trade_record('0048K0')}"
    events = parser.parse(packet)
    assert [event.symbol for event in events] == ["005930", "0048K0"]
    assert events[0].data["source_timestamp"] == "2026-09-25T09:10:00+09:00"
    with pytest.raises(StreamProtocolError):
        parser.parse(packet.replace("|002|", "|001|"))
    with pytest.raises(StreamProtocolError):
        parser.parse("0|H0STMKO0|001|anything")


def test_stream_notice_aes_ack_vs_fill_and_secret_redaction(tmp_path):
    parser = KisStreamParser(settings(tmp_path), now=lambda: NOW)
    key, iv = b"0123456789abcdef0123456789abcdef", b"0123456789abcdef"
    ack = parser.parse(
        json.dumps(
            {
                "header": {"tr_id": NOTICE_CHANNEL, "tr_key": "fixture-hts"},
                "body": {"rt_cd": "0", "output": {"key": key.decode(), "iv": iv.decode()}},
            }
        )
    )[0]
    assert "fixture-hts" not in repr(ack)

    def packet(flag, account="0123456701"):
        row = dict.fromkeys(NOTICE_FIELDS, "0")
        row.update(
            CUST_ID="private-customer",
            ACNT_NAME="private-name",
            ACNT_NO=account,
            ODER_NO="000123",
            OODER_NO="000000",
            SELN_BYOV_CLS="02",
            STCK_SHRN_ISCD="0048K0",
            CNTG_QTY="1",
            CNTG_UNPR="10500",
            ODER_QTY="2",
            CNTG_YN=flag,
            RFUS_YN="0",
        )
        plain = "^".join(row[field] for field in NOTICE_FIELDS).encode()
        padder = padding.PKCS7(128).padder()
        padded = padder.update(plain) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        encoded = base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()
        return f"1|{NOTICE_CHANNEL}|001|{encoded}"

    notice, fill = parser.parse(packet("1"))[0], parser.parse(packet("2"))[0]
    assert notice.kind == "order_notice" and fill.kind == "fill"
    assert fill.data["requires_reconciliation"] and not fill.data["date_confirmed"]
    assert "private-name" not in repr(fill) and "private-name" not in json.dumps(fill.data)
    with pytest.raises(StreamProtocolError, match="account does not match"):
        parser.parse(packet("2", "1111111101"))
    parser.reset()
    with pytest.raises(StreamProtocolError):
        parser.parse(packet("2"))


def test_subscription_budget_prioritizes_holdings_and_unsubscribes_before_add(tmp_path):
    manager = SubscriptionManager(settings(tmp_path), max_subscriptions=4)
    result = manager.configure(
        holdings=["005930"], candidates=["0048K0", "000660", "035420"], books=True
    )
    assert result["count"] == 4
    assert result["excluded_candidates"] == ["035420"]
    requests = manager.requests("private-approval")
    assert all(row["header"]["tr_type"] == "1" for row in requests)
    for request in requests:
        row = request["body"]["input"]
        manager.acknowledge(StreamEvent("ack", row["tr_id"], NOW, data={"key": row["tr_key"]}))
    assert manager.ready
    manager.configure(holdings=["005930"], candidates=["035420"])
    removals = manager.requests("private-approval")
    assert len(removals) == 2 and all(row["header"]["tr_type"] == "2" for row in removals)
    assert not manager.requests("private-approval")
    for request in removals:
        row = request["body"]["input"]
        manager.acknowledge(StreamEvent("ack", row["tr_id"], NOW, data={"key": row["tr_key"]}))
    assert manager.requests("private-approval")[0]["body"]["input"]["tr_key"] == "035420"
    manager.disconnected()
    assert not manager.ready


def test_stream_runner_replays_subscriptions_and_reconciles_before_readiness(tmp_path):
    events, calls = [], []

    class Client:
        def __init__(self):
            self.settings = settings(tmp_path)

        def approval_key(self):
            return "fixture-approval"

    async def scenario():
        stop = asyncio.Event()

        class Socket:
            def __init__(self):
                self.pending = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def send(self, text):
                request = json.loads(text)
                row = request["body"]["input"]
                body = {"rt_cd": "0"}
                if row["tr_id"] == NOTICE_CHANNEL:
                    body["output"] = {
                        "key": "0123456789abcdef0123456789abcdef",
                        "iv": "0123456789abcdef",
                    }
                self.pending.append(json.dumps({"header": row, "body": body}))

            async def recv(self):
                if self.pending:
                    return self.pending.pop(0)
                return f"0|{TRADE_CHANNEL}|001|{trade_record()}"

        async def receive(event):
            events.append(event)
            if runner.state["reconciled"]:
                stop.set()
            else:
                # Price reception can proceed while REST reconciliation is pending.
                await asyncio.sleep(0)

        async def reconcile():
            calls.append("reconciled")

        runner = KisPaperStream(
            Client(),
            on_event=receive,
            reconcile=reconcile,
            connect=lambda *args, **kwargs: Socket(),
            now=lambda: NOW,
        )
        runner.subscriptions.configure(candidates=["005930"])
        await asyncio.wait_for(runner.run(stop), timeout=3)
        assert not runner.state["connected"]

    asyncio.run(scenario())
    assert calls == ["reconciled"] and len(events) >= 1
