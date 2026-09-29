"""Independently check all saved ledgers, metrics, raw hashes, target caps and comparison scope."""

import argparse
import hashlib
import json
import math
from datetime import date
from pathlib import Path

import numpy as np
from invest_lab.adapters.etf_store import FileEtfStore
from invest_lab.domain.auto_replay import audit, performance
from invest_lab.domain.factor_research import audit_factors, curve_metrics, panel
from invest_lab.selection_cli import engine_revision


def metrics_independent(result):
    capital = result["config"]["initial_krw"]
    krw = np.array([capital] + [d["equity_krw"] for d in result["daily"]])
    usd = np.array(
        [capital / result["initial_fx"]] + [d["equity_usd"] for d in result["daily"]]
    )
    days = (
        date.fromisoformat(result["end"]) - date.fromisoformat(result["start"])
    ).days
    values = {
        "cagr_krw": (krw[-1] / krw[0]) ** (365.25 / days) - 1,
        "cagr_usd": (usd[-1] / usd[0]) ** (365.25 / days) - 1,
        "max_drawdown_krw": float(min(krw / np.maximum.accumulate(krw) - 1)),
        "max_drawdown_usd": float(min(usd / np.maximum.accumulate(usd) - 1)),
        "volatility_krw": float(np.std(krw[1:] / krw[:-1] - 1, ddof=1) * np.sqrt(252)),
        "volatility_usd": float(np.std(usd[1:] / usd[:-1] - 1, ddof=1) * np.sqrt(252)),
        "sessions": len(result["daily"]),
    }
    for k in ("cagr_krw", "max_drawdown_krw", "max_drawdown_usd"):
        if not math.isclose(values[k], result["metrics"][k], abs_tol=1e-12):
            raise ValueError("Independent curve calculation mismatch")
    return values


def monthly_logs(result):
    last = {}
    for row in result["daily"]:
        last[row["session"][:7]] = row["equity_krw"]
    # Exclude the final month even if complete: a conservative common comparison policy.
    previous, returns = result["config"]["initial_krw"], {}
    for m, equity in sorted(last.items()):
        if m != result["end"][:7]:
            returns[m] = math.log(equity / previous)
        previous = equity
    return returns


