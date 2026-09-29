import copy
import json
from dataclasses import FrozenInstanceError, replace
from datetime import date, timedelta

import pytest

from invest_lab.adapters.etf_sources import fx_on, parse_fx, parse_treasury
from invest_lab.adapters.etf_store import FileEtfStore
from invest_lab.application.etf_research import EtfResearchService
from invest_lab.domain.etf_research import (
    EtfReplayConfig,
    VisibleSeries,
    audit_etf,
    metrics,
    replay_etf,
    validate_etf_dataset,
)
from invest_lab.domain.etf_strategies import POLICIES, allocation


@pytest.fixture
def dataset():
    days = [date(2002, 1, 1) + timedelta(days=i) for i in range(850)]
    days = [d for d in days if d.weekday() < 5]
    bars = [
        {
            "session": str(d),
            "open_at": f"{d}T09:30:00-05:00",
            "close_at": f"{d}T16:00:00-05:00",
            "open": 100,
            "close": 100,
            "fx_open": 1000,
            "fx_close": 1000,
            "month_end": False,
        }
        for d in days
    ]
    for a, b in zip(bars, bars[1:]):
        a["month_end"] = a["session"][:7] != b["session"][:7]
    macro = {
        key: [{"date": str(d), "available_at": f"{d}T17:00:00-05:00", "value": v} for d in days]
        for key, v in {
            "VIX": 20,
            "VXN": 22,
            "RATE3M": 4,
            "RATE10": 5,
            "RATE2": 3,
            "REAL10": 1,
            "NFCI": -1,
            "WTI": 60,
            "FX_SIGNAL": 1000,
        }.items()
    }
    return {
        "format_version": 1,
        "symbol": "SPY",
        "source_kind": "synthetic",
        "price_basis": "dividend_split_adjusted_research_units",
        "bars": bars,
        "macro": macro,
        "provenance": {"point_in_time_verified": False},
    }


def config(**kwargs):
    return EtfReplayConfig(
        "2004-01-02", "2004-04-28", fee_bps=0, slippage_bps=0, fx_bps=0, **kwargs
    )


def test_fx_is_applied_once_and_total_return_units_do_not_get_extra_dividends(dataset):
    for b in dataset["bars"]:
        if b["session"] >= "2004-01-02":
            b["close"], b["fx_close"] = 110, 900
    result = replay_etf(dataset, POLICIES["buy_hold"], config())
    assert result["metrics"]["return_krw"] == pytest.approx(-0.01)
    assert result["metrics"]["return_usd"] == pytest.approx(0.1)
    assert len(result["trades"]) == 1
    assert result["audit"]["ledger_verified"]


def test_cash_currency_preserves_fx_exposure(dataset):
    for b in dataset["bars"]:
        if b["session"] >= "2004-01-02":
            b["fx_close"] = 900
    cash_policy = replace(POLICIES["fixed_50"], defensive_weight=0)
    krw = replay_etf(dataset, cash_policy, config())
    usd = replay_etf(dataset, cash_policy, config(cash_currency="USD"))
    assert krw["metrics"]["return_krw"] == 0
    assert usd["metrics"]["return_krw"] == pytest.approx(-0.1)


def test_costs_respect_cash_budget_and_have_independent_expected_value(dataset):
    cfg = EtfReplayConfig("2004-01-02", "2004-04-28", fee_bps=10, slippage_bps=20, fx_bps=30)
    result = replay_etf(dataset, POLICIES["buy_hold"], cfg)
    expected = cfg.initial_krw / (1.001 * 1.002 * 1.003)
    assert result["metrics"]["end_equity_krw"] == pytest.approx(expected)
    assert result["cost_krw"] == pytest.approx(cfg.initial_krw - expected)
    assert min(r["cash_krw"] for r in result["daily"]) >= 0


def test_interest_accrues_over_weekends_without_broker_balance(dataset):
    policy = replace(POLICIES["fixed_50"], defensive_weight=0)
    cfg = replace(config(krw_cash_rate=0.1), end="2004-01-05")
    result = replay_etf(dataset, policy, cfg)
    assert result["daily"][-1]["cash_krw"] == pytest.approx(10_000_000 * (1 + 0.1 * 3 / 365))


