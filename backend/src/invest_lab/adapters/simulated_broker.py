"""Persistent synthetic broker used to exercise the same order lifecycle as KIS paper."""

import json
from datetime import datetime
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from invest_lab.adapters.runtime_store import encode
from invest_lab.domain.errors import TradingBlocked, UnknownOutcome
from invest_lab.domain.orders import decimal, execution_state


class SimulatedBroker:
    source = "synthetic_demo"

    def __init__(self, store, market, clock, initial=None):
        self.store, self.market, self.clock = store, market, clock
        self.timeout_after_accept = False
        with store.connect() as con:
            initial = initial or {"cash": "10000000", "positions": []}
            con.execute(
                "INSERT OR IGNORE INTO simulated_accounts VALUES(?,?)",
                (store.namespace, encode(initial)),
            )

    def quote(self, symbol):
        quote = self.market.quote(symbol)
        return {
            "symbol": symbol,
            "price": str(quote.price),
            "observed_at": quote.observed_at.isoformat(),
            "source": self.source,
            "tick_size": "1",
            "upper_limit": None,
            "lower_limit": None,
        }

    def account(self):
        with self.store.connect() as con:
            account = json.loads(
                con.execute(
                    "SELECT payload FROM simulated_accounts WHERE namespace=?",
                    (self.store.namespace,),
                ).fetchone()[0]
            )
            pending = [
                json.loads(row[0])
                for row in con.execute(
                    "SELECT payload FROM simulated_orders WHERE namespace=?",
                    (self.store.namespace,),
                )
            ]
        positions, total, cost = [], Decimal(0), Decimal(0)
        reserve_cash = sum(
            decimal(o["limit_price"] or o["execution_price"]) * o["remaining_quantity"]
            for o in pending
            if o["side"] == "buy"
        )
        for p in account["positions"]:
            q = self.quote(p["symbol"])
            value = decimal(q["price"]) * p["quantity"]
            basis = decimal(p["average_price"]) * p["quantity"]
            reserved = sum(
                o["remaining_quantity"]
                for o in pending
                if o["symbol"] == p["symbol"] and o["side"] == "sell"
            )
            positions.append(
                {
                    **p,
                    "name": p["symbol"],
                    "price": q["price"],
                    "value": str(value),
                    "profit": str(value - basis),
                    "sellable_quantity": max(0, p["quantity"] - reserved),
                }
            )
            total += value
            cost += basis
        cash = decimal(account["cash"])
        return {
            "cash": str(cash),
            "buying_power": str(max(Decimal(0), cash - reserve_cash)),
            "positions": positions,
            "holdings_value": str(total),
            "equity": str(cash + total),
            "profit": str(total - cost),
            "settlement_d1": str(cash),
            "settlement_d2": str(cash),
            "observed_at": self.clock.now().isoformat(),
            "source": self.source,
            "realized_profit": account.get("realized_profit", "0"),
        }

    def capacity(self, symbol, price, order_type):
        account = self.account()
        price = decimal(price or self.quote(symbol)["price"], positive=True)
        amount = decimal(account["buying_power"])
        return {"amount": str(amount), "quantity": int(amount / (price * Decimal("1.00015")))}

    def _fill(self, con, order, quantity, price):
        account = json.loads(
            con.execute(
                "SELECT payload FROM simulated_accounts WHERE namespace=?", (self.store.namespace,)
            ).fetchone()[0]
        )
        cash = decimal(account["cash"])
        positions = {p["symbol"]: p for p in account["positions"]}
        old = positions.get(
            order["symbol"], {"symbol": order["symbol"], "quantity": 0, "average_price": "0"}
        )
        amount = price * quantity
        fee = (amount * Decimal("0.00015")).quantize(Decimal("0.01"))
        if order["side"] == "buy":
            if amount + fee > cash:
                raise TradingBlocked("가용 현금이 부족합니다.")
            new_quantity = old["quantity"] + quantity
            average = (
                decimal(old["average_price"]) * old["quantity"] + amount + fee
            ) / new_quantity
            positions[order["symbol"]] = {
                "symbol": order["symbol"],
                "quantity": new_quantity,
                "average_price": str(average),
            }
            cash -= amount + fee
        else:
            if old["quantity"] < quantity:
                raise TradingBlocked("매도 수량이 부족합니다.")
            cash += amount - fee
            account["realized_profit"] = str(
                decimal(account.get("realized_profit", "0"))
                + amount
                - fee
                - decimal(old["average_price"]) * quantity
            )
            if old["quantity"] == quantity:
                positions.pop(order["symbol"])
            else:
                positions[order["symbol"]] = {**old, "quantity": old["quantity"] - quantity}
        account.update(cash=str(cash), positions=list(positions.values()))
        order["filled_quantity"] += quantity
        order["filled_amount"] = str(decimal(order["filled_amount"]) + amount)
        order["fee"] = str(decimal(order.get("fee", "0")) + fee)
        order["remaining_quantity"] -= quantity
        order["status"] = execution_state(
            order["quantity"], order["filled_quantity"], order["cancelled_quantity"]
        )
        con.execute(
            "UPDATE simulated_accounts SET payload=? WHERE namespace=?",
            (encode(account), self.store.namespace),
        )

    def place_order(self, payload):
        price = decimal(self.quote(payload["symbol"])["price"], positive=True)
        if payload["side"] == "buy":
            if (
                self.capacity(
                    payload["symbol"],
                    payload.get("limit_price") or str(price),
                    payload["order_type"],
                )["quantity"]
                < payload["quantity"]
            ):
                raise TradingBlocked("가용 현금이 부족합니다.")
        else:
            position = next(
                (p for p in self.account()["positions"] if p["symbol"] == payload["symbol"]), None
            )
            if not position or position["sellable_quantity"] < payload["quantity"]:
                raise TradingBlocked("매도 가능 수량이 부족합니다.")
        at = self.clock.now().isoformat()
        order = {
            **payload,
            "broker_order_id": str(uuid4()),
            "branch_id": "simulation",
            "trading_date": self.clock.now().astimezone(ZoneInfo("Asia/Seoul")).date().isoformat(),
            "ordered_at": at,
            "original_order_id": None,
            "quantity": payload["quantity"],
            "filled_quantity": 0,
            "filled_amount": "0",
            "cancelled_quantity": 0,
            "remaining_quantity": payload["quantity"],
            "status": "acknowledged",
            "execution_price": str(price),
            "fee": "0",
        }
        crosses = payload["order_type"] == "market" or (
            decimal(payload["limit_price"]) >= price
            if payload["side"] == "buy"
            else decimal(payload["limit_price"]) <= price
        )
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if crosses:
                self._fill(con, order, order["quantity"], price)
            con.execute(
                "INSERT INTO simulated_orders VALUES(?,?,?)",
                (self.store.namespace, order["broker_order_id"], encode(order)),
            )
        if self.timeout_after_accept:
            self.timeout_after_accept = False
            raise UnknownOutcome("접수 후 응답 유실 fixture")
        return {
            "broker_order_id": order["broker_order_id"],
            "branch_id": "simulation",
            "ordered_at": at,
        }

    def fill_order(self, broker_order_id, quantity):
        """Explicit deterministic partial fill; tests may control market evolution."""
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT payload FROM simulated_orders WHERE namespace=? AND id=?",
                (self.store.namespace, broker_order_id),
            ).fetchone()
            if not row:
                raise LookupError("주문 없음")
            order = json.loads(row[0])
            if type(quantity) is not int or not 0 < quantity <= order["remaining_quantity"]:
                raise ValueError("체결 수량 범위")
            self._fill(
                con, order, quantity, decimal(order["limit_price"] or order["execution_price"])
            )
            con.execute(
                "UPDATE simulated_orders SET payload=? WHERE namespace=? AND id=?",
                (encode(order), self.store.namespace, broker_order_id),
            )

    def cancel_order(self, order, quantity, price=None):
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT payload FROM simulated_orders WHERE namespace=? AND id=?",
                (self.store.namespace, order["broker_order_id"]),
            ).fetchone()
            if not row:
                raise LookupError("모의 주문 없음")
            current = json.loads(row[0])
            if type(quantity) is not int or not 0 < quantity <= current["remaining_quantity"]:
                raise TradingBlocked("주문 잔량을 확인하세요.")
            if price is None:
                current["cancelled_quantity"] += quantity
                current["remaining_quantity"] -= quantity
                current["status"] = execution_state(
                    current["quantity"], current["filled_quantity"], current["cancelled_quantity"]
                )
            else:
                if quantity != current["remaining_quantity"]:
                    raise TradingBlocked("가격 정정은 남은 수량 전체를 대상으로 합니다.")
                current["limit_price"] = str(decimal(price, positive=True))
            con.execute(
                "UPDATE simulated_orders SET payload=? WHERE namespace=? AND id=?",
                (encode(current), self.store.namespace, current["broker_order_id"]),
            )
            return {
                "broker_order_id": current["broker_order_id"],
                "branch_id": "simulation",
                "ordered_at": self.clock.now().isoformat(),
            }

    def orders(self, start, end):
        start, end = str(start).replace("-", "")[:8], str(end).replace("-", "")[:8]
        with self.store.connect() as con:
            rows = [
                json.loads(row[0])
                for row in con.execute(
                    "SELECT payload FROM simulated_orders WHERE namespace=?",
                    (self.store.namespace,),
                )
            ]
        for order in rows:
            order["trading_date"] = (
                datetime.fromisoformat(order["ordered_at"])
                .astimezone(ZoneInfo("Asia/Seoul"))
                .date()
                .isoformat()
            )
        return [o for o in rows if start <= o["trading_date"].replace("-", "") <= end]
