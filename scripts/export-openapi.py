"""Export the public schema using only a temporary local simulation workspace."""

import argparse
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from invest_lab.adapters.http import create_app
from invest_lab.adapters.runtime import build_runtime
from invest_lab.bootstrap import build_workspace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="invest-openapi-") as directory:
        root = Path(directory)
        # Explicit mode/path overrides prevent existing environment or user state use.
        with patch.dict(
            os.environ,
            {
                "INVEST_MODE": "local_simulation",
                "INVEST_STATE_DIR": str(root / "state"),
            },
        ):
            workspace = build_workspace(root / "workspace.db")
            runtime = build_runtime(
                workspace,
                root=root,
                mode="local_simulation",
                db_path=root / "runtime.db",
            )
            schema = create_app(workspace, runtime).openapi()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Exported public OpenAPI schema to {args.output}")


if __name__ == "__main__":
    main()
