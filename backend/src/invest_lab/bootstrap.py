import os
from pathlib import Path

from invest_lab.adapters.demo import DemoMarket, SystemClock
from invest_lab.adapters.sqlite import SQLiteStore
from invest_lab.application.service import Workspace

ROOT = Path(__file__).resolve().parents[3]


def build_workspace(db_path: Path | None = None) -> Workspace:
    path = db_path or Path(os.getenv("INVEST_DB_PATH", str(ROOT / ".local" / "workspace.db")))
    store = SQLiteStore(path)
    clock = SystemClock()
    return Workspace(DemoMarket(clock), store, store, clock)
