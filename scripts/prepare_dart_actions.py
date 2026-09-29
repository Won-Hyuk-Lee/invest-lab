"""Extract dated split/consolidation terms; retain evidence and unresolved cases.

This produces an exploratory action ledger, not a claim of complete PIT data.
KRX master changes corroborate ordinary splits; reductions need manual review.
"""

import gzip
import json
import re
from collections import defaultdict
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path

BASE = Path(".local/sell-engine-v3-20260929")


def read(path):
    with gzip.open(path, "rt", encoding="utf-8") as file:
        return json.load(file)


class Rows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "tr":
            self.row = []
        elif tag.lower() in ("td", "th", "te", "tu"):
            self.cell = []

    def handle_data(self, data):
        self.text.append(data)
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag.lower() in ("td", "th", "te", "tu") and self.cell is not None:
            if self.row is not None:
                self.row.append(" ".join(" ".join(self.cell).split()))
            self.cell = None
        if tag.lower() == "tr" and self.row:
            self.rows.append(self.row)
            self.row = None


def parse_date(text):
    found = re.search(r"(20\d\d)[년./-]\s*(\d{1,2})[월./-]\s*(\d{1,2})", text)
    return date(*map(int, found.groups())).isoformat() if found else None


def number(text):
    value = text.replace(",", "").strip()
    return float(value) if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) else None


def event_identity(action, basis):
    # Capital reduction and stock split can share both board and relisting dates.
    # Court plans can also have multiple reductions on the same board date.
    return (
        f"{action['symbol']}:{action['action_class']}:{action.get('phase', '')}:{basis}"
    )


def parse_split(payload):
    filing = payload["filing"]
    name = filing["report_nm"]
    if not any(word in name for word in ("주식분할결정", "주식병합결정", "감자결정")):
        return None
    if "자회사" in name:
        return None
    parser = Rows()
    parser.feed("\n".join(payload["documents"].values()))
    rows = parser.rows
    split = "감자" not in name
    ratio = legal = basis = board = None
    method, expected_shares = "", None
    evidence = []
    for cells in rows:
        key = re.sub(r"\s+", "", cells[0])
        text = " | ".join(cells)
        if split and key == "1주당가액(원)" and len(cells) == 3:
            before, after = number(cells[-2]), number(cells[-1])
            if before and after:
                ratio = before / after
                evidence.append(text)
        if not split and "감자비율" in key and "보통주식" in text:
            reduction = number(cells[-1])
            if reduction is not None:
                ratio = 1 - reduction / 100
                evidence.append(text)
        if key in ("신주의효력발생일", "신주효력발생일"):
            legal = parse_date(cells[-1])
            evidence.append(text)
        if not split and "감자방법" in key:
            method = cells[-1]
            evidence.append(text)
        if "보통주식(주)" in cells and len(cells) >= 3:
            expected_shares = number(cells[-1])
        if not split and "감자기준일" in key:
            record = parse_date(cells[-1])
            if record:
                legal = (date.fromisoformat(record) + timedelta(days=1)).isoformat()
                evidence.append(text)
        if key in ("신주권상장예정일", "신주상장예정일"):
            basis = parse_date(cells[-1])
            evidence.append(text)
        if "이사회결의일" in key:
            board = parse_date(cells[-1])
        if "기타투자판단" in key:
            evidence.append(text)
    if not split:
        if "병합" not in method or any(
            word in method for word in ("차등", "출자전환", "단,")
        ):
            return None
        matched = re.search(
            r"보통주(?:식)?\s*(?:각\s*)?(\d+(?:\.\d+)?)주.*?(\d+(?:\.\d+)?)주로", method
        )
        if matched:
            ratio = float(matched[2]) / float(matched[1])
        else:
            matched = re.search(r"(\d+)\s*[:대]\s*(\d+)", method)
            ratio = float(matched[2]) / float(matched[1]) if matched else None
    if not ratio or ratio == 1 or not legal or not basis or not board:
        return {
            "unresolved": True,
            "filing": filing,
            "evidence": evidence,
            "parsed": {"ratio": ratio, "legal": legal, "basis": basis, "board": board},
        }
    action = {
        "event_id": f"{filing['stock_code']}:{'split' if split else 'reduction'}:{board}",
        "symbol": filing["stock_code"],
        "type": "split",
        "ratio": ratio,
        "effective_date": legal,
        "price_basis_date": basis,
        "available_at": (
            date.fromisoformat(
                filing["rcept_dt"][:4]
                + "-"
                + filing["rcept_dt"][4:6]
                + "-"
                + filing["rcept_dt"][6:]
            )
            + timedelta(days=1)
        ).isoformat()
        + "T00:00:00+09:00",
        "ingested_at": payload["collected_at"],
        "source": "dart_original",
        "receipt_id": filing["rcept_no"],
        "source_url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="
        + filing["rcept_no"],
        "availability_policy": "disclosure_date_end",
        "evidence": evidence,
        "action_class": "stock_split_or_consolidation"
        if split
        else "capital_reduction",
        "expected_post_shares": expected_shares,
    }
    phase = re.search(r"제\s*(\d+)\s*차", method)
    action["phase"] = phase[1] if phase else ""
    action["event_id"] = event_identity(action, board)
    notes = " ".join(" ".join(parser.text).split())
    if (
        ratio < 1
        and "종가" in notes
        and ("단주" in notes or "단수주" in notes)
        and "현금" in notes
    ):
        action.update(
            fractional_policy="cash_at_close",
            fractional_price_date=basis,
            payment_date=None,
        )
    elif ratio < 1 and "매각" in notes and "처분대금" in notes and "현금" in notes:
        action.update(fractional_policy="sale_proceeds_unconfirmed", payment_date=None)
    elif ratio < 1 and re.search(r"단주.{0,80}무상\s*소각", notes):
        action.update(
            fractional_policy="cancel_without_compensation", payment_date=None
        )
    return action


