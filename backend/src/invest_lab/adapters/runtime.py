"""Composition of local operations; HTTP reads stored state and queues external work."""

import hashlib
import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo

from invest_lab.adapters.runtime_store import RuntimeStore
from invest_lab.adapters.simulated_broker import SimulatedBroker
from invest_lab.application.execution import ExecutionService
from invest_lab.domain.errors import TradingBlocked, VersionConflict
from invest_lab.domain.indicators import analyze
from invest_lab.domain.orders import TERMINAL, decimal
from invest_lab.domain.research import latest_visible, macro_features
from invest_lab.domain.strategies import (
    StrategyRules,
    evaluate_strategy,
    size_position,
    technical_features,
)

KST = ZoneInfo("Asia/Seoul")
JOB_KINDS = frozenset(
    {
        "reconcile",
        "collect_market",
        "analyze",
        "daily_pipeline",
        "backup",
        "submit_order",
        "cancel_order",
        "amend_order",
        "create_demo_dataset",
        "research_experiment",
        "collect_source",
        "auto_trade",
        "report",
        "daily_finalize",
        "build_dataset",
        "close_session",
    }
)


class Runtime:
    def __init__(
        self,
        workspace,
        store,
        broker,
        *,
        research=None,
        settings=None,
        config_error=None,
        market_store=None,
        ingestion=None,
        root=None,
    ):
        self.workspace, self.store, self.broker = workspace, store, broker
        self.research, self.settings, self.config_error = research, settings, config_error
        self.market_store, self.ingestion = market_store, ingestion
        self.root = Path(root) if root else store.path.parent.parent
        self.execution = ExecutionService(
            store,
            broker,
            workspace.clock,
            instruments=workspace.market.instruments,
            session_check=self._session_open if store.mode == "kis_paper" else None,
            strategy=workspace.strategies.latest,
            preflight=self._broker_preflight if store.mode == "kis_paper" else None,
            realtime_quote=self._realtime_quote if store.mode == "kis_paper" else None,
        )
        if store.document("control") is None:
            store.put_document(
                "control",
                {
                    "entries_enabled": store.mode == "local_simulation",
                    "risk_policy": {"max_position_pct": 20, "max_invested_pct": 50},
                },
            )
        if store.document("deployment") is None:
            store.put_document(
                "deployment",
                {
                    "version": 1,
                    "enabled": False,
                    "strategy_type": "kospi_pullback",
                    "rules": asdict(StrategyRules()),
                    "model_id": None,
                    "allocation_policy": "single_active",
                    "updated_at": self.at(),
                },
            )
        if store.mode == "local_simulation" and store.document("screen") is None:
            self.analyze_market()

    def at(self):
        return self.workspace.clock.now().isoformat()

    def _broker_preflight(self):
        if not self.settings:
            raise TradingBlocked("한투 모의 설정을 확인하세요.")
        try:
            self.settings.require(account=True, orders=True)
        except ValueError:
            raise TradingBlocked(
                "모의 키/계좌 설정과 KIS_PAPER_ORDERS_ENABLED를 확인하세요."
            ) from None

    def _session_open(self, at):
        if self.market_store is None:
            return False
        try:
            return self.market_store.is_open(at)
        except ValueError:
            return False

    def _realtime_quote(self, symbol):
        state = self.store.document("stream") or {}
        heartbeat = self.store.document("worker") or {}
        if (
            not all(state.get(key) for key in ("connected", "subscribed", "reconciled"))
            or state.get("worker_owner") != heartbeat.get("owner")
            or heartbeat.get("state") != "running"
        ):
            raise TradingBlocked("실시간 연결 복구와 계좌 대조가 필요합니다.")
        self.execution._fresh(state.get("at"), "실시간 연결 상태", 15)
        self.execution._fresh(heartbeat.get("at"), "작업자 상태", 90)
        try:
            return self.market_store.quote(symbol)
        except (ValueError, LookupError):
            raise TradingBlocked("해당 종목의 실시간 시세가 없습니다.") from None

    def context(self):
        account = self.store.document("account")
        heartbeat = self.store.document("worker") or {}
        age = None
        if heartbeat.get("at"):
            age = (
                self.workspace.clock.now() - datetime.fromisoformat(heartbeat["at"])
            ).total_seconds()
        configured = (
            self.settings.readiness() if self.settings else {"configured": False, "missing": []}
        )
        account_fresh = (
            bool(account)
            and 0
            <= (
                self.workspace.clock.now() - datetime.fromisoformat(account["observed_at"])
            ).total_seconds()
            <= 300
        )
        readiness = [
            {
                "name": "worker",
                "ready": heartbeat.get("state") != "stopped" and age is not None and 0 <= age < 90,
                "message": "작업자 실행 상태",
            },
            {
                "name": "broker",
                "ready": account_fresh if self.store.mode == "kis_paper" else True,
                "message": "한투 모의 조회 결과" if self.store.mode == "kis_paper" else "샘플 계좌",
            },
        ]
        if self.store.mode == "kis_paper":
            readiness += [
                {
                    "name": "credentials",
                    "ready": configured["configured"],
                    "message": self.config_error
                    or (
                        ", ".join(configured["missing"])
                        if configured["missing"]
                        else "로컬 설정 확인됨"
                    ),
                },
                {
                    "name": "calendar",
                    "ready": self._session_open(self.workspace.clock.now()),
                    "message": "검증된 정규장 세션만 주문 가능",
                },
            ]
        return {
            "mode": self.store.mode,
            "account_id": self.store.account_id,
            "account_alias": "한투 모의계좌"
            if self.store.mode == "kis_paper"
            else "로컬 샘플 계좌",
            "broker_connected": self.store.mode == "kis_paper" and account_fresh,
            "live_orders_enabled": False,
            "entries_enabled": self.store.document("control")["entries_enabled"],
            "readiness": readiness,
            "server_time": self.at(),
            "source": self.workspace.market.source,
        }

    def account(self):
        if self.store.mode == "local_simulation":
            value = self.broker.account()
            self.store.put_document("account", value)
        else:
            value = self.store.document("account")
            if value is None:
                raise TradingBlocked(
                    "모의계좌 조회 결과가 없습니다. 작업자에서 계좌 대조를 실행하세요."
                )
        baseline = self.store.document("baseline") or {}
        names = {i.symbol: i.name for i in self.workspace.market.instruments()}
        orders = self.store.list_orders(limit=10)["items"]
        return {
            **value,
            "profit": value.get(
                "profit", str(sum(decimal(p["profit"]) for p in value["positions"]))
            ),
            "mode": self.store.mode,
            "account_id": self.store.account_id,
            "initial_cash": baseline.get("equity"),
            "positions": [
                {**p, "name": names.get(p["symbol"], p["symbol"])} for p in value["positions"]
            ],
            "orders": [
                {
                    **o,
                    "request_id": o["client_request_id"],
                    "price": o.get("submitted_price") or o.get("limit_price") or "0",
                    "fee": o.get("fee"),
                    "at": o["created_at"],
                    "source": self.store.mode,
                }
                for o in orders
            ],
            "baseline": baseline or None,
            "reconciliation": self.store.document("reconciliation"),
        }

    def order(self, order_id):
        commands = self.store.commands_for(order_id)
        known = {c["id"] for c in commands}
        for job in self.store.jobs(10000):
            if (
                job["kind"] in {"submit_order", "cancel_order", "amend_order"}
                and job["payload"].get("order_id") == order_id
            ):
                identifier = job["payload"].get("client_request_id", f"submit:{order_id}")
                if job["state"] in {"queued", "running"} and identifier not in known:
                    commands.append({"id": identifier, "kind": job["kind"], "state": job["state"]})
        return {**self.store.get_order(order_id), "pending_commands": commands}

    def queue_submit(self, order_id):
        order = self.store.get_order(order_id)
        if order["state"] == "registered":
            job = self.store.enqueue("submit_order", {"order_id": order_id}, f"submit:{order_id}")
            if job["state"] in {"failed", "interrupted"}:
                self.store.enqueue(
                    "submit_order", {"order_id": order_id}, f"submit:{order_id}:retry:{uuid4()}"
                )
        return self.order(order_id)

    def queue_command(self, order_id, payload, amend=False):
        order = self.store.get_order(order_id)
        if order["state"] in TERMINAL:
            raise TradingBlocked("완료된 주문입니다.")
        kind = "amend_order" if amend else "cancel_order"
        command = {"order_id": order_id, **payload}
        # A job's immutable key also protects against a response-loss retry before worker pickup.
        previous = next(
            (
                j
                for j in self.store.jobs(10000)
                if j["job_key"] == "command:" + payload["client_request_id"]
            ),
            None,
        )
        if previous and (previous["payload"] != command or previous["kind"] != kind):
            raise VersionConflict("같은 명령 식별자를 다른 내용에 사용할 수 없습니다.")
        self.store.enqueue(kind, command, "command:" + payload["client_request_id"])
        return self.order(order_id)

    def control(self, entries_enabled):
        if type(entries_enabled) is not bool:
            raise ValueError("중지 상태를 확인하세요.")
        if entries_enabled and any(
            o["state"] in {"unknown", "sending"} for o in self.store.all_orders()
        ):
            raise TradingBlocked("미확정 주문을 먼저 대조하세요.")
        old = self.store.document("control") or {}
        if entries_enabled and old.get("restore_reconciliation_required"):
            reconciliation = self.store.document("reconciliation") or {}
            restored = self.store.document("restore") or {}
            if reconciliation.get("status") != "matched" or reconciliation.get(
                "at", ""
            ) <= restored.get("at", ""):
                raise TradingBlocked("복원 이후 브로커 전체 대조가 필요합니다.")
            old["restore_reconciliation_required"] = False
            self.store.put_document(
                "restore", {**restored, "status": "reconciled", "reenabled_at": self.at()}
            )
        self.store.put_document(
            "control", {**old, "entries_enabled": entries_enabled, "updated_at": self.at()}
        )
        return self.store.document("control")

    def save_deployment(self, payload):
        old = self.store.document("deployment")
        if payload["expected_version"] != old["version"]:
            raise VersionConflict(
                "운용 설정이 변경됐습니다. 초안을 유지하고 최신 버전을 확인하세요."
            )
        rules = StrategyRules.from_dict(
            {**payload.get("rules", {}), "strategy_type": payload["strategy_type"]}
        )
        model_id = payload.get("model_id")
        if model_id:
            model = next((m for m in self.research.list_models() if m["id"] == model_id), None)
            if not model or model.get("status") != "selected":
                raise ValueError("선택 완료한 모델만 운용에 연결할 수 있습니다.")
            if self.store.mode == "kis_paper" and (
                model.get("source_kind") == "synthetic" or not model.get("adoption_ready")
            ):
                raise TradingBlocked(
                    "실제 자료의 검증과 규칙 대비 비교를 통과한 모델이 필요합니다."
                )
        new = {
            "version": old["version"] + 1,
            "enabled": payload["enabled"],
            "strategy_type": rules.strategy_type,
            "rules": asdict(rules),
            "model_id": model_id,
            "allocation_policy": "single_active",
            "updated_at": self.at(),
        }
        # Compare-and-set is one transaction: two simultaneous editors cannot overwrite each other.
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            import json

            from invest_lab.adapters.runtime_store import encode

            row = con.execute(
                "SELECT payload FROM runtime_documents WHERE namespace=? AND "
                "kind='deployment' AND key='latest'",
                (self.store.namespace,),
            ).fetchone()
            if json.loads(row[0])["version"] != old["version"]:
                raise VersionConflict("운용 설정 저장 충돌")
            con.execute(
                "UPDATE runtime_documents SET payload=? WHERE namespace=? AND "
                "kind='deployment' AND key='latest'",
                (encode(new), self.store.namespace),
            )
        self.store.put_document("deployment_versions", new, str(new["version"]))
        return new

    def analyze_market(self):
        strategy = self.workspace.strategies.latest()
        deployment = self.store.document("deployment") or {"rules": asdict(StrategyRules())}
        rules = StrategyRules.from_dict(deployment["rules"])
        data = None
        if self.research:
            datasets = self.research.list_datasets()
            suitable = [
                d
                for d in datasets
                if self.store.mode == "local_simulation" or d["source_kind"] != "synthetic"
            ]
            if suitable:
                data = self.research.repository.get_dataset(suitable[0]["id"])
        at = self.workspace.clock.now()
        research_rows = {}
        if data:
            research_rows = {
                r["symbol"]: r
                for r in self.research.features(data["id"], as_of=at.isoformat())["rows"]
            }
        macro = macro_features(data["data"]["macro"], at) if data else None
        metadata = (
            {m["symbol"]: m for m in latest_visible(data["data"]["etf_metadata"], at, ("symbol",))}
            if data
            else {}
        )
        catalog = (
            {i["symbol"]: i for i in self.market_store.instruments(as_of=at)}
            if self.market_store
            else {}
        )
        rows, signals, errors = [], [], []
        for instrument in self.workspace.market.instruments():
            try:
                instrument_data = {**asdict(instrument), **catalog.get(instrument.symbol, {})}
                bars = self.workspace.market.history(instrument.symbol)
                analysis = analyze(instrument.symbol, bars, strategy)
                quote = self.workspace.market.quote(instrument.symbol)
                row = {
                    **asdict(instrument),
                    **asdict(analysis),
                    "as_of": analysis.as_of.isoformat() if analysis.as_of else None,
                    "quote": {
                        **asdict(quote),
                        "price": str(quote.price),
                        "observed_at": quote.observed_at.isoformat(),
                    },
                }
                bar_rows = (
                    self.market_store.bar_rows(instrument.symbol, adjusted=True, as_of=at)
                    if self.market_store
                    else [
                        {**asdict(b), "session": b.session.isoformat(), "turnover": 0} for b in bars
                    ]
                )
                if self.market_store and bar_rows:
                    coverage = self.market_store.coverage(
                        instrument.symbol,
                        bar_rows[0]["session"],
                        bar_rows[-1]["session"],
                        adjusted=True,
                        as_of=at,
                    )
                    if not coverage["complete"]:
                        raise TradingBlocked("중간 거래일/달력 누락이 있습니다.")
                research_row = research_rows.get(instrument.symbol, {})
                technical = technical_features(bar_rows)
                if research_row.get("technical", {}).get("session") == technical.get("session"):
                    technical = research_row["technical"]
                financial = research_row.get("financial")
                symbol_macro = research_row.get("macro", macro)
                etf = (research_row.get("etf") or {}).get(
                    "metadata", metadata.get(instrument.symbol)
                )
                result = evaluate_strategy(
                    technical,
                    instrument_data,
                    rules,
                    financial=financial,
                    macro=symbol_macro,
                    etf=etf,
                )
                signal = {
                    **instrument_data,
                    **result,
                    "as_of": row["as_of"],
                    "technical": technical,
                    "financial": financial,
                    "macro": symbol_macro,
                    "etf": etf,
                    "strategy_version": deployment.get("version", 1),
                    "source": self.workspace.market.source,
                    "dataset_id": data["id"] if data else None,
                }
                signals.append(signal)
                rows.append({**row, "strategy_signal": result, "technical": technical})
            except (ValueError, LookupError, RuntimeError) as error:
                errors.append({"symbol": instrument.symbol, "reason": type(error).__name__})
        screen = {
            "rows": rows,
            "source": self.workspace.market.source,
            "strategy": asdict(strategy),
            "generated_at": self.at(),
            "period_basis": "trading_sessions",
            "errors": errors,
            "coverage": {
                "expected": len(rows) + len(errors),
                "available": len(rows),
                "missing": len(errors),
            },
        }
        self.store.put_document("screen", screen)
        if deployment.get("model_id"):
            model = next(
                (m for m in self.research.list_models() if m["id"] == deployment["model_id"]), None
            )
            if (
                not model
                or model.get("status") != "selected"
                or (self.store.mode == "kis_paper" and model.get("source_kind") == "synthetic")
            ):
                raise TradingBlocked("운용 모델의 선택 상태/자료 원천을 확인하세요.")
            if not data or (
                self.store.mode == "kis_paper"
                and (not model.get("adoption_ready") or not data["coverage"]["pit_ready"])
            ):
                raise TradingBlocked("운용 모델과 추론 자료의 검증이 필요합니다.")
            predictions = self.research.predict(model["id"], data["id"], as_of=at.isoformat())
            prediction_map = {r["symbol"]: r["prediction"] for r in predictions["rows"]}
            threshold = predictions["threshold"]
            for signal in signals:
                prediction = prediction_map.get(signal["symbol"])
                feature_session = (
                    research_rows.get(signal["symbol"], {}).get("technical", {}).get("session")
                )
                allowed = (
                    prediction is not None
                    and prediction > threshold
                    and feature_session == signal["as_of"]
                )
                signal.update(
                    model_id=model["id"],
                    prediction=prediction,
                    rank=prediction,
                    eligible=signal["eligible"] and allowed,
                )
                if not allowed:
                    signal["reasons"].append("model_filter_or_feature_coverage")
        eligible = sorted(
            [s for s in signals if s["eligible"]], key=lambda s: s.get("rank") or 0, reverse=True
        )[:20]
        watch = {
            "items": eligible,
            "all_signals": signals,
            "as_of": max((s["as_of"] for s in signals if s["as_of"]), default=None),
            "generated_at": self.at(),
            "source": self.workspace.market.source,
            "errors": errors,
            "deployment_version": deployment.get("version", 1),
        }
        self.store.put_document("watchlist", watch)
        return {"analyzed": len(rows), "candidates": len(eligible), "errors": errors}

    def operations(self):
        return {
            "jobs": self.store.jobs(),
            "reconciliations": self.store.documents("reconciliation_history")[-50:],
            "health": self.context(),
            "entries_enabled": self.store.document("control")["entries_enabled"],
            "worker": self.store.document("worker"),
            "backup": self.store.document("backup"),
            "data_quality": (self.store.document("screen") or {}).get("coverage", {}),
            "stream": self.store.document("stream"),
        }

    def report(self):
        account = self.account()
        if self.store.mode == "kis_paper":
            self.execution._fresh(account.get("observed_at"), "보고 계좌")
        session = self.workspace.clock.now().astimezone(KST).date().isoformat()
        orders = [o for o in self.store.all_orders() if o.get("trading_date") == session]
        filled = [o for o in orders if o["filled_quantity"]]
        fees_known = all(o.get("fee") is not None for o in filled)
        fees = str(sum(decimal(o["fee"]) for o in filled)) if fees_known else None
        baseline = self.store.document("baseline") or {}
        closed = False
        if self.market_store:
            try:
                calendar = self.market_store.calendar(session)
                closed = bool(
                    calendar["is_open"]
                    and self.workspace.clock.now() >= datetime.fromisoformat(calendar["close_at"])
                )
            except ValueError:
                pass
        snapshot = {
            "at": self.at(),
            "session": session,
            "segment_id": baseline.get("segment_id"),
            "equity": account["equity"],
            "cash": account["cash"],
            "holdings_value": account["holdings_value"],
            "source": account["source"],
            "mode": self.store.mode,
            "session_closed": closed,
            "deployment_version": self.store.document("deployment")["version"],
            "model_id": self.store.document("deployment").get("model_id"),
            "fees": fees,
            "costs_verified": fees_known,
            "order_count": len(orders),
            "filled_order_count": len(filled),
            "realized_profit": account.get("realized_profit"),
            "unresolved_count": sum(o["state"] in {"unknown", "sending"} for o in orders),
            "buy_amount": str(
                sum(decimal(o["filled_amount"]) for o in filled if o["side"] == "buy")
            ),
            "sell_amount": str(
                sum(decimal(o["filled_amount"]) for o in filled if o["side"] == "sell")
            ),
            "no_trade_reason": "체결 없음" if not filled else None,
            "financial_status": (
                (self.store.document("reconciliation") or {}).get("financial") or {}
            ).get("cumulative_status", "unverified"),
            "reconciliation": (self.store.document("reconciliation") or {}).get(
                "status", "not_checked"
            ),
        }
        self.store.put_document(
            "daily_report", snapshot, f"{session}:{baseline.get('segment_id', 'none')}"
        )
        return snapshot

    def reports(self):
        daily = sorted(
            self.store.documents("daily_report"), key=lambda row: (row["session"], row["at"])
        )
        baseline = self.store.document("baseline")
        baselines = {b["segment_id"]: b for b in self.store.documents("baseline_history")}
        if baseline:
            baselines[baseline["segment_id"]] = baseline
        monthly = []
        for month, segment in sorted(
            {(d["session"][:7], d.get("segment_id") or "legacy") for d in daily}
        ):
            records = [
                d
                for d in daily
                if d["session"].startswith(month) and (d.get("segment_id") or "legacy") == segment
            ]
            previous = [
                d for d in daily if d["session"][:7] < month and d.get("segment_id") == segment
            ]
            start = (
                previous[-1]["equity"]
                if previous
                else baselines.get(segment, {}).get("equity", records[0]["equity"])
            )
            last = records[-1]
            peak = decimal(start)
            drawdown = 0.0
            for record in records:
                value = decimal(record["equity"])
                peak = max(peak, value)
                if peak:
                    drawdown = min(drawdown, float(value / peak - 1) * 100)
            monthly.append(
                {
                    "month": month,
                    "segment_id": segment,
                    "observed_sessions": len(records),
                    "start_equity": start,
                    "end_equity": last["equity"],
                    "return_pct": float(decimal(last["equity"]) / decimal(start) - 1) * 100
                    if decimal(start)
                    else None,
                    "max_drawdown_pct": drawdown,
                    "fees": str(sum(decimal(r["fees"]) for r in records))
                    if all(r.get("fees") is not None for r in records)
                    else None,
                    "verified": all(
                        r["reconciliation"] == "matched" and r.get("financial_status") == "verified"
                        for r in records
                    ),
                    "source": self.workspace.market.source,
                }
            )
        observation = self.store.document("observation")
        reasons, required = [], []
        if self.store.mode != "kis_paper":
            reasons.append("synthetic_mode")
        if not observation:
            reasons.append("observation_period_not_set")
        else:
            if self.workspace.clock.now().astimezone(KST).date().isoformat() <= observation["end"]:
                reasons.append("observation_period_not_elapsed")
            if self.market_store:
                try:
                    required = [
                        r["session"]
                        for r in self.market_store.sessions(
                            observation["start"], observation["end"]
                        )
                    ]
                except ValueError:
                    reasons.append("calendar_coverage_missing")
            else:
                reasons.append("calendar_coverage_missing")
            if len(required) < observation["minimum_sessions"]:
                reasons.append("insufficient_trading_sessions")
            covered = {
                r["session"]
                for r in daily
                if r.get("session_closed")
                and r["reconciliation"] == "matched"
                and r.get("financial_status") == "verified"
                and r.get("costs_verified")
                and not r.get("unresolved_count")
                and r["deployment_version"] == observation["deployment_version"]
            }
            if set(required) - covered:
                reasons.append("missing_or_unverified_daily_reports")
            if any(o["state"] in {"sending", "unknown"} for o in self.store.all_orders()):
                reasons.append("unresolved_orders")
        return {
            "daily": daily,
            "monthly": monthly,
            "baseline": baseline,
            "baseline_history": list(baselines.values()),
            "mode": self.store.mode,
            "orders": self.store.list_orders(limit=100),
            "observation_complete": not reasons,
            "observation": observation,
            "observation_reasons": reasons,
            "required_sessions": required,
            "note": (
                "실제 기록만 집계합니다. 비용 미제공은 0원으로 추정하지 않으며 "
                "계좌 초기화는 새 구간으로 분리합니다."
            ),
        }

    def run_job(self, job):
        kind, payload = job["kind"], job["payload"]
        if kind == "submit_order":
            result = self.execution.submit(payload["order_id"])
            self.store.enqueue("reconcile", job_key=f"reconcile:{job['id']}")
            return result
        if kind in {"cancel_order", "amend_order"}:
            result = self.execution.cancel(
                payload["order_id"],
                payload["client_request_id"],
                payload["quantity"],
                payload.get("limit_price") if kind == "amend_order" else None,
            )
            self.store.enqueue("reconcile", job_key=f"reconcile:{job['id']}")
            return result
        if kind == "reconcile":
            return self.execution.reconcile()
        if kind == "analyze":
            return self.analyze_market()
        if kind == "report":
            return self.report()
        if kind == "backup":
            from invest_lab.adapters.backup import create_backup_bundle

            destination = (
                self.store.path.parent
                / "backups"
                / f"{self.store.mode}-{datetime.now(UTC):%Y%m%dT%H%M%S}-{str(uuid4())[:8]}"
            )
            databases = {
                f"runtime-{self.store.mode}": self.store.path,
                "workspace": self.workspace.portfolio.path,
                "research": self.research.repository.path,
            }
            if self.market_store:
                databases["market"] = self.market_store.path
            with self.store.execution_lock():
                result = create_backup_bundle(
                    destination,
                    databases=databases,
                    artifact_dir=self.research.repository.artifact_dir,
                    expected_namespace=self.store.namespace,
                )
            self.store.put_document(
                "backup", {"at": self.at(), "path": str(destination), "verified": True}
            )
            return result
        if kind == "create_demo_dataset":
            return self.research.create_demo_dataset()
        if kind == "research_experiment":
            return self.research.run_experiment(payload)
        if kind == "collect_source":
            return self.collect_source(payload)
        if kind == "collect_market":
            return self.collect_market(payload)
        if kind == "daily_pipeline":
            if self.store.mode == "local_simulation":
                self.analyze_market()
                self.execution.reconcile()
                return self.report()
            self.store.enqueue(
                "collect_market", {**payload, "finalize": True}, f"collect:{job['id']}"
            )
            return {"collection_queued": True}
        if kind == "daily_finalize":
            analysis = self.analyze_market()
            if analysis["errors"]:
                raise TradingBlocked("데이터 품질 검사 실패. 자동 운용을 진행하지 않습니다.")
            reconciliation = self.execution.reconcile()
            if reconciliation["status"] != "matched":
                raise TradingBlocked("일 마감 대조가 완료되지 않았습니다.")
            report = self.report()
            self.store.enqueue("backup", job_key=f"backup:{job['id']}")
            return {"analysis": analysis, "reconciliation": reconciliation, "report": report}
        if kind == "build_dataset":
            from invest_lab.adapters.data_pipeline import ResearchDataPipeline

            if not self.market_store:
                raise TradingBlocked("실제 시장 저장소가 필요합니다.")
            return ResearchDataPipeline(self.market_store, self.research).import_market_dataset(
                **payload
            )
        if kind == "close_session":
            return self.close_session()
        if kind == "auto_trade":
            return self.auto_trade()
        raise ValueError("지원하지 않는 작업입니다.")

    def collect_market(self, payload):
        if self.store.mode == "local_simulation":
            return {"source": "synthetic_demo", "collected": 0, "message": "고정 샘플 데이터"}
        if self.ingestion is None:
            raise TradingBlocked("시세 수집 어댑터가 구성되지 않았습니다.")
        symbols = payload.get("symbols") or [i.symbol for i in self.workspace.market.instruments()]
        if not symbols:
            raise TradingBlocked("먼저 공식 종목 마스터와 거래 달력을 가져오세요.")
        end = payload.get("end") or self.workspace.clock.now().astimezone(KST).date().isoformat()
        start = (
            payload.get("start")
            or (self.workspace.clock.now().astimezone(KST).date() - timedelta(days=400)).isoformat()
        )
        index = int(payload.get("index", 0))
        group = payload.get("group") or str(uuid4())
        if not 0 <= index < len(symbols):
            raise ValueError("수집 체크포인트가 올바르지 않습니다.")
        # A bounded symbol chunk yields execution priority between history requests.
        result = self.ingestion.collect(
            [symbols[index]], start, end, adjusted=payload.get("adjusted", True)
        )
        if any(value.get("status") != "complete" for value in result.values()):
            self.store.put_document(
                "collection_progress",
                {
                    "group": group,
                    "state": "incomplete",
                    "index": index,
                    "symbol": symbols[index],
                    "result": result,
                    "at": self.at(),
                },
                group,
            )
            raise TradingBlocked(
                "일봉/달력 누락으로 수집을 멈췄습니다. "
                "같은 범위로 재수집하면 체크포인트부터 이어집니다."
            )
        quotes = self.ingestion.refresh_quotes([symbols[index]])
        checkpoint = {
            "group": group,
            "completed": index + 1,
            "total": len(symbols),
            "last_symbol": symbols[index],
            "at": self.at(),
        }
        self.store.put_document("collection_progress", checkpoint, group)
        if index + 1 < len(symbols):
            self.store.enqueue(
                "collect_market",
                {
                    **payload,
                    "symbols": symbols,
                    "start": start,
                    "end": end,
                    "index": index + 1,
                    "group": group,
                },
                f"collect:{group}:{index + 1}",
            )
        elif payload.get("finalize"):
            self.store.enqueue("daily_finalize", {}, f"finalize:{group}")
        return {
            "history": result,
            "quotes": len(quotes),
            "source": "kis_paper",
            "checkpoint": checkpoint,
        }

    def close_session(self):
        results = []
        for order in self.store.all_orders():
            if order["state"] == "registered":
                self.store.transition(
                    order["id"], {"registered"}, "expired", self.at(), error="주문 창 종료"
                )
            elif (
                order["state"] in {"acknowledged", "partially_filled"}
                and order["remaining_quantity"]
            ):
                command = str(uuid5(NAMESPACE_URL, f"close:{self.store.namespace}:{order['id']}"))
                results.append(
                    self.execution.cancel(order["id"], command, order["remaining_quantity"])
                )
        return {"cancel_requests": len(results), "reconciliation": self.execution.reconcile()}

    def collect_source(self, payload):
        from dotenv.parser import parse_stream

        from invest_lab.adapters.research_sources import DartSource, FredSource, KrxSource

        providers = {
            "dart": (DartSource, "DART_API_KEY"),
            "fred": (FredSource, "FRED_API_KEY"),
            "krx": (KrxSource, "KRX_API_KEY"),
        }
        name = payload.get("provider")
        if name not in providers:
            raise ValueError("원천은 dart/fred/krx 중 하나입니다.")
        values = {}
        env_path = self.root / ".env"
        if env_path.exists():
            with env_path.open(encoding="utf-8-sig") as stream:
                for binding in parse_stream(stream):
                    if binding.error:
                        raise TradingBlocked("로컬 .env 형식을 표준 형식으로 바꿔주세요.")
                    if binding.key:
                        values[binding.key] = binding.value
        cls, key_name = providers[name]
        key = os.getenv(key_name) or values.get(key_name)
        if not key:
            raise TradingBlocked(f"{key_name}가 필요합니다.")
        provider = cls(key)
        try:
            if payload.get("base_dataset_id"):
                from invest_lab.adapters.data_pipeline import ResearchDataPipeline

                result = ResearchDataPipeline(
                    self.market_store, self.research
                ).collect_into_dataset(
                    payload["base_dataset_id"],
                    provider,
                    payload,
                    symbol_map=payload.get("symbol_map"),
                    instrument_overrides=payload.get("instrument_overrides"),
                )
            else:
                result = self.research.collect_source(provider, payload)
            self.store.put_document("source_collection", result, f"{name}:{self.at()}")
            # Response records are persisted with provenance, never their request key.
            return result
        finally:
            provider.close()

    def auto_trade(self):
        deployment = self.store.document("deployment")
        if not deployment["enabled"]:
            return {"submitted": 0, "reason": "자동 운용 비활성"}
        if self.store.mode == "kis_paper" and not self._session_open(self.workspace.clock.now()):
            return {"submitted": 0, "reason": "정규장 세션 미확인"}
        reconciliation = self.execution.reconcile()
        if reconciliation["status"] != "matched":
            raise TradingBlocked("대조가 완료되지 않았습니다.")
        watch = self.store.document("watchlist") or {}
        if watch.get("deployment_version") != deployment["version"]:
            raise TradingBlocked("현재 운용 버전으로 후보를 다시 계산하세요.")
        # Execution generation and risk checks remain mandatory for every emitted intent.
        # Strategy-to-order integration adds session validity and managed-position exits below.
        return self._trade_signals(deployment, watch)

    def _trade_signals(self, deployment, watch):
        now = self.workspace.clock.now().astimezone(KST)
        today = now.date().isoformat()
        if not watch.get("as_of") or watch.get("errors"):
            raise TradingBlocked("확정 후보/데이터 품질 검사를 먼저 완료하세요.")
        entry_window = True
        if self.market_store:
            target_session = self.market_store.next_session(watch["as_of"], as_of=now)
            if target_session["session"] != today:
                raise TradingBlocked(
                    "후보 신호의 다음 거래일이 아닙니다. 오래된 매수를 몰아서 실행하지 않습니다."
                )
            opened = datetime.fromisoformat(target_session["open_at"])
            entry_window = 0 <= (now - opened).total_seconds() <= 30 * 60
        elif self.store.mode != "local_simulation":
            raise TradingBlocked("거래 달력이 없습니다.")
        rules = StrategyRules.from_dict(deployment["rules"])
        account = self.broker.account()
        daily = self.store.document("daily_start", today)
        if daily is None:
            daily = {"equity": account["equity"], "at": self.at()}
            self.store.put_document("daily_start", daily, today)
        loss_limit = decimal(account["equity"]) <= decimal(daily["equity"]) * (
            1 - decimal(rules.daily_loss_limit_pct) / 100
        )
        if loss_limit:
            self.control(False)
        signals = {s["symbol"]: s for s in watch.get("all_signals", watch["items"])}
        indices = {s: (v.get("etf") or {}).get("benchmark_id") for s, v in signals.items()}
        instruments = {i.symbol: i for i in self.workspace.market.instruments()}
        submitted, skipped = [], []

        def active_orders():
            return [o for o in self.store.all_orders() if o["state"] not in TERMINAL]

        def execute(signal, side, quantity, quote):
            symbol = signal["symbol"]
            client_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"{self.store.namespace}:{today}:{deployment['version']}:{symbol}:{side}",
                )
            )
            previous = self.store.find_intent(client_id)
            if previous:
                # The accepted payload is immutable even if position size has changed since.
                return previous["state"] not in {"unknown", "sending"}
            context = {
                "origin": "automatic",
                "deployment_version": deployment["version"],
                "signal": signal,
                "dataset_id": signal.get("dataset_id"),
                "model_id": deployment.get("model_id"),
                "index_id": indices.get(symbol),
                "index_map": indices,
                "stop_price": float(quote["price"]) - (signal.get("stop_distance") or 0),
                "valid_session": today,
                "risk_policy": {
                    "max_position_pct": rules.max_position_pct,
                    "max_invested_pct": rules.max_total_pct,
                    "max_sector_pct": rules.max_sector_pct,
                    "max_index_pct": rules.max_index_pct,
                    "max_positions": rules.max_positions,
                },
            }
            intent = self.execution.register(
                {
                    "client_request_id": client_id,
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "order_type": "limit",
                    "limit_price": quote["price"],
                },
                decision_context=context,
            )
            try:
                result = self.execution.submit(intent["id"])
                submitted.append({"id": result["id"], "state": result["state"]})
                if result["state"] in {"unknown", "sending"}:
                    return False
                return self.execution.reconcile()["status"] == "matched"
            except TradingBlocked as error:
                skipped.append({"symbol": symbol, "reason": str(error)})
                # A failed preflight must not leave a stale unsubmitted order reserving the symbol.
                if self.store.get_order(intent["id"])["state"] == "registered":
                    self.store.transition(
                        intent["id"], {"registered"}, "expired", self.at(), error=str(error)
                    )
                return True

        # Risk stops and entry-window closure do not suppress exits from managed holdings.
        for position in account["positions"]:
            symbol = position["symbol"]
            allocation = self.store.document("allocation", symbol) or {}
            managed = min(allocation.get("managed_quantity", 0), position["sellable_quantity"])
            if (
                not managed
                or symbol not in signals
                or any(o["symbol"] == symbol for o in active_orders())
            ):
                continue
            opened = allocation.get("opened_session", today)
            if self.market_store:
                holding_sessions = max(
                    0, len(self.market_store.sessions(opened, today, as_of=now)) - 1
                )
            else:
                holding_sessions = sum(
                    opened <= b.session.isoformat() <= today
                    for b in self.workspace.market.history(symbol)
                )
            signal = signals[symbol]
            result = evaluate_strategy(
                signal["technical"],
                asdict(instruments[symbol]),
                rules,
                position={
                    "stop_price": allocation.get("stop_price", 0),
                    "holding_sessions": holding_sessions,
                },
            )
            quote = self.execution._quote(symbol, automatic=True)
            intraday_stop = rules.intraday_stop and float(quote["price"]) <= allocation.get(
                "stop_price", 0
            )
            if result["action"] == "sell" or intraday_stop:
                if not execute(signal, "sell", managed, quote):
                    return {
                        "submitted": len(submitted),
                        "orders": submitted,
                        "skipped": skipped,
                        "session": today,
                    }
        if (
            entry_window
            and not loss_limit
            and self.store.document("control").get("entries_enabled")
        ):
            for signal in watch["items"]:
                symbol = signal["symbol"]
                account = self.broker.account()
                positions = {p["symbol"]: p for p in account["positions"]}
                pending = [o for o in active_orders() if o["side"] == "buy"]
                if symbol in positions or any(o["symbol"] == symbol for o in active_orders()):
                    continue
                exposure = {s: float(p["value"]) for s, p in positions.items()}
                for order in pending:
                    exposure[order["symbol"]] = (
                        exposure.get(order["symbol"], 0)
                        + float(order.get("submitted_price") or order.get("limit_price") or 0)
                        * order["remaining_quantity"]
                    )
                if len(exposure) >= rules.max_positions:
                    skipped.append({"symbol": symbol, "reason": "동시 보유/예약 수 제한"})
                    continue
                quote = self.execution._quote(symbol, automatic=True)
                price = float(quote["price"])
                if abs(price / signal["technical"]["close"] - 1) * 100 > rules.max_gap_pct:
                    skipped.append({"symbol": symbol, "reason": "다음 세션 가격 갭 제한"})
                    continue
                sector = sum(
                    v
                    for s, v in exposure.items()
                    if s in instruments and instruments[s].sector == signal["sector"]
                )
                index_id = indices.get(symbol)
                index_value = sum(
                    v for s, v in exposure.items() if index_id and indices.get(s) == index_id
                )
                quantity = size_position(
                    equity=float(account["equity"]),
                    cash=float(account.get("buying_power", account["cash"])),
                    price=price,
                    stop_distance=signal.get("stop_distance") or 0,
                    total_value=sum(exposure.values()),
                    sector_value=sector,
                    index_value=index_value,
                    volume20=signal["technical"].get("volume20"),
                    rules=rules,
                )
                if quantity and not execute(signal, "buy", quantity, quote):
                    break
        return {
            "submitted": len(submitted),
            "orders": submitted,
            "skipped": skipped,
            "session": today,
            "entry_window": entry_window,
            "loss_limit": loss_limit,
        }


