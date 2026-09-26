"""Local operator tools. Credentials stay in .env; no order submission command exists."""

import argparse
import json
import os
from datetime import date, timedelta
from pathlib import Path

from invest_lab.adapters.runtime import JOB_KINDS, build_runtime
from invest_lab.bootstrap import build_workspace
from invest_lab.domain.errors import TradingBlocked

ROOT = Path(__file__).resolve().parents[3]


def read_json(path):
    path = Path(path)
    if path.stat().st_size > 100_000_000:
        raise ValueError("입력 파일이 100MB를 초과합니다.")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def parser():
    result = argparse.ArgumentParser(description="invest-lab local operations")
    result.add_argument(
        "--mode",
        choices=["local_simulation", "kis_paper"],
        default=os.getenv("INVEST_MODE", "local_simulation"),
    )
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("paper-read-check")
    master = sub.add_parser("master-download")
    master.add_argument("--classifications", help="Reviewed symbol -> kospi/etf_kr/etf_us JSON")
    market = sub.add_parser("import-market")
    market.add_argument("kind", choices=["instruments", "calendar", "bars"])
    market.add_argument(
        "file", help="JSON with rows/source/as_of or symbol/rows/source/available_at"
    )
    dataset = sub.add_parser("import-dataset")
    dataset.add_argument("file")
    build = sub.add_parser("build-dataset")
    build.add_argument("file", help="ResearchDataPipeline parameter JSON")
    queue = sub.add_parser("enqueue")
    queue.add_argument("kind", choices=sorted(JOB_KINDS))
    queue.add_argument("--payload", help="JSON filename")
    observe = sub.add_parser("observe")
    observe.add_argument("start")
    observe.add_argument("end")
    observe.add_argument("--minimum-sessions", type=int, default=20)
    baseline = sub.add_parser("rebaseline")
    baseline.add_argument(
        "--reason", required=True, help="Confirmed external flow/reset/corporate action reason"
    )
    baseline.add_argument("--confirm", action="store_true")
    resolve = sub.add_parser("resolve-command")
    resolve.add_argument("command_id")
    resolve.add_argument(
        "evidence", help="JSON with receipt, outcome=acknowledged/rejected, reason"
    )
    intent = sub.add_parser("resolve-order")
    intent.add_argument("order_id")
    intent.add_argument("evidence", help="JSON with outcome=matched/not_submitted, receipt, reason")
    verify = sub.add_parser("verify-amendment")
    verify.add_argument("order_id")
    verify.add_argument("--reason", required=True)
    verify.add_argument("--confirm-semantics", action="store_true")
    costs = sub.add_parser("record-costs")
    costs.add_argument("file", help="JSON array of broker statement costs and exact fill totals")
    sub.add_parser("backup")
    restore = sub.add_parser("restore")
    restore.add_argument("bundle")
    restore.add_argument("destination")
    return result


