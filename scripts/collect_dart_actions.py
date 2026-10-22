"""Cache public KOSPI filing indices and receipt-addressed corporate-action originals.

Keys stay in .env, never request URLs or manifests. Downloading a decision does
not assert that it was executed: dates, ratios and corrections require review.
"""

import argparse
import gzip
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))
from invest_lab.adapters.research_sources import (
    DartSource,
    ResearchSourceError,
)

ACTION = re.compile(
    "감자|주식분할|주식병합|무상증자|주식배당|현금.*배당|배당.*결정|회사분할|회사합병"
)


def save(path, value):
    with gzip.open(path, "wt", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False)


def read(path):
    with gzip.open(path, "rt", encoding="utf-8") as file:
        return json.load(file)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-10-01")
    parser.add_argument("--end", default="2026-09-23")
    parser.add_argument("--output", default=".local/sell-engine-v3-20260929/dart")
    parser.add_argument("--originals", action="store_true")
    parser.add_argument("--include-dividends", action="store_true")
    args = parser.parse_args()
    out = Path(args.output)
    pages, originals = out / "filing-pages", out / "originals"
    pages.mkdir(parents=True, exist_ok=True)
    originals.mkdir(parents=True, exist_ok=True)
    source = DartSource(
        dotenv_values(Path(__file__).resolve().parents[1] / ".env")["DART_API_KEY"]
    )
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    found = {}
    try:
        while start <= end:
            next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
            stop = min(end, next_month - timedelta(days=1))
            for kind in ("B", "I"):
                page = 1
                while True:
                    path = pages / f"{start}-{stop}-{kind}-{page}.json.gz"
                    if path.exists():
                        body = read(path)["raw"]
                    else:
                        body = source._json(
                            "list",
                            {
                                "bgn_de": start.strftime("%Y%m%d"),
                                "end_de": stop.strftime("%Y%m%d"),
                                "corp_cls": "Y",
                                "pblntf_ty": kind,
                                "last_reprt_at": "N",
                                "page_no": page,
                                "page_count": 100,
                            },
                        )
                        save(
                            path,
                            {
                                "collected_at": datetime.now(timezone.utc).isoformat(),
                                "raw": body,
                            },
                        )
                        time.sleep(0.25)
                    for row in body["list"]:
                        if ACTION.search(row["report_nm"]):
                            found[row["rcept_no"]] = row
                    if not body["list"] or page >= int(body.get("total_page", page)):
                        break
                    page += 1
            print(f"{stop}: matching receipts={len(found)}", flush=True)
            start = next_month
        rows = sorted(found.values(), key=lambda row: row["rcept_no"])
        save(out / "corporate-action-filings.json.gz", rows)
        if args.originals:
            for index, row in enumerate(rows):
                if "배당" in row["report_nm"] and not args.include_dividends:
                    continue
                path = originals / (row["rcept_no"] + ".json.gz")
                if not path.exists():
                    try:
                        docs = source.original(row["rcept_no"])
                        save(
                            path,
                            {
                                "filing": row,
                                "collected_at": datetime.now(timezone.utc).isoformat(),
                                "documents": {
                                    name: body.decode("utf-8")
                                    for name, body in docs.items()
                                },
                            },
                        )
                    except (ResearchSourceError, UnicodeError) as exc:
                        print(
                            f"{row['rcept_no']}: unavailable ({type(exc).__name__})",
                            flush=True,
                        )
                    time.sleep(0.25)
                if index % 25 == 0:
                    print(f"originals: {index + 1}/{len(rows)}", flush=True)
        print(f"done: {len(rows)} matching filing versions", flush=True)
    finally:
        source.close()


if __name__ == "__main__":
    main()
