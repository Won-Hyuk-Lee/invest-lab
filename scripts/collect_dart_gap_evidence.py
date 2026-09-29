"""Company-specific cross-check of filing coverage for residual price-unit breaks."""

import re
from datetime import datetime, timezone
from pathlib import Path

from collect_dart_actions import read, save
from dotenv import dotenv_values
from invest_lab.adapters.research_sources import DartSource, ResearchSourceError

BASE = Path(".local/sell-engine-v3-20260929")


def main():
    import json

    companies = {
        r["stock_code"]: r["corp_code"] for r in read(BASE / "dart/companies.json.gz")
    }
    gaps = json.loads(
        (BASE / "data/remaining-price-breaks.json").read_text(encoding="utf-8")
    )
    symbols = sorted({r["symbol"] for r in gaps})
    sessions = read(BASE / "data/dataset.json.gz")["sessions"] + [
        "2026-09-24",
        "2026-09-25",
        "2026-09-28",
        "2026-09-29",
        "2026-09-30",
    ]
    source = DartSource(dotenv_values(".env")["DART_API_KEY"])
    out = BASE / "dart/company-filings"
    out.mkdir(exist_ok=True)
    errors = []
    try:
        for symbol in symbols:
            if symbol not in companies:
                errors.append({"symbol": symbol, "reason": "company_code_unavailable"})
                continue
            path = out / (symbol + ".json.gz")
            if path.exists():
                body = read(path)
            else:
                body = source.filings(
                    companies[symbol], "2024-01-01", "2026-09-23", sessions
                )
                save(path, body)
            relevant = [
                r
                for r in body["records"]
                if re.search(
                    "주식분할|주식병합|감자|무상증자|회사분할|주식배당", r["report_nm"]
                )
            ]
            for row in relevant:
                target = BASE / "dart/originals" / (row["rcept_no"] + ".json.gz")
                if target.exists():
                    continue
                try:
                    documents = source.original(row["rcept_no"])
                    if not documents:
                        raise ResearchSourceError("dart: no_original_document")
                    save(
                        target,
                        {
                            "filing": row,
                            "collected_at": datetime.now(timezone.utc).isoformat(),
                            "documents": {
                                name: b.decode("utf-8") for name, b in documents.items()
                            },
                        },
                    )
                except (ResearchSourceError, UnicodeError) as exc:
                    errors.append(
                        {
                            "symbol": symbol,
                            "receipt": row["rcept_no"],
                            "error_type": type(exc).__name__,
                        }
                    )
            print(
                symbol,
                "filings",
                len(body["records"]),
                "action reports",
                len(relevant),
                flush=True,
            )
    finally:
        source.close()
        (out / "errors.json").write_text(json.dumps(errors, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
