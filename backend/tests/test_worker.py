import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from invest_lab.adapters.broker_stream import StreamEvent, SubscriptionManager
from invest_lab.adapters.market_data import MarketStore
from invest_lab.adapters.runtime_store import RuntimeStore
from invest_lab.adapters.settings import KisSettings
from invest_lab.worker import Worker, WorkerStream

NOW = datetime(2026, 9, 25, 1, tzinfo=timezone.utc)


def runtime(tmp_path, mode="kis_paper"):
    settings = KisSettings(
        app_key="fixture",
        app_secret="fixture",
        account_number="01234567",
        account_product_code="01",
        hts_id="fixture-hts",
    )
    return SimpleNamespace(
        store=RuntimeStore(tmp_path / "runtime.db", mode, "fixture"),
        market_store=MarketStore(tmp_path / "market.db"),
        settings=settings,
        workspace=SimpleNamespace(clock=SimpleNamespace(now=lambda: NOW)),
        broker=SimpleNamespace(settings=settings),
        at=lambda: NOW.isoformat(),
    )


def test_worker_stream_never_starts_for_local_simulation(tmp_path):
    bridge = WorkerStream(
        runtime(tmp_path, "local_simulation"),
        "owner",
        stream_factory=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("No network class should be constructed")
        ),
    )
    assert bridge.start() is False
    assert bridge.thread is None


def test_stream_events_coalesce_keep_exchange_clock_and_never_apply_fills(tmp_path):
    live = runtime(tmp_path)
    bridge = WorkerStream(live, "owner")

    async def scenario():
        for price in ("100", "101"):
            await bridge.on_event(
                StreamEvent(
                    "trade",
                    "H0STCNT0",
                    NOW,
                    "0048K0",
                    {
                        "symbol": "0048K0",
                        "price": price,
                        "source": "kis_paper_ws",
                        "halted": False,
                        "timestamp_basis": "exchange",
                        "source_timestamp": "2026-09-25T09:59:59+09:00",
                        "observed_at": NOW.isoformat(),
                    },
                )
            )
        await bridge.on_event(
            StreamEvent(
                "fill",
                "H0STCNI9",
                NOW,
                "0048K0",
                {
                    "broker_order_id": "001",
                    "fill_quantity": 100,
                    "event_fingerprint": "fixture-hash",
                    "private": "must-not-be-persisted",
                },
            )
        )

    asyncio.run(scenario())
    bridge.flush_events()
    quote = live.market_store.quote("0048K0")
    assert quote["price"] == "101"
    assert quote["source_timestamp"] == "2026-09-25T00:59:59+00:00"
    assert quote["timestamp_basis"] == "exchange"
    assert live.store.all_orders() == []
    assert live.store.document("account") is None
    assert "private" not in live.store.document("last_account_notice")
    assert [job["kind"] for job in live.store.jobs()] == ["reconcile"]


def test_notice_reconciliation_has_one_pending_and_one_trailing_request(tmp_path):
    live = runtime(tmp_path)
    bridge = WorkerStream(live, "owner")
    bridge._notice_dirty = True
    bridge.flush_events()
    first = live.store.jobs()[0]
    bridge._notice_dirty = True
    bridge.flush_events()
    assert len(live.store.jobs()) == 1
    job = live.store.claim_job("worker")
    assert job["id"] == first["id"]
    live.store.finish_job(job["id"], "worker", result={"status": "matched"})
    bridge.flush_events()
    assert len(live.store.jobs()) == 2
    assert not bridge._notice_dirty


def test_reconnect_readiness_waits_for_actual_reconciliation_job_result(tmp_path):
    live = runtime(tmp_path)
    bridge = WorkerStream(live, "owner")

    async def scenario():
        bridge.stop_event = asyncio.Event()
        task = asyncio.create_task(bridge.reconcile())
        await asyncio.sleep(0)
        assert not task.done()
        job = live.store.claim_job("worker")
        live.store.finish_job(job["id"], "worker", result={"status": "matched"})
        assert await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())


