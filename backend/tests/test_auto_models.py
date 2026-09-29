import copy
from dataclasses import replace
from datetime import date, timedelta

import pytest

from invest_lab.adapters.etf_store import FileEtfStore
from invest_lab.application.auto_research import AutoResearchService
from invest_lab.domain.auto_models import BLENDS, MODELS, decide, features
from invest_lab.domain.auto_replay import AutoConfig, audit, blend, prepare, replay


@pytest.fixture
def datasets():
    days = [date(2000, 1, 3) + timedelta(days=i) for i in range(1250)]
    days = [d for d in days if d.weekday() < 5]
    result = {}
    for symbol in ("SPY", "QQQ", "IEF", "GLD"):
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
        result[symbol] = {
            "format_version": 1,
            "symbol": symbol,
            "source_kind": "synthetic",
            "price_basis": "dividend_split_adjusted_research_units",
            "bars": bars,
            "macro": {
                "VIX": [
                    {"date": str(d), "value": 20, "available_at": f"{d}T16:15:00-05:00"}
                    for d in days
                ]
            },
            "provenance": {"point_in_time_verified": False},
        }
    return result


def cfg(**kwargs):
    return AutoConfig("2002-01-02", "2002-04-30", fee_bps=0, slippage_bps=0, fx_bps=0, **kwargs)


def tape(datasets):
    return prepare(datasets, tuple(datasets))


def test_features_are_prefix_only_and_rsi_wilder_handles_flat_market(datasets):
    bars = datasets["QQQ"]["bars"]
    full = features(bars)
    assert features(bars[:600]) == full[:600]
    assert full[500]["rsi2"] == 50
    for b in bars[600:]:
        b["close"] *= 50
    assert features(bars)[:600] == full[:600]
    bars[0]["close"] = 100
    bars[1]["close"] = 102
    bars[2]["close"] = 101
    assert features(bars[:3])[2]["rsi2"] == pytest.approx(100 * 2 / 3)


def test_multiple_buys_share_one_cash_budget_including_costs(datasets):
    config = replace(cfg(), fee_bps=10, slippage_bps=20, fx_bps=30)
    result = replay(tape(datasets), MODELS["equal_equity"], config)
    expected = config.initial_krw / (1.001 * 1.002 * 1.003)
    assert result["daily"][0]["equity_krw"] == pytest.approx(expected)
    assert len(result["trades"]) == 2
    assert result["trades"][0]["units"] == result["trades"][1]["units"]
    assert min(d["cash_usd"] for d in result["daily"]) >= 0
    assert result["audit"]["ledger_verified"]


def test_extra_delay_uses_later_actual_open_not_same_signal_fill(datasets):
    for d in datasets.values():
        for b in d["bars"]:
            if b["session"] >= "2002-01-03":
                b["open"] = b["close"] = 200
    prepared = tape(datasets)
    normal = replay(prepared, MODELS["hold_qqq"], cfg())
    late = replay(prepared, MODELS["hold_qqq"], cfg(extra_delay=1))
    assert normal["trades"][0]["session"] == "2002-01-02"
    assert late["trades"][0]["session"] == "2002-01-03"
    assert normal["metrics"]["return_krw"] == 1
    assert late["metrics"]["return_krw"] == 0


def test_next_open_gap_can_exceed_close_stop(datasets):
    prepared = tape(datasets)
    start = prepared["sessions"].index("2002-01-02")
    # Isolate the execution contract from indicator calculation.
    f = prepared["features"]["QQQ"][start - 1]
    f.update(close=110, sma200=100, prior_sma200=99, rsi2=5)
    prepared["bars"]["QQQ"][start]["close"] = 80
    prepared["features"]["QQQ"][start].update(close=80, sma5=100, rsi2=5)
    prepared["bars"]["QQQ"][start + 1]["open"] = 50
    result = replay(prepared, MODELS["pullback_rsi"], cfg())
    assert result["trades"][1]["side"] == "sell"
    assert result["trades"][1]["reason"] == "stop"
    assert result["trades"][1]["price"] == 50
    assert result["trades"][1]["realized_krw"] == pytest.approx(-5_000_000)


def test_rotation_sales_fund_buys_without_credit(datasets):
    prepared = tape(datasets)
    for s in ("SPY", "QQQ"):
        for index, f in enumerate(prepared["features"][s]):
            if f["ready"]:
                first = prepared["sessions"][index] < "2002-02-01"
                winner = (s == "SPY") == first
                f.update(
                    close=110,
                    sma200=100,
                    ret126=0.2 if winner else 0.1,
                    ret252=0.2 if winner else 0.1,
                )
    result = replay(prepared, MODELS["dual_momentum"], cfg())
    assert result["trades"][0]["symbol"] == "SPY"
    feb = [t for t in result["trades"] if t["session"] == "2002-03-01"]
    assert [(t["side"], t["symbol"]) for t in feb] == [("sell", "SPY"), ("buy", "QQQ")]
    assert result["audit"]["ledger_verified"]