def relative_growth_interval(result, benchmark):
    a, b = monthly_logs(result), monthly_logs(benchmark)
    if a.keys() != b.keys():
        raise ValueError("Benchmark dates differ")
    differences = np.array([a[m] - b[m] for m in a])
    rng = np.random.default_rng(20260929)
    n, block = len(differences), 12
    draws = []
    for _ in range(1000):
        starts = rng.integers(0, n, size=math.ceil(n / block))
        indices = np.concatenate([(start + np.arange(block)) % n for start in starts])[
            :n
        ]
        draws.append(math.expm1(float(np.mean(differences[indices])) * 12))
    return {
        "months": n,
        "annual_relative_growth": math.expm1(float(np.mean(differences)) * 12),
        "block12_interval_95": [float(v) for v in np.quantile(draws, [0.025, 0.975])],
        "interpretation": "Exploratory dependent-block resampling, not adjusted for multiple models; not future success probability",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".local/selection-research")
    args = parser.parse_args()
    root = Path(args.root)
    revision = engine_revision()
    source = root / f"suite-{revision[:12]}.json"
    suite = json.loads(source.read_text(encoding="utf-8"))
    if (
        suite["failures"]
        or len(suite["rows"]) != 315
        or len(suite["factor_rows"]) != 108
    ):
        raise ValueError("Predeclared experiment matrix is incomplete")
    if suite["engine_revision"] != revision:
        raise ValueError("Engine changed after the experiment")
    store = FileEtfStore(root)
    datasets = {s: store.load_dataset(i) for s, i in suite["dataset_ids"].items()}
    factors = panel(store.load_dataset(suite["factor_dataset_id"]))
    raw = []
    for p in sorted((root / "raw").glob("*.json")):
        meta = json.loads(p.read_text(encoding="utf-8"))
        if (
            hashlib.sha256((root / "raw" / meta["sha256"]).read_bytes()).hexdigest()
            != meta["sha256"]
        ):
            raise ValueError("Raw source hash mismatch")
        raw.append({"url": meta["url"], "sha256": meta["sha256"]})
    max_error, supplements, bases = 0.0, [], {}
    for row in suite["rows"]:
        r = store.load_run(row["run_id"])
        if r["engine_revision"] != revision or r["metrics"] != performance(r):
            raise ValueError("Saved calculation/revision differs")
        if any(v != r["metrics"][k] for k, v in row["metrics"].items()):
            raise ValueError("Suite metrics differ from run")
        max_error = max(max_error, audit(r)["maximum_error_krw"])
        values = metrics_independent(r)
        policy = r["policy"]
        for detail in r.get("allocation_diagnostics", []):
            w = detail["weights"]
            if sum(w.values()) > 1 + 1e-12 or any(v < 0 for v in w.values()):
                raise ValueError("Target budget exceeded")
            if (
                policy.get("target_vol")
                and detail["volatility_after_defensive"] > policy["target_vol"] + 1e-10
            ):
                raise ValueError("Target volatility exceeded")
            if detail["equity_weight"] > policy["equity_cap"] + 1e-10:
                raise ValueError("Equity group cap exceeded")
            if any(
                w.get(s, 0) > policy["max_weight"] + 1e-10 for s in policy["candidates"]
            ):
                raise ValueError("Individual target cap exceeded")
        supplements.append(
            {
                "cohort": row["cohort"],
                "scenario": row["scenario"],
                "model": row["model"],
                "run_id": row["run_id"],
                **values,
            }
        )
        if row["scenario"] == "base":
            bases[(row["cohort"], row["model"])] = r
    comparisons = []
    for (cohort, model), result in bases.items():
        for bench in ("hold_spy", "hold_qqq", cohort + "_equal"):
            if model != bench:
                comparisons.append(
                    {
                        "cohort": cohort,
                        "model": model,
                        "benchmark": bench,
                        **relative_growth_interval(result, bases[(cohort, bench)]),
                    }
                )
    for row in suite["factor_rows"]:
        r = store.load_run(row["run_id"])
        audit_factors(r, factors)
        if r["metrics"]["usd"] != curve_metrics(
            [1.0] + [d["equity"] for d in r["monthly"]]
        ):
            raise ValueError("Factor curve mismatch")
        if r["metrics"] != row["metrics"] or r["engine_revision"] != revision:
            raise ValueError("Factor report mismatch")
        # Check each component, including the terminal month, without trusting saved balances.
        returns = {d["month"]: d["returns"] for d in factors}
        for d in r["monthly"]:
            usable = d["opening_equity"] - d["switch_cost"] - d["annual_drag_cost"]
            cash = (
                usable
                * (1 - sum(d["weights"].values()))
                * (1 + returns[d["month"]]["rf"])
            )
            for s, value in d["balances"].items():
                expected = (
                    usable * d["weights"].get(s, 0) * (1 + returns[d["month"]][s])
                )
                if not math.isclose(value, expected, abs_tol=1e-9):
                    raise ValueError("Factor component balance mismatch")
            if not math.isclose(cash, d["cash"], abs_tol=1e-9):
                raise ValueError("Factor cash mismatch")
    output = {
        "engine_revision": revision,
        "suite_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "etf_runs_verified": len(suite["rows"]),
        "factor_runs_verified": len(suite["factor_rows"]),
        "raw_sources_verified": len(raw),
        "raw": raw,
        "maximum_ledger_error_krw": max_error,
        "coverage": {
            s: {
                "sessions": len(d["bars"]),
                "start": d["bars"][0]["session"],
                "end": d["bars"][-1]["session"],
            }
            for s, d in datasets.items()
        },
        "supplements": supplements,
        "comparisons": comparisons,
        "performance_validated": False,
    }
    (root / f"audit-{revision[:12]}.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                k: v
                for k, v in output.items()
                if k not in {"raw", "coverage", "supplements", "comparisons"}
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
