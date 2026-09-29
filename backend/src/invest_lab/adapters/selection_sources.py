"""Public sources for wider ETF and academic fundamental-portfolio research."""

import csv
import io
import math
import re
import zipfile

from invest_lab.adapters.auto_sources import AutoModelSource
from invest_lab.domain.selection_models import GLOBAL, SECTORS, STYLES

UNIVERSE = tuple(sorted(set((*GLOBAL, *SECTORS, *STYLES, "SHY"))))


class SelectionSource(AutoModelSource):
    universe = UNIVERSE


def parse_french_monthly(data):
    """First monthly table only; preserve missing sentinels as null, never zero-fill."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1 or archive.getinfo(names[0]).file_size > 30_000_000:
            raise ValueError("Unexpected French archive")
        content = archive.read(names[0]).decode("utf-8-sig")
    lines = content.splitlines()
    header, rows = None, []
    for i, line in enumerate(lines):
        columns = next(csv.reader([line]))
        if columns and re.fullmatch(r"\d{6}", columns[0].strip()):
            if header is None:
                header = [c.strip() for c in next(csv.reader([lines[i - 1]]))[1:]]
                if not header or len(set(header)) != len(header):
                    raise ValueError("Invalid French monthly header")
            if len(columns) != len(header) + 1:
                raise ValueError("French row width mismatch")
            values = [float(v.strip()) for v in columns[1:]]
            if any(not math.isfinite(v) or (v < -100 and v != -999) for v in values):
                raise ValueError("Invalid French return")
            month = columns[0].strip()
            rows.append(
                {
                    "month": f"{month[:4]}-{month[4:]}",
                    "returns": dict(
                        zip(header, [None if v in {-99.99, -999} else v / 100 for v in values])
                    ),
                }
            )
        elif rows:
            break
    if not rows or [r["month"] for r in rows] != sorted({r["month"] for r in rows}):
        raise ValueError("Missing/duplicate French monthly observations")
    return rows


def collect_french(cache, series_urls):
    result = {}
    for name, url in series_urls.items():
        data, source = cache.get(url)
        result[name] = {
            "source_kind": "real",
            "kind": "published_value_weighted_monthly_portfolios",
            "rows": parse_french_monthly(data),
            "source": source,
            "point_in_time_verified": False,
            "limitations": [
                "Current revised academic return aggregates, not constituent-level filings.",
                "No observable internal turnover, taxes, market impact or executable share ledger.",
                "Fundamental sort is performed by the source, not reconstructed by this program.",
            ],
        }
    return result
