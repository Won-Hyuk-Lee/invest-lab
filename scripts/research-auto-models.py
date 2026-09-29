"""Predeclared automatic-model comparison, robustness and early-crash cohorts."""

import argparse
import json
from dataclasses import replace
from pathlib import Path

from invest_lab.adapters.etf_store import FileEtfStore
from invest_lab.application.auto_research import AutoResearchService
from invest_lab.auto_cli import engine_revision
from invest_lab.domain.auto_models import BLENDS, MODELS
from invest_lab.domain.auto_replay import AutoConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".local/auto-models")
    parser.add_argument("--end", required=True)
    args = parser.parse_args()
    root = Path(args.root)
    ids = json.loads((root / "datasets-latest.json").read_text(encoding="utf-8"))
    revision = engine_revision()
    store = FileEtfStore(root)
    service = AutoResearchService(store, revision=revision)
    base = AutoConfig("2006-01-01", args.end)
    scenarios = {
        "base_usd": base,
        "krw_cash": replace(base, cash_currency="KRW"),
        "cost_2x": replace(base, fee_bps=5, slippage_bps=10, fx_bps=20),
        "cost_4x": replace(base, fee_bps=10, slippage_bps=20, fx_bps=40),
        "delay_1": replace(base, extra_delay=1),
        "usd_cash_2pct": replace(base, usd_cash_rate=0.02),
    }
    for start, end in [
        ("2006-01-01", "2012-12-31"),
        ("2013-01-01", "2019-12-31"),
        ("2020-01-01", "2023-12-31"),
        ("2024-01-01", args.end),
    ]:
        scenarios[f"period_{start[:4]}_{end[:4]}"] = replace(base, start=start, end=end)
    rows, failures = [], []

    def run(scenario, datasets, model, config, params=None):
        try:
            result = service.run(datasets, model, config, params)
            # Keep compact metrics in the suite; full year/ledger/decision detail is in each run.
            result["metrics"] = {
                k: v for k, v in result["metrics"].items() if k != "years"
            }
            rows.append({"scenario": scenario, "parameters": params or {}, **result})
        except (ValueError, KeyError) as exc:
            failures.append({"scenario": scenario, "model": model, "error": str(exc)})

    for scenario, config in scenarios.items():
        for model in [*MODELS, *BLENDS]:
            run(scenario, ids, model, config)
        print(f"{scenario}: {len(rows)} completed, {len(failures)} failed", flush=True)
    equity_ids = {s: ids[s] for s in ("SPY", "QQQ")}
    for name, start, end in [
        ("long_2001", "2001-02-01", args.end),
        ("dotcom_2001_2003", "2001-02-01", "2003-12-31"),
    ]:
        for model in [*MODELS, *BLENDS]:
            if model != "asset_rotation":
                run(name, equity_ids, model, replace(base, start=start, end=end))
        print(f"{name}: {len(rows)} completed, {len(failures)} failed", flush=True)
    variations = [
        ("pullback_rsi", "rsi_entry", (5, 15)),
        ("pullback_z", "z_entry", (-1.5, -2.5)),
        ("panic_immediate", "fear_level", (25, 35)),
        ("panic_recovery", "fear_level", (25, 35)),
        ("breakout", "stop_fraction", (0.08, 0.12)),
        ("long_trend", "trend_floor", (0, 0.75)),
    ]
    for model, key, values in variations:
        for value in values:
            run("threshold_sensitivity", ids, model, base, {key: value})
    # Diagnostic controls chosen after seeing a model's average exposure, not deployable models.
    originals = [
        r
        for r in rows
        if r["scenario"] == "base_usd"
        and r["model"]
        in {
            "breakout",
            "pullback_rsi",
            "pullback_z",
            "panic_immediate",
            "panic_recovery",
        }
    ]
    for row in originals:
        run(
            f"exposure_control_{row['model']}",
            ids,
            "fixed_half",
            base,
            {"trend_floor": row["metrics"]["mean_exposure"]},
        )
    result = {
        "engine_revision": revision,
        "dataset_ids": ids,
        "rows": rows,
        "failures": failures,
        "source_kind": "real",
        "performance_validated": False,
        "evaluation_kind": "Historical fixed-rule comparisons, not untouched holdout validation",
        "exposure_controls": "Ex-post approximate controls, never eligible for selection",
    }
    path = root / f"suite-{revision[:12]}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report {path}: {len(rows)} completed, {len(failures)} failed", flush=True)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
