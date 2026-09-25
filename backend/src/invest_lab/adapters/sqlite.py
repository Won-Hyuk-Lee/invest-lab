import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from invest_lab.domain.models import Strategy
from invest_lab.domain.trading import plan_purchase


class VersionConflict(Exception):
    pass


class SQLiteStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS strategies (
                    version INTEGER PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY CHECK (id = 1), cash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY, quantity INTEGER NOT NULL,
                    average_price TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    request_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
                    quantity INTEGER NOT NULL, price TEXT NOT NULL, fee TEXT NOT NULL,
                    at TEXT NOT NULL, source TEXT NOT NULL
                );
            """)
            connection.execute("INSERT OR IGNORE INTO account VALUES (1, '10000000')")
            connection.execute(
                "INSERT OR IGNORE INTO strategies VALUES (?, ?)",
                (1, json.dumps(asdict(Strategy()), ensure_ascii=False)),
            )

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def latest(self) -> Strategy:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM strategies ORDER BY version DESC LIMIT 1"
            ).fetchone()
            return Strategy(**json.loads(row[0]))

    def versions(self) -> list[Strategy]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM strategies ORDER BY version DESC"
            ).fetchall()
            return [Strategy(**json.loads(row[0])) for row in rows]

    def save(self, strategy: Strategy, expected_version: int) -> Strategy:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute("SELECT MAX(version) FROM strategies").fetchone()[0]
            if latest != expected_version:
                raise VersionConflict("다른 변경이 저장됐습니다. 새로고침한 뒤 다시 시도하세요.")
            saved = replace(strategy, version=latest + 1)
            connection.execute(
                "INSERT INTO strategies VALUES (?, ?)",
                (saved.version, json.dumps(asdict(saved), ensure_ascii=False)),
            )
            return saved

    def snapshot(self) -> dict:
        with self.connect() as connection:
            connection.execute("BEGIN")
            cash = connection.execute("SELECT cash FROM account WHERE id=1").fetchone()[0]
            positions = connection.execute("SELECT * FROM positions ORDER BY symbol").fetchall()
            orders = connection.execute(
                "SELECT * FROM orders ORDER BY at DESC LIMIT 100"
            ).fetchall()
            return {
                "cash": cash,
                "initial_cash": "10000000",
                "positions": [dict(row) for row in positions],
                "orders": [dict(row) for row in orders],
            }

    def buy(
        self,
        symbol: str,
        quantity: int,
        price: str,
        request_id: str,
        max_position_pct: float,
        at: datetime,
        source: str,
        marks: dict[str, str],
    ) -> dict:
        unit = Decimal(price)
        if not unit.is_finite() or unit <= 0 or quantity <= 0:
            raise ValueError("가격과 수량을 확인하세요.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT * FROM orders WHERE request_id=?", (request_id,)
            ).fetchone()
            if previous:
                if previous["symbol"] != symbol or previous["quantity"] != quantity:
                    raise ValueError("같은 주문 식별자를 다른 주문에 사용할 수 없습니다.")
                return dict(previous)
            cash = Decimal(connection.execute("SELECT cash FROM account WHERE id=1").fetchone()[0])
            positions = connection.execute("SELECT * FROM positions").fetchall()
            current = next((p for p in positions if p["symbol"] == symbol), None)
            old_qty = current["quantity"] if current else 0
            old_average = Decimal(current["average_price"]) if current else Decimal(0)
            equity = cash + sum(Decimal(marks[p["symbol"]]) * p["quantity"] for p in positions)
            purchase = plan_purchase(
                cash=cash,
                equity=equity,
                old_quantity=old_qty,
                old_average=old_average,
                quantity=quantity,
                price=unit,
                max_position_pct=max_position_pct,
            )
            connection.execute("UPDATE account SET cash=? WHERE id=1", (str(purchase.cash_after),))
            connection.execute(
                "INSERT INTO positions VALUES (?, ?, ?) ON CONFLICT(symbol) "
                "DO UPDATE SET quantity=excluded.quantity, "
                "average_price=excluded.average_price",
                (symbol, purchase.quantity_after, str(purchase.average_price_after)),
            )
            order = {
                "request_id": request_id,
                "symbol": symbol,
                "quantity": quantity,
                "price": price,
                "fee": str(purchase.fee),
                "at": at.isoformat(),
                "source": source,
            }
            connection.execute(
                "INSERT INTO orders VALUES (:request_id, :symbol, :quantity, "
                ":price, :fee, :at, :source)",
                order,
            )
            return order
