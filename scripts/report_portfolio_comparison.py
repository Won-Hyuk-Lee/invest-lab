"""Summarize preserved trials and flag held price breaks without inferring actions."""

import argparse
import gzip
import json
import math
from collections import defaultdict
from pathlib import Path

from invest_lab.domain.research import decision_time, timestamp


def read(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def price_breaks(data):
    rows = defaultdict(dict)
    for bar in sorted(
        data["bars"],
        key=lambda b: (timestamp(b["available_at"]), timestamp(b["ingested_at"])),
    ):
        if bar["price_basis"] == "raw" and timestamp(
            bar["available_at"]
        ) <= decision_time(bar["session"]):
            rows[bar["symbol"]][bar["session"]] = bar
    actions = defaultdict(list)
    for action in data["actions"]:
        actions[action["symbol"]].append(action)
    breaks = []
    for symbol, history in rows.items():
        previous = None
        for day, row in sorted(history.items()):
            if (
                row.get("valuation_only")
                or not row.get("tradable", True)
                or not float(row["open"])
            ):
                continue
            if previous:
                visible = {}
                for action in sorted(
                    actions[symbol],
                    key=lambda a: (
                        timestamp(a["available_at"]),
                        timestamp(a["ingested_at"]),
                    ),
                ):
                    if timestamp(action["available_at"]) <= decision_time(day):
                        visible[action["event_id"]] = action
                factor = math.prod(
                    float(a["ratio"])
                    for a in visible.values()
                    if a["type"] == "split"
                    and previous["session"]
                    < a.get("price_basis_date", a["effective_date"])
                    <= day
                )
                ratio = float(row["open"]) / float(previous["close"]) * factor
                if abs(ratio - 1) > 0.4:
                    breaks.append(
                        {
                            "symbol": symbol,
                            "session": day,
                            "previous_session": previous["session"],
                            "normalized_open_ratio": ratio,
                            "known_share_factor": factor,
                        }
                    )
            previous = row
    return breaks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    parser.add_argument(
        "--dataset", default=".local/sell-engine-v3-20260929/data/dataset.json.gz"
    )
    args = parser.parse_args()
    root = Path(args.run)
    data = read(args.dataset)
    gaps = price_breaks(data)
    summaries = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    diagnostics = {}
    for name, metrics in summaries.items():
        result = read(root / (name + ".json.gz"))
        # A position interval ends only when the current lifecycle sells its remaining shares.
        # Derive the end of each interval from the next buy or terminal open position;
        # the last sale in that interval closes it unless the final position remains open.
        by_symbol = defaultdict(list)
        for trade in result["trades"]:
            by_symbol[trade["symbol"]].append(trade)
        intervals = []
        for symbol, history in by_symbol.items():
            opened, closed = None, None
            for trade in history:
                if trade["side"] == "buy":
                    if opened:
                        intervals.append((symbol, opened, closed))
                    opened, closed = trade["session"], None
                else:
                    closed = trade["session"]
            if opened:
                intervals.append(
                    (
                        symbol,
                        opened,
                        None if symbol in result["open_positions"] else closed,
                    )
                )
        affected = [
            gap
            for gap in gaps
            if any(
                s == gap["symbol"] and start < gap["session"] <= (end or "9999-12-31")
                for s, start, end in intervals
            )
        ]
        end = result["equity_curve"][-1]
        diagnostics[name] = {
            "held_price_breaks": affected,
            "terminal_untradable_mark": end.get("untradable_mark_value"),
            "terminal_cash": end["cash"],
            "terminal_receivable": end["receivable"],
            "open_positions": {
                s: {k: p.get(k) for k in ("quantity", "entry_session", "stop_price")}
                for s, p in result["open_positions"].items()
            },
            "entitlements": result.get("corporate_action_entitlements", []),
            "rotation_statuses": {
                status: sum(p["status"] == status for p in result["rotations"])
                for status in {p["status"] for p in result["rotations"]}
            },
            "performance_validated": False,
        }
        print(
            name,
            f"return={metrics['return'] * 100:.6f}%",
            "held_breaks=" + str(len(affected)),
            flush=True,
        )
    (root / "data-quality-diagnostics.json").write_text(
        json.dumps(
            {
                "all_price_breaks": gaps,
                "trials": diagnostics,
                "note": "Price breaks are review flags, not proof of an action. No inferred ratio or outcome-based exclusion.",
                "performance_validated": False,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
