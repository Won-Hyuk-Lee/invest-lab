"""Local operational persistence. No brokerage I/O is performed inside a transaction."""

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from invest_lab.domain.errors import TradingBlocked, VersionConflict

SCHEMA = """
CREATE TABLE runtime_documents (
 namespace TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(namespace,kind,key));
CREATE TABLE execution_orders (
 id TEXT PRIMARY KEY, namespace TEXT NOT NULL, client_id TEXT NOT NULL,
 symbol TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL,
 UNIQUE(namespace,client_id));
CREATE INDEX execution_orders_list ON execution_orders(namespace,created_at,id);
CREATE TABLE execution_commands (
 namespace TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
 result TEXT NOT NULL, PRIMARY KEY(namespace,id));
CREATE TABLE runtime_jobs (
 id TEXT PRIMARY KEY, namespace TEXT NOT NULL, job_key TEXT NOT NULL, kind TEXT NOT NULL,
 payload TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
 started_at TEXT, finished_at TEXT, owner TEXT, result TEXT, error TEXT,
 UNIQUE(namespace,job_key));
CREATE TABLE simulated_accounts(namespace TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE simulated_orders(namespace TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,
 PRIMARY KEY(namespace,id));
"""


def encode(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def now_iso():
    return datetime.now(UTC).isoformat()


class RuntimeStore:
    _locks: dict[str, threading.Lock] = {}
    _guard = threading.Lock()

    def __init__(self, path: Path, mode="local_simulation", account_id="local"):
        if mode not in {"local_simulation", "kis_paper"}:
            raise ValueError("실전 실행은 지원하지 않습니다.")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.mode, self.account_id = mode, account_id
        self.namespace = f"{mode}:{account_id}"
        with self.connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(
                "CREATE TABLE IF NOT EXISTS runtime_migrations(version INTEGER "
                "PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            con.execute("BEGIN IMMEDIATE")
            checksum = hashlib.sha256(SCHEMA.encode()).hexdigest()
            row = con.execute("SELECT checksum FROM runtime_migrations WHERE version=1").fetchone()
            if row and row[0] != checksum:
                raise RuntimeError("저장소 migration checksum이 일치하지 않습니다.")
            if not row:
                # Schema and version record commit together after an interrupted setup.
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        con.execute(statement)
                con.execute("INSERT INTO runtime_migrations VALUES(1,?,?)", (checksum, now_iso()))

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=15000")
        try:
            with con:
                yield con
        finally:
            con.close()

    @contextmanager
    def execution_lock(self):
        """OS lock prevents a resumed old process from racing a replacement executor."""
        suffix = hashlib.sha256(self.namespace.encode()).hexdigest()[:16]
        path = self.path.parent / f".executor-{suffix}.lock"
        with self._guard:
            lock = self._locks.setdefault(str(path.resolve()), threading.Lock())
        if not lock.acquire(blocking=False):
            raise TradingBlocked("다른 계좌 작업이 실행 중입니다.")
        handle = None
        acquired = False
        try:
            handle = path.open("a+b")
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as error:
                raise TradingBlocked("다른 프로세스가 계좌를 실행 중입니다.") from error
            generation = int((self.document("execution_generation") or {}).get("value", 0)) + 1
            self.put_document("execution_generation", {"value": generation})
            yield generation
        finally:
            if handle is not None:
                if acquired:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
            lock.release()

    def assert_generation(self, generation):
        if (self.document("execution_generation") or {}).get("value") != generation:
            raise TradingBlocked("계좌 실행권이 변경되었습니다.")

    def document(self, kind, key="latest"):
        with self.connect() as con:
            row = con.execute(
                "SELECT payload FROM runtime_documents WHERE namespace=? AND kind=? AND key=?",
                (self.namespace, kind, key),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def put_document(self, kind, value, key="latest"):
        with self.connect() as con:
            con.execute(
                "INSERT INTO runtime_documents VALUES(?,?,?,?) ON "
                "CONFLICT(namespace,kind,key) DO UPDATE SET "
                "payload=excluded.payload",
                (self.namespace, kind, key, encode(value)),
            )

    def documents(self, kind):
        with self.connect() as con:
            return [
                json.loads(row[0])
                for row in con.execute(
                    "SELECT payload FROM runtime_documents WHERE namespace=? AND "
                    "kind=? ORDER BY key",
                    (self.namespace, kind),
                )
            ]

    def create_intent(self, payload, at, context):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT payload FROM execution_orders WHERE namespace=? AND client_id=?",
                (self.namespace, payload["client_request_id"]),
            ).fetchone()
            if row:
                order = json.loads(row[0])
                if order["request"] != payload:
                    raise VersionConflict("같은 주문 식별자를 다른 주문에 사용할 수 없습니다.")
                return order
            order = {
                **payload,
                "request": payload,
                "id": str(uuid4()),
                "mode": self.mode,
                "account_id": self.account_id,
                "state": "registered",
                "created_at": at,
                "updated_at": at,
                "filled_quantity": 0,
                "filled_amount": "0",
                "cancelled_quantity": 0,
                "remaining_quantity": payload["quantity"],
                "context": context,
                "events": [{"at": at, "state": "registered"}],
                "broker_order_id": None,
                "error": None,
            }
            con.execute(
                "INSERT INTO execution_orders VALUES(?,?,?,?,?,?,?)",
                (
                    order["id"],
                    self.namespace,
                    payload["client_request_id"],
                    payload["symbol"],
                    "registered",
                    at,
                    encode(order),
                ),
            )
            return order

    def get_order(self, order_id):
        with self.connect() as con:
            row = con.execute(
                "SELECT payload FROM execution_orders WHERE namespace=? AND id=?",
                (self.namespace, order_id),
            ).fetchone()
            if not row:
                raise LookupError("주문을 찾을 수 없습니다.")
            return json.loads(row[0])

    def find_intent(self, client_id):
        with self.connect() as con:
            row = con.execute(
                "SELECT payload FROM execution_orders WHERE namespace=? AND client_id=?",
                (self.namespace, client_id),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def list_orders(self, limit=50, cursor=None, status=None, symbol=None, start=None, end=None):
        limit = min(max(int(limit), 1), 100)
        where, args = ["namespace=?"], [self.namespace]
        for name, value in (("state", status), ("symbol", symbol)):
            if value:
                where.append(f"{name}=?")
                args.append(value)
        if start:
            where.append("created_at>=?")
            args.append(start)
        if end:
            where.append("created_at<=?")
            args.append(end)
        condition = " AND ".join(where)
        with self.connect() as con:
            total = con.execute(
                f"SELECT COUNT(*) FROM execution_orders WHERE {condition}", args
            ).fetchone()[0]
            if cursor:
                previous = con.execute(
                    "SELECT created_at,id FROM execution_orders WHERE namespace=? AND id=?",
                    (self.namespace, cursor),
                ).fetchone()
                if not previous:
                    raise ValueError("페이지 커서가 유효하지 않습니다.")
                condition += " AND (created_at < ? OR (created_at=? AND id<?))"
                args += [previous[0], previous[0], previous[1]]
            rows = con.execute(
                f"SELECT payload FROM execution_orders WHERE {condition} ORDER BY "
                "created_at DESC,id DESC LIMIT ?",
                [*args, limit + 1],
            ).fetchall()
        items = [json.loads(row[0]) for row in rows[:limit]]
        return {
            "items": items,
            "total": total,
            "next_cursor": items[-1]["id"] if len(rows) > limit else None,
        }

    def all_orders(self):
        with self.connect() as con:
            return [
                json.loads(row[0])
                for row in con.execute(
                    "SELECT payload FROM execution_orders WHERE namespace=? ORDER BY created_at,id",
                    (self.namespace,),
                )
            ]

    def transition(self, order_id, expected, state, at, **values):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT payload FROM execution_orders WHERE namespace=? AND id=?",
                (self.namespace, order_id),
            ).fetchone()
            if not row:
                raise LookupError("주문을 찾을 수 없습니다.")
            order = json.loads(row[0])
            if order["state"] not in expected:
                raise VersionConflict("주문 상태가 변경되었습니다. 저장된 결과를 확인하세요.")
            order.update(values)
            order.update(state=state, updated_at=at)
            order["events"].append({"at": at, "state": state, **values})
            con.execute(
                "UPDATE execution_orders SET state=?,payload=? WHERE id=? AND namespace=?",
                (state, encode(order), order_id, self.namespace),
            )
            return order

    def command(self, command_id, payload, at):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT * FROM execution_commands WHERE namespace=? AND id=?",
                (self.namespace, command_id),
            ).fetchone()
            if row:
                if json.loads(row["payload"]) != payload:
                    raise VersionConflict("명령 식별자와 내용이 다릅니다.")
                return {"state": row["state"], "result": json.loads(row["result"])}, False
            con.execute(
                "INSERT INTO execution_commands VALUES(?,?,?,?,?)",
                (self.namespace, command_id, encode(payload), "sending", encode({"at": at})),
            )
            return {"state": "sending", "result": {}}, True

    def finish_command(self, command_id, state, result):
        with self.connect() as con:
            con.execute(
                "UPDATE execution_commands SET state=?,result=? WHERE namespace=? AND id=?",
                (state, encode(result), self.namespace, command_id),
            )

    def commands_for(self, order_id, *, details=False):
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM execution_commands WHERE namespace=?", (self.namespace,)
            ).fetchall()
        return [
            {
                "id": row["id"],
                "state": row["state"],
                "kind": "amend"
                if json.loads(row["payload"]).get("price") is not None
                else "cancel",
                **(
                    {"payload": json.loads(row["payload"]), "result": json.loads(row["result"])}
                    if details
                    else {}
                ),
            }
            for row in rows
            if json.loads(row["payload"]).get("order_id") == order_id
        ]

    def enqueue(self, kind, payload=None, job_key=None):
        job_id, at = str(uuid4()), now_iso()
        job_key = job_key or job_id
        with self.connect() as con:
            con.execute(
                "INSERT OR IGNORE INTO "
                "runtime_jobs(id,namespace,job_key,kind,payload,state,created_at) "
                "VALUES(?,?,?,?,?,'queued',?)",
                (job_id, self.namespace, job_key, kind, encode(payload or {}), at),
            )
            return self._job(
                con.execute(
                    "SELECT * FROM runtime_jobs WHERE namespace=? AND job_key=?",
                    (self.namespace, job_key),
                ).fetchone()
            )

    @staticmethod
    def _job(row):
        data = dict(row)
        data.pop("namespace", None)
        for key in ("payload", "result"):
            data[key] = json.loads(data[key]) if data.get(key) else None
        return data

    def jobs(self, limit=100):
        with self.connect() as con:
            return [
                self._job(row)
                for row in con.execute(
                    "SELECT * FROM runtime_jobs WHERE namespace=? ORDER BY created_at DESC LIMIT ?",
                    (self.namespace, limit),
                )
            ]

    def job(self, job_id):
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM runtime_jobs WHERE namespace=? AND id=?", (self.namespace, job_id)
            ).fetchone()
        if row is None:
            raise LookupError("작업을 찾을 수 없습니다.")
        return self._job(row)

    def claim_job(self, owner, exclude_kinds=None):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            excluded = list(exclude_kinds or [])
            clause = (
                " AND kind NOT IN (" + ",".join("?" for _ in excluded) + ")" if excluded else ""
            )
            row = con.execute(
                "SELECT * FROM runtime_jobs WHERE namespace=? AND state='queued'"
                + clause
                + (
                    " ORDER BY CASE WHEN kind IN "
                    "('reconcile','cancel_order','close_session') THEN 0 WHEN "
                    "kind='submit_order' THEN 1 ELSE 2 END,created_at LIMIT 1"
                ),
                (self.namespace, *excluded),
            ).fetchone()
            if not row:
                return None
            con.execute(
                "UPDATE runtime_jobs SET state='running',started_at=?,owner=? WHERE id=?",
                (now_iso(), owner, row["id"]),
            )
            return self._job(
                con.execute("SELECT * FROM runtime_jobs WHERE id=?", (row["id"],)).fetchone()
            )

    def finish_job(self, job_id, owner, result=None, error=None):
        with self.connect() as con:
            con.execute(
                "UPDATE runtime_jobs SET state=?,finished_at=?,result=?,error=? "
                "WHERE id=? AND namespace=? AND owner=? AND state='running'",
                (
                    "failed" if error else "completed",
                    now_iso(),
                    encode(result),
                    error,
                    job_id,
                    self.namespace,
                    owner,
                ),
            )

    def recover_jobs(self):
        # Called only after obtaining the process-level worker lock. Commands are reconciled first.
        with self.connect() as con:
            con.execute(
                "UPDATE runtime_jobs SET "
                "state='interrupted',finished_at=?,error='작업자 중단 후 대조 필요' WHERE "
                "namespace=? AND state='running'",
                (now_iso(), self.namespace),
            )

    def backup(self, destination: Path):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.resolve() == self.path.resolve():
            raise ValueError("백업 대상은 원본과 달라야 합니다.")
        with self.connect() as source, sqlite3.connect(destination) as target:
            source.backup(target)
        self.put_document("backup", {"at": now_iso(), "path": str(destination)})
