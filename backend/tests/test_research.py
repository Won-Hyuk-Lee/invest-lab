import math
from datetime import date, timedelta

import pytest

from invest_lab.adapters.research import (
    SklearnResearchLearner,
    build_research_service,
    purged_date_split,
)
from invest_lab.application.research import (
    build_feature_rows,
    synthetic_research_dataset,
    training_rows,
)
from invest_lab.domain.research import content_hash, coverage, validate_dataset


def test_research_dataset_is_immutable_hashed_and_synthetic_not_pit_ready(tmp_path):
    service = build_research_service(tmp_path / "research.db")
    payload = synthetic_research_dataset(135)
    first = service.import_dataset(payload)
    again = service.import_dataset(payload)
    assert first["id"] == again["id"]
    assert len(service.list_datasets()) == 1
    assert first["coverage"]["pit_ready"] is False
    assert first["coverage"]["real_validation_complete"] is False
    assert first["export"]["tables"]["bars"]["rows"] == 540
    payload["bars"][0]["volume"] += 1
    changed = service.import_dataset(payload)
    assert changed["id"] != first["id"]
    assert service.repository.get_dataset(first["id"])["data"]["bars"][0]["volume"] == 100000


def test_missing_session_is_excluded_not_treated_as_one_fewer_trading_day():
    data = synthetic_research_dataset(150)
    data["bars"] = [
        r
        for r in data["bars"]
        if not (r["symbol"] == "005930" and r["session"] == data["sessions"][-5])
    ]
    row = next(r for r in build_feature_rows(data, data["sessions"][-1]) if r["symbol"] == "005930")
    assert "missing_trading_session" in row["missing_reasons"]
    assert all(not s["eligible"] for s in row["signals"])
    assert coverage(data)["missing_bar_count"] == 1


def test_system_time_replay_excludes_data_not_ingested_then():
    data = synthetic_research_dataset(130)
    for row in data["financial_facts"]:
        row["ingested_at"] = "2030-01-01T00:00:00+00:00"
    rows = build_feature_rows(data, data["sessions"][-1], observed_by="2023-12-01T00:00:00+00:00")
    assert next(r for r in rows if r["symbol"] == "005930")["financial"]["values"] == {}


def learning_rows():
    rows = []
    start = date(2020, 1, 1)
    for i in range(180):
        session = (start + timedelta(days=i)).isoformat()
        for j in range(4):
            feature = math.sin(i / 5 + j)
            rows.append(
                {
                    "session": session,
                    "symbol": str(j),
                    "features": {"return7": feature, "rsi14": 50 + 20 * feature, "atr_pct": 2 + j},
                    "target": feature * 0.02 + 0.002 * math.cos(i),
                    "label_start": (start + timedelta(days=i + 1)).isoformat(),
                    "label_end": (start + timedelta(days=i + 2)).isoformat(),
                    "label_available_at": (start + timedelta(days=i + 2)).isoformat()
                    + "T20:00:00+00:00",
                }
            )
    return rows


def test_date_split_purges_both_training_and_calibration_targets():
    rows = learning_rows()
    dates = sorted({r["session"] for r in rows})[-20:]
    train, calibration, test = purged_date_split(rows, dates)
    assert max(r["label_end"] for r in train) < min(r["session"] for r in calibration)
    assert max(r["label_end"] for r in calibration) < min(r["session"] for r in test)
    assert not {r["session"] for r in train} & {r["session"] for r in test}


@pytest.mark.parametrize("kind", ["ridge", "logistic"])
def test_models_reproduce_predictions_and_holdout_not_used_for_tuning(kind):
    learner = SklearnResearchLearner()
    rows = learning_rows()
    params = {
        "model_type": kind,
        "folds": 2,
        "holdout_dates": 20,
        "calibration_dates": 15,
        "seed": 42,
    }
    report = learner.train(rows, params)
    again = learner.train(rows, params)
    assert content_hash(report) == content_hash(again)
    assert report["final_test_used_for_selection"] is False
    predicted = learner.predict(
        report["artifact"],
        [r["features"] for r in rows if r["session"] >= report["artifact"]["holdout_start"]],
    )
    assert predicted == pytest.approx([r["prediction"] for r in report["predictions"]])
    assert report["artifact"]["max_training_label_available_at"] < report["artifact"]["fit_cutoff"]
    # Holdout labels may change evaluation, but cannot alter fitted parameters/selected alpha.
    cutoff = report["artifact"]["holdout_start"]
    mutated = [
        {**r, "target": -r["target"] if r["session"] >= cutoff else r["target"]} for r in rows
    ]
    altered = learner.train(mutated, params)
    assert altered["artifact"] == report["artifact"]


