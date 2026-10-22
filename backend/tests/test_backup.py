import json
from pathlib import Path

import pytest

from invest_lab.adapters.backup import (
    create_backup_bundle,
    restore_backup_bundle,
    verify_backup_bundle,
)
from invest_lab.adapters.market_data import MarketStore
from invest_lab.adapters.research import build_research_service
from invest_lab.adapters.runtime_store import RuntimeStore
from invest_lab.application.research import synthetic_research_dataset
from invest_lab.domain.research import canonical, content_hash


def backup_fixture(tmp_path, mode="kis_paper"):
    state = tmp_path / "state"
    store = RuntimeStore(state / f"runtime-{mode}.db", mode, "account-hash")
    store.put_document("control", {"entries_enabled": True})
    store.put_document("deployment", {"enabled": True, "version": 3})
    store.put_document("agent_control", {"enabled": True, "version": 2})
    store.put_document("reconciliation", {"status": "matched"})
    store.put_document("stream", {"connected": True, "reconciled": True})
    request = {
        "client_request_id": "unsubmitted",
        "symbol": "005930",
        "side": "buy",
        "quantity": 1,
        "order_type": "limit",
        "limit_price": "100",
    }
    order = store.create_intent(request, "2023-01-02T01:00:00+00:00", {})
    store.enqueue("submit_order", {"order_id": order["id"]}, "job-1")
    research = build_research_service(state / "research.db")
    dataset = research.import_dataset(synthetic_research_dataset(5))
    market = MarketStore(state / "market.db")
    paths = {
        f"runtime-{mode}": store.path,
        "research": research.repository.path,
        "market": market.path,
    }
    return store, research, dataset, paths, order


@pytest.mark.parametrize("mode", ["kis_paper", "kis_live"])
def test_bundle_restores_all_stores_and_quarantines_even_registered_intents(tmp_path, mode):
    store, research, dataset, paths, order = backup_fixture(tmp_path, mode)
    result = create_backup_bundle(
        tmp_path / "bundle",
        databases=paths,
        artifact_dir=research.repository.artifact_dir,
        expected_namespace=store.namespace,
    )
    assert result["verified"] is True
    receipt = restore_backup_bundle(
        tmp_path / "bundle", tmp_path / "restored", expected_namespace=store.namespace
    )
    restored = RuntimeStore(Path(receipt["databases"][f"runtime-{mode}"]), mode, "account-hash")
    assert restored.document("control")["entries_enabled"] is False
    assert restored.document("deployment")["enabled"] is False
    assert restored.document("deployment")["version"] == 4
    assert restored.document("agent_control")["enabled"] is False
    assert restored.document("agent_control")["version"] == 3
    assert restored.document("restore")["status"] == "needs_reconciliation"
    assert restored.document("reconciliation")["status"] == "mismatch"
    assert restored.get_order(order["id"])["state"] == "unknown"
    assert restored.get_order(order["id"])["restore_previous_state"] == "registered"
    assert restored.jobs()[0]["state"] == "interrupted"
    assert restored.document("watchlist")["items"] == []
    assert restored.document("stream")["connected"] is False
    again = build_research_service(receipt["databases"]["research"])
    assert again.repository.get_dataset(dataset["id"])["hash"] == dataset["hash"]
    assert (
        tmp_path / "restored" / "research_artifacts" / dataset["id"] / "manifest.json"
    ).is_file()
    # Original state and already-verified bundle are never rewritten by restore.
    assert store.get_order(order["id"])["state"] == "registered"
    assert store.document("control")["entries_enabled"] is True
    assert store.document("agent_control")["enabled"] is True
    assert verify_backup_bundle(tmp_path / "bundle")["namespaces"] == [store.namespace]


def test_restore_refuses_wrong_namespace_existing_destination_and_tampering(tmp_path):
    store, research, _, paths, _ = backup_fixture(tmp_path)
    create_backup_bundle(
        tmp_path / "bundle", databases=paths, artifact_dir=research.repository.artifact_dir
    )
    with pytest.raises(ValueError, match="namespace"):
        restore_backup_bundle(
            tmp_path / "bundle", tmp_path / "wrong", expected_namespace="kis_paper:other"
        )
    existing = tmp_path / "exists"
    existing.mkdir()
    with pytest.raises(ValueError, match="새 폴더"):
        restore_backup_bundle(tmp_path / "bundle", existing, expected_namespace=store.namespace)
    copied = tmp_path / "bundle" / "databases" / "market.db"
    with copied.open("ab") as output:
        output.write(b"corrupted")
    with pytest.raises(ValueError, match="checksum"):
        verify_backup_bundle(tmp_path / "bundle")


def test_bundle_rejects_unfinished_export_and_artifact_path_escape(tmp_path):
    store, research, dataset, paths, _ = backup_fixture(tmp_path)
    directory = research.repository.artifact_dir / dataset["id"]
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tables"]["bars"]["file"] = "../../outside.txt"
    manifest_path.write_text(canonical(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="상대 경로"):
        create_backup_bundle(
            tmp_path / "bad",
            databases=paths,
            artifact_dir=research.repository.artifact_dir,
            expected_namespace=store.namespace,
        )
    assert not (tmp_path / "bad" / "manifest.json").exists()


def test_manifest_cannot_escape_restore_even_with_recomputed_manifest_checksum(tmp_path):
    store, research, _, paths, _ = backup_fixture(tmp_path)
    result = create_backup_bundle(
        tmp_path / "bundle", databases=paths, artifact_dir=research.repository.artifact_dir
    )
    manifest = result["manifest"]
    manifest["databases"]["market"]["file"] = "../../market.db"
    (tmp_path / "bundle" / "manifest.json").write_text(canonical(manifest), encoding="utf-8")
    (tmp_path / "bundle" / "manifest.sha256").write_text(content_hash(manifest), encoding="ascii")
    with pytest.raises(ValueError, match="상대 경로"):
        restore_backup_bundle(
            tmp_path / "bundle", tmp_path / "escape", expected_namespace=store.namespace
        )
    assert not (tmp_path / "escape").exists()
