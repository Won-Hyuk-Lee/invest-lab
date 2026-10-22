"""Immutable, content-addressed ETF datasets and separate reproducible run artifacts."""

import gzip
import hashlib
import json
import re
from pathlib import Path


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def fingerprint(value):
    return hashlib.sha256(encoded(value)).hexdigest()


class FileEtfStore:
    def __init__(self, root):
        self.root = Path(root)
        for kind in ("datasets", "runs"):
            (self.root / kind).mkdir(parents=True, exist_ok=True)

    def _save(self, kind, value):
        content = encoded(value)
        digest = hashlib.sha256(content).hexdigest()
        path = self.root / kind / f"{digest}.json.gz"
        if not path.exists():
            # Exclusive creation prevents a second invocation from replacing an artifact.
            with path.open("xb") as file:
                file.write(gzip.compress(content, mtime=0))
        elif self._load(kind, digest) != value:
            raise ValueError("ETF artifact content mismatch")
        return digest

    def _load(self, kind, identifier):
        if not re.fullmatch(r"[0-9a-f]{64}", identifier):
            raise ValueError("Invalid ETF artifact ID")
        content = gzip.decompress((self.root / kind / f"{identifier}.json.gz").read_bytes())
        if hashlib.sha256(content).hexdigest() != identifier:
            raise ValueError("ETF artifact integrity failure")
        return json.loads(content)

    def save_dataset(self, dataset):
        return self._save("datasets", dataset)

    def load_dataset(self, identifier):
        return self._load("datasets", identifier)

    def save_run(self, result):
        return self._save("runs", result)

    def load_run(self, identifier):
        return self._load("runs", identifier)

    def list_runs(self):
        results = []
        for path in sorted((self.root / "runs").glob("*.json.gz")):
            identifier = path.name.removesuffix(".json.gz")
            run = self.load_run(identifier)
            results.append(
                {
                    "id": identifier,
                    "symbol": run["symbol"],
                    "policy": run["policy"]["id"],
                    "config": run["config"],
                    "metrics": run["metrics"],
                    "audit": run["audit"],
                }
            )
        return results
