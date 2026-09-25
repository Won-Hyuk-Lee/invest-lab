from datetime import datetime, timezone

import pytest

from invest_lab.adapters.demo import DemoMarket
from invest_lab.adapters.sqlite import SQLiteStore
from invest_lab.application.service import Workspace


class FixedClock:
    def now(self):
        return datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def workspace(tmp_path):
    clock = FixedClock()
    store = SQLiteStore(tmp_path / "test.db")
    return Workspace(DemoMarket(clock), store, store, clock)