def test_blend_partitions_capital_and_matches_weighted_nav(datasets):
    prepared = tape(datasets)
    for rows in prepared["bars"].values():
        for b in rows:
            if b["session"] >= "2002-01-02":
                b["fx_close"] = 900
    config = replace(cfg(usd_cash_rate=0.02), fee_bps=3, slippage_bps=5, fx_bps=10)
    core = replay(prepared, MODELS["hold_qqq"], config)
    cash = replay(prepared, MODELS["cash"], config)
    combined = blend("test", [(core, 0.7), (cash, 0.3)])
    assert combined["daily"][-1]["equity_krw"] == pytest.approx(
        0.7 * core["daily"][-1]["equity_krw"] + 0.3 * cash["daily"][-1]["equity_krw"]
    )
    assert combined["audit"]["ledger_verified"]
    assert combined["daily"][0]["exposure"] < 0.71
    with pytest.raises(ValueError, match="sum to one"):
        blend("bad", [(core, 0.7), (cash, 0.7)])
    cash["config"]["cash_currency"] = "KRW"
    with pytest.raises(ValueError, match="identical"):
        blend("bad", [(core, 0.7), (cash, 0.3)])


@pytest.mark.parametrize("model", list(MODELS))
def test_independent_runs_leave_tape_and_other_models_unchanged(datasets, model):
    prepared = tape(datasets)
    old = copy.deepcopy(prepared)
    result = replay(prepared, MODELS[model], cfg())
    replay(prepared, MODELS["hold_spy"], cfg(cash_currency="KRW"))
    assert replay(prepared, MODELS[model], cfg()) == result
    assert prepared == old


def test_future_macro_release_cannot_change_prior_decisions(datasets):
    prepared = tape(datasets)
    a = replay(prepared, MODELS["panic_immediate"], cfg())
    for d in datasets.values():
        d["macro"]["VIX"].append(
            {"date": "2002-01-01", "value": 999, "available_at": "2003-01-01T00:00:00Z"}
        )
    assert replay(tape(datasets), MODELS["panic_immediate"], cfg()) == a


def test_missing_calendar_and_mismatched_fx_fail(datasets):
    bad = copy.deepcopy(datasets)
    bad["QQQ"]["bars"].pop(400)
    with pytest.raises(ValueError, match="sessions"):
        tape(bad)
    datasets["QQQ"]["bars"][400]["fx_close"] = 999
    with pytest.raises(ValueError, match="FX"):
        tape(datasets)


def test_missing_fear_series_and_pit_claim_are_rejected(datasets):
    prepared = tape(datasets)
    with pytest.raises(ValueError, match="Strict PIT"):
        replay(prepared, MODELS["hold_qqq"], cfg(strict_pit=True))
    datasets["QQQ"]["macro"]["VIX"] = []
    with pytest.raises(ValueError, match="Missing"):
        replay(tape(datasets), MODELS["panic_immediate"], cfg())


def test_fear_arming_recovery_and_expiry_are_stateful():
    model = MODELS["panic_recovery"]
    f = {
        "ready": True,
        "close": 80,
        "sma200": 100,
        "prior_sma200": 99,
        "high63": 100,
        "rsi14": 20,
        "high3": 85,
    }
    state = {}
    assert decide(model, {"QQQ": f}, {}, state, 100, False, False, [40] * 20)[0] is None
    f.update(close=86, high3=85, rsi14=40)
    assert decide(model, {"QQQ": f}, {}, state, 105, False, False, [40] * 19 + [30])[0] == {
        "QQQ": 1
    }
    assert state["armed_until"] == -1
    assert (
        decide(model, {"QQQ": f}, {}, {"armed_until": 120}, 121, False, False, [40] * 19 + [30])[0]
        is None
    )


def test_filled_entry_controls_maximum_holding_period():
    f = {"ready": True, "close": 99, "sma5": 110, "rsi2": 20}
    position = {"QQQ": {"entry_index": 100, "entry_price": 100, "peak": 101}}
    assert (
        decide(MODELS["pullback_rsi"], {"QQQ": f}, position, {}, 108, False, False, None)[0] is None
    )
    assert decide(MODELS["pullback_rsi"], {"QQQ": f}, position, {}, 109, False, False, None) == (
        {},
        "time_exit",
    )


@pytest.mark.parametrize("field", ["fee_usd", "price", "units", "realized_krw"])
def test_independent_audit_detects_changed_trade_fields(datasets, field):
    prepared = tape(datasets)
    for i, f in enumerate(prepared["features"]["QQQ"]):
        if f["ready"]:
            f["month10"] = 90 if prepared["sessions"][i] < "2002-02-01" else 110
    result = replay(prepared, MODELS["long_trend"], cfg())
    sale = next(t for t in result["trades"] if t["side"] == "sell")
    event = next(e for e in result["events"] if e["kind"] == "trade" and e["side"] == "sell")
    sale[field] += 100
    event[field] += 100
    with pytest.raises(ValueError):
        audit(result)


@pytest.mark.parametrize("identifier", ["hold_qqq", *BLENDS])
def test_store_reload_and_repeat_are_identical(datasets, tmp_path, identifier):
    store = FileEtfStore(tmp_path / "research")
    ids = {s: store.save_dataset(d) for s, d in datasets.items()}
    sentinel = tmp_path / "operation.db"
    sentinel.write_bytes(b"untouched")
    service = AutoResearchService(store, revision="test")
    a = service.run(ids, identifier, cfg())
    b = service.run(ids, identifier, cfg())
    assert a == b
    assert audit(store.load_run(a["run_id"]))["ledger_verified"]
    assert sentinel.read_bytes() == b"untouched"


@pytest.mark.parametrize(
    "params",
    [{"extra_delay": -1}, {"rebalance_band": 1}, {"extra_delay": 1.5}, {"initial_krw": -1}],
)
def test_invalid_execution_config(params):
    with pytest.raises(ValueError):
        replace(cfg(), **params)
