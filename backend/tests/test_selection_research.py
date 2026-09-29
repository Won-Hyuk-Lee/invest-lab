import copy
import io
import math
import zipfile
from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import pytest

from invest_lab.adapters.etf_store import FileEtfStore
from invest_lab.adapters.selection_sources import parse_french_monthly
from invest_lab.application.selection_research import SelectionResearchService
from invest_lab.domain.auto_models import MODELS
from invest_lab.domain.auto_replay import AutoConfig, replay
from invest_lab.domain.factor_research import audit_factors, panel, replay_factors
from invest_lab.domain.selection_models import (
    SECTORS,
    SELECTION_MODELS,
    SelectionModel,
    decision_function,
    prepare_selection,
    target_weights,
)


@pytest.fixture
def datasets():
    days = [date(2000, 1, 3) + timedelta(days=i) for i in range(850)]
    days = [d for d in days if d.weekday() < 5]
    output = {}
    for j, symbol in enumerate(("SPY", "QQQ", "GLD", "SHY")):
        bars = []
        for i, day in enumerate(days):
            price = 100 * math.exp(i * 0.001 * (j + 1) + 0.02 * math.sin(i / 3 + j))
            bars.append(
                {
                    "session": str(day),
                    "open_at": f"{day}T09:30:00-05:00",
                    "close_at": f"{day}T16:00:00-05:00",
                    "open": price,
                    "close": price,
                    "fx_open": 1000,
                    "fx_close": 1000,
                    "month_end": False,
                }
            )
        for a, b in zip(bars, bars[1:]):
            a["month_end"] = a["session"][:7] != b["session"][:7]
        output[symbol] = {
            "format_version": 1,
            "symbol": symbol,
            "source_kind": "synthetic",
            "price_basis": "dividend_split_adjusted_research_units",
            "bars": bars,
            "macro": {},
            "provenance": {"point_in_time_verified": False},
        }
    return output


def config():
    return AutoConfig("2001-02-01", "2002-02-28")


def model(**kwargs):
    return SelectionModel("test", "test", ("SPY", "QQQ", "GLD"), top_k=2, **kwargs)


def test_covariance_is_past_only_and_future_changes_do_not_change_fills(datasets):
    tape = prepare_selection(datasets)
    m = model(target_vol=0.12, weighting="inverse_vol")
    before = replay(tape, m, config(), decide_fn=decision_function(tape, []))
    changed = copy.deepcopy(datasets)
    for d in changed.values():
        for b in d["bars"]:
            if b["session"] >= "2001-10-01":
                b["close"] *= 10
                b["open"] *= 10
    after_tape = prepare_selection(changed)
    after = replay(after_tape, m, config(), decide_fn=decision_function(after_tape, []))
    assert [d for d in before["daily"] if d["session"] < "2001-10-01"] == [
        d for d in after["daily"] if d["session"] < "2001-10-01"
    ]
    assert before["audit"]["ledger_verified"]


def test_correlation_affects_portfolio_volatility_and_never_leverages():
    m = model(target_vol=0.12)
    high, detail = target_weights(
        m, ["SPY", "QQQ"], np.array([[0.04, 0.04], [0.04, 0.04]]), ("SPY", "QQQ")
    )
    low, _ = target_weights(
        m, ["SPY", "QQQ"], np.array([[0.04, -0.02], [-0.02, 0.04]]), ("SPY", "QQQ")
    )
    assert sum(high.values()) == pytest.approx(0.6)
    assert sum(low.values()) == pytest.approx(1)
    assert detail["volatility_after_defensive"] == pytest.approx(0.12)


def test_single_slot_and_group_caps_leave_budget_unspent():
    covariance = np.eye(3) * 0.04
    weights, _ = target_weights(model(equity_cap=0.3), ["SPY"], covariance, ("SPY", "QQQ", "GLD"))
    assert weights == {"SPY": 0.3}
    weights, _ = target_weights(
        model(max_weight=0.2), ["SPY", "QQQ"], covariance, ("SPY", "QQQ", "GLD")
    )
    assert sum(weights.values()) == pytest.approx(0.4)


