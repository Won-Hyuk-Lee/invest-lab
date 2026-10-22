"""Collect/replay independent selection research without brokerage credentials."""

import argparse
import hashlib
import json
from pathlib import Path

from invest_lab.adapters.etf_store import FileEtfStore
from invest_lab.adapters.selection_sources import UNIVERSE, SelectionSource, collect_french
from invest_lab.application.selection_research import SelectionResearchService
from invest_lab.domain.auto_replay import AutoConfig
from invest_lab.domain.selection_models import GLOBAL, SECTORS, STYLES, catalog

FRENCH_URLS = {
    "profitability": "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/Portfolios_Formed_on_OP_CSV.zip",
    "value": "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/Portfolios_Formed_on_BE-ME_CSV.zip",
    "market": "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_Factors_CSV.zip",
}
COHORTS = {
    "sector": tuple(sorted(set((*SECTORS, "SPY", "QQQ")))),
    "global": tuple(sorted(set((*GLOBAL, "SHY")))),
    "style": tuple(sorted(set((*STYLES, "SPY", "QQQ")))),
}


def engine_revision():
    root = Path(__file__).parent
    digest = hashlib.sha256()
    files = [
        p
        for p in root.rglob("*.py")
        if p.name.startswith(("selection_", "auto_", "etf_", "factor_"))
    ]
    for path in sorted(files):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_text(encoding="utf-8").encode())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".local/selection-research")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("catalog")
    collect = sub.add_parser("collect")
    collect.add_argument("--start", default="2000-01-04")
    collect.add_argument("--end", required=True)
    run = sub.add_parser("run")
    run.add_argument("--model", required=True)
    run.add_argument("--cohort", choices=list(COHORTS), required=True)
    run.add_argument("--start", required=True)
    run.add_argument("--end", required=True)
    run.add_argument("--cash", choices=["USD", "KRW"], default="USD")
    run.add_argument("--cost-multiplier", type=float, default=1)
    run.add_argument("--extra-delay", type=int, default=0)
    args = parser.parse_args()
    if args.command == "catalog":
        print(json.dumps(catalog(), ensure_ascii=False, indent=2))
        return
    root = Path(args.root)
    store, source = FileEtfStore(root), SelectionSource(root / "raw")
    service = SelectionResearchService(store, source, engine_revision())
    if args.command == "collect":
        # Persist each completed symbol, so a single source outage does not lose collection work.
        ids = {}
        for symbol in UNIVERSE:
            ids.update(service.collect((symbol,), args.start, args.end))
            data = store.load_dataset(ids[symbol])
            print(
                f"{symbol}: {len(data['bars'])} sessions "
                f"{data['bars'][0]['session']}..{data['bars'][-1]['session']}",
                flush=True,
            )
        (root / "datasets-latest.json").write_text(json.dumps(ids, indent=2), encoding="utf-8")
        french = collect_french(source.cache, FRENCH_URLS)
        factor_id = store.save_dataset(french)
        (root / "factors-latest.json").write_text(
            json.dumps({"dataset_id": factor_id}), encoding="utf-8"
        )
        print(f"French dataset: {factor_id}", flush=True)
    else:
        ids = json.loads((root / "datasets-latest.json").read_text(encoding="utf-8"))
        config = AutoConfig(
            args.start,
            args.end,
            cash_currency=args.cash,
            fee_bps=2.5 * args.cost_multiplier,
            slippage_bps=5 * args.cost_multiplier,
            fx_bps=10 * args.cost_multiplier,
            extra_delay=args.extra_delay,
        )
        result = service.run({s: ids[s] for s in COHORTS[args.cohort]}, args.model, config)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
