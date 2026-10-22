"""Read-only offline replay and comparison; no credentials or broker calls.

Preserved inputs are never replaced. Each output records code/input/result hashes.
Reusing a completed output is allowed only when its exact inputs still match.
"""

import argparse
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean

from invest_lab.domain.research import content_hash, coverage
from invest_lab.domain.strategies import StrategyRules, replay

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=6) as stream:
        json.dump(
            value,
            stream,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    temporary.replace(path)


def audit(result, data):
    if data["actions"]:
        raise ValueError(
            "This independent comparison audit requires the current no-action dataset"
        )
    cash = float(result["initial_cash"])
    positions = {}
    grouped = defaultdict(list)
    for trade in result["trades"]:
        grouped[trade["session"]].append(trade)
    largest_cash_error, largest_pnl_error = 0.0, 0.0
    counts = Counter()
    for point in result["equity_curve"]:
        for trade in grouped[point["session"]]:
            symbol, qty, price, fee = (
                trade[k] for k in ("symbol", "quantity", "price", "fee")
            )
            if trade["side"] == "buy":
                assert symbol not in positions
                positions[symbol] = (qty, price * qty + fee)
                cash -= price * qty + fee
            else:
                old_qty, cost = positions.pop(symbol)
                assert qty == old_qty
                pnl = price * qty - fee - cost
                largest_pnl_error = max(largest_pnl_error, abs(pnl - trade["profit"]))
                cash += price * qty - fee
        largest_cash_error = max(largest_cash_error, abs(cash - point["cash"]))
        assert (
            abs(
                point["equity"]
                - point["cash"]
                - point["holdings_value"]
                - point["receivable"]
            )
            < 1e-6
        )
        assert cash >= -1e-6
        counts[len(positions)] += 1
    assert largest_cash_error < 1e-6 and largest_pnl_error < 1e-6
    assert {s: p[0] for s, p in positions.items()} == {
        s: p["quantity"] for s, p in result["open_positions"].items()
    }
    return {
        "cash_max_error": largest_cash_error,
        "realized_pnl_max_error": largest_pnl_error,
        "holding_count_days": dict(sorted(counts.items())),
        "verified": True,
    }


def summary(result):
    exits = [t for t in result["trades"] if t["side"] == "sell"]
    gains = sum(max(t["profit"], 0) for t in exits)
    losses = -sum(min(t["profit"], 0) for t in exits)
    buys = [t for t in result["trades"] if t["side"] == "buy"]
    years, base = {}, result["initial_cash"]
    for year in sorted({p["session"][:4] for p in result["equity_curve"]}):
        curve = [p for p in result["equity_curve"] if p["session"].startswith(year)]
        years[year] = {
            "return": curve[-1]["equity"] / base - 1,
            "mean_exposure": mean(p["exposure"] for p in curve),
        }
        base = curve[-1]["equity"]
    return {
        **result["metrics"],
        "profit_factor": gains / losses if losses else None,
        "buy_count": len(buys),
        "mean_buy_notional": mean(t["quantity"] * t["price"] for t in buys)
        if buys
        else 0,
        "end_equity": result["equity_curve"][-1]["equity"],
        "years": years,
        "sizing_constraints": result.get("diagnostics", {}).get(
            "sizing_constraints", {}
        ),
        "funnel": result.get("diagnostics", {}).get("funnel", {}),
    }


def differences(before, after):
    def indexed(result):
        values = {}
        for trade in result["trades"]:
            key = (
                trade["session"],
                trade["symbol"],
                trade["side"],
                trade["signal_session"],
            )
            if key in values:
                raise ValueError("Ambiguous trade comparison key")
            values[key] = trade
        return values

    left, right = indexed(before), indexed(after)
    rows = []
    for key in sorted(left.keys() | right.keys()):
        old, new = left.get(key), right.get(key)
        fields = ("quantity", "price", "fee", "profit", "reason")
        if old and new and all(old.get(f) == new.get(f) for f in fields):
            continue
        rows.append(
            {
                "key": key,
                "status": "changed" if old and new else "added" if new else "removed",
                "before": old,
                "after": new,
            }
        )
    daily = [
        {
            "session": a["session"],
            "equity_delta": b["equity"] - a["equity"],
            "cash_delta": b["cash"] - a["cash"],
            "holdings_delta": b["holdings_value"] - a["holdings_value"],
        }
        for a, b in zip(before["equity_curve"], after["equity_curve"], strict=True)
    ]
    assert all(
        a["session"] == b["session"]
        for a, b in zip(before["equity_curve"], after["equity_curve"], strict=True)
    )
    return {
        "trade_changes": rows,
        "trade_change_counts": dict(Counter(r["status"] for r in rows)),
        "daily": daily,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--original", type=Path, default=ROOT / ".local/backtest-20260929/krx"
    )
    parser.add_argument(
        "--corrected-data",
        type=Path,
        default=ROOT / ".local/engine-review-20260929/data/dataset.json.gz",
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / ".local/engine-review-20260929/comparison"
    )
    args = parser.parse_args()
    baseline = read(args.original / "result_full_2025_to_latest.json.gz")
    source_files = [
        "backend/src/invest_lab/domain/strategies.py",
        "backend/src/invest_lab/domain/research.py",
        "scripts/research_krx_backtest.py",
        "scripts/review_krx_backtest.py",
    ]
    code_hashes = {
        p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in source_files
    }
    for relative, digest in code_hashes.items():
        snapshot = args.output / "source" / relative
        content = (ROOT / relative).read_bytes()
        if (
            snapshot.exists()
            and hashlib.sha256(snapshot.read_bytes()).hexdigest() != digest
        ):
            raise ValueError("Source snapshot differs; use a new --output directory")
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_bytes(content)
    rules = StrategyRules.from_dict(baseline["strategy"])
    results = {"baseline": baseline}
    report = {
        "scope": "exploratory_daily_research_not_runtime_validation",
        "code_hashes": code_hashes,
        "baseline_result_hash": content_hash(baseline),
        "runs": {},
        "comparisons": {},
    }
    for name, path in (
        ("engine_only", args.original / "dataset.json.gz"),
        ("corrected", args.corrected_data),
    ):
        data = read(path)
        signature = {
            "dataset_hash": content_hash(data),
            "code_hashes": code_hashes,
            "rules": baseline["strategy"],
            "start": baseline["equity_curve"][0]["session"],
            "end": baseline["equity_curve"][-1]["session"],
            "initial_cash": baseline["initial_cash"],
        }
        result_path, meta_path = (
            args.output / (name + ".json.gz"),
            args.output / (name + "-manifest.json.gz"),
        )
        if result_path.exists():
            meta = read(meta_path)
            if meta["inputs"] != signature:
                raise ValueError(
                    "Existing output uses different inputs; choose a new --output directory"
                )
            result = read(result_path)
            assert content_hash(result) == meta["result_hash"]
        else:

            def progress(session, done, total, run_name=name):
                if done % 25 == 0 or done == total:
                    print(
                        json.dumps(
                            {
                                "run": run_name,
                                "session": session,
                                "done": done,
                                "total": total,
                            }
                        ),
                        flush=True,
                    )

            print(json.dumps({"stage": "start", "run": name}), flush=True)
            result = replay(
                data,
                rules,
                initial_cash=signature["initial_cash"],
                start_session=signature["start"],
                end_session=signature["end"],
                record_decisions=True,
                on_session=progress,
            )
            checks = audit(result, data)
            write(result_path, result)
            write(
                meta_path,
                {
                    "inputs": signature,
                    "result_hash": content_hash(result),
                    "audit": checks,
                },
            )
        results[name] = result
        report["runs"][name] = {
            "summary": summary(result),
            "coverage": coverage(data),
            "audit": audit(result, data),
            "inputs": signature,
            "result_hash": content_hash(result),
        }
        if name == "engine_only":
            report["runs"]["baseline"] = {
                "summary": summary(baseline),
                "audit": audit(baseline, data),
            }
        print(
            json.dumps({"stage": "done", "run": name, "metrics": result["metrics"]}),
            flush=True,
        )
    for left, right in (("baseline", "engine_only"), ("engine_only", "corrected")):
        diff = differences(results[left], results[right])
        label = left + "-to-" + right
        write(args.output / (label + "-differences.json.gz"), diff)
        report["comparisons"][label] = {
            "trade_change_counts": diff["trade_change_counts"],
            "end_equity_delta": diff["daily"][-1]["equity_delta"],
        }
    write(args.output / "comparison.json.gz", report)
    printable = {
        "runs": {k: v["summary"] for k, v in report["runs"].items()},
        "comparisons": report["comparisons"],
    }
    (args.output / "summary.json").write_text(
        json.dumps(printable, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(printable, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