def execute(args):
    runtime = build_runtime(build_workspace(), root=ROOT, mode=args.mode)
    if args.command == "doctor":
        return {
            "context": runtime.context(),
            "market": runtime.market_store.overview() if runtime.market_store else None,
            "source_keys": source_key_readiness(ROOT / ".env"),
            "orders_enabled": bool(runtime.settings and runtime.settings.orders_enabled),
            "deployment": runtime.store.document("deployment"),
            "secrets_displayed": False,
        }
    if args.command == "paper-read-check":
        if runtime.store.mode != "kis_paper":
            raise ValueError("--mode kis_paper를 사용하세요.")
        runtime.settings.require(account=True)
        quote = runtime.broker.quote("005930")
        result = runtime.execution.reconcile()
        return {
            "mode": "kis_paper",
            "quote_read": bool(quote["price"]),
            "account_read": bool(runtime.store.document("account")),
            "reconciliation": result["status"],
            "broker_order_count": result["broker_order_count"],
            "orders_submitted": 0,
            "at": runtime.at(),
        }
    if (
        args.command in {"master-download", "import-market", "build-dataset"}
        and not runtime.market_store
    ):
        raise ValueError("실제 시장 저장소는 --mode kis_paper에서 사용하세요.")
    if args.command == "master-download":
        from invest_lab.adapters.market_data import MarketMasterDownloader

        return MarketMasterDownloader(runtime.market_store).download_kis_kospi(
            classifications=read_json(args.classifications) if args.classifications else {}
        )
    if args.command == "import-market":
        payload = read_json(args.file)
        count = getattr(runtime.market_store, "import_" + args.kind)(**payload)
        return {"imported": count, "kind": args.kind}
    if args.command == "import-dataset":
        result = runtime.research.import_dataset(read_json(args.file))
        return {key: result[key] for key in ("id", "name", "coverage") if key in result}
    if args.command == "build-dataset":
        from invest_lab.adapters.data_pipeline import ResearchDataPipeline

        result = ResearchDataPipeline(runtime.market_store, runtime.research).import_market_dataset(
            **read_json(args.file)
        )
        return {
            key: result[key]
            for key in ("id", "name", "coverage", "transfer_report")
            if key in result
        }
    if args.command == "enqueue":
        return runtime.store.enqueue(args.kind, read_json(args.payload) if args.payload else {})
    if args.command == "observe":
        start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
        if end < start + timedelta(days=27) or args.minimum_sessions < 15:
            raise ValueError("관찰 기간은 28일 이상, 최소 거래일은 15일 이상으로 설정하세요.")
        if any(o["state"] in {"sending", "unknown"} for o in runtime.store.all_orders()):
            raise TradingBlocked("미확정 주문을 먼저 해결하세요.")
        value = {
            "start": str(start),
            "end": str(end),
            "minimum_sessions": args.minimum_sessions,
            "deployment_version": runtime.store.document("deployment")["version"],
            "created_at": runtime.at(),
        }
        runtime.store.put_document("observation", value)
        return value
    if args.command == "rebaseline":
        if not args.confirm or not args.reason.strip():
            raise ValueError("계좌 외부변동을 확인한 뒤 --confirm과 사유를 입력하세요.")
        with runtime.store.execution_lock():
            if any(
                o["state"] not in {"filled", "cancelled", "rejected", "expired"}
                for o in runtime.store.all_orders()
            ):
                raise TradingBlocked(
                    "접수/미확정/등록 주문이 남아 있으면 기준선을 바꿀 수 없습니다."
                )
            account = runtime.broker.account()
            old = runtime.store.document("baseline")
            if old:
                runtime.store.put_document(
                    "baseline_history",
                    {**old, "ended_at": runtime.at(), "reason": args.reason},
                    old["segment_id"],
                )
            baseline = {
                **account,
                "started_at": runtime.at(),
                "segment_id": runtime.at(),
                "reason": args.reason,
            }
            runtime.store.put_document("baseline", baseline)
            runtime.store.put_document("account", account)
            runtime.store.put_document(
                "position_ledger",
                {
                    "quantities": {p["symbol"]: p["quantity"] for p in account["positions"]},
                    "applied": {o["id"]: o["filled_quantity"] for o in runtime.store.all_orders()},
                },
            )
            for allocation in runtime.store.documents("allocation"):
                # Rebaseline never assumes external holdings belong to an automated strategy.
                if allocation.get("symbol"):
                    runtime.store.put_document(
                        "allocation", {**allocation, "managed_quantity": 0}, allocation["symbol"]
                    )
            with runtime.store.connect() as con:
                con.execute(
                    "DELETE FROM runtime_documents WHERE namespace=? AND kind='allocation'",
                    (runtime.store.namespace,),
                )
            monetary = runtime.store.document("monetary_ledger")
            if monetary:
                runtime.store.put_document(
                    "monetary_history", {**monetary, "reason": args.reason}, runtime.at()
                )
                with runtime.store.connect() as con:
                    con.execute(
                        "DELETE FROM runtime_documents WHERE namespace=? "
                        "AND kind='monetary_ledger'",
                        (runtime.store.namespace,),
                    )
            runtime.control(False)
            runtime.store.enqueue("reconcile")
            return {
                "new_segment": baseline["segment_id"],
                "entries_enabled": False,
                "reason": args.reason,
            }
    if args.command == "resolve-command":
        return resolve_command(runtime, args.command_id, read_json(args.evidence))
    if args.command == "resolve-order":
        return resolve_order(runtime, args.order_id, read_json(args.evidence))
    if args.command == "verify-amendment":
        return verify_amendment(runtime, args.order_id, args.reason, args.confirm_semantics)
    if args.command == "record-costs":
        return record_costs(runtime, read_json(args.file))
    if args.command == "backup":
        return runtime.run_job({"kind": "backup", "payload": {}, "id": "manual"})
    if args.command == "restore":
        from invest_lab.adapters.backup import restore_backup_bundle

        return restore_backup_bundle(
            Path(args.bundle), Path(args.destination), expected_namespace=runtime.store.namespace
        )
    raise ValueError("지원하지 않는 명령입니다.")