def test_defensive_asset_is_explicit_and_included_in_risk_diagnostic():
    covariance = np.eye(4) * 0.04
    weights, details = target_weights(
        model(defensive="SHY"), [], covariance, ("SPY", "QQQ", "GLD", "SHY")
    )
    assert weights == {"SHY": 1}
    assert details["volatility_after_defensive"] == pytest.approx(0.2)


def test_monthly_selection_weekly_risk_and_next_open(datasets):
    tape = prepare_selection(datasets)
    diagnostics = []
    result = replay(
        tape, model(target_vol=0.12), config(), decide_fn=decision_function(tape, diagnostics)
    )
    reasons = {d["reason"] for d in result["decisions"]}
    assert reasons == {"monthly_selection", "weekly_risk_update"}
    assert all(t["signal_session"] < t["session"] for t in result["trades"])
    assert result["trades"][0]["session"] == "2001-02-01"
    delayed = replay(
        tape,
        model(target_vol=0.12),
        replace(config(), extra_delay=1),
        decide_fn=decision_function(tape, []),
    )
    assert delayed["trades"][0]["session"] == "2001-02-02"
    monthly = replay(
        tape,
        model(target_vol=0.12, weekly_risk=False),
        config(),
        decide_fn=decision_function(tape, []),
    )
    assert {d["reason"] for d in monthly["decisions"]} == {"monthly_selection"}


def test_buffer_retains_existing_selection_but_trend_still_filters(datasets):
    tape = prepare_selection(datasets)
    i = tape["sessions"].index("2001-02-01")
    current = {s: tape["features"][s][i] for s in tape["bars"]}
    for s, score in zip(("SPY", "QQQ", "GLD"), (0.3, 0.2, 0.1)):
        current[s].update(ret63=score, ret126=score, ret252=score, close=110, sma200=100)
    state = {"selected": ["GLD", "QQQ"]}
    weights, _ = decision_function(tape, [])(
        model(buffer_rank=3), current, {}, state, i, True, False, None
    )
    assert set(weights) == {"GLD", "QQQ"}
    current["GLD"]["close"] = 90
    weights, _ = decision_function(tape, [])(
        model(buffer_rank=3), current, {}, state, i, True, False, None
    )
    assert weights == {"QQQ": 0.5}


def test_old_policy_output_is_unchanged_with_injected_decider(datasets):
    tape = prepare_selection(datasets)
    assert replay(tape, MODELS["hold_qqq"], config()) == replay(
        tape, MODELS["hold_qqq"], config(), decide_fn=decision_function(tape, [])
    )


def test_service_replay_is_offline_reproducible_and_strict_pit_rejects(datasets, tmp_path):
    store = FileEtfStore(tmp_path)
    ids = {s: store.save_dataset(d) for s, d in datasets.items()}
    service = SelectionResearchService(store, revision="test")
    a = service.run(ids, "qqq_risk", config())
    b = service.run(ids, "qqq_risk", config())
    assert a["run_id"] == b["run_id"]
    assert store.load_run(a["run_id"])["allocation_diagnostics"]
    with pytest.raises(ValueError, match="PIT"):
        service.run(ids, "qqq_risk", replace(config(), strict_pit=True))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"top_k": 5},
        {"target_vol": float("nan")},
        {"max_weight": 1.1},
        {"defensive": "SPY"},
        {"buffer_rank": 1},
        {"equity_cap": 0},
    ],
)
def test_invalid_selection_parameters(kwargs):
    base = dict(id="t", label="t", candidates=("SPY", "QQQ"), top_k=2)
    with pytest.raises(ValueError):
        SelectionModel(**(base | kwargs))


def archive(text):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        z.writestr("table.csv", text)
    return output.getvalue()


def test_french_parser_stops_before_equal_weight_table_and_preserves_missing():
    raw = archive(
        "Value weighted\n,Lo 30,Hi 30\n200001,1,2\n200002,-99.99,3\n\n"
        "Equal weighted\n,Lo 30,Hi 30\n200001,80,90\n"
    )
    rows = parse_french_monthly(raw)
    assert len(rows) == 2
    assert rows[0]["returns"]["Hi 30"] == 0.02
    assert rows[1]["returns"]["Lo 30"] is None


