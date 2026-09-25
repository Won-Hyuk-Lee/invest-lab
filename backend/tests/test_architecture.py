import ast
from pathlib import Path


def test_inner_layers_do_not_import_adapters_or_frameworks():
    root = Path(__file__).parents[1] / "src" / "invest_lab"
    forbidden = {"fastapi", "sqlite3", "httpx", "uvicorn", "pydantic"}
    for layer in ("domain", "application", "ports"):
        for path in (root / layer).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    assert name.split(".")[0] not in forbidden, (path, name)
                    assert not name.startswith("invest_lab.adapters"), (path, name)
