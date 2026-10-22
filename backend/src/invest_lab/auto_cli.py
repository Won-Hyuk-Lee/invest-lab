"""Separate automatic-model research entry point; no account credentials or order routing."""

import argparse
import hashlib
import json
from pathlib import Path

from invest_lab.adapters.auto_sources import UNIVERSE, AutoModelSource
from invest_lab.adapters.etf_store import FileEtfStore
from invest_lab.application.auto_research import AutoResearchService
from invest_lab.domain.auto_models import BLENDS, MODELS, catalog
from invest_lab.domain.auto_replay import AutoConfig


def engine_revision():
    root = Path(__file__).parent
    files = sorted(p for p in root.rglob("*.py") if p.name.startswith(("auto_", "etf_")))
    digest = hashlib.sha256()
    for p in files:
        digest.update(p.relative_to(root).as_posix().encode())
        digest.update(p.read_text(encoding="utf-8").encode())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".local/auto-models")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("catalog")
    collect = sub.add_parser("collect")
    collect.add_argument("--start", default="2000-01-04")
    collect.add_argument("--end", required=True)
    run = sub.add_parser("run")
    run.add_argument("--model", choices=[*MODELS, *BLENDS, "all"], default="all")
    run.add_argument("--symbols", nargs="+", choices=list(UNIVERSE), default=list(UNIVERSE))
    run.add_argument("--start", default="2006-01-01")
    run.add_argument("--end", required=True)
    run.add_argument("--cash", choices=["KRW", "USD"], default="USD")
    run.add_argument("--cost-multiplier", type=float, default=1)
    run.add_argument("--extra-delay", type=int, default=0)
    run.add_argument("--cash-rate", type=float, default=0)
    run.add_argument("--strict-pit", action="store_true")
    run.add_argument("--parameters", default="{}")
    args = parser.parse_args()
    if args.command == "catalog":
        print(json.dumps(catalog(), ensure_ascii=False, indent=2))
        return
    root = Path(args.root)
    store = FileEtfStore(root)
    service = AutoResearchService(store, AutoModelSource(root / "raw"), engine_revision())
    if args.command == "collect":
        ids = service.collect(list(UNIVERSE), args.start, args.end)
        (root / "datasets-latest.json").write_text(json.dumps(ids, indent=2), encoding="utf-8")
        print(json.dumps(ids, indent=2))
    else:
        ids = json.loads((root / "datasets-latest.json").read_text(encoding="utf-8"))
        ids = {s: ids[s] for s in args.symbols}
        config = AutoConfig(
            args.start,
            args.end,
            cash_currency=args.cash,
            fee_bps=2.5 * args.cost_multiplier,
            slippage_bps=5 * args.cost_multiplier,
            fx_bps=10 * args.cost_multiplier,
            extra_delay=args.extra_delay,
            usd_cash_rate=args.cash_rate,
            krw_cash_rate=args.cash_rate,
            strict_pit=args.strict_pit,
        )
        for model in [*MODELS, *BLENDS] if args.model == "all" else [args.model]:
            result = service.run(ids, model, config, json.loads(args.parameters))
            print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
