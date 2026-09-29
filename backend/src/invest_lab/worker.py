"""Durable local worker: python -m invest_lab.worker [--once]."""

import argparse
import asyncio
import logging
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from invest_lab.adapters.runtime import build_runtime
from invest_lab.adapters.runtime_store import RuntimeStore
from invest_lab.bootstrap import build_workspace
from invest_lab.domain.errors import TradingBlocked

ROOT = Path(__file__).resolve().parents[3]
log = logging.getLogger(__name__)


def research_job(db_path, artifacts, payload):
    from invest_lab.adapters.research import build_research_service

    return build_research_service(Path(db_path), Path(artifacts)).run_experiment(payload)


class WorkerStream:
    """One paper stream per OS-locked worker; only the job loop calls reconciliation.

    Incoming prices are coalesced before SQLite writes. Account notices request a
    cumulative REST reconciliation, with at most one pending notice job and a trailing
    request when another event arrived while the previous snapshot was being read.
    """

    def __init__(self, runtime, owner, *, stream_factory=None):
        from invest_lab.adapters.broker_stream import KisPaperStream

        self.runtime, self.owner = runtime, owner
        self.stream_factory = stream_factory or KisPaperStream
        self.stream = None
        self.thread = None
        self.loop = None
        self.stop_event = None
        self._quotes, self._books = {}, {}
        self._notice_dirty = False
        self._notice_job = None
        self._subscription_status = None
        self._last_configuration = None
        self._shutdown_requested = threading.Event()

    def start(self):
        settings = self.runtime.settings
        if self.runtime.store.mode != "kis_paper":
            return False
        if not settings or not settings.readiness()["stream_configured"]:
            self.runtime.store.put_document(
                "stream",
                {
                    "at": self.runtime.at(),
                    "connected": False,
                    "subscribed": False,
                    "reconciled": False,
                    "last_error": "PAPER_STREAM_CONFIGURATION_MISSING",
                },
            )
            return False
        if self.thread is not None:
            return True
        self.thread = threading.Thread(
            target=self._thread_main, name="kis-paper-stream", daemon=True
        )
        self.thread.start()
        return True

    def _thread_main(self):
        try:
            asyncio.run(self._serve())
        except Exception:
            self.runtime.store.put_document(
                "stream",
                {
                    "at": self.runtime.at(),
                    "connected": False,
                    "subscribed": False,
                    "reconciled": False,
                    "last_error": "STREAM_WORKER_FAILED",
                },
            )

    async def _serve(self):
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        if self._shutdown_requested.is_set():
            self.stop_event.set()
            return
        self.stream = self.stream_factory(
            self.runtime.broker,
            on_event=self.on_event,
            reconcile=self.reconcile,
            now=self.runtime.workspace.clock.now,
        )
        self.refresh_subscriptions()
        runner = asyncio.create_task(self.stream.run(self.stop_event))
        last_status = 0.0
        try:
            while not self.stop_event.is_set():
                if runner.done():
                    await runner
                    break
                self.flush_events()
                now = self.loop.time()
                if now - last_status >= 5:
                    self.refresh_subscriptions()
                    self.publish_state()
                    last_status = now
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=0.25)
                except TimeoutError:
                    pass
        finally:
            self.stop_event.set()
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass
            self.flush_events()
            self.publish_state(stopped=True)

    def refresh_subscriptions(self):
        account = self.runtime.store.document("account") or {}
        holdings = sorted(
            p["symbol"] for p in account.get("positions", []) if p.get("quantity", 0) > 0
        )
        watch = self.runtime.store.document("watchlist") or {}
        deployment = self.runtime.store.document("deployment") or {}
        candidate_limit = deployment.get("rules", {}).get("candidate_limit", 20)
        candidates = [row["symbol"] for row in watch.get("items", [])][:candidate_limit]
        books = deployment.get("stream_books", False) is True
        signature = (tuple(holdings), tuple(candidates), books)
        if signature != self._last_configuration:
            self._subscription_status = self.stream.subscriptions.configure(
                holdings=holdings, candidates=candidates, books=books
            )
            self._last_configuration = signature

    async def on_event(self, event):
        if event.kind == "trade":
            # Received time and exchange time are preserved separately by the parser.
            self._quotes[event.symbol] = dict(event.data)
        elif event.kind == "book":
            self._books[event.symbol] = {
                **event.data,
                "symbol": event.symbol,
                "observed_at": event.observed_at.isoformat(),
            }
        elif event.kind in {"fill", "order_notice"}:
            self._notice_dirty = True
            self.runtime.store.put_document(
                "last_account_notice",
                {
                    "at": event.observed_at.isoformat(),
                    "kind": event.kind,
                    "broker_order_id": event.data["broker_order_id"],
                    "symbol": event.symbol,
                    "rejected": event.data.get("rejected", False),
                    "event_fingerprint": event.data.get("event_fingerprint"),
                    "requires_reconciliation": True,
                },
            )

    def flush_events(self):
        quotes, books = self._quotes, self._books
        self._quotes, self._books = {}, {}
        for row in quotes.values():
            self.runtime.market_store.put_quote(row)
        for symbol, row in books.items():
            self.runtime.store.put_document("order_book", row, symbol)
        if self._notice_dirty:
            pending = self.runtime.store.job(self._notice_job) if self._notice_job else None
            if pending is None or pending["state"] not in {"queued", "running"}:
                job = self.runtime.store.enqueue("reconcile", job_key=f"stream-notice:{uuid4()}")
                self._notice_job = job["id"]
                self._notice_dirty = False

    async def reconcile(self):
        job = self.runtime.store.enqueue(
            "reconcile", job_key=f"stream-connect:{self.owner}:{uuid4()}"
        )
        deadline = asyncio.get_running_loop().time() + 120
        while self.stop_event is not None and not self.stop_event.is_set():
            current = self.runtime.store.job(job["id"])
            if current["state"] == "completed":
                return (current.get("result") or {}).get("status") == "matched"
            if current["state"] not in {"queued", "running"}:
                return False
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.25)
        return False

    def publish_state(self, *, stopped=False):
        state = dict(self.stream.state)
        if stopped:
            state.update(connected=False, subscribed=False, reconciled=False)
        self.runtime.store.put_document(
            "stream",
            {
                **state,
                "at": self.runtime.at(),
                "worker_owner": self.owner,
                "subscriptions": self._subscription_status,
                "source": "kis_paper_ws",
            },
        )

    def stop(self):
        self._shutdown_requested.set()
        if self.loop is not None and self.stop_event is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.stop_event.set)
        if self.thread is not None:
            self.thread.join(timeout=20)
            if self.thread.is_alive():
                self.runtime.store.put_document(
                    "stream",
                    {
                        "at": self.runtime.at(),
                        "connected": False,
                        "subscribed": False,
                        "reconciled": False,
                        "last_error": "STREAM_SHUTDOWN_PENDING",
                    },
                )