def test_facade_backtest_persists_report_and_failed_experiment(tmp_path):
    service = build_research_service(tmp_path / "research.db")
    dataset = service.import_dataset(synthetic_research_dataset(150))
    features = service.features(dataset["id"])
    assert any(r["symbol"] == "0048K0" for r in features["rows"])
    result = service.run_experiment(
        {
            "dataset_id": dataset["id"],
            "kind": "backtest",
            "parameters": {"strategy_type": "etf_trend"},
        }
    )
    assert service.get_experiment(result["id"])["source_kind"] == "synthetic"
    with pytest.raises(ValueError):
        service.run_experiment({"dataset_id": dataset["id"], "kind": "train"})
    assert any(e["status"] == "failed" for e in service.list_experiments())


def test_dataset_unknown_symbol_and_price_basis_rejected():
    data = synthetic_research_dataset(5)
    data["bars"][0]["symbol"] = "UNKNOWN"
    with pytest.raises(ValueError, match="마스터"):
        validate_dataset(data)


def test_readiness_is_derived_and_separate_from_broker_validation():
    data = synthetic_research_dataset(130)
    data["source_kind"] = "imported"
    data["provenance"].update(
        historical_universe_verified=True,
        corporate_actions_verified=True,
        calendar_verified=True,
        verification_report="approved-import-evidence-001",
    )
    assert coverage(data)["research_validation_ready"] is True
    assert coverage(data)["broker_validation_complete"] is False
    data["bars"][0]["available_at"] = "2030-01-01T00:00:00+00:00"
    assert coverage(data)["research_validation_ready"] is False
    assert "bars_not_available_at_decision" in coverage(data)["issues"]


def test_labels_record_delisting_gaps_and_include_supplied_settlement():
    data = synthetic_research_dataset(145)
    event_session = data["sessions"][131]
    data["actions"].append(
        {
            "event_id": "exit-A",
            "symbol": "005930",
            "type": "delisting",
            "effective_date": event_session,
            "available_at": data["sessions"][129] + "T08:00:00+00:00",
            "ingested_at": data["sessions"][129] + "T08:00:00+00:00",
        }
    )
    for row in data["universe"]:
        if row["session"] >= event_session:
            row["symbols"].remove("005930")
    data["bars"] = [
        r for r in data["bars"] if r["symbol"] != "005930" or r["session"] < event_session
    ]
    _, report = training_rows(data, with_report=True)
    assert report["survivorship_limited"] is True
    assert report["omitted_reasons"]["missing_delisting_settlement"] > 0
    data["actions"][0]["settlement_price"] = "0"
    rows, report = training_rows(data, with_report=True)
    assert report["settled_delisting_rows"] > 0
    assert any(r["symbol"] == "005930" and r["target"] < -0.9 for r in rows)


def test_label_availability_includes_late_entry_price_revision():
    data = synthetic_research_dataset(140)
    row = next(
        r for r in data["bars"] if r["symbol"] == "005930" and r["session"] == data["sessions"][125]
    )
    data["bars"].append(
        {
            **row,
            "open": str(float(row["open"]) * 0.99),
            "available_at": "2030-01-01T00:00:00+00:00",
            "ingested_at": "2030-01-01T00:00:00+00:00",
        }
    )
    labels = training_rows(data)
    target = next(
        r for r in labels if r["session"] == data["sessions"][124] and r["symbol"] == "005930"
    )
    assert target["label_available_at"].startswith("2030-")