def test_subscription_refresh_uses_holdings_then_top_twenty_candidates(tmp_path):
    live = runtime(tmp_path)
    bridge = WorkerStream(live, "owner")
    bridge.stream = SimpleNamespace(subscriptions=SubscriptionManager(live.settings))
    live.store.put_document("account", {"positions": [{"symbol": "005930", "quantity": 1}]})
    live.store.put_document(
        "watchlist", {"items": [{"symbol": f"{index:06}"} for index in range(30)]}
    )
    bridge.refresh_subscriptions()
    status = bridge._subscription_status
    assert status["subscribed_symbols"][0] == "005930"
    assert len(status["subscribed_symbols"]) == 21
    assert status["count"] == 22  # plus account notice subscription


def test_stream_lifecycle_stops_and_publishes_disconnected_without_network(tmp_path):
    live = runtime(tmp_path)
    events = []

    class Stream:
        def __init__(self, *args, **kwargs):
            self.state = {"connected": False, "subscribed": False, "reconciled": False}
            self.subscriptions = SubscriptionManager(live.settings)

        async def run(self, stop):
            self.state.update(connected=True)
            events.append("started")
            stop.set()

    bridge = WorkerStream(live, "owner", stream_factory=Stream)
    asyncio.run(bridge._serve())
    assert events == ["started"]
    state = live.store.document("stream")
    assert not state["connected"] and not state["reconciled"]
    assert "fixture-hts" not in str(state)


def test_delayed_stream_record_cannot_overwrite_later_exchange_quote(tmp_path):
    live = runtime(tmp_path)
    base = {"symbol": "0048K0", "source": "kis_paper_ws", "timestamp_basis": "exchange"}
    live.market_store.put_quote(
        {
            **base,
            "price": "101",
            "source_timestamp": "2026-09-25T10:00:00+09:00",
            "observed_at": "2026-09-25T01:00:01+00:00",
        }
    )
    live.market_store.put_quote(
        {
            **base,
            "price": "99",
            "source_timestamp": "2026-09-25T09:59:00+09:00",
            "observed_at": "2026-09-25T01:00:02+00:00",
        }
    )
    assert live.market_store.quote("0048K0")["price"] == "101"


def test_schedule_closes_orders_even_when_new_auto_entries_disabled(tmp_path):
    live = runtime(tmp_path)
    current = datetime.fromisoformat("2026-09-25T15:20:00+09:00")
    live.workspace.clock = SimpleNamespace(now=lambda: current)
    live.at = lambda: current.isoformat()
    live.store.put_document("deployment", {"enabled": False, "version": 1})
    live.market_store.import_calendar(
        [
            {
                "session": "2026-09-25",
                "is_open": True,
                "open_at": "2026-09-25T09:00:00+09:00",
                "close_at": "2026-09-25T15:30:00+09:00",
            }
        ],
        source="fixture",
        as_of="2026-09-24T00:00:00Z",
    )
    worker = Worker(live)
    worker.schedule()
    worker.schedule()
    assert [job["kind"] for job in live.store.jobs()] == ["close_session"]
    live.store.put_document("deployment", {"enabled": True, "version": 1})
    live._session_open = lambda _: True
    worker.schedule()
    assert "auto_trade" not in [job["kind"] for job in live.store.jobs()]


def test_worker_restarts_only_read_collection_and_never_order_submission(tmp_path):
    live = runtime(tmp_path)
    for kind in ("collect_market", "submit_order", "amend_order"):
        live.store.enqueue(kind, {"symbol": "005930"})
        live.store.claim_job("old-worker")
    live.store.recover_jobs()
    worker = Worker(live)
    worker.recover_read_jobs()
    worker.recover_read_jobs()
    queued = [job for job in live.store.jobs() if job["state"] == "queued"]
    assert len(queued) == 1 and queued[0]["kind"] == "collect_market"