class Worker:
    def __init__(self, runtime):
        self.runtime, self.owner = runtime, str(uuid4())
        self.pool = None
        self.pending = {}
        self.stream_worker = None

    def heartbeat(self):
        self.runtime.store.put_document(
            "worker",
            {
                "at": self.runtime.at(),
                "owner": self.owner,
                "state": "running",
                "research_jobs": len(self.pending),
            },
        )

    def schedule(self):
        runtime = self.runtime
        now = runtime.workspace.clock.now().astimezone(ZoneInfo("Asia/Seoul"))
        deployment = runtime.store.document("deployment")
        if runtime.store.mode != "kis_paper":
            # Synthetic fixture dates never become live calendar evidence.
            return
        try:
            calendar = runtime.market_store.calendar(now.date(), as_of=now)
        except ValueError:
            runtime.store.put_document(
                "schedule_status", {"at": runtime.at(), "error": "거래 달력 누락"}
            )
            return
        if not calendar["is_open"]:
            return
        session = now.date().isoformat()
        close = datetime.fromisoformat(calendar["close_at"])
        if now >= close - timedelta(minutes=10):
            runtime.store.enqueue("close_session", job_key=f"close-session:{session}")
        if not deployment.get("enabled"):
            return
        if now >= close:
            if (now - close).total_seconds() >= 20 * 60:
                runtime.store.enqueue(
                    "daily_pipeline", {"end": session}, f"daily:{session}:{deployment['version']}"
                )
        elif runtime._session_open(now):
            minute = now.strftime("%Y%m%d%H%M")
            runtime.store.enqueue("reconcile", job_key=f"reconcile:{minute}")
            if now < close - timedelta(minutes=10):
                runtime.store.enqueue(
                    "auto_trade", job_key=f"auto:{minute}:{deployment['version']}"
                )

    def recover_read_jobs(self):
        for job in self.runtime.store.jobs(10000):
            if job["state"] == "interrupted" and job["kind"] == "collect_market":
                # The same recovery key is stable on every restart; no repeated fanout.
                self.runtime.store.enqueue(
                    "collect_market", job["payload"], job_key=f"recovered-read:{job['id']}"
                )

    def tick(self):
        self.heartbeat()
        for identifier, (future, job) in list(self.pending.items()):
            if future.done():
                try:
                    self.runtime.store.finish_job(job["id"], self.owner, result=future.result())
                except Exception as error:
                    self.runtime.store.finish_job(
                        job["id"],
                        self.owner,
                        error=type(error).__name__
                        + ": 연구 작업 실패. 데이터 품질과 실행 기록을 확인하세요.",
                    )
                del self.pending[identifier]
        self.schedule()
        job = self.runtime.store.claim_job(
            self.owner, exclude_kinds={"research_experiment"} if self.pending else set()
        )
        if not job:
            return False
        try:
            if job["kind"] == "research_experiment":
                if self.pool is None:
                    self.pool = ProcessPoolExecutor(max_workers=1)
                repository = self.runtime.research.repository
                future = self.pool.submit(
                    research_job, str(repository.path), str(repository.artifact_dir), job["payload"]
                )
                self.pending[job["id"]] = (future, job)
            else:
                result = self.runtime.run_job(job)
                self.runtime.store.finish_job(job["id"], self.owner, result=result)
        except (ValueError, LookupError) as error:
            self.runtime.store.finish_job(job["id"], self.owner, error=str(error))
        except Exception as error:
            # Adapter exception names are safe; raw responses/URLs may contain private values.
            self.runtime.store.finish_job(
                job["id"],
                self.owner,
                error=f"{type(error).__name__}: 작업 실패. 연결 및 대조 상태를 확인하세요.",
            )
        return True

    def run(self, *, once=False):
        worker_lock = RuntimeStore(
            self.runtime.store.path,
            self.runtime.store.mode,
            self.runtime.store.account_id + ":worker",
        )
        with worker_lock.execution_lock():
            self.runtime.store.recover_jobs()
            self.recover_read_jobs()
            # Recovery first; no prior SENDING order is blindly replayed.
            self.runtime.store.enqueue("reconcile", job_key=f"startup:{self.owner}")
            if not once:
                self.stream_worker = WorkerStream(self.runtime, self.owner)
                self.stream_worker.start()
            try:
                while True:
                    worked = self.tick()
                    if once and not worked and not self.pending:
                        break
                    time.sleep(0.2 if worked else 1)
            finally:
                if self.stream_worker is not None:
                    self.stream_worker.stop()
                if self.pool:
                    self.pool.shutdown(wait=True, cancel_futures=True)
                self.runtime.store.put_document(
                    "worker", {"at": self.runtime.at(), "owner": self.owner, "state": "stopped"}
                )


def main():
    parser = argparse.ArgumentParser(description="invest-lab 로컬 작업자")
    parser.add_argument("--once", action="store_true", help="대기 작업을 처리한 뒤 종료")
    args = parser.parse_args()
    runtime = build_runtime(build_workspace(), root=ROOT)
    try:
        Worker(runtime).run(once=args.once)
    except KeyboardInterrupt:
        pass
    except TradingBlocked:
        raise SystemExit("이미 같은 계좌의 작업자가 실행 중입니다.") from None


if __name__ == "__main__":
    main()