def test_partial_reduction_next_open_and_reentry_are_separate_decisions(dataset):
    for b in dataset["bars"]:
        if b["session"] >= "2003-12-01":
            b["open"] = b["close"] = 110
        if "2004-02-01" <= b["session"] < "2004-03-01":
            b["open"] = b["close"] = 60
    result = replay_etf(dataset, POLICIES["trend_monthly"], config())
    sides = [(t["session"], t["side"]) for t in result["trades"]]
    assert ("2004-03-01", "sell") in sides
    assert ("2004-04-01", "buy") in sides
    sale = next(t for t in result["trades"] if t["side"] == "sell")
    assert sale["target"] == 0.5
    assert sale["signal_at"].startswith("2004-02-27")
    assert 0 < sale["units"] < result["trades"][0]["units"]


def test_future_price_suffix_cannot_change_prior_trades(dataset):
    a = replay_etf(dataset, POLICIES["trend_monthly"], config())
    changed = copy.deepcopy(dataset)
    for b in changed["bars"]:
        if b["session"] >= "2004-03-01":
            b["close"] = b["open"] = 200
    b = replay_etf(changed, POLICIES["trend_monthly"], config())
    assert [t for t in a["trades"] if t["session"] < "2004-03-01"] == [
        t for t in b["trades"] if t["session"] < "2004-03-01"
    ]


def test_macro_revision_is_applied_only_after_its_release():
    series = VisibleSeries(
        [
            {"date": "2020-01-01", "available_at": "2020-01-02T00:00:00Z", "value": 1},
            {"date": "2020-01-01", "available_at": "2020-01-06T00:00:00Z", "value": 9},
        ]
    )
    assert series.at("2020-01-03T00:00:00Z") == [1]
    assert series.at("2020-01-06T00:00:00Z") == [9]
    with pytest.raises(ValueError, match="monotonic"):
        series.at("2020-01-05T00:00:00Z")


@pytest.mark.parametrize("policy_id", list(POLICIES))
def test_strategy_runs_are_independent_and_input_is_immutable(dataset, policy_id):
    original = copy.deepcopy(dataset)
    a = replay_etf(dataset, POLICIES[policy_id], config())
    replay_etf(dataset, POLICIES["fixed_50"], config(cash_currency="USD"))
    b = replay_etf(dataset, POLICIES[policy_id], config())
    assert a == b
    assert dataset == original
    with pytest.raises(FrozenInstanceError):
        POLICIES[policy_id].defensive_weight = 0.2


def test_missing_stale_and_unverified_macro_never_silently_pass(dataset):
    with pytest.raises(ValueError, match="Strict PIT"):
        replay_etf(dataset, POLICIES["buy_hold"], config(strict_pit=True))
    dataset["macro"]["NFCI"] = dataset["macro"]["NFCI"][:3]
    with pytest.raises(ValueError, match="stale"):
        replay_etf(dataset, POLICIES["trend_credit"], config())
    dataset["macro"].pop("REAL10")
    with pytest.raises(ValueError, match="Missing"):
        replay_etf(dataset, POLICIES["trend_rates"], config())


def test_bad_calendar_basis_and_warmup_fail(dataset):
    wrong = copy.deepcopy(dataset)
    wrong["bars"][0]["month_end"] = True
    with pytest.raises(ValueError, match="Month-end"):
        validate_etf_dataset(wrong)
    wrong["price_basis"] = "raw"
    with pytest.raises(ValueError, match="price basis"):
        validate_etf_dataset(wrong)
    dataset["bars"] = dataset["bars"][-70:]
    with pytest.raises(ValueError, match="warmup"):
        replay_etf(dataset, POLICIES["trend_monthly"], config())


@pytest.mark.parametrize("field", ["fee_usd", "price", "units", "fx_cost_krw"])
def test_audit_detects_corrupted_ledger(dataset, field):
    result = replay_etf(dataset, POLICIES["buy_hold"], EtfReplayConfig("2004-01-02", "2004-04-28"))
    next(e for e in result["events"] if e["kind"] == "trade")[field] += 100
    with pytest.raises(ValueError):
        audit_etf(result)


def test_negative_wti_is_real_data_not_a_log_failure():
    weight, reasons = allocation(
        POLICIES["trend_oil"], [110] * 100, [100] * 12, {"WTI": [-37] + [50] * 63}, 1
    )
    assert weight == 0.5
    assert "nonpositive_oil_price_regime" in reasons


