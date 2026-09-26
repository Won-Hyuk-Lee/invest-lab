import json

import pytest

from invest_lab.adapters.data_pipeline import ResearchDataPipeline
from invest_lab.adapters.market_data import MarketStore
from invest_lab.adapters.research import build_research_service
from invest_lab.domain.research import bars_at, decision_time


def pipeline_fixture(tmp_path, *, master_at="2023-01-01T00:00:00+00:00", source="approved_history"):
    store = MarketStore(tmp_path / "market.db")
    service = build_research_service(tmp_path / "research.db")
    dates = ["2023-01-02", "2023-01-03", "2023-01-04"]
    store.import_calendar(
        [
            {
                "session": day,
                "is_open": True,
                "open_at": day + "T00:00:00+00:00",
                "close_at": day + "T06:30:00+00:00",
            }
            for day in dates
        ],
        source=source,
        as_of="2023-01-01T00:00:00+00:00",
    )
    store.import_instruments(
        [{"symbol": "005930", "name": "stock", "kind": "kospi", "sector": "technology"}],
        source=source,
        as_of=master_at,
    )
    for day in dates:
        store.import_bars(
            "005930",
            [
                {
                    "session": day,
                    "open": "100",
                    "high": "101",
                    "low": "99",
                    "close": "100",
                    "volume": 1000,
                }
            ],
            source=source,
            available_at=day + "T07:00:00+00:00",
            recorded_at="2026-01-02T00:00:00+00:00",
        )
    return store, service, ResearchDataPipeline(store, service), dates


def test_market_bridge_keeps_price_revisions_and_never_marks_incomplete_pit_ready(tmp_path):
    store, service, pipeline, dates = pipeline_fixture(tmp_path)
    store.import_bars(
        "005930",
        [
            {
                "session": dates[0],
                "open": "110",
                "high": "111",
                "low": "109",
                "close": "110",
                "volume": 1000,
            }
        ],
        source="approved_history",
        available_at="2023-02-01T00:00:00+00:00",
        recorded_at="2026-01-02T00:00:00+00:00",
    )
    result = pipeline.import_market_dataset(dates[0], dates[-1], as_of="2026-02-01T00:00:00+00:00")
    data = service.repository.get_dataset(result["id"])["data"]
    assert len(data["bars"]) == 4
    assert bars_at(data, "005930", dates[0], decision_time(dates[0]))[0]["close"] == 100
    assert bars_at(data, "005930", dates[0], decision_time("2023-02-02"))[0]["close"] == 110
    assert all(r["ingested_at"] == "2026-01-02T00:00:00+00:00" for r in data["bars"])
    assert result["coverage"]["pit_ready"] is False
    assert "historical_universe_not_supplied" in result["coverage"]["issues"]
    assert "benchmark_history_not_supplied" in result["coverage"]["issues"]
    assert "corporate_action_coverage_not_supplied" in result["coverage"]["issues"]


def test_current_master_is_not_backfilled_into_old_universe(tmp_path):
    _, service, pipeline, dates = pipeline_fixture(tmp_path, master_at="2026-01-01T00:00:00+00:00")
    result = pipeline.import_market_dataset(dates[0], dates[-1], as_of="2026-02-01T00:00:00+00:00")
    assert service.repository.get_dataset(result["id"])["data"]["universe"] == []
    assert result["coverage"]["missing_universe_sessions"] == dates


def test_calendar_holes_stop_bridge_instead_of_weekday_guess(tmp_path):
    _, _, pipeline, dates = pipeline_fixture(tmp_path)
    with pytest.raises(ValueError, match="Calendar"):
        pipeline.build_market_dataset(dates[0], "2023-01-05")


def test_explicit_historic_material_can_pass_data_gate_without_claiming_broker_check(tmp_path):
    _, _, pipeline, dates = pipeline_fixture(tmp_path)
    universe = [
        {
            "session": day,
            "symbols": ["005930"],
            "available_at": day + "T00:00:00+00:00",
            "ingested_at": "2026-01-02T00:00:00+00:00",
        }
        for day in dates
    ]
    benchmarks = [
        {
            "symbol": "KOSPI",
            "session": day,
            "open": "100",
            "high": "101",
            "low": "99",
            "close": "100",
            "volume": 1,
            "price_basis": "raw",
            "venue": "KRX",
            "session_type": "regular",
            "final": True,
            "available_at": day + "T07:00:00+00:00",
            "ingested_at": "2026-01-02T00:00:00+00:00",
        }
        for day in dates
    ]
    result = pipeline.import_market_dataset(
        dates[0],
        dates[-1],
        as_of="2026-02-01T00:00:00+00:00",
        historical_universe=universe,
        actions=[],
        benchmarks=benchmarks,
        extra_instruments=[{"symbol": "KOSPI", "name": "KOSPI", "kind": "benchmark"}],
        provenance={
            "source": "reviewed_archive",
            "verification_report": "archive-review-001",
            "historical_universe_verified": True,
            "corporate_actions_verified": True,
            "calendar_verified": True,
        },
    )
    assert result["coverage"]["pit_ready"] is True
    assert result["coverage"]["broker_validation_complete"] is False


def test_source_merge_preserves_vintage_and_originals_without_promoting_synthetic(tmp_path):
    _, service, pipeline, dates = pipeline_fixture(tmp_path, source="deterministic_fixture")
    dataset = pipeline.import_market_dataset(dates[0], dates[-1], as_of="2026-02-01T00:00:00+00:00")
    record = {
        "series": "VIXCLS",
        "observation_date": dates[0],
        "vintage_date": dates[1],
        "value": 22,
        "available_at": dates[1] + "T08:00:00+00:00",
        "ingested_at": "2026-01-02T00:00:00+00:00",
    }
    source = {"provider": "fred", "table": "macro", "records": [record], "raw": {"original": 22}}
    merged = pipeline.merge_source_result(dataset["id"], source)
    again = pipeline.merge_source_result(merged["id"], source)
    data = service.repository.get_dataset(again["id"])["data"]
    assert data["macro"] == [record]
    assert data["source_kind"] == "synthetic"
    assert next(iter(data["source_artifacts"].values()))["raw"] == {"original": 22}
    assert again["collection"]["appended_records"] == 0
    assert not again["coverage"]["pit_ready"]


def test_ingestion_replay_and_file_import_do_not_change_availability(tmp_path):
    _, _, pipeline, dates = pipeline_fixture(tmp_path)
    early = pipeline.build_market_dataset(
        dates[0],
        dates[-1],
        as_of="2026-02-01T00:00:00+00:00",
        observed_by="2025-01-01T00:00:00+00:00",
    )
    assert early["payload"]["bars"] == []
    loaded = pipeline.import_file_content(json.dumps(early["payload"]))
    assert loaded["coverage"]["missing_bar_count"] == 3
