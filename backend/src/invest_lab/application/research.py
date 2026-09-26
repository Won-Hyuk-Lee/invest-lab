"""Research use cases composed from pure rules, repository and estimator ports."""

import csv
import io
import json
import math
from collections import defaultdict
from dataclasses import asdict, fields
from datetime import date, datetime, timedelta, timezone
from statistics import mean
from uuid import uuid4

from invest_lab.domain.research import (
    bars_at,
    content_hash,
    decision_time,
    etf_metrics,
    financial_features,
    latest_visible,
    macro_features,
    timestamp,
    universe_at,
    validate_dataset,
)
from invest_lab.domain.strategies import (
    StrategyRules,
    evaluate_strategy,
    replay,
    technical_features,
)
from invest_lab.ports.research import ResearchLearner, ResearchRepository


def index_research_rows(data):
    indexed = {}
    for table in ("bars", "actions", "financial_facts", "benchmarks"):
        groups = defaultdict(list)
        for row in data[table]:
            groups[row["symbol"]].append(row)
        indexed[table] = groups
    return indexed


def build_feature_rows(data, session, *, as_of=None, symbol=None, observed_by=None, _index=None):
    at = timestamp(as_of) if as_of else decision_time(session)
    observed = timestamp(observed_by) if observed_by else None
    universe = universe_at(data, session, at, observed_by=observed)
    instruments = {r["symbol"]: r for r in data["instruments"]}
    indexed = _index or index_research_rows(data)
    macro = macro_features(data["macro"], at, observed_by=observed)
    metadata = {
        r["symbol"]: r
        for r in latest_visible(data["etf_metadata"], at, ("symbol",), observed_by=observed)
    }
    benchmarks = latest_visible(
        data["benchmarks"], at, ("symbol", "session", "price_basis"), observed_by=observed
    )
    universe_history = {
        r["session"]: set(r["symbols"])
        for r in latest_visible(data["universe"], at, ("session",), observed_by=observed)
    }
    rows = []
    for ticker in universe:
        if symbol and ticker != symbol:
            continue
        instrument = instruments[ticker]
        etf = metadata.get(ticker)
        benchmark_id = (etf or {}).get("benchmark_id", instrument.get("benchmark_id", "KOSPI"))
        bm = sorted(
            [
                r
                for r in benchmarks
                if r["symbol"] == benchmark_id
                and r["session"] <= session
                and r["price_basis"] == "raw"
            ],
            key=lambda r: r["session"],
        )
        local_data = {"bars": indexed["bars"][ticker], "actions": indexed["actions"][ticker]}
        history = bars_at(local_data, ticker, session, at, observed_by=observed)
        technical = technical_features(history, bm)
        missing = list(technical.get("missing_reasons", []))
        if not history or history[-1]["session"] != session:
            missing.append("missing_final_bar")
        # Interior holes must not masquerade as N trading sessions.
        expected = [
            s
            for s in data["sessions"]
            if history
            and history[0]["session"] <= s <= session
            and ticker in universe_history.get(s, set())
        ]
        if set(expected) - {r["session"] for r in history}:
            missing.append("missing_trading_session")
        raw_rows = [
            r
            for r in indexed["bars"][ticker]
            if r["symbol"] == ticker and r["session"] == session and r["price_basis"] == "raw"
        ]
        raw_rows = latest_visible(raw_rows, at, ("symbol", "session"), observed_by=observed)
        market_cap = raw_rows[0].get("market_cap") if raw_rows else None
        financial = (
            financial_features(
                indexed["financial_facts"][ticker], ticker, at, market_cap, observed_by=observed
            )
            if instrument["kind"] == "kospi"
            else {"values": {}, "missing_reasons": {"financial": "not_applicable_to_etf"}}
        )
        signals = []
        for kind in ("kospi_pullback", "etf_trend"):
            signal = evaluate_strategy(
                technical,
                instrument,
                StrategyRules(strategy_type=kind),
                financial=financial,
                macro=macro,
                etf=etf,
            )
            if missing:
                signal.update({"eligible": False, "action": "excluded", "rank": None})
                signal["reasons"] = sorted(set(signal["reasons"] + missing))
            signals.append(signal)
        etf_report = None
        if instrument["kind"].startswith("etf"):
            total_benchmark_id = (etf or {}).get("total_return_benchmark_id", benchmark_id)
            bm_map = {
                b["session"]: float(b["close"])
                for b in benchmarks
                if b["symbol"] == total_benchmark_id
                and b["price_basis"] == "total_return"
                and b["session"] <= session
            }
            actions = latest_visible(
                indexed["actions"][ticker], at, ("event_id",), observed_by=observed
            )
            distributions = [
                sum(
                    float(a["amount"])
                    / math.prod(
                        float(split["ratio"])
                        for split in actions
                        if split["symbol"] == ticker
                        and split["type"] == "split"
                        and a["effective_date"] < split["effective_date"] <= session
                    )
                    for a in actions
                    if a["symbol"] == ticker
                    and a["type"] == "distribution"
                    and a["effective_date"] == b["session"]
                )
                for b in history
            ]
            compatible = bool(
                etf
                and etf.get("benchmark_return_basis") == "total_return"
                and etf.get("currency") == etf.get("benchmark_currency")
                and all(b["session"] in bm_map for b in history)
            )
            nav = (
                float(raw_rows[0]["nav"])
                if raw_rows
                and raw_rows[0].get("nav")
                and raw_rows[0].get("nav_as_of") == raw_rows[0].get("available_at")
                else None
            )
            etf_report = {
                "metadata": etf,
                "metrics": etf_metrics(
                    [float(b["close"]) for b in history],
                    [bm_map[b["session"]] for b in history if b["session"] in bm_map],
                    distributions=distributions,
                    nav=nav,
                    same_basis=compatible,
                ),
            }
            distribution_verified = data["provenance"].get("corporate_actions_verified") is True
            etf_report["distribution_coverage"] = (
                "synthetic_fixture"
                if data["source_kind"] == "synthetic"
                else "verified"
                if distribution_verified
                else "unverified"
            )
            if not distribution_verified and data["source_kind"] != "synthetic":
                etf_report["metrics"].update(
                    total_return=None, tracking_difference=None, tracking_error=None
                )
                etf_report["metrics"]["missing_reasons"].append("distribution_coverage_unverified")
        rows.append(
            {
                **instrument,
                "as_of": at.isoformat(),
                "session": session,
                "technical": technical,
                "financial": financial,
                "macro": macro,
                "etf": etf_report,
                "signals": signals,
                "missing_reasons": missing,
            }
        )
    valid = [
        r
        for r in rows
        if r["kind"] == "kospi" and not r["missing_reasons"] and r["technical"]["sma"]["120"]
    ]
    breadth = (
        mean(r["technical"]["close"] > r["technical"]["sma"]["120"] for r in valid)
        if valid
        else None
    )
    for row in rows:
        row["macro"] = {
            **row["macro"],
            "values": {**row["macro"]["values"], "breadth": breadth},
            "breadth_denominator": len(valid),
        }
    return rows


