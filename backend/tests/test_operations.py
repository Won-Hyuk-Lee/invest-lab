from types import SimpleNamespace
from uuid import uuid4

import pytest

from invest_lab.adapters.backup import restore_backup_bundle
from invest_lab.adapters.runtime import build_runtime
from invest_lab.adapters.runtime_store import RuntimeStore
from invest_lab.cli import parser, record_costs, resolve_command, resolve_order, verify_amendment
from invest_lab.domain.errors import TradingBlocked


@pytest.fixture
def runtime(workspace, tmp_path, monkeypatch):
    monkeypatch.delenv("INVEST_STATE_DIR", raising=False)
    return build_runtime(workspace, root=tmp_path, mode="local_simulation")


def test_reports_preserve_unknown_costs_and_never_label_demo_a_month(runtime):
    runtime.execution.reconcile()
    order = runtime.execution.register(
        {
            "client_request_id": str(uuid4()),
            "symbol": "005930",
            "side": "buy",
            "quantity": 1,
            "order_type": "market",
        }
    )
    runtime.execution.submit(order["id"])
    runtime.execution.reconcile()
    saved = runtime.store.get_order(order["id"])
    runtime.store.transition(order["id"], {saved["state"]}, saved["state"], runtime.at(), fee=None)
    snapshot = runtime.report()
    assert snapshot["fees"] is None and snapshot["costs_verified"] is False
    report = runtime.reports()
    assert report["monthly"][0]["fees"] is None
    assert report["monthly"][0]["start_equity"] == "10000000"
    assert report["observation_complete"] is False
    assert "synthetic_mode" in report["observation_reasons"]


def test_whole_runtime_backup_restores_disabled_new_namespace_path(runtime, tmp_path):
    runtime.execution.reconcile()
    runtime.research.create_demo_dataset()
    result = runtime.run_job({"kind": "backup", "payload": {}, "id": "test"})
    bundle = runtime.store.document("backup")["path"]
    assert result
    restored = restore_backup_bundle(
        bundle, tmp_path / "restored", expected_namespace=runtime.store.namespace
    )
    store = RuntimeStore(restored["databases"]["runtime-local_simulation"])
    assert store.document("control")["entries_enabled"] is False
    assert store.document("control")["restore_reconciliation_required"] is True
    assert (tmp_path / "restored" / "research_artifacts").is_dir()


def test_restore_cannot_enable_entries_before_new_broker_reconciliation(runtime):
    runtime.store.put_document("restore", {"at": runtime.at(), "status": "needs_reconciliation"})
    runtime.store.put_document(
        "control", {"entries_enabled": False, "restore_reconciliation_required": True}
    )
    with pytest.raises(TradingBlocked, match="복원"):
        runtime.control(True)


def test_market_collection_stops_on_gap_without_queueing_daily_finalize(runtime):
    runtime.store.mode = "kis_paper"
    runtime.ingestion = SimpleNamespace(
        collect=lambda *a, **k: {"005930": {"status": "incomplete", "coverage": {}}},
        refresh_quotes=lambda symbols: pytest.fail("must not refresh after failed history"),
    )
    with pytest.raises(TradingBlocked, match="누락"):
        runtime.collect_market(
            {"symbols": ["005930"], "start": "2026-01-01", "end": "2026-09-25", "finalize": True}
        )
    assert not runtime.store.jobs()
    assert runtime.store.documents("collection_progress")[0]["state"] == "incomplete"


def test_market_collection_yields_between_symbols_and_priority_jobs(runtime):
    runtime.store.mode = "kis_paper"
    received = []

    def collect(symbols, *args, **kwargs):
        received.extend(symbols)
        return {symbol: {"status": "complete"} for symbol in symbols}

    runtime.ingestion = SimpleNamespace(collect=collect, refresh_quotes=lambda symbols: symbols)
    result = runtime.collect_market(
        {"symbols": ["005930", "000660"], "start": "2026-01-01", "end": "2026-09-25"}
    )
    assert received == ["005930"] and result["checkpoint"]["completed"] == 1
    priority = runtime.store.enqueue("close_session")
    assert runtime.store.claim_job("worker")["id"] == priority["id"]
    following = runtime.store.claim_job("worker")
    assert following["kind"] == "collect_market" and following["payload"]["index"] == 1