def record_costs(runtime, rows):
    from invest_lab.domain.orders import decimal

    if not isinstance(rows, list):
        raise ValueError("비용 증빙은 JSON 배열로 입력하세요.")
    with runtime.store.execution_lock():
        checked = []
        for row in rows:
            order = runtime.store.get_order(row["order_id"])
            if (
                row.get("verified_by_user") is not True
                or not row.get("source")
                or order["state"] not in {"filled", "cancelled"}
                or not order["filled_quantity"]
                or row.get("filled_quantity") != order["filled_quantity"]
                or decimal(row.get("filled_amount")) != decimal(order["filled_amount"])
                or decimal(row.get("fee")) < 0
            ):
                raise TradingBlocked("브로커 비용 증빙의 출처/완료 체결 수량/금액을 확인하세요.")
            checked.append(row)
        for row in checked:
            runtime.store.put_document("order_cost", {**row, "at": runtime.at()}, row["order_id"])
        runtime.store.enqueue("reconcile")
        return {"recorded": len(checked), "reconciliation_required": True, "order_resent": False}


def resolve_order(runtime, identifier, evidence):
    if not evidence.get("operator_checked") or not evidence.get("reason"):
        raise ValueError("브로커 확인 여부 operator_checked와 확인 사유를 입력하세요.")
    with runtime.store.execution_lock():
        order = runtime.store.get_order(identifier)
        if order["state"] != "unknown" or order.get("broker_order_id"):
            raise ValueError("접수번호가 없는 미확정 주문만 이 절차로 확인할 수 있습니다.")
        session = order.get("trading_date") or order["context"]["valid_session"]
        rows = runtime.broker.orders(session, session)
        matches = [
            r for r in rows if all(r[key] == order[key] for key in ("symbol", "side", "quantity"))
        ]
        if evidence.get("outcome") == "not_submitted":
            if matches:
                raise TradingBlocked(
                    "유사한 브로커 주문이 있습니다. 미전송으로 처리할 수 없습니다."
                )
            result = runtime.store.transition(
                identifier,
                {"unknown"},
                "expired",
                runtime.at(),
                operator_resolution=evidence,
                error="브로커 미전송 확인. 재전송하지 않고 만료 처리.",
            )
        elif evidence.get("outcome") == "matched":
            receipt = evidence.get("receipt") or {}
            actual = [r for r in matches if r["broker_order_id"] == receipt.get("broker_order_id")]
            if len(actual) != 1:
                raise TradingBlocked("제공한 접수번호와 주문 내용을 대조하지 못했습니다.")
            if any(
                o.get("broker_order_id") == receipt["broker_order_id"]
                and o.get("trading_date") == session
                for o in runtime.store.all_orders()
                if o["id"] != identifier
            ):
                raise TradingBlocked("다른 로컬 주문과 연결된 접수번호입니다.")
            result = runtime.store.transition(
                identifier,
                {"unknown"},
                "acknowledged",
                runtime.at(),
                broker_order_id=receipt["broker_order_id"],
                branch_id=actual[0].get("branch_id"),
                trading_date=session,
                operator_resolution=evidence,
            )
        else:
            raise ValueError("outcome은 matched/not_submitted 중 하나입니다.")
        runtime.store.enqueue("reconcile")
        return {"order_id": identifier, "state": result["state"], "order_resent": False}