def feature_vector(row):
    t, f, m = row["technical"], row["financial"]["values"], row["macro"]["values"]
    vector = {"return" + n: t.get("returns", {}).get(n) for n in ("7", "30", "90")}
    vector.update(
        {
            k: t.get(k)
            for k in (
                "rsi14",
                "atr_pct",
                "pullback_atr",
                "relative63",
                "volume_ratio",
                "volatility20",
            )
        }
    )
    vector.update(f)
    vector.update(m)
    vector["vix_atr_interaction"] = (
        m["VIXCLS"] * t["atr_pct"]
        if m.get("VIXCLS") is not None and t.get("atr_pct") is not None
        else None
    )
    return vector


def training_rows(
    data,
    *,
    horizon=10,
    commission_bps=1.5,
    sell_tax_bps=0,
    slippage_bps=5,
    asset_kind="kospi",
    return_basis="price",
    with_report=False,
):
    if type(horizon) is not int or not 1 <= horizon <= 90:
        raise ValueError("레이블 기간은 1~90거래일 정수입니다.")
    if asset_kind not in {"kospi", "etf"}:
        raise ValueError("모델 상품군은 kospi/etf입니다.")
    if return_basis not in {"price", "total_return"}:
        raise ValueError("레이블 수익 기준은 price/total_return입니다.")
    rows, omissions = [], []
    sessions = data["sessions"]
    raw = {
        (r["symbol"], r["session"]): r
        for r in sorted(
            data["bars"], key=lambda b: (timestamp(b["available_at"]), timestamp(b["ingested_at"]))
        )
        if r["price_basis"] == "raw"
    }
    benchmark = {
        (r["symbol"], r["session"]): r
        for r in sorted(
            data["benchmarks"],
            key=lambda b: (timestamp(b["available_at"]), timestamp(b["ingested_at"])),
        )
        if r["price_basis"] == ("raw" if return_basis == "price" else "total_return")
    }
    actions = {
        r["event_id"]: r
        for r in sorted(
            data["actions"],
            key=lambda a: (timestamp(a["available_at"]), timestamp(a["ingested_at"])),
        )
    }
    eligible_count = 0
    indexed = index_research_rows(data)
    for i in range(124, len(sessions) - horizon):
        session, entry_session, end_session = sessions[i], sessions[i + 1], sessions[i + horizon]
        for feature in build_feature_rows(data, session, _index=indexed):
            if (asset_kind == "kospi" and feature["kind"] != "kospi") or (
                asset_kind == "etf" and not feature["kind"].startswith("etf")
            ):
                continue
            eligible_count += 1
            symbol = feature["symbol"]

            def omit(reason):
                omissions.append({"session": session, "symbol": symbol, "reason": reason})

            if feature["missing_reasons"]:
                omit("missing_feature_history")
                continue
            entry, end = raw.get((symbol, entry_session)), raw.get((symbol, end_session))
            outcome_actions = sorted(
                [
                    a
                    for a in actions.values()
                    if a["symbol"] == symbol and entry_session < a["effective_date"] <= end_session
                ],
                key=lambda a: (a["effective_date"], a["event_id"]),
            )
            delist = next((a for a in outcome_actions if a["type"] == "delisting"), None)
            if delist and "settlement_price" not in delist:
                omit("missing_delisting_settlement")
                continue
            last_required = delist["effective_date"] if delist else end_session
            required_sessions = [
                s
                for s in sessions[i + 1 : i + horizon + 1]
                if s < last_required or (not delist and s == last_required)
            ]
            if (
                not entry
                or (not end and not delist)
                or any((symbol, s) not in raw for s in required_sessions)
            ):
                omit("missing_outcome_bar")
                continue
            if not entry.get("tradable", True) or float(entry["volume"]) == 0:
                omit("untradable_entry")
                continue
            benchmark_id = ((feature.get("etf") or {}).get("metadata") or {}).get(
                "benchmark_id", feature.get("benchmark_id", "KOSPI")
            )
            if return_basis == "total_return":
                benchmark_id = ((feature.get("etf") or {}).get("metadata") or {}).get(
                    "total_return_benchmark_id",
                    feature.get("total_return_benchmark_id", benchmark_id),
                )
            bm_entry, bm_end = (
                benchmark.get((benchmark_id, entry_session)),
                benchmark.get((benchmark_id, end_session)),
            )
            if not bm_entry or not bm_end:
                omit("missing_benchmark_outcome")
                continue
            quantity, cash_dividend = 1.0, 0.0
            outcome_available = [
                entry["available_at"],
                bm_entry["available_at"],
                bm_end["available_at"],
            ]
            for action in outcome_actions:
                if delist and action["effective_date"] > delist["effective_date"]:
                    continue
                outcome_available.append(action["available_at"])
                if action["type"] == "split":
                    quantity *= float(action["ratio"])
                elif (
                    action["type"] in {"dividend", "distribution"}
                    and return_basis == "total_return"
                ):
                    cash_dividend += quantity * float(action["amount"])
            if delist:
                exit_price = float(delist["settlement_price"])
            else:
                outcome_available.append(end["available_at"])
                exit_price = float(end["close"]) * (1 - slippage_bps / 10000)
            entry_price = float(entry["open"]) * (1 + slippage_bps / 10000)
            cost = entry_price * (1 + commission_bps / 10000)
            proceeds = (
                quantity
                * exit_price
                * (1 if delist else 1 - (commission_bps + sell_tax_bps) / 10000)
                + cash_dividend
            )
            benchmark_return = float(bm_end["close"]) / float(bm_entry["open"]) - 1
            rows.append(
                {
                    "session": session,
                    "symbol": symbol,
                    "features": feature_vector(feature),
                    "target": proceeds / cost - 1 - benchmark_return,
                    "label_start": entry_session,
                    "label_end": end_session,
                    "label_available_at": max(outcome_available, key=timestamp),
                    "benchmark_id": benchmark_id,
                    "delisting_settlement_used": bool(delist),
                }
            )
    reasons = {
        name: sum(o["reason"] == name for o in omissions)
        for name in sorted({o["reason"] for o in omissions})
    }
    incomplete = any(name != "untradable_entry" for name in reasons)
    report = {
        "eligible_decisions": eligible_count,
        "label_rows": len(rows),
        "omitted_count": len(omissions),
        "omitted_reasons": reasons,
        "omitted_examples": omissions[:100],
        "survivorship_limited": incomplete,
        "label_coverage_complete": not incomplete,
        "settled_delisting_rows": sum(r["delisting_settlement_used"] for r in rows),
        "benchmark_return_basis": return_basis,
        "asset_kind": asset_kind,
    }
    return (rows, report) if with_report else rows


