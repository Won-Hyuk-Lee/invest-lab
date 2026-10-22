"""Reproduce the predeclared ETF comparison matrix from cached datasets, offline."""

import argparse
import json
from dataclasses import replace
from pathlib import Path

from invest_lab.adapters.etf_store import FileEtfStore
from invest_lab.application.etf_research import EtfResearchService
from invest_lab.domain.etf_research import EtfReplayConfig
from invest_lab.domain.etf_strategies import POLICIES
from invest_lab.etf_cli import engine_revision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".local/etf-research")
    parser.add_argument(
        "--end", required=True, help="Explicit common data cutoff for every policy"
    )
    args = parser.parse_args()
    root = Path(args.root)
    datasets = json.loads((root / "datasets-latest.json").read_text(encoding="utf-8"))
    store = FileEtfStore(root)
    service = EtfResearchService(store, engine_revision=engine_revision())
    base = EtfReplayConfig("2004-01-01", args.end)
    scenarios = {
        "base_krw_cash": base,
        "usd_cash_zero": replace(base, cash_currency="USD"),
        "double_cost_krw": replace(base, fee_bps=5, slippage_bps=10, fx_bps=20),
        "krw_cash_2pct": replace(base, krw_cash_rate=0.02),
        "usd_treasury_proxy": replace(
            base, cash_currency="USD", use_treasury_cash_rate=True
        ),
    }
    for start, end in [
        ("2004-01-01", "2012-12-31"),
        ("2013-01-01", "2019-12-31"),
        ("2020-01-01", "2023-12-31"),
        ("2024-01-01", args.end),
    ]:
        scenarios[f"period_{start[:4]}_{end[:4]}"] = replace(base, start=start, end=end)
    rows, failures = [], []
    for scenario, config in scenarios.items():
        for symbol, dataset_id in sorted(datasets.items()):
            for policy_id in POLICIES:
                try:
                    result = service.run(dataset_id, policy_id, config)
                    rows.append({"scenario": scenario, **result})
                except (ValueError, KeyError) as exc:
                    failures.append(
                        {
                            "scenario": scenario,
                            "symbol": symbol,
                            "policy": policy_id,
                            "error": str(exc),
                        }
                    )
            print(
                f"{scenario} {symbol}: {len(rows)} completed, {len(failures)} failed",
                flush=True,
            )
    # VXN begins in the supplied Cboe source in 2009. Use an identical later window.
    for symbol, dataset_id in sorted(datasets.items()):
        config = replace(base, start="2010-01-01")
        for policy, parameters in [
            ("buy_hold", {}),
            ("vix_recovery", {}),
            ("vix_recovery", {"implied_series": "VXN"}),
        ]:
            try:
                result = service.run(dataset_id, policy, config, parameters)
                rows.append(
                    {"scenario": "vxn_2010" if parameters else "vix_2010", **result}
                )
            except (ValueError, KeyError) as exc:
                failures.append(
                    {
                        "scenario": "vxn_comparison",
                        "symbol": symbol,
                        "policy": policy,
                        "error": str(exc),
                    }
                )
    report = {
        "engine_revision": engine_revision(),
        "datasets": datasets,
        "rows": rows,
        "failures": failures,
        "performance_validated": False,
        "note": "Predeclared fixed rules; historical scenario comparisons, not unseen validation.",
    }
    output = root / f"suite-{engine_revision()[:12]}.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"Report: {output}; {len(rows)} completed, {len(failures)} failed", flush=True
    )
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