def verify_amendment(runtime, identifier, reason, confirmed):
    from invest_lab.domain.order_chain import reconcile_order_chain

    if not confirmed or not reason.strip():
        raise ValueError("브로커 정정 회계를 확인한 뒤 --confirm-semantics와 사유를 입력하세요.")
    with runtime.store.execution_lock():
        order = runtime.store.get_order(identifier)
        commands = runtime.store.commands_for(identifier, details=True)
        rows = runtime.broker.orders(order["trading_date"], order["trading_date"])
        graph = reconcile_order_chain(
            rows,
            order["broker_order_id"],
            confirmed_amend_receipts=order.get("amendments", []),
            unresolved_commands=[c for c in commands if c["state"] in {"unknown", "sending"}],
            semantics_verified=True,
        )
        if graph["status"] == "unknown" or len(graph["chain_order_ids"]) < 2:
            raise TradingBlocked("원주문/자식 연결과 체결·이동·취소 수량이 검증되지 않았습니다.")
        runtime.store.put_document(
            "capabilities",
            {
                "amendment_semantics_verified": True,
                "at": runtime.at(),
                "reason": reason,
                "evidence_order_id": identifier,
                "chain": graph,
            },
        )
        runtime.store.enqueue("reconcile")
        return {"evidence_saved": True, "order_resent": False, "reconciliation_required": True}


def resolve_command(runtime, identifier, evidence):
    """Accept operator-supplied broker evidence, not inferred success or resubmission."""
    if evidence.get("outcome") not in {"acknowledged", "rejected"} or not evidence.get("reason"):
        raise ValueError("증권사 확인 결과 outcome/receipt/reason을 입력하세요.")
    with runtime.store.execution_lock():
        order, command = None, None
        for candidate in runtime.store.all_orders():
            match = next(
                (
                    c
                    for c in runtime.store.commands_for(candidate["id"], details=True)
                    if c["id"] == identifier
                ),
                None,
            )
            if match:
                order, command = candidate, match
                break
        if not order or command["state"] not in {"unknown", "sending"}:
            raise ValueError("결과 미확정 명령을 찾지 못했습니다.")
        receipt = evidence.get("receipt") or {}
        if evidence["outcome"] == "acknowledged":
            rows = runtime.broker.orders(order["trading_date"], order["trading_date"])
            confirmed = [
                r
                for r in rows
                if r["broker_order_id"] == receipt.get("broker_order_id")
                and r["symbol"] == order["symbol"]
                and r["side"] == order["side"]
            ]
            if len(confirmed) != 1:
                raise TradingBlocked(
                    "제공된 접수번호를 해당 거래일/종목/방향의 브로커 이력에서 확인하지 못했습니다."
                )
            if command["kind"] == "amend":
                runtime.store.transition(
                    order["id"],
                    {order["state"]},
                    order["state"],
                    runtime.at(),
                    amendments=[*order.get("amendments", []), receipt],
                )
        runtime.store.finish_command(
            identifier,
            evidence["outcome"],
            {"receipt": receipt, "operator_evidence": evidence["reason"], "at": runtime.at()},
        )
        runtime.store.put_document(
            "command_resolution", {"id": identifier, **evidence, "at": runtime.at()}, identifier
        )
        runtime.store.enqueue("reconcile")
        return {
            "command_id": identifier,
            "evidence_recorded": True,
            "reconciliation_required": True,
            "order_resent": False,
        }


def source_key_readiness(path):
    from dotenv.parser import parse_stream

    values = {}
    if path.exists():
        with path.open(encoding="utf-8-sig") as stream:
            for binding in parse_stream(stream):
                if not binding.error and binding.key:
                    values[binding.key] = binding.value
    return {
        key: bool(os.getenv(key) or values.get(key))
        for key in ("DART_API_KEY", "FRED_API_KEY", "KRX_API_KEY")
    }


def main():
    try:
        result = execute(parser().parse_args())
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    except (ValueError, LookupError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        raise SystemExit(2) from None
    except Exception as error:
        # Broker URLs, raw response bodies and credentials are deliberately omitted.
        print(
            json.dumps(
                {
                    "error": type(error).__name__,
                    "message": "작업 실패. 연결/자료/대조 상태를 확인하세요.",
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