def test_content_addressed_store_reproducibility_and_path_safety(dataset, tmp_path):
    sentinel = tmp_path / "operational.sqlite3"
    sentinel.write_bytes(b"existing user state")
    store = FileEtfStore(tmp_path / "etf")
    key = store.save_dataset(dataset)
    assert key == store.save_dataset(dataset)
    service = EtfResearchService(store, engine_revision="test")
    a = service.run(key, "buy_hold", config())
    b = service.run(key, "buy_hold", config())
    assert a["run_id"] == b["run_id"]
    assert sentinel.read_bytes() == b"existing user state"
    assert len(store.list_runs()) == 1
    with pytest.raises(ValueError, match="Invalid ETF artifact"):
        store.load_run("../operational.sqlite3")
    with pytest.raises(ValueError, match="identity"):
        service.run(key, "buy_hold", config(), {"family": "trend"})
    path = tmp_path / "etf" / "datasets" / f"{key}.json.gz"
    import gzip

    path.write_bytes(gzip.compress(json.dumps({}).encode()))
    with pytest.raises(ValueError, match="integrity"):
        store.load_dataset(key)


def test_underwater_duration_includes_recovery_day():
    daily = [
        {"session": d, "equity_krw": v, "equity_usd": v, "exposure": 1}
        for d, v in [("2020-01-01", 100), ("2020-01-02", 90), ("2020-01-06", 100)]
    ]
    assert metrics(daily, 100, 1)["longest_underwater_days"] == 5


def test_parsers_preserve_units_and_skip_only_explicit_source_missing_values():
    fx = parse_fx(b"Time Period,RXI_N.B.KO\n2020-01-01,1200\n2020-01-02,ND\n")
    assert fx == {"2020-01-01": 1200}
    assert fx_on(fx, sorted(fx), "2020-01-02", before=True) == 1200
    with pytest.raises(ValueError, match="stale"):
        fx_on(fx, sorted(fx), "2020-02-01")
    xml = (
        b"<feed><properties><NEW_DATE>2020-01-01T00:00:00</NEW_DATE>"
        b"<BC_3MONTH>1.5</BC_3MONTH></properties></feed>"
    )
    assert parse_treasury(xml)["RATE3M"][0]["value"] == 1.5


@pytest.mark.parametrize(
    "parameters",
    [
        {"trend_months": 2.5},
        {"implied_series": "BOGUS"},
        {"defensive_weight": 2},
        {"target_volatility": float("nan")},
    ],
)
def test_invalid_policy_parameters_are_rejected(parameters):
    with pytest.raises(ValueError):
        replace(POLICIES["trend_monthly"], **parameters)


def test_macro_filters_and_fx_staging_change_only_their_own_policy():
    closes, months = [100 + i for i in range(100)], [100] * 12
    rates = {"REAL10": [1] * 63 + [1.6], "RATE2": [3] * 64}
    assert allocation(POLICIES["trend_rates"], closes, months, rates, 1)[0] == 0.5
    assert allocation(POLICIES["trend_monthly"], closes, months, rates, 1)[0] == 1
    fx = {"FX_SIGNAL": [1000] * 251 + [1400]}
    assert allocation(POLICIES["trend_fx_entry"], closes, months, fx, 0.5)[0] == 0.75
    assert allocation(POLICIES["trend_fx_entry"], closes, months, fx, 0.75)[0] == 1
    assert (
        allocation(POLICIES["trend_credit"], closes, months, {"NFCI": [-1] * 4 + [1]}, 1)[0] == 0.5
    )
    assert (
        allocation(POLICIES["trend_curve"], closes, months, {"RATE10": [3], "RATE3M": [5]}, 1)[0]
        == 0.5
    )


def test_vix_recovery_wait_hold_and_reset():
    policy = POLICIES["vix_recovery"]
    rising, months = list(range(100, 200)), [100] * 12
    calm, recovery = {"VIX": [20] * 20}, {"VIX": [40] + [30] * 19}
    assert allocation(policy, rising, months, calm, 0.5)[0] == 0.5
    assert allocation(policy, rising, months, recovery, 0.5)[0] == 1
    assert allocation(policy, rising, months, calm, 1)[0] == 1
    assert allocation(policy, [80] * 100, months, calm, 1)[0] == 0.5


def test_adjusted_unit_rescaling_preserves_returns_and_decisions(dataset):
    baseline = replay_etf(dataset, POLICIES["trend_monthly"], config())
    for bar in dataset["bars"]:
        bar["open"] *= 0.6
        bar["close"] *= 0.6
    changed = replay_etf(dataset, POLICIES["trend_monthly"], config())
    assert baseline["metrics"] == changed["metrics"]
    assert baseline["decisions"] == changed["decisions"]


def test_future_macro_vintage_does_not_change_existing_replay(dataset):
    baseline = replay_etf(dataset, POLICIES["trend_rates"], config())
    dataset["macro"]["REAL10"].append(
        {"date": "2004-01-01", "available_at": "2005-01-01T00:00:00Z", "value": 999}
    )
    changed = replay_etf(dataset, POLICIES["trend_rates"], config())
    assert baseline == changed