def main():
    data = read(Path(".local/engine-review-20260929/data/dataset.json.gz"))
    symbols = {item["symbol"] for item in data["instruments"]}
    changes = json.loads((BASE / "krx-parval-changes.json").read_text(encoding="utf-8"))
    by_symbol = defaultdict(list)
    for row in changes:
        by_symbol[row["symbol"]].append(row)
    parsed, unresolved = [], []
    for path in sorted((BASE / "dart/originals").glob("*.json.gz")):
        action = parse_split(read(path))
        if not action:
            continue
        if action.get("unresolved"):
            unresolved.append(action)
        elif action["symbol"] in symbols:
            parsed.append(action)
    groups = defaultdict(list)
    for action in parsed:
        groups[action["event_id"]].append(action)
    accepted, review = [], []
    for versions in groups.values():
        latest = max(versions, key=lambda row: row["receipt_id"])
        corroborated = any(
            row["date"] == latest["price_basis_date"]
            and abs(
                float(row["previous_parval"]) / float(row["parval"]) - latest["ratio"]
            )
            < 1e-9
            for row in by_symbol[latest["symbol"]]
        )
        if latest.get("expected_post_shares"):
            master_path = Path(".local/backtest-20260929/krx/kospi_master") / (
                latest["price_basis_date"] + ".json.gz"
            )
            if master_path.exists():
                rows = read(master_path)["raw"]["OutBlock_1"]
                corroborated = corroborated or any(
                    row["ISU_SRT_CD"] == latest["symbol"]
                    and float(row["LIST_SHRS"]) == latest["expected_post_shares"]
                    for row in rows
                )
        # A reduction followed by a split can have an intermediate share count.
        # Confirm only explicitly disclosed tandem terms against the final KRX count.
        tandem = False
        if latest["action_class"] == "capital_reduction" and any(
            "감자 후" in text and "주식분할" in text for text in latest["evidence"]
        ):
            for other in groups.values():
                following = max(other, key=lambda row: row["receipt_id"])
                if (
                    following["symbol"] == latest["symbol"]
                    and following["action_class"] == "stock_split_or_consolidation"
                    and following["price_basis_date"] == latest["price_basis_date"]
                    and latest.get("expected_post_shares")
                    and following.get("expected_post_shares")
                    and abs(
                        latest["expected_post_shares"] * following["ratio"]
                        - following["expected_post_shares"]
                    )
                    < 1
                    and master_path.exists()
                ):
                    tandem = any(
                        row["ISU_SRT_CD"] == latest["symbol"]
                        and float(row["LIST_SHRS"]) == following["expected_post_shares"]
                        for row in rows
                    )
        # The ORIENTBIO reduction is separately verified against its DART original
        # and the dated KRX share count/relisting notice retained in evidence.
        manual = (
            latest["symbol"] == "002630"
            and latest["price_basis_date"] == "2026-09-22"
            and latest["ratio"] == 0.5
        )
        if latest["price_basis_date"] > data["sessions"][-1]:
            continue
        if not corroborated and not manual and not tandem:
            review.append({"reason": "needs_execution_corroboration", "action": latest})
            continue
        if latest["ratio"] < 1 and not latest.get("fractional_policy"):
            review.append({"reason": "fractional_terms_missing", "action": latest})
            # Replay fails if an actual odd lot needs unresolved settlement terms.
        for version in versions:
            # A new board date in a correction is not a second stock split.
            version["event_id"] = event_identity(latest, latest["price_basis_date"])
            version["verification"] = (
                "dart_tandem_terms_and_krx_final_shares"
                if tandem
                else "krx_parval_and_dart_terms"
                if corroborated
                else "manual_dart_and_krx_share_count"
            )
            accepted.append(version)
    data["actions"] = accepted
    data["name"] += " / exploratory DART split ledger"
    data["provenance"]["corporate_actions_verified"] = False
    data["provenance"]["action_coverage_note"] = (
        "Partial verified splits only; dividends, other reductions, mergers and delistings remain under review."
    )
    out = BASE / "data"
    out.mkdir(exist_ok=True)
    with gzip.open(out / "dataset.json.gz", "wt", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False)
    report = {
        "accepted_events": len({row["event_id"] for row in accepted}),
        "accepted_versions": len(accepted),
        "actions": accepted,
        "review": review,
        "unresolved": unresolved,
        "performance_validated": False,
    }
    (out / "action-review.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {key: report[key] for key in ("accepted_events", "accepted_versions")},
            ensure_ascii=False,
        )
    )
    print("review events", len(review), "unparsed reports", len(unresolved))


if __name__ == "__main__":
    main()
