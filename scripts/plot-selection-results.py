"""Export historical research curves (optional matplotlib dependency, no portfolio mutation)."""

import argparse
import json
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from invest_lab.adapters.etf_store import FileEtfStore
from invest_lab.selection_cli import engine_revision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".local/selection-research")
    parser.add_argument(
        "--output", default="docs/images/selection-research-2026-09-29.png"
    )
    args = parser.parse_args()
    root = Path(args.root)
    report = json.loads(
        (root / f"suite-{engine_revision()[:12]}.json").read_text(encoding="utf-8")
    )
    store = FileEtfStore(root)
    cohorts = {
        "sector": [
            ("hold_spy", "SPY hold"),
            ("hold_qqq", "QQQ hold"),
            ("sector_trend", "Sector + trend"),
            ("sector_risk", "Sector + risk sizing"),
        ],
        "global": [
            ("hold_spy", "SPY hold"),
            ("hold_qqq", "QQQ hold"),
            ("asset_rotation", "Original 4-asset model"),
            ("global_trend", "Expanded global + trend"),
        ],
        "style": [
            ("hold_spy", "SPY hold"),
            ("hold_qqq", "QQQ hold"),
            ("hold_qual", "QUAL hold"),
            ("style_rank", "Style rotation"),
        ],
    }
    plt.rcParams.update(
        {"font.size": 10, "axes.spines.top": False, "axes.spines.right": False}
    )
    fig, axes = plt.subplots(
        2, 3, figsize=(17, 9), gridspec_kw={"height_ratios": [1.25, 1]}
    )
    colors = ["#64748b", "#2563eb", "#d97706", "#059669"]
    for col, (cohort, models) in enumerate(cohorts.items()):
        for color, (name, label) in zip(colors, models):
            row = next(
                r
                for r in report["rows"]
                if r["cohort"] == cohort
                and r["scenario"] == "base"
                and r["model"] == name
            )
            run = store.load_run(row["run_id"])
            days = [date.fromisoformat(r["session"]) for r in run["daily"]]
            values = np.array([r["equity_krw"] for r in run["daily"]])
            peak = np.maximum.accumulate(np.r_[10_000_000, values])[1:]
            axes[0, col].plot(
                days, values / 1_000_000, label=label, color=color, lw=1.55
            )
            axes[1, col].plot(days, 100 * (values / peak - 1), color=color, lw=1.1)
        axes[0, col].set_title(
            f"{cohort.title()} | {row['start']} to {row['end']}",
            fontsize=11,
            fontweight="bold",
        )
        axes[0, col].set_yscale("log")
        axes[0, col].legend(loc="upper left", fontsize=9, frameon=False)
        axes[0, col].set_ylabel("KRW million (log scale)")
        axes[1, col].set_ylabel("Drawdown in KRW (%)")
        for ax in axes[:, col]:
            ax.grid(alpha=0.2)
    fig.suptitle(
        "Investment selection research | Historical results after assumed trading costs",
        fontsize=17,
        fontweight="bold",
        y=0.98,
    )
    fig.text(
        0.5,
        0.015,
        "Independent KRW 10 million per model. Different cohort periods; compare within each column. Pretax, revised data, no leverage. Not live performance.",
        ha="center",
        fontsize=10,
    )
    fig.tight_layout(rect=[0, 0.035, 1, 0.95])
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140)
    fig.savefig(path.with_suffix(".svg"))
    print(str(path))


if __name__ == "__main__":
    main()