@pytest.fixture
def factor_rows():
    return [
        {
            "month": f"{2000 + i // 12}-{i % 12 + 1:02d}",
            "returns": {
                "market": 0.01,
                "profit_high": 0.02,
                "profit_low": 0.001,
                "value_high": 0.015,
                "value_low": 0.005,
                "rf": 0.002,
            },
        }
        for i in range(60)
    ]


def test_factor_compounding_known_return_and_fx(factor_rows):
    fx = {r["month"]: 1000 for r in factor_rows}
    fx["2002-12"] = 1200
    result = replay_factors(factor_rows, "profit_high", "2001-01", "2002-12", switch_bps=0, fx=fx)
    assert result["metrics"]["usd"]["total_return"] == pytest.approx(1.02**24 - 1)
    assert result["metrics"]["krw"]["total_return"] == pytest.approx(1.02**24 * 1.2 - 1)
    assert result["audit"]["monthly_arithmetic_verified"]
    assert not result["executable_portfolio"]


def test_factor_trend_excludes_last_month_and_future_changes(factor_rows):
    before = replay_factors(factor_rows, "profit_trend", "2001-01", "2003-12")
    changed = copy.deepcopy(factor_rows)
    for r in changed:
        if r["month"] == "2002-06":
            r["returns"]["profit_high"] = -0.9
    after = replay_factors(changed, "profit_trend", "2001-01", "2003-12")

    def by_month(result):
        return {r["month"]: r for r in result["monthly"]}

    a, b = by_month(before), by_month(after)
    assert a["2002-07"]["weights"] == b["2002-07"]["weights"]
    assert a["2002-08"]["weights"] != b["2002-08"]["weights"]
    assert a["2002-07"]["signal_through"] == "2002-05"


def test_factor_costs_and_audit_detect_corruption(factor_rows):
    base = replay_factors(factor_rows, "profit_high", "2001-01", "2002-12")
    costly = replay_factors(factor_rows, "profit_high", "2001-01", "2002-12", annual_drag=0.02)
    assert costly["metrics"]["usd"]["cagr"] < base["metrics"]["usd"]["cagr"]
    costly["monthly"][-1]["equity"] += 0.01
    with pytest.raises(ValueError, match="arithmetic"):
        audit_factors(costly, factor_rows)


def test_factor_missing_month_and_values_are_errors():
    data = {
        name: {
            "rows": [
                {"month": m, "returns": {"Hi 30": 0.01, "Lo 30": 0.02, "RF": 0.001, "Mkt-RF": 0.01}}
                for m in ("2000-01", "2000-03")
            ]
        }
        for name in ("profitability", "value", "market")
    }
    with pytest.raises(ValueError, match="month"):
        panel(data)
    for d in data.values():
        d["rows"] = d["rows"][:1]
    data["value"]["rows"][0]["returns"]["Hi 30"] = None
    with pytest.raises(ValueError, match="Missing"):
        panel(data)


def test_registry_preserves_predeclared_default():
    assert SELECTION_MODELS["global_risk"].target_vol == 0.12
    assert SELECTION_MODELS["global_group"].equity_cap == 0.6


def test_defensive_etf_cannot_push_target_above_volatility_limit():
    covariance = np.eye(4) * 0.04
    weights, details = target_weights(
        model(defensive="SHY", target_vol=0.12), [], covariance, ("SPY", "QQQ", "GLD", "SHY")
    )
    assert weights == {"SHY": pytest.approx(0.6)}
    assert details["volatility_after_defensive"] == pytest.approx(0.12)


def test_blend_diagnostics_are_not_mislabeled_as_combined_risk(datasets, tmp_path):
    for symbol in SECTORS:
        datasets[symbol] = copy.deepcopy(datasets["GLD"])
        datasets[symbol]["symbol"] = symbol
    store = FileEtfStore(tmp_path)
    ids = {s: store.save_dataset(d) for s, d in datasets.items()}
    service = SelectionResearchService(store, revision="blend-test")
    result = store.load_run(service.run(ids, "sector_pullback", config())["run_id"])
    assert "allocation_diagnostics" not in result
    assert len(result["component_ids"]) == 2
    assert sum(c["weight"] for c in result["policy"]["components"]) == 1
    assert result["audit"]["ledger_verified"]