def build_runtime(workspace, *, root: Path, mode=None, db_path=None):
    from invest_lab.adapters.research import build_research_service

    mode = mode or os.getenv("INVEST_MODE", "local_simulation")
    state_dir = Path(os.getenv("INVEST_STATE_DIR", str(root / ".local")))
    path = Path(db_path) if db_path else state_dir / f"runtime-{mode}.db"
    settings, error, market_store, ingestion = None, None, None, None
    if mode == "kis_paper":
        from invest_lab.adapters.kis import KisPaperClient
        from invest_lab.adapters.settings import KisSettings, SettingsError

        try:
            settings = KisSettings.from_env_file(root / ".env")
        except SettingsError:
            error = "설정 형식을 확인하세요. 비밀값은 화면에 표시하지 않습니다."
            settings = KisSettings.from_mapping({}, root=root)
        account_id = hashlib.sha256(
            (settings.account_number + settings.account_product_code).encode()
        ).hexdigest()[:12]
        store = RuntimeStore(path, mode, account_id)
        broker = KisPaperClient(settings)
        from invest_lab.adapters.market_data import CachedMarket, MarketIngestion, MarketStore

        market_store = MarketStore(state_dir / "market.db")
        workspace.market = CachedMarket(market_store, workspace.clock)
        ingestion = MarketIngestion(broker, market_store, workspace.clock)
    else:
        store = RuntimeStore(path, mode)
        broker = SimulatedBroker(
            store, workspace.market, workspace.clock, initial=workspace.portfolio.snapshot()
        )
    research = build_research_service(state_dir / "research.db", state_dir / "research_artifacts")
    return Runtime(
        workspace,
        store,
        broker,
        research=research,
        settings=settings,
        config_error=error,
        market_store=market_store,
        ingestion=ingestion,
        root=root,
    )