class ResearchService:
    def __init__(self, repository: ResearchRepository, learner: ResearchLearner):
        self.repository, self.learner = repository, learner

    def overview(self):
        experiments = self.list_experiments()
        return {
            "datasets": self.list_datasets(),
            "experiments": experiments,
            "models": self.list_models(),
            "source_status": {
                "dart": "explicit_credentials_required",
                "fred": "explicit_credentials_required",
                "krx": "explicit_credentials_and_service_approval_required",
            },
            "real_validation_complete": any(
                e.get("research_validation_ready") for e in experiments
            ),
            "research_validation_ready": any(
                e.get("research_validation_ready") for e in experiments
            ),
            "broker_validation_complete": False,
            "live_orders_enabled": False,
        }

    def list_datasets(self):
        return self.repository.list_datasets()

    def import_dataset(self, payload):
        if payload.get("demo") is True:
            return self.create_demo_dataset()
        if payload.get("format") in {"json", "csv"}:
            if not payload.get("base_dataset_id") or payload.get("table") not in {
                "bars",
                "universe",
                "actions",
                "financial_facts",
                "macro",
                "benchmarks",
                "etf_metadata",
            }:
                raise ValueError("행 가져오기는 base_dataset_id와 허용된 table이 필요합니다.")
            data = self.repository.get_dataset(payload["base_dataset_id"])["data"]
            content = (
                payload.get("csv_text") if payload["format"] == "csv" else payload.get("json_text")
            )
            if not isinstance(content, str) or len(content.encode()) > 20_000_000:
                raise ValueError("20MB 이하 텍스트 자료를 넣어주세요.")
            rows = (
                list(csv.DictReader(io.StringIO(content)))
                if payload["format"] == "csv"
                else json.loads(content)
            )
            if not isinstance(rows, list):
                raise ValueError("가져온 자료는 배열이어야 합니다.")
            for row in rows:
                for key in ("final", "tradable", "inverse"):
                    if isinstance(row.get(key), str):
                        if row[key].lower() not in {"true", "false"}:
                            raise ValueError("CSV bool은 true/false입니다.")
                        row[key] = row[key].lower() == "true"
                if isinstance(row.get("symbols"), str):
                    row["symbols"] = json.loads(row["symbols"])
            data[payload["table"]].extend(rows)
            data["name"] = payload.get("name", data["name"] + " (import)")
            data["provenance"]["parent_dataset"] = payload["base_dataset_id"]
            payload = data
        result = self.repository.save_dataset(validate_dataset(payload))
        result["export"] = self.repository.export_dataset(result["id"])
        return result

    def create_demo_dataset(self):
        return self.import_dataset(synthetic_research_dataset())

    def features(self, dataset_id, as_of=None, symbol=None, *, observed_by=None):
        stored = self.repository.get_dataset(dataset_id)
        data = stored["data"]
        at = timestamp(as_of) if as_of else decision_time(data["sessions"][-1])
        sessions = [s for s in data["sessions"] if decision_time(s) <= at]
        if not sessions:
            raise ValueError("요청 시점 이전 확정 세션이 없습니다.")
        return {
            "dataset_id": dataset_id,
            "source_kind": data["source_kind"],
            "as_of": at.isoformat(),
            "rows": build_feature_rows(
                data, sessions[-1], as_of=at, symbol=symbol, observed_by=observed_by
            ),
            "coverage": stored["coverage"],
            "feature_version": "daily-pit-v1",
            "research_validation_ready": stored["coverage"]["pit_ready"],
            "real_validation_complete": stored["coverage"]["pit_ready"],
            "broker_validation_complete": False,
        }

    def run_experiment(self, payload):
        stored = self.repository.get_dataset(payload["dataset_id"])
        data = stored["data"]
        kind = payload.get("kind", "backtest")
        if kind not in {"backtest", "train", "compare"}:
            raise ValueError("실험 kind는 backtest/train/compare입니다.")
        params = dict(payload.get("parameters", {}))
        if payload.get("model_type"):
            params["model_type"] = payload["model_type"]
        rule_names = {f.name for f in fields(StrategyRules)}
        rules = StrategyRules.from_dict({k: v for k, v in params.items() if k in rule_names})
        if payload.get("strategy_version"):
            rules = StrategyRules.from_dict(
                {**asdict(rules), "version": str(payload["strategy_version"])}
            )
        identifier = uuid4().hex
        experiment = {
            "id": identifier,
            "dataset_id": stored["id"],
            "dataset_hash": stored["hash"],
            "kind": kind,
            "source_kind": data["source_kind"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "parameters": params,
            "coverage": stored["coverage"],
            "status": "completed",
            "models": [],
            "real_validation_complete": False,
            "code_contract": "research-v1",
            "result": {},
        }
        try:
            if kind in {"backtest", "compare"}:
                strategies = (
                    [rules]
                    if kind == "backtest"
                    else [
                        StrategyRules.from_dict({**asdict(rules), "strategy_type": name})
                        for name in ("kospi_pullback", "etf_trend")
                    ]
                )
                runs = [
                    replay(
                        data,
                        rule,
                        initial_cash=params.get("initial_cash", 10_000_000),
                        start_session=params.get("start_session"),
                        end_session=params.get("end_session"),
                        benchmark_symbol=params.get("benchmark_symbol", "KOSPI"),
                    )
                    for rule in strategies
                ]
                experiment["result"]["backtests"] = runs
                sensitivity = []
                multipliers = params.get("cost_multipliers", [2])
                if not isinstance(multipliers, list) or len(multipliers) > 5:
                    raise ValueError("비용 민감도 시나리오는 최대 5개 배열입니다.")
                for multiplier in multipliers:
                    if (
                        isinstance(multiplier, bool)
                        or not isinstance(multiplier, (int, float))
                        or not math.isfinite(multiplier)
                        or not 1 <= multiplier <= 10
                    ):
                        raise ValueError("비용 민감도 배수는 1~10 숫자입니다.")
                    for rule in strategies:
                        stressed = StrategyRules.from_dict(
                            {
                                **asdict(rule),
                                "commission_bps": rule.commission_bps * multiplier,
                                "slippage_bps": rule.slippage_bps * multiplier,
                            }
                        )
                        stress = replay(
                            data,
                            stressed,
                            initial_cash=params.get("initial_cash", 10_000_000),
                            start_session=params.get("start_session"),
                            end_session=params.get("end_session"),
                            benchmark_symbol=params.get("benchmark_symbol", "KOSPI"),
                        )
                        sensitivity.append(
                            {
                                "strategy_type": rule.strategy_type,
                                "multiplier": multiplier,
                                "metrics": stress["metrics"],
                                "tax_policy": "held_constant",
                            }
                        )
                experiment["result"]["cost_sensitivity"] = sensitivity
            if kind in {"train", "compare"}:
                rows, label_report = training_rows(
                    data,
                    horizon=params.get("horizon", 10),
                    commission_bps=rules.commission_bps,
                    sell_tax_bps=rules.sell_tax_bps,
                    slippage_bps=rules.slippage_bps,
                    asset_kind=params.get("asset_kind", "kospi"),
                    return_basis=params.get("return_basis", "price"),
                    with_report=True,
                )
                experiment["result"]["label_coverage"] = label_report
                if not rows:
                    raise ValueError("레이블을 만들 실제 일봉/벤치마크/이력이 없습니다.")
                model_types = (
                    [params.get("model_type", "ridge")]
                    if kind == "train"
                    else ["ridge", "logistic"]
                )
                ablations = (
                    [params.get("ablation", "technical")]
                    if kind == "train"
                    else ["technical", "financial", "macro", "all"]
                )
                reports = []
                baseline_replays = {}
                for model_type in model_types:
                    for ablation in ablations:
                        report = self.learner.train(
                            rows, {**params, "model_type": model_type, "ablation": ablation}
                        )
                        model_rules = StrategyRules.from_dict(
                            {
                                **asdict(rules),
                                "strategy_type": "etf_trend"
                                if params.get("asset_kind") == "etf"
                                else "kospi_pullback",
                            }
                        )
                        comparison_args = {
                            "initial_cash": params.get("initial_cash", 10_000_000),
                            "start_session": report["artifact"]["holdout_start"],
                            "end_session": report["artifact"]["holdout_end"],
                            "benchmark_symbol": params.get("benchmark_symbol", "KOSPI"),
                        }
                        scores = {
                            (p["session"], p["symbol"]): p["prediction"]
                            for p in report["predictions"]
                        }
                        baseline_key = content_hash(
                            {"rules": asdict(model_rules), **comparison_args}
                        )
                        if baseline_key not in baseline_replays:
                            baseline_replays[baseline_key] = replay(
                                data, model_rules, **comparison_args
                            )
                        rule_backtest = baseline_replays[baseline_key]
                        model_backtest = replay(
                            data,
                            model_rules,
                            **comparison_args,
                            model_scores=scores,
                            model_threshold=0.5 if model_type == "logistic" else 0,
                        )
                        report["portfolio_comparison"] = {
                            "rule": rule_backtest,
                            "model": model_backtest,
                            "contract": (
                                "same_holdout_rules_costs_risk; "
                                "model_ranks_and_filters_rule_entries"
                            ),
                        }
                        feature_ready = (
                            bool(report["feature_coverage"])
                            and min(report["feature_coverage"].values()) >= 0.8
                        )
                        validation_ready = (
                            stored["coverage"]["pit_ready"]
                            and label_report["label_coverage_complete"]
                            and feature_ready
                            and report["evaluation_complete"]
                        )
                        adoption_ready = (
                            validation_ready
                            and report["outperforms_constant_baseline"]
                            and model_backtest["metrics"]["return"]
                            > rule_backtest["metrics"]["return"]
                        )
                        model = {
                            "id": uuid4().hex,
                            "created_at": experiment["created_at"],
                            "experiment_id": identifier,
                            "dataset_id": stored["id"],
                            "dataset_hash": stored["hash"],
                            "source_kind": data["source_kind"],
                            "status": "candidate",
                            "model_type": model_type,
                            "ablation": ablation,
                            "artifact": report["artifact"],
                            "metrics": report["holdout"],
                            "coverage": stored["coverage"],
                            "feature_coverage": report["feature_coverage"],
                            "label_coverage": label_report,
                            "validation_ready": validation_ready,
                            "research_validation_ready": validation_ready,
                            "adoption_ready": adoption_ready,
                            "evaluation_complete": True,
                            "real_validation_complete": validation_ready,
                            "broker_validation_complete": False,
                            "selection_reasons": (
                                ["data_pit_not_ready"]
                                if not stored["coverage"]["pit_ready"]
                                else []
                            )
                            + (
                                ["label_coverage_incomplete"]
                                if not label_report["label_coverage_complete"]
                                else []
                            )
                            + (["feature_coverage_below_80_percent"] if not feature_ready else [])
                            + (["holdout_baselines_not_improved"] if not adoption_ready else []),
                            "target": {
                                "asset_kind": params.get("asset_kind", "kospi"),
                                "return_basis": params.get("return_basis", "price"),
                                "horizon_sessions": params.get("horizon", 10),
                                "entry": "next_open",
                                "exit": "horizon_close",
                                "costs": {
                                    k: getattr(rules, k)
                                    for k in ("commission_bps", "sell_tax_bps", "slippage_bps")
                                },
                            },
                        }
                        self.repository.save_model(model)
                        experiment["models"].append(model["id"])
                        reports.append(
                            {
                                "model_id": model["id"],
                                "model_type": model_type,
                                "ablation": ablation,
                                **report,
                            }
                        )
                experiment["result"]["training"] = reports
                experiment["result"]["training_rows"] = len(rows)
                experiment["result"]["comparison_scope"] = (
                    "same_labels_date_splits_and_holdout_portfolio_rules_risk_costs"
                )
            experiment["research_validation_ready"] = stored["coverage"]["pit_ready"] and (
                kind == "backtest"
                or all(
                    m["research_validation_ready"]
                    for m in self.list_models()
                    if m["id"] in experiment["models"]
                )
            )
            experiment["real_validation_complete"] = experiment["research_validation_ready"]
            experiment["broker_validation_complete"] = False
        except (ValueError, LookupError) as error:
            experiment["status"] = "failed"
            experiment["result"]["error"] = str(error)
            self.repository.save_experiment(experiment)
            raise
        self.repository.save_experiment(experiment)
        return experiment

    def list_experiments(self):
        return self.repository.list_experiments()

    def get_experiment(self, identifier):
        return self.repository.get_experiment(identifier)

    def list_models(self):
        return self.repository.list_models()

    def set_model_status(self, identifier, status):
        if status not in {"candidate", "shadow", "selected", "rejected"}:
            raise ValueError("모델 상태를 확인하세요.")
        model = next((m for m in self.list_models() if m["id"] == identifier), None)
        if model is None:
            raise LookupError("모델을 찾을 수 없습니다.")
        if status == "selected" and not model.get("adoption_ready"):
            raise ValueError(
                "PIT/피처/레이블 검증과 비용 후 baseline 비교를 통과한 모델만 선택할 수 있습니다. "
                "미검증 모델은 shadow로 비교하세요."
            )
        if (
            status == "selected"
            and model.get("experiment_id")
            and self.get_experiment(model["experiment_id"])["status"] != "completed"
        ):
            raise ValueError("완료되지 않은 실험의 모델은 선택할 수 없습니다.")
        model.setdefault("status_history", []).append(
            {"from": model["status"], "to": status, "at": datetime.now(timezone.utc).isoformat()}
        )
        model["status"] = status
        model["updated_at"] = datetime.now(timezone.utc).isoformat()
        return self.repository.save_model(model)

    def predict(self, identifier, dataset_id, as_of=None):
        model = next((m for m in self.list_models() if m["id"] == identifier), None)
        if model is None:
            raise LookupError("모델을 찾을 수 없습니다.")
        features = self.features(dataset_id, as_of)
        artifact = model["artifact"]
        fit_cutoff = timestamp(artifact["fit_cutoff"])
        if timestamp(features["as_of"]) < fit_cutoff:
            raise ValueError("학습 이전 시점에 미래 모델을 적용할 수 없습니다.")
        if timestamp(artifact["max_training_label_available_at"]) >= fit_cutoff:
            raise ValueError("모델 학습/보정 레이블이 fit_cutoff 이전에 확정되지 않았습니다.")
        names = artifact["feature_names"]
        if not names or len(set(names)) != len(names):
            raise ValueError("모델 피처 목록이 올바르지 않습니다.")
        asset_kind = model.get("target", {}).get("asset_kind", "kospi")
        required_technical = {
            "return7",
            "return30",
            "return90",
            "rsi14",
            "atr_pct",
            "pullback_atr",
            "relative63",
            "volume_ratio",
            "volatility20",
        }
        valid, vectors, excluded = [], [], []
        for row in features["rows"]:
            reasons = list(row["missing_reasons"])
            reasons.extend(row["technical"].get("missing_reasons", []))
            if (asset_kind == "kospi" and row["kind"] != "kospi") or (
                asset_kind == "etf" and not row["kind"].startswith("etf")
            ):
                reasons.append("model_asset_kind_mismatch")
            vector = feature_vector(row)
            present = {
                key
                for key in names
                if isinstance(vector.get(key), (int, float))
                and not isinstance(vector[key], bool)
                and math.isfinite(vector[key])
            }
            if len(present) / len(names) < 0.8:
                reasons.append("model_feature_coverage_below_80_percent")
            if required_technical - present:
                reasons.append("missing_required_technical_features")
            if reasons:
                excluded.append(
                    {
                        "symbol": row["symbol"],
                        "reasons": sorted(set(reasons)),
                        "feature_coverage": len(present) / len(names),
                    }
                )
            else:
                valid.append(row)
                vectors.append(vector)
        predictions = self.learner.predict(artifact, vectors)
        threshold = 0.5 if model["model_type"] == "logistic" else 0
        return {
            "model_id": identifier,
            "dataset_id": dataset_id,
            "as_of": features["as_of"],
            "rows": [
                {"symbol": r["symbol"], "prediction": p, "eligible_prediction": p > threshold}
                for r, p in zip(valid, predictions)
            ],
            "excluded": excluded,
            "threshold": threshold,
            "asset_kind": asset_kind,
            "model_type": model["model_type"],
            "prediction_contract": artifact.get("prediction_contract"),
            "source_kind": features["source_kind"],
            "research_validation_ready": bool(
                model.get("validation_ready") and features["coverage"]["pit_ready"]
            ),
            "broker_validation_complete": False,
        }

    def collect_source(self, provider, request):
        """Job entry point: supplied adapter object; credentials are never persisted here."""
        operation = request.get("operation")
        allowed = {
            "observations",
            "vintages",
            "filings",
            "companies",
            "latest_financials",
            "daily",
            "financial_facts",
        }
        if operation not in allowed or not hasattr(provider, operation):
            raise ValueError("지원하지 않는 수집 작업입니다.")
        result = getattr(provider, operation)(**request.get("parameters", {}))
        return {
            "provider": getattr(provider, "provider", "external"),
            "operation": operation,
            "result": result,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "requires_coverage_review": True,
        }


def synthetic_research_dataset(session_count=500):
    """Deterministic integration fixture, never represented as historical market data."""
    sessions, current = [], date(2023, 1, 2)
    while len(sessions) < session_count:
        if current.weekday() < 5:
            sessions.append(current.isoformat())
        current += timedelta(days=1)
    instruments = [
        {
            "symbol": "005930",
            "name": "합성 주식 A",
            "kind": "kospi",
            "sector": "technology",
            "benchmark_id": "KOSPI",
        },
        {
            "symbol": "105560",
            "name": "합성 주식 B",
            "kind": "kospi",
            "sector": "financial",
            "benchmark_id": "KOSPI",
        },
        {
            "symbol": "069500",
            "name": "합성 국내 ETF",
            "kind": "etf_kr",
            "sector": "index",
            "benchmark_id": "KOSPI",
        },
        {
            "symbol": "0048K0",
            "name": "합성 영숫자 ETF",
            "kind": "etf_us",
            "sector": "index",
            "benchmark_id": "NASDAQ",
        },
    ]
    data = {
        "name": "합성 연구 통합 fixture",
        "source_kind": "synthetic",
        "provenance": {
            "source": "deterministic_fixture",
            "calendar_verified": False,
            "historical_universe_verified": False,
            "corporate_actions_verified": False,
        },
        "sessions": sessions,
        "instruments": instruments,
        "bars": [],
        "universe": [],
        "actions": [],
        "financial_facts": [],
        "macro": [],
        "benchmarks": [],
        "etf_metadata": [],
    }
    before = sessions[0] + "T00:00:00+00:00"
    for instrument in instruments:
        if instrument["kind"].startswith("etf"):
            data["etf_metadata"].append(
                {
                    "symbol": instrument["symbol"],
                    "benchmark_id": instrument["benchmark_id"],
                    "currency": "KRW",
                    "benchmark_currency": "KRW",
                    "benchmark_return_basis": "price",
                    "leverage": 1,
                    "inverse": False,
                    "hedged": False,
                    "annual_fee_pct": 0.1,
                    "available_at": before,
                    "ingested_at": before,
                }
            )
    for i, session in enumerate(sessions):
        available = session + "T08:00:00+00:00"
        data["universe"].append(
            {
                "session": session,
                "symbols": [r["symbol"] for r in instruments],
                "available_at": session + "T00:00:00+00:00",
                "ingested_at": available,
            }
        )
        for j, instrument in enumerate(instruments):
            value = (10000 + j * 3000) * math.exp(
                0.0007 * i
                + 0.065
                * math.sin(i / (10 if j < 2 else 10 + j * 2) + (math.pi * j if j < 2 else j))
            )
            opening = value * (1 + 0.002 * math.sin(i * 0.7 + j))
            data["bars"].append(
                {
                    "symbol": instrument["symbol"],
                    "session": session,
                    "open": str(round(opening, 4)),
                    "high": str(round(max(value, opening) * 1.007, 4)),
                    "low": str(round(min(value, opening) * 0.993, 4)),
                    "close": str(round(value, 4)),
                    "volume": 100000 + (i * 997 + j * 700) % 50000,
                    "turnover": str(round(value * 120000)),
                    "market_cap": str(round(value * 10000000)),
                    "venue": "KRX",
                    "session_type": "regular",
                    "price_basis": "raw",
                    "final": True,
                    "available_at": available,
                    "ingested_at": available,
                }
            )
        for name, scale in (("KOSPI", 1000), ("NASDAQ", 2000)):
            value = scale * math.exp(0.0004 * i + 0.01 * math.sin(i / 20))
            data["benchmarks"].append(
                {
                    "symbol": name,
                    "session": session,
                    "open": str(value),
                    "high": str(value),
                    "low": str(value),
                    "close": str(value),
                    "volume": 1,
                    "turnover": "1",
                    "venue": "KRX",
                    "session_type": "regular",
                    "price_basis": "raw",
                    "final": True,
                    "available_at": available,
                    "ingested_at": available,
                }
            )
        for name, value in (
            ("VIXCLS", 20 + 7 * math.sin(i / 25)),
            ("VXNCLS", 24 + 8 * math.sin(i / 25)),
            ("DEXKOUS", 1300 + 80 * math.sin(i / 40)),
            ("DGS2", 4 + 0.5 * math.sin(i / 60)),
            ("DGS10", 4.2 + 0.4 * math.sin(i / 60)),
            ("BAMLH0A0HYM2", 4 + math.sin(i / 50)),
        ):
            data["macro"].append(
                {
                    "series": name,
                    "observation_date": session,
                    "vintage_date": session,
                    "value": value,
                    "available_at": available,
                    "ingested_at": available,
                }
            )
    # The benchmark is an instrument identity too, but never an investable universe member.
    data["instruments"].extend(
        {"symbol": s, "name": "합성 " + s, "kind": "benchmark"} for s in ("KOSPI", "NASDAQ")
    )
    for symbol in ("005930", "105560"):
        for year in range(2021, 2026):
            for q, (month, day) in enumerate(((3, 31), (6, 30), (9, 30), (12, 31)), 1):
                end = date(year, month, day)
                start = date(year, (q - 1) * 3 + 1, 1)
                published = (end + timedelta(days=45)).isoformat() + "T00:00:00+00:00"
                for account, value in (
                    ("revenue", 20_000_000_000),
                    ("operating_income", 2_000_000_000),
                    ("net_income", 1_000_000_000),
                    ("operating_cash_flow", 1_500_000_000),
                    ("equity", 50_000_000_000),
                    ("liabilities", 20_000_000_000),
                ):
                    data["financial_facts"].append(
                        {
                            "symbol": symbol,
                            "receipt_id": f"synthetic-{year}-{q}",
                            "account": account,
                            "scope": "CFS",
                            "attribution": "total",
                            "currency": "KRW",
                            "period_type": "instant"
                            if account in {"equity", "liabilities"}
                            else "quarter",
                            "period_start": start.isoformat(),
                            "period_end": end.isoformat(),
                            "value": str(value * (1 + 0.03 * (year - 2021))),
                            "available_at": published,
                            "ingested_at": published,
                        }
                    )
    return data