def test_unvalidated_model_can_shadow_but_cannot_be_selected(tmp_path):
    service = build_research_service(tmp_path / "research.db")
    service.repository.save_model(
        {
            "id": "model",
            "created_at": "2023-01-01T00:00:00+00:00",
            "status": "candidate",
            "source_kind": "synthetic",
            "validation_ready": False,
            "adoption_ready": False,
        }
    )
    assert service.set_model_status("model", "shadow")["status"] == "shadow"
    with pytest.raises(ValueError, match="shadow"):
        service.set_model_status("model", "selected")


def test_prediction_enforces_time_product_missing_features_and_probability_threshold(tmp_path):
    from invest_lab.adapters.research import TECHNICAL_COLUMNS

    service = build_research_service(tmp_path / "research.db")
    dataset = service.import_dataset(synthetic_research_dataset(150))
    count = len(TECHNICAL_COLUMNS)
    artifact = {
        "feature_names": TECHNICAL_COLUMNS,
        "fit_cutoff": "2022-01-01T00:00:00+00:00",
        "max_training_label_available_at": "2021-12-30T00:00:00+00:00",
        "imputation": [0] * count,
        "mean": [0] * count,
        "scale": [1] * count,
        "coefficients": [0] * count,
        "intercept": -1,
        "calibration": None,
    }
    model = {
        "id": "predictor",
        "created_at": "2023-01-01T00:00:00+00:00",
        "status": "shadow",
        "model_type": "ridge",
        "artifact": artifact,
        "target": {"asset_kind": "kospi"},
        "source_kind": "synthetic",
    }
    service.repository.save_model(model)
    result = service.predict("predictor", dataset["id"])
    assert len(result["rows"]) == 2
    assert result["threshold"] == 0
    assert all(r["prediction"] == -1 and not r["eligible_prediction"] for r in result["rows"])
    assert all("model_asset_kind_mismatch" in r["reasons"] for r in result["excluded"])
    data = service.repository.get_dataset(dataset["id"])["data"]
    data["benchmarks"] = []
    missing = service.import_dataset(data)
    assert service.predict("predictor", missing["id"])["rows"] == []
    model["artifact"]["max_training_label_available_at"] = model["artifact"]["fit_cutoff"]
    service.repository.save_model(model)
    with pytest.raises(ValueError, match="fit_cutoff"):
        service.predict("predictor", dataset["id"])


def test_price_labels_and_total_return_labels_use_same_basis_benchmarks():
    data = synthetic_research_dataset(140)
    data["actions"].append(
        {
            "event_id": "income",
            "symbol": "005930",
            "type": "dividend",
            "effective_date": data["sessions"][128],
            "payment_date": data["sessions"][139],
            "amount": "100",
            "available_at": data["sessions"][120] + "T08:00:00+00:00",
            "ingested_at": data["sessions"][120] + "T08:00:00+00:00",
        }
    )
    _, missing = training_rows(data, return_basis="total_return", with_report=True)
    assert missing["omitted_reasons"]["missing_benchmark_outcome"] > 0
    data["benchmarks"].extend([{**b, "price_basis": "total_return"} for b in data["benchmarks"]])
    price = next(r for r in training_rows(data) if r["symbol"] == "005930")
    total = next(
        r for r in training_rows(data, return_basis="total_return") if r["symbol"] == "005930"
    )
    assert total["target"] > price["target"]
    # A second basis must not create duplicate observations in technical features.
    feature = build_feature_rows(data, data["sessions"][124], symbol="005930")[0]
    assert feature["technical"]["history_count"] == 125


def test_unverified_etf_distribution_history_does_not_claim_total_return():
    data = synthetic_research_dataset(130)
    data["source_kind"] = "imported"
    row = next(r for r in build_feature_rows(data, data["sessions"][-1]) if r["kind"] == "etf_kr")
    assert row["etf"]["metrics"]["price_return"] is not None
    assert row["etf"]["metrics"]["total_return"] is None
    assert "distribution_coverage_unverified" in row["etf"]["metrics"]["missing_reasons"]
