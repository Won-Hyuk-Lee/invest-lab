"""Consistent local SQLite/artifact bundles and safe, quarantined new-path restore."""

import hashlib
import json
import re
import shutil
import sqlite3
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from invest_lab.domain.research import canonical, content_hash


def _hash(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _safe_file(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("백업 내부의 상대 경로만 허용합니다.")
    path = root / relative
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("백업 경로가 bundle 밖을 가리킵니다.")
    if any(part.is_symlink() for part in [path, *path.parents] if part != root.parent):
        raise ValueError("백업은 symlink를 허용하지 않습니다.")
    return path


@contextmanager
def _readonly(path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
    try:
        yield connection
    finally:
        connection.close()


def _inspect_database(path):
    with _readonly(path) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite 무결성 검사가 실패했습니다.")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("SQLite 외래키 검사가 실패했습니다.")
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        namespaces = set()
        for table in (
            "runtime_documents",
            "execution_orders",
            "execution_commands",
            "runtime_jobs",
        ):
            if table in tables:
                namespaces.update(
                    row[0] for row in connection.execute(f"SELECT DISTINCT namespace FROM {table}")
                )
        for namespace in namespaces:
            if not isinstance(namespace, str) or ":" not in namespace:
                raise ValueError("잘못된 운영 namespace입니다.")
            mode, account = namespace.split(":", 1)
            if mode not in {"local_simulation", "kis_paper"} or not account:
                raise ValueError("허용하지 않은 운영 mode/account namespace입니다.")
        if "execution_orders" in tables:
            for namespace, payload in connection.execute(
                "SELECT namespace,payload FROM execution_orders"
            ):
                order = json.loads(payload)
                if namespace != str(order.get("mode")) + ":" + str(order.get("account_id")):
                    raise ValueError("주문 payload와 namespace가 다릅니다.")
        if "runtime_migrations" in tables:
            from invest_lab.adapters.runtime_store import SCHEMA

            versions = connection.execute(
                "SELECT version,checksum FROM runtime_migrations"
            ).fetchall()
            if versions != [(1, hashlib.sha256(SCHEMA.encode()).hexdigest())]:
                raise ValueError("운영 DB migration 버전/checksum이 현재 코드와 다릅니다.")
        datasets = []
        if "research_datasets" in tables:
            datasets = [
                json.loads(row[0])
                for row in connection.execute("SELECT payload FROM research_datasets ORDER BY id")
            ]
            for item in datasets:
                if item["hash"] != content_hash(item["data"]) or item["id"] != item["hash"][:24]:
                    raise ValueError("연구 데이터셋 content hash가 다릅니다.")
        return {"tables": sorted(tables), "namespaces": sorted(namespaces), "datasets": datasets}


def create_backup_bundle(destination, *, databases, artifact_dir=None, expected_namespace=None):
    """Writer locks span all DB snapshots and immutable artifact verification.

    databases maps logical names to explicit local DB paths. No directory scans,
    environment files, tokens, or other credentials are included implicitly.
    A caller must invoke this outside any active SQLite write transaction.
    """
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("백업 대상은 새 폴더여야 합니다.")
    if not databases or any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,40}", name) for name in databases):
        raise ValueError("명시적인 DB 이름/경로 목록이 필요합니다.")
    sources = {name: Path(path).resolve() for name, path in databases.items()}
    if len(set(sources.values())) != len(sources):
        raise ValueError("동일 DB 경로를 중복 지정할 수 없습니다.")
    if any(not path.is_file() for path in sources.values()):
        raise ValueError("백업할 DB 파일을 찾을 수 없습니다.")
    destination.mkdir(parents=True)
    (destination / "databases").mkdir()
    manifest = {
        "format_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "consistency": "all_sqlite_writers_locked_before_any_snapshot",
        "databases": {},
        "artifacts": {},
        "namespaces": [],
        "implicit_secret_files_included": False,
    }
    with ExitStack() as stack:
        for source in sorted(sources.values()):
            lock = sqlite3.connect(source, timeout=15)
            stack.callback(lock.close)
            lock.execute("BEGIN IMMEDIATE")
            stack.callback(lock.rollback)
        datasets = {}
        for name, source in sorted(sources.items()):
            relative = "databases/" + name + ".db"
            target = destination / relative
            # A separate reader observes the committed state protected by writer locks.
            copied = sqlite3.connect(target)
            try:
                with _readonly(source) as original:
                    original.backup(copied)
                    copied.execute("PRAGMA journal_mode=DELETE")
            finally:
                copied.close()
            inspection = _inspect_database(target)
            manifest["databases"][name] = {
                "file": relative,
                "sha256": _hash(target),
                "tables": inspection["tables"],
                "namespaces": inspection["namespaces"],
            }
            manifest["namespaces"].extend(inspection["namespaces"])
            for data in inspection["datasets"]:
                datasets[data["id"]] = data
        manifest["namespaces"] = sorted(set(manifest["namespaces"]))
        if expected_namespace and expected_namespace not in manifest["namespaces"]:
            raise ValueError("예상한 계좌 namespace가 백업 DB에 없습니다.")
        if datasets and artifact_dir is None:
            raise ValueError("연구 DB와 함께 immutable artifact 경로를 지정하세요.")
        if datasets:
            artifacts = Path(artifact_dir).resolve()
            for identifier, dataset in datasets.items():
                source_dir = _safe_file(artifacts, identifier)
                original_json = _safe_file(source_dir, "dataset.json")
                source_manifest = _safe_file(source_dir, "manifest.json")
                if not original_json.is_file() or not source_manifest.is_file():
                    raise ValueError(
                        "데이터셋 export가 완료되지 않았습니다. 완료 후 다시 백업하세요."
                    )
                stored_json = json.loads(original_json.read_text(encoding="utf-8"))
                exported = json.loads(source_manifest.read_text(encoding="utf-8"))
                if stored_json != dataset or exported.get("dataset_hash") != dataset["hash"]:
                    raise ValueError("연구 DB와 artifact의 snapshot/hash가 다릅니다.")
                files = {
                    "dataset.json": _hash(original_json),
                    "manifest.json": _hash(source_manifest),
                }
                for table in exported.get("tables", {}).values():
                    source_file = _safe_file(source_dir, table["file"])
                    if _hash(source_file) != table["sha256"]:
                        raise ValueError("Parquet artifact checksum이 다릅니다.")
                    files[table["file"]] = table["sha256"]
                for filename, digest in files.items():
                    original_file = _safe_file(source_dir, filename)
                    relative = f"research_artifacts/{identifier}/{filename}"
                    target = _safe_file(destination, relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original_file, target)
                    if _hash(target) != digest or _hash(original_file) != digest:
                        raise ValueError(
                            "복사 도중 artifact가 바뀌었습니다. 백업을 다시 실행하세요."
                        )
                    manifest["artifacts"][relative] = {
                        "sha256": digest,
                        "bytes": target.stat().st_size,
                    }
    (destination / "manifest.json").write_text(canonical(manifest), encoding="utf-8")
    (destination / "manifest.sha256").write_text(content_hash(manifest), encoding="ascii")
    return {"path": str(destination), "manifest": manifest, "verified": True}


def verify_backup_bundle(bundle, *, expected_namespace=None):
    root = Path(bundle).resolve()
    manifest_path = _safe_file(root, "manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format_version") != 1 or content_hash(manifest) != (
        _safe_file(root, "manifest.sha256").read_text(encoding="ascii").strip()
    ):
        raise ValueError("백업 manifest 버전/checksum이 다릅니다.")
    namespaces = set()
    for item in manifest["databases"].values():
        path = _safe_file(root, item["file"])
        if _hash(path) != item["sha256"]:
            raise ValueError("백업 DB checksum이 다릅니다.")
        actual = _inspect_database(path)
        if actual["namespaces"] != item["namespaces"] or actual["tables"] != item["tables"]:
            raise ValueError("백업 DB schema/namespace가 manifest와 다릅니다.")
        namespaces.update(actual["namespaces"])
    if sorted(namespaces) != manifest["namespaces"]:
        raise ValueError("백업 namespace 목록이 일치하지 않습니다.")
    if expected_namespace and expected_namespace not in namespaces:
        raise ValueError("이 백업은 요청한 계좌 namespace를 포함하지 않습니다.")
    for relative, item in manifest["artifacts"].items():
        path = _safe_file(root, relative)
        if _hash(path) != item["sha256"] or path.stat().st_size != item["bytes"]:
            raise ValueError("백업 artifact checksum/크기가 다릅니다.")
    return manifest


def _quarantine_runtime(path, *, bundle_hash):
    inspection = _inspect_database(path)
    if "runtime_documents" not in inspection["tables"]:
        return []
    now = datetime.now(timezone.utc).isoformat()
    summaries = []
    connection = sqlite3.connect(path)
    try:
        with connection:
            for namespace in inspection["namespaces"]:

                def document(kind, default):
                    row = connection.execute(
                        "SELECT payload FROM runtime_documents "
                        "WHERE namespace=? AND kind=? AND key='latest'",
                        (namespace, kind),
                    ).fetchone()
                    return json.loads(row[0]) if row else default

                def put(kind, payload):
                    connection.execute(
                        "INSERT INTO runtime_documents VALUES(?,?,'latest',?) "
                        "ON CONFLICT(namespace,kind,key) DO UPDATE SET payload=excluded.payload",
                        (namespace, kind, canonical(payload)),
                    )

                control = document("control", {})
                put(
                    "control",
                    {
                        **control,
                        "entries_enabled": False,
                        "restore_reconciliation_required": True,
                        "updated_at": now,
                    },
                )
                deployment = document("deployment", {})
                put(
                    "deployment",
                    {
                        **deployment,
                        "enabled": False,
                        "version": int(deployment.get("version", 0)) + 1,
                        "restore_reconciliation_required": True,
                        "updated_at": now,
                    },
                )
                pending = []
                for identifier, state, payload in connection.execute(
                    "SELECT id,state,payload FROM execution_orders WHERE namespace=?",
                    (namespace,),
                ).fetchall():
                    if state in {"filled", "cancelled", "rejected", "expired"}:
                        continue
                    order = json.loads(payload)
                    order.update(
                        state="unknown",
                        updated_at=now,
                        restore_previous_state=state,
                        restore_quarantined=True,
                        error="복원 snapshot 이후 전송 여부를 전체 브로커 이력과 대조해야 합니다.",
                    )
                    order.setdefault("events", []).append(
                        {"at": now, "state": "unknown", "reason": "restored_snapshot_quarantine"}
                    )
                    connection.execute(
                        "UPDATE execution_orders SET state='unknown',payload=? WHERE id=?",
                        (canonical(order), identifier),
                    )
                    pending.append(identifier)
                connection.execute(
                    "UPDATE runtime_jobs SET state='interrupted',finished_at=?,error=? "
                    "WHERE namespace=? AND state IN ('queued','running')",
                    (
                        now,
                        "복원된 작업은 재전송하지 않습니다. 계좌 대조 후 새 작업을 등록하세요.",
                        namespace,
                    ),
                )
                connection.execute(
                    "UPDATE execution_commands SET state='unknown',result=? "
                    "WHERE namespace=? AND state IN ('sending','registered','queued')",
                    (canonical({"at": now, "restore_quarantined": True}), namespace),
                )
                put(
                    "reconciliation",
                    {
                        "status": "mismatch",
                        "at": now,
                        "reason": "restored_full_broker_reconciliation_required",
                    },
                )
                put("worker", {"state": "stopped", "at": now, "reason": "restored"})
                put("stream", {"connected": False, "reconciled": False, "at": now})
                put(
                    "watchlist",
                    {
                        "items": [],
                        "all_signals": [],
                        "as_of": None,
                        "restored": True,
                        "generated_at": now,
                    },
                )
                previous_generation = document("execution_generation", {}).get("value", 0)
                put("execution_generation", {"value": int(previous_generation) + 1})
                restored = {
                    "status": "needs_reconciliation",
                    "at": now,
                    "bundle_hash": bundle_hash,
                    "pending_order_ids": pending,
                    "manual_reenable_required": True,
                }
                put("restore", restored)
                summaries.append({"namespace": namespace, **restored})
    finally:
        connection.close()
    return summaries


def restore_backup_bundle(bundle, destination, *, expected_namespace):
    """Verify first, then restore into a new directory; never overwrite active state."""
    root, destination = Path(bundle).resolve(), Path(destination).resolve()
    if not expected_namespace:
        raise ValueError("복원할 계좌 namespace를 명시하세요.")
    if destination.exists() or destination.is_relative_to(root):
        raise ValueError("복원 대상은 bundle 밖의 새 폴더여야 합니다.")
    manifest = verify_backup_bundle(root, expected_namespace=expected_namespace)
    destination.mkdir(parents=True)
    restored_paths, quarantined = {}, []
    for name, item in manifest["databases"].items():
        # Logical names are validated independently of untrusted manifest paths.
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,40}", name):
            raise ValueError("백업 DB 이름 형식이 잘못되었습니다.")
        target = destination / (name + ".db")
        shutil.copyfile(_safe_file(root, item["file"]), target)
        if _hash(target) != item["sha256"]:
            raise ValueError("복원 도중 DB가 바뀌었습니다.")
        quarantined.extend(_quarantine_runtime(target, bundle_hash=content_hash(manifest)))
        _inspect_database(target)
        restored_paths[name] = str(target)
    for relative in manifest["artifacts"]:
        target = _safe_file(destination, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_safe_file(root, relative), target)
        if _hash(target) != manifest["artifacts"][relative]["sha256"]:
            raise ValueError("복원 도중 artifact가 바뀌었습니다.")
    receipt = {
        "source_bundle_hash": content_hash(manifest),
        "restored_at": datetime.now(timezone.utc).isoformat(),
        "databases": restored_paths,
        "runtime": quarantined,
        "entries_enabled": False,
        "deployment_enabled": False,
        "requires_full_broker_reconciliation": True,
        "restored_database_hashes": {
            name: _hash(Path(path)) for name, path in restored_paths.items()
        },
    }
    (destination / "restore-receipt.json").write_text(canonical(receipt), encoding="utf-8")
    return {"path": str(destination), **receipt}
