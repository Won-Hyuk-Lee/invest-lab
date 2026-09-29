"""Standalone ETF research CLI. Does not load .env, runtime DBs, or a broker."""

import argparse
import hashlib
import json
from pathlib import Path

from invest_lab.adapters.etf_sources import PublicEtfSource
from invest_lab.adapters.etf_store import FileEtfStore
from invest_lab.application.etf_research import EtfResearchService
from invest_lab.domain.etf_research import EtfReplayConfig
from invest_lab.domain.etf_strategies import POLICIES, catalog


def engine_revision():
    root = Path(__file__).parent
    files = sorted(p for p in root.rglob("*.py") if "etf_" in p.name)
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".local/etf-research")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("catalog")
    collect = sub.add_parser("collect")
    collect.add_argument("--start", default="2002-01-01")
    collect.add_argument("--end", required=True)
    collect.add_argument("--symbols", nargs="+", default=["SPY", "QQQ"])
    run = sub.add_parser("run")
    run.add_argument("--dataset", required=True)
    run.add_argument("--policy", choices=[*POLICIES, "all"], default="all")
    run.add_argument("--start", default="2004-01-01")
    run.add_argument("--end", required=True)
    run.add_argument("--cash", choices=["KRW", "USD"], default="KRW")
    run.add_argument("--cost-multiplier", type=float, default=1)
    run.add_argument("--cash-rate", type=float, default=0)
    run.add_argument("--treasury-cash", action="store_true")
    run.add_argument("--strict-pit", action="store_true")
    run.add_argument("--parameters", default="{}", help="JSON policy parameter overrides")
    sub.add_parser("list")
    args = parser.parse_args()
    if args.command == "catalog":
        print(json.dumps(catalog(), ensure_ascii=False, indent=2))
        return
    store = FileEtfStore(args.root)
    service = EtfResearchService(store, PublicEtfSource(Path(args.root) / "raw"), engine_revision())
    if args.command == "collect":
        result = service.collect(args.symbols, args.start, args.end)
        # Each immutable dataset retains its own raw source hashes.
        (Path(args.root) / "datasets-latest.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
        print(json.dumps(result, indent=2))
    elif args.command == "list":
        print(json.dumps(store.list_runs(), ensure_ascii=False, indent=2))
    else:
        config = EtfReplayConfig(
            args.start,
            args.end,
            cash_currency=args.cash,
            fee_bps=2.5 * args.cost_multiplier,
            slippage_bps=5 * args.cost_multiplier,
            fx_bps=10 * args.cost_multiplier,
            krw_cash_rate=args.cash_rate,
            usd_cash_rate=args.cash_rate,
            use_treasury_cash_rate=args.treasury_cash,
            strict_pit=args.strict_pit,
        )
        for policy in POLICIES if args.policy == "all" else [args.policy]:
            result = service.run(args.dataset, policy, config, json.loads(args.parameters))
            print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
