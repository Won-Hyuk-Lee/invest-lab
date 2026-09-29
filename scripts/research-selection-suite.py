"""Fixed selection/position-sizing experiments; persist every success and failure."""

import argparse
import json
from dataclasses import replace
from pathlib import Path

from invest_lab.adapters.etf_store import FileEtfStore
from invest_lab.application.selection_research import SelectionResearchService
from invest_lab.domain.auto_replay import AutoConfig
from invest_lab.domain.factor_research import FACTOR_MODELS, panel, replay_factors
from invest_lab.domain.selection_models import SELECTION_MODELS
from invest_lab.selection_cli import COHORTS, engine_revision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".local/selection-research")
    parser.add_argument("--end", required=True)
    args = parser.parse_args()
    root = Path(args.root)
    ids = json.loads((root / "datasets-latest.json").read_text(encoding="utf-8"))
    revision = engine_revision()
    store = FileEtfStore(root)
    service = SelectionResearchService(store, revision=revision)
    report = {
        "engine_revision": revision,
        "dataset_ids": ids,
        "rows": [],
        "factor_rows": [],
        "failures": [],
        "performance_validated": False,
    }
    path = root / f"suite-{revision[:12]}.json"

    def save():
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )

    def run(cohort, scenario, model, config, params=None):
        try:
            result = service.run(
                {s: ids[s] for s in COHORTS[cohort]}, model, config, params
            )
            result["metrics"] = {
                k: v for k, v in result["metrics"].items() if k != "years"
            }
            report["rows"].append(
                {
                    "cohort": cohort,
                    "scenario": scenario,
                    "parameters": params or {},
                    **result,
                }
            )
        except (ValueError, KeyError) as exc:
            report["failures"].append(
                {
                    "cohort": cohort,
                    "scenario": scenario,
                    "model": model,
                    "error": str(exc),
                }
            )

    for cohort, start in (
        ("sector", "2001-02-01"),
        ("global", "2006-01-01"),
        ("style", "2015-01-01"),
    ):
        base = AutoConfig(start, args.end)
        models = ["hold_spy", "hold_qqq", "long_trend", "qqq_risk"]
        models += [m for m in SELECTION_MODELS if m.startswith(cohort + "_")]
        if cohort == "sector":
            models += ["sector_pullback", "pullback_rsi"]
        if cohort == "global":
            models += ["asset_rotation"]
        if cohort == "style":
            models += ["hold_qual", "quality_momentum"]
        scenarios = {
            "base": base,
            "krw_cash": replace(base, cash_currency="KRW"),
            "cost_2x": replace(base, fee_bps=5, slippage_bps=10, fx_bps=20),
            "cost_4x": replace(base, fee_bps=10, slippage_bps=20, fx_bps=40),
            "delay_1": replace(base, extra_delay=1),
        }
        for begin, finish in (
            (start, "2012-12-31"),
            ("2013-01-01", "2019-12-31"),
            ("2020-01-01", "2023-12-31"),
            ("2024-01-01", args.end),
        ):
            begin = max(begin, start)
            if begin < finish:
                scenarios[f"period_{begin[:4]}_{finish[:4]}"] = replace(
                    base, start=begin, end=finish
                )
        if cohort == "sector":
            scenarios["common_2006"] = replace(base, start="2006-01-01")
            scenarios["dotcom_2001_2003"] = replace(base, end="2003-12-31")
        for scenario, config in scenarios.items():
            for model in models:
                run(cohort, scenario, model, config)
            save()
            print(
                f"{cohort}/{scenario}: {len(report['rows'])} runs, {len(report['failures'])} failures",
                flush=True,
            )
        # One-step ablations and target perturbations. None replaces the predeclared default.
        for name, params in (
            ("inverse_vol_only", {"target_vol": None}),
            ("equal_weight_vol", {"weighting": "equal", "max_weight": 1}),
            ("monthly_risk", {"weekly_risk": False}),
            ("vol_10pct", {"target_vol": 0.10}),
            ("vol_15pct", {"target_vol": 0.15}),
        ):
            run(cohort, name, f"{cohort}_risk", base, params)
        save()

    factor_id = json.loads((root / "factors-latest.json").read_text(encoding="utf-8"))[
        "dataset_id"
    ]
    report["factor_dataset_id"] = factor_id
    data = panel(store.load_dataset(factor_id))
    # Use only completed source months, never invent partial-month returns.
    end = min(args.end[:7], data[-1]["month"])
    fx = {}
    for b in store.load_dataset(ids["SPY"])["bars"]:
        fx[b["session"][:7]] = b["fx_close"]
    for start in ("1965-01", "2001-02", "2006-01", "2015-01"):
        for drag in (0.0, 0.01, 0.02):
            for model in FACTOR_MODELS:
                try:
                    result = replay_factors(
                        data,
                        model,
                        start,
                        end,
                        annual_drag=drag,
                        fx=fx if start >= "2001" else None,
                    )
                    result["engine_revision"] = revision
                    result["dataset_id"] = factor_id
                    run_id = store.save_run(result)
                    report["factor_rows"].append(
                        {k: v for k, v in result.items() if k != "monthly"}
                        | {"run_id": run_id}
                    )
                except (ValueError, KeyError) as exc:
                    report["failures"].append(
                        {
                            "factor": model,
                            "start": start,
                            "drag": drag,
                            "error": str(exc),
                        }
                    )
            save()
    print(
        f"{path}: ETF {len(report['rows'])}, factors {len(report['factor_rows'])}, failures {len(report['failures'])}",
        flush=True,
    )
    if report["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