def test_unknown_command_requires_explicit_operator_evidence(runtime):
    order = runtime.execution.register(
        {
            "client_request_id": str(uuid4()),
            "symbol": "005930",
            "side": "buy",
            "quantity": 1,
            "order_type": "limit",
            "limit_price": "10000",
        }
    )
    runtime.execution.submit(order["id"])
    original = runtime.broker.cancel_order

    def lose_receipt(*args):
        original(*args)
        raise TimeoutError()

    runtime.broker.cancel_order = lose_receipt
    command = str(uuid4())
    runtime.execution.cancel(order["id"], command, 1)
    assert runtime.execution.reconcile()["status"] == "pending"
    with pytest.raises(ValueError):
        resolve_command(runtime, command, {})
    evidence = {
        "outcome": "acknowledged",
        "reason": "Operator checked broker record",
        "receipt": {"broker_order_id": runtime.store.get_order(order["id"])["broker_order_id"]},
    }
    result = resolve_command(runtime, command, evidence)
    assert result["order_resent"] is False
    assert runtime.execution.reconcile()["status"] == "matched"
    assert runtime.store.get_order(order["id"])["state"] == "cancelled"


def test_cli_exposes_only_paper_modes_and_read_probe():
    args = parser().parse_args(["--mode", "kis_paper", "paper-read-check"])
    assert args.mode == "kis_paper"
    with pytest.raises(SystemExit):
        parser().parse_args(["--mode", "live", "paper-read-check"])


def test_restored_unsubmitted_order_requires_evidence_and_expires_without_retry(runtime):
    order = runtime.execution.register(
        {
            "client_request_id": str(uuid4()),
            "symbol": "005930",
            "side": "buy",
            "quantity": 1,
            "order_type": "market",
        }
    )
    runtime.store.transition(order["id"], {"registered"}, "unknown", runtime.at())
    with pytest.raises(ValueError):
        resolve_order(runtime, order["id"], {"outcome": "not_submitted"})
    result = resolve_order(
        runtime,
        order["id"],
        {
            "outcome": "not_submitted",
            "operator_checked": True,
            "reason": "Broker history and restored snapshot reviewed",
        },
    )
    assert result["state"] == "expired" and result["order_resent"] is False
    assert runtime.execution.submit(order["id"])["state"] == "expired"


def test_broker_cost_evidence_requires_exact_final_fill(runtime):
    order = runtime.execution.register(
        {
            "client_request_id": str(uuid4()),
            "symbol": "005930",
            "side": "buy",
            "quantity": 1,
            "order_type": "market",
        }
    )
    runtime.execution.submit(order["id"])
    runtime.execution.reconcile()
    order = runtime.store.get_order(order["id"])
    proof = {
        "order_id": order["id"],
        "filled_quantity": 2,
        "filled_amount": order["filled_amount"],
        "fee": "0",
        "source": "broker_statement",
        "verified_by_user": True,
    }
    with pytest.raises(TradingBlocked):
        record_costs(runtime, [proof])
    assert record_costs(runtime, [{**proof, "filled_quantity": 1}])["recorded"] == 1


def test_amendment_capability_not_enabled_by_ack_without_chain_evidence(runtime):
    order = runtime.execution.register(
        {
            "client_request_id": str(uuid4()),
            "symbol": "005930",
            "side": "buy",
            "quantity": 1,
            "order_type": "limit",
            "limit_price": "10000",
        }
    )
    runtime.execution.submit(order["id"])
    with pytest.raises(ValueError):
        verify_amendment(runtime, order["id"], "", False)
    with pytest.raises(TradingBlocked):
        verify_amendment(runtime, order["id"], "operator reviewed", True)
    assert runtime.store.document("capabilities") is None
