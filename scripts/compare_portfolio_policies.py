"""Predeclared offline portfolio experiments; no broker, credentials or deployment."""

import argparse
import gzip
import hashlib
import inspect
import json
import time
from dataclasses import asdict
from pathlib import Path

from invest_lab.domain.replay_ledger_audit import audit_ledger
from invest_lab.domain.research import content_hash, validate_dataset
from invest_lab.domain.strategies import StrategyRules, replay, technical_features
from review_krx_backtest import summary, write


def experiments(base):
    result = {"baseline-5": base}
    for count in (5, 10, 20):
        result[f"risk-fixed-{count}"] = {
            **base,
            "max_positions": count,
            "max_open_risk_pct": 1.25,
        }
        if count != 5:
            result[f"risk-expanded-{count}"] = {
                **base,
                "max_positions": count,
                "max_open_risk_pct": count * 0.25,
            }
    ten = result["risk-fixed-10"]
    result["trail-10"] = {**ten, "exit_policy": "trail"}
    result["partial-trail-10"] = {**ten, "exit_policy": "partial_trail"}
    result["rotation-10"] = {**ten, "rotation_enabled": True}
    result["trail-rotation-10"] = {**result["trail-10"], "rotation_enabled": True}
    result["stop-width-15"] = {**ten, "max_initial_stop_pct": 15}
    result["stagnation-5"] = {**ten, "stagnation_sessions": 5}
    result["trail-extension-10"] = {
        **result["trail-10"],
        "holding_extension_sessions": 10,
    }
    result["rebalance-10"] = {**ten, "rebalance_risk": True}
    result["holding-5"] = {**ten, "max_holding_sessions": 5}
    result["holding-20"] = {**ten, "max_holding_sessions": 20}
    result["candidates-40"] = {**ten, "candidate_limit": 40}
    result["cost-double-10"] = {
        **ten,
        "commission_bps": base["commission_bps"] * 2,
        "sell_tax_bps": base["sell_tax_bps"] * 2,
        "slippage_bps": base["slippage_bps"] * 2,
    }
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset", default=".local/sell-engine-v3-20260929/data/dataset.json.gz"
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--only", nargs="*")
    parser.add_argument(
        "--feature-cache", help="Optional local JSON.gz indicator checkpoint"
    )
    args = parser.parse_args()
    out = Path(args.output)
    if out.exists():
        raise ValueError("Use a fresh output directory to preserve previous evidence.")
    out.mkdir(parents=True)
    with gzip.open(args.dataset, "rt", encoding="utf-8") as file:
        data = validate_dataset(json.load(file))
    old = Path(".local/engine-review-20260929/verified/corrected.json.gz")
    with gzip.open(old, "rt", encoding="utf-8") as file:
        base = json.load(file)["strategy"]
    plans = experiments(base)
    if args.only:
        plans = {name: plans[name] for name in args.only}
    source = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in Path("backend/src/invest_lab/domain").glob("*.py")
    }
    manifest = {
        "dataset_hash": content_hash(data),
        "code_hashes": source,
        "plans": plans,
        "start": "2025-01-02",
        "end": "2026-09-23",
        "performance_validated": False,
        "validation_note": "Exploratory price-only study; incomplete dividends/other actions and historical availability.",
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    cache, summaries = {}, {}
    feature_code = hashlib.sha256(
        (
            inspect.getsource(technical_features)
            + Path("backend/src/invest_lab/domain/indicators.py").read_text(
                encoding="utf-8"
            )
            + Path("backend/src/invest_lab/domain/research.py").read_text(
                encoding="utf-8"
            )
        ).encode()
    ).hexdigest()
    cache_path = Path(args.feature_cache) if args.feature_cache else None
    if cache_path and cache_path.exists():
        with gzip.open(cache_path, "rt", encoding="utf-8") as stream:
            saved = json.load(stream)
        if (
            saved["dataset_hash"] != manifest["dataset_hash"]
            or saved["feature_code_hash"] != feature_code
        ):
            raise ValueError(
                "Indicator checkpoint has different input or feature code."
            )
        cache = {
            "dataset_hash": saved["dataset_hash"],
            "values": {
                (day, symbol): (technical, financial)
                for day, symbol, technical, financial in saved["values"]
            },
        }
        print("LOADED indicator checkpoint", len(cache["values"]), flush=True)
    for name, values in plans.items():
        started = time.monotonic()
        print("START", name, flush=True)

        def progress(day, index, count, name=name):
            if index % 50 == 0:
                print(name, index, count, day, flush=True)

        try:
            result = replay(
                data,
                StrategyRules.from_dict(values),
                start_session=manifest["start"],
                end_session=manifest["end"],
                feature_cache=cache,
                on_session=progress,
            )
        finally:
            if cache_path:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                write(
                    cache_path,
                    {
                        "dataset_hash": manifest["dataset_hash"],
                        "feature_code_hash": feature_code,
                        "values": [
                            [day, symbol, *value]
                            for (day, symbol), value in cache.get("values", {}).items()
                        ],
                    },
                )
        audit = audit_ledger(data, result)
        write(out / (name + ".json.gz"), result)
        pairs = result["rotations"]
        summaries[name] = {
            **summary(result),
            "rules": asdict(StrategyRules.from_dict(values)),
            "audit": audit,
            "rotation_count": len(pairs),
            "rotation_completed": sum(p["status"] == "completed" for p in pairs),
            "rotation_failed_entry": sum(p["status"] == "entry_failed" for p in pairs),
            "mean_positions": sum(p["position_count"] for p in result["equity_curve"])
            / len(result["equity_curve"]),
            "elapsed_seconds": time.monotonic() - started,
            "result_hash": content_hash(result),
        }
        (out / "summary.json").write_text(
            json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            name,
            json.dumps(result["metrics"]),
            "seconds",
            summaries[name]["elapsed_seconds"],
            flush=True,
        )


if __name__ == "__main__":
    main()
