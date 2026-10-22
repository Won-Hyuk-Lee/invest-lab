"""Offline result-quality gate. Exit 2 means findings need review, not a valid result."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path

from invest_lab.domain.replay_quality import audit_no_action_replay
from invest_lab.domain.research import content_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() in {args.dataset.resolve(), args.result.resolve()}:
        parser.error("감사 출력은 데이터·결과 입력과 다른 파일이어야 합니다.")
    with gzip.open(args.dataset, "rt", encoding="utf-8") as stream:
        data = json.load(stream)
    with gzip.open(args.result, "rt", encoding="utf-8") as stream:
        result = json.load(stream)
    report = audit_no_action_replay(data, result)
    root = Path(__file__).resolve().parents[1]
    report["evidence"] = {
        "dataset_hash": content_hash(data),
        "result_hash": content_hash(result),
        "code_hashes": {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in (
                "backend/src/invest_lab/domain/replay_quality.py",
                "backend/src/invest_lab/domain/research.py",
                "scripts/audit_krx_replay_quality.py",
            )
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "coverage"}, ensure_ascii=False
        )
    )
    return 2 if report["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
