from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from invest_lab.domain.errors import TradingBlocked
from invest_lab.domain.order_chain import reconcile_order_chain
from invest_lab.domain.orders import ACTIVE, TERMINAL, OrderRequest, decimal, valid_price


class ExecutionService:
    def __init__(
        self,
        repository,
        broker,
        clock,
        *,
        instruments,
        session_check=None,
        strategy=None,
        preflight=None,
        realtime_quote=None,
    ):
        self.repo, self.broker, self.clock = repository, broker, clock
        self.instruments, self.session_check, self.strategy = instruments, session_check, strategy
        self.preflight = preflight
        self.realtime_quote = realtime_quote

    def at(self):
        return self.clock.now().isoformat()

    def register(self, payload, *, decision_context=None):
        request = OrderRequest(**payload)
        previous = self.repo.find_intent(request.client_request_id)
        if previous:
            return self.repo.create_intent(request.payload(), self.at(), {})
        if request.symbol not in {i.symbol for i in self.instruments()}:
            raise LookupError("종목 마스터에서 확인되지 않은 종목입니다.")
        version = self.strategy() if self.strategy else None
        if (
            request.strategy_version is not None
            and version
            and request.strategy_version != version.version
        ):
            raise TradingBlocked("전략 버전이 변경되었습니다. 주문 근거를 다시 확인하세요.")
        control = self.repo.document("control") or {}
        context = {
            "strategy_version": version.version if version else None,
            "deployment": self.repo.document("deployment"),
            "risk_policy": control.get(
                "risk_policy", {"max_position_pct": 20, "max_invested_pct": 50}
            ),
            "created_at": self.at(),
            "origin": "manual",
            "valid_session": self.clock.now().astimezone(ZoneInfo("Asia/Seoul")).date().isoformat(),
        }
        if decision_context:
            context.update(decision_context)
        return self.repo.create_intent(request.payload(), self.at(), context)

    def _fresh(self, value, label, maximum=300):
        try:
            at = datetime.fromisoformat(value)
            age = (self.clock.now() - at).total_seconds()
        except (ValueError, TypeError):
            raise TradingBlocked(f"{label} 기준 시각이 유효하지 않습니다.") from None
        if not 0 <= age <= maximum:
            raise TradingBlocked(f"{label}가 오래되었거나 미래 시각입니다.")
        return at

    def _monetary_snapshot(self, account):
        return {
            "namespace": self.repo.namespace,
            "account": account,
            "fills": {
                order["id"]: {
                    "side": order["side"],
                    "quantity": order["filled_quantity"],
                    "amount": order["filled_amount"],
                    "fee": order.get("fee"),
                }
                for order in self.repo.all_orders()
            },
        }

    def _reconcile_money(self, account, *, unresolved, position_uncertain=False):
        """Compare economic cash with fills from the same durable account checkpoint.

        The UI account cache is not a checkpoint: it can be refreshed independently.
        An unexplained difference keeps its old anchor until explicit investigation.
        """
        previous = self.repo.document("monetary_ledger")
        current = self._monetary_snapshot(account)
        cash = decimal(account["equity"]) - decimal(account["holdings_value"])
        result = {
            "status": "baseline",
            "cumulative_status": "verified",
            "previous_observed_at": None,
            "observed_at": account.get("observed_at"),
            "economic_cash": str(cash),
            "expected_economic_cash": None,
            "difference": None,
            "fill_cash_delta": "0",
            "fee_delta": None,
            "tolerance": "1",
            "reasons": [],
        }
        if previous is None:
            if unresolved or position_uncertain:
                result.update(status="unverified", cumulative_status="unverified")
                result["reasons"].append("미확정 주문이 있어 금액 대조 기준을 정할 수 없습니다.")
                return result
            current["cumulative_status"] = "verified"
            self.repo.put_document("monetary_ledger", current)
            return result
        result["cumulative_status"] = previous.get("cumulative_status", "unverified")
        result["previous_observed_at"] = previous["account"].get("observed_at")
        if previous.get("namespace") != self.repo.namespace:
            result.update(status="unverified", cumulative_status="unverified")
            result["reasons"].append("금액 대조 기준의 계좌/운용 모드가 다릅니다.")
            return result
        if previous.get("mismatch_reasons"):
            result.update(
                status="mismatch",
                cumulative_status="mismatch",
                reasons=previous["mismatch_reasons"],
            )
            return result

        def mismatch():
            result.update(status="mismatch", cumulative_status="mismatch")
            previous["mismatch_reasons"] = result["reasons"]
            previous["cumulative_status"] = "mismatch"
            self.repo.put_document("monetary_ledger", previous)
            return result

        if unresolved or position_uncertain:
            result.update(status="unverified", cumulative_status="unverified")
            result["reasons"].append(
                "미확정 주문 또는 계좌/체결 조회 시점 차이를 먼저 확인해야 합니다."
            )
            return result
        old_cash = decimal(previous["account"]["equity"]) - decimal(
            previous["account"]["holdings_value"]
        )
        fill_delta, fee_delta = Decimal(0), Decimal(0)
        missing_fees = False
        for identifier in previous["fills"].keys() | current["fills"].keys():
            before = previous["fills"].get(identifier)
            after = current["fills"].get(identifier)
            if after is None or (before and before["side"] != after["side"]):
                result["reasons"].append("금액 대조 기준의 주문 이력이 사라지거나 변경되었습니다.")
                continue
            before = before or {"quantity": 0, "amount": "0", "fee": "0"}
            amount = decimal(after["amount"]) - decimal(before["amount"])
            quantity = after["quantity"] - before["quantity"]
            if amount < 0 or quantity < 0:
                result["reasons"].append("금액 대조 기준보다 누적 체결이 감소했습니다.")
                continue
            fill_delta += amount * (1 if after["side"] == "sell" else -1)
            prior_fee = before["fee"] if before["quantity"] else "0"
            next_fee = after["fee"] if after["quantity"] else "0"
            if prior_fee is None or next_fee is None:
                if amount or quantity or prior_fee != next_fee:
                    missing_fees = True
            else:
                delta = decimal(next_fee) - decimal(prior_fee)
                if delta < 0:
                    result["reasons"].append("금액 대조 기준보다 누적 수수료가 감소했습니다.")
                fee_delta += delta
        result["fill_cash_delta"] = str(fill_delta)
        result["fee_delta"] = None if missing_fees else str(fee_delta)
        if result["reasons"]:
            return mismatch()
        if missing_fees:
            result.update(status="unverified", cumulative_status="unverified")
            result["reasons"].append("체결 수수료가 확인되지 않아 금액 일치를 확정할 수 없습니다.")
            # Keep the entire account/fill anchor: later statement evidence must
            # explain the original cash movement, not charge a historical fee twice.
            return result
        else:
            expected = old_cash + fill_delta - fee_delta
            difference = cash - expected
            result.update(expected_economic_cash=str(expected), difference=str(difference))
            if abs(difference) > Decimal(1):
                result["reasons"].append(
                    "체결로 설명되지 않는 계좌 금액 변동입니다. "
                    "입출금/배당/비용/계좌 초기화를 확인하세요."
                )
                return mismatch()
            result["status"] = "matched"
        # Preserve the preceding account and fill checkpoint before advancing together.
        self.repo.put_document("monetary_previous", previous)
        current["cumulative_status"] = result["cumulative_status"]
        self.repo.put_document("monetary_ledger", current)
        return result

    def _quote(self, symbol, *, automatic=False):
        quote = (
            self.realtime_quote(symbol)
            if automatic and self.realtime_quote
            else self.broker.quote(symbol)
        )
        at = self._fresh(quote["observed_at"], "시세")
        age = (self.clock.now() - at).total_seconds()
        if not 0 <= age <= 300:
            raise TradingBlocked("시세가 오래되었거나 미래 시각입니다.")
        if automatic and self.repo.mode == "kis_paper":
            if quote.get("timestamp_basis") != "exchange" or not quote.get("source_timestamp"):
                raise TradingBlocked("자동 주문에는 거래소 시각이 확인된 실시간 시세가 필요합니다.")
            self._fresh(quote["source_timestamp"], "실시간 체결 시세", 60)
            if quote.get("halted") is not False:
                raise TradingBlocked("종목 거래 가능 상태를 확인하지 못했습니다.")
            bounds = self.broker.quote(symbol)
            self._fresh(bounds.get("observed_at"), "호가 단위/가격제한")
            if (
                not bounds.get("tick_size")
                or not bounds.get("upper_limit")
                or not bounds.get("lower_limit")
            ):
                raise TradingBlocked("호가 단위/가격제한을 확인하지 못했습니다.")
            quote = {
                **quote,
                **{key: bounds[key] for key in ("tick_size", "upper_limit", "lower_limit")},
            }
        decimal(quote["price"], positive=True)
        return quote

    def _checks(self, order, *, amendment=False):
        if self.preflight:
            self.preflight()
        automatic = order["context"].get("origin") == "automatic"
        if (
            order["context"].get("valid_session")
            != self.clock.now().astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
        ):
            raise TradingBlocked(
                "주문 의도의 거래일이 지났습니다. 오늘의 근거로 새 주문을 등록하세요."
            )
        if automatic:
            deployment = self.repo.document("deployment") or {}
            if not deployment.get("enabled") or deployment.get("version") != order["context"].get(
                "deployment_version"
            ):
                raise TradingBlocked("자동 운용 설정이 변경되었거나 중지되었습니다.")
        control = self.repo.document("control") or {"entries_enabled": False}
        if order["side"] == "buy" and not control.get("entries_enabled"):
            raise TradingBlocked("신규 진입이 중지되어 있습니다.")
        if self.session_check and not self.session_check(self.clock.now()):
            raise TradingBlocked("검증된 정규장 주문 시간이 아닙니다.")
        unresolved = [
            o
            for o in self.repo.all_orders()
            if o["id"] != order["id"] and o["state"] in {"unknown", "sending"}
        ]
        if unresolved:
            raise TradingBlocked("결과 미확정 주문을 먼저 대조하세요.")
        reconciliation = self.repo.document("reconciliation")
        if reconciliation and reconciliation.get("status") == "mismatch":
            raise TradingBlocked("계좌 대조 불일치를 먼저 확인하세요.")
        target = self._quote(order["symbol"], automatic=automatic)
        account = self.broker.account()
        self._fresh(account.get("observed_at"), "계좌")
        if account.get("buying_power_observed_at"):
            self._fresh(account["buying_power_observed_at"], "주문 가능액")
        if self.repo.document("baseline") is None:
            self.repo.put_document(
                "baseline", {**account, "started_at": self.at(), "segment_id": self.at()}
            )
            self.repo.put_document(
                "position_ledger",
                {
                    "quantities": {p["symbol"]: p["quantity"] for p in account["positions"]},
                    "applied": {},
                },
            )
            if self.repo.document("monetary_ledger") is None:
                self.repo.put_document(
                    "monetary_ledger",
                    {**self._monetary_snapshot(account), "cumulative_status": "verified"},
                )
        quotes = {order["symbol"]: target}
        for p in account["positions"]:
            if p["quantity"] > 0 and p["symbol"] not in quotes:
                quotes[p["symbol"]] = self._quote(p["symbol"], automatic=automatic)
        times = [datetime.fromisoformat(q["observed_at"]) for q in quotes.values()]
        if (max(times) - min(times)).total_seconds() > 60:
            raise TradingBlocked("보유 종목 시세의 기준 시각 차이가 큽니다.")
        price = decimal(target["price"], positive=True)
        if order["order_type"] == "limit":
            price = valid_price(
                order["limit_price"],
                target.get("tick_size", "1"),
                target.get("lower_limit"),
                target.get("upper_limit"),
                order["side"],
            )
        positions = {p["symbol"]: p for p in account["positions"]}
        equity = (
            decimal(account["equity"])
            - decimal(account["holdings_value"])
            + sum(
                decimal(quotes[p["symbol"]]["price"]) * p["quantity"] for p in account["positions"]
            )
        )
        quantity = order["quantity"]
        old = positions.get(order["symbol"], {"quantity": 0, "sellable_quantity": 0})
        own_remaining = 0
        own_price = None
        if amendment:
            active_target = order.get("current_active_target") or order
            remaining = active_target.get("remaining_quantity")
            if (
                order["state"] not in {"acknowledged", "partially_filled"}
                or type(remaining) is not int
                or remaining < quantity
                or type(order.get("remaining_quantity")) is not int
            ):
                raise TradingBlocked("정정 대상의 확인된 예약 잔량이 부족합니다.")
            own_remaining = min(remaining, order["remaining_quantity"])
            own_price = decimal(order.get("submitted_price"), positive=True)
        if order["side"] == "buy":
            cap = self.broker.capacity(order["symbol"], str(price), order["order_type"])
            if self.repo.mode == "kis_paper" or cap.get("observed_at"):
                self._fresh(cap.get("observed_at"), "매수 가능액")
            available = decimal(
                cap.get("amount", cap.get("nrcvb_buy_amt", account.get("buying_power", "0")))
            )
            max_quantity = int(cap.get("quantity", cap.get("nrcvb_buy_qty", 0)))
            if amendment:
                # A replacement may reuse only its own confirmed unfilled reservation.
                # Never add the cash reserved for another local or external order.
                credit = own_price * own_remaining
                account_available = decimal(account.get("buying_power", available))
                available = max(Decimal(0), min(available, account_available)) + credit
                max_quantity += int(credit / (price * Decimal("1.001")))
            amount = price * quantity * Decimal("1.001")
            if amount > available or quantity > max_quantity:
                raise TradingBlocked("미수 없는 매수 가능액/수량을 초과합니다.")
            risk = order["context"].get("risk_policy") or {}
            held_value = sum(
                decimal(quotes[p["symbol"]]["price"]) * p["quantity"] for p in account["positions"]
            )
            # Reserve accepted-but-unfilled entries in concentration and total-exposure limits.
            pending = [
                o
                for o in self.repo.all_orders()
                if o["id"] != order["id"] and o["side"] == "buy" and o["state"] in ACTIVE
            ]
            reserved_target = sum(
                o["remaining_quantity"] for o in pending if o["symbol"] == order["symbol"]
            )
            reserved_total = sum(
                decimal(
                    o.get("submitted_price") or o["limit_price"] or o.get("valuation_price", "0")
                )
                * o["remaining_quantity"]
                for o in pending
            )
            projected = (old["quantity"] + reserved_target + quantity) * decimal(target["price"])
            if equity <= 0 or projected / equity * 100 > decimal(risk.get("max_position_pct", 20)):
                raise TradingBlocked("종목별 보유 비중 제한을 초과합니다.")
            if (held_value + reserved_total + price * quantity) / equity * 100 > decimal(
                risk.get("max_invested_pct", 50)
            ):
                raise TradingBlocked("전체 투자 비중 제한을 초과합니다.")
            if automatic:
                instruments = {i.symbol: i for i in self.instruments()}
                exposed = {
                    symbol: decimal(quotes[symbol]["price"]) * p["quantity"]
                    for symbol, p in positions.items()
                }
                for entry in pending:
                    exposed[entry["symbol"]] = (
                        exposed.get(entry["symbol"], Decimal(0))
                        + decimal(entry.get("submitted_price") or entry.get("limit_price") or "0")
                        * entry["remaining_quantity"]
                    )
                exposed[order["symbol"]] = (
                    exposed.get(order["symbol"], Decimal(0)) + price * quantity
                )
                if len([v for v in exposed.values() if v > 0]) > int(risk.get("max_positions", 5)):
                    raise TradingBlocked("동시 보유/예약 종목 수 제한을 초과합니다.")
                sector = instruments[order["symbol"]].sector
                sector_value = sum(
                    v
                    for symbol, v in exposed.items()
                    if symbol in instruments and instruments[symbol].sector == sector
                )
                if sector_value / equity * 100 > decimal(risk.get("max_sector_pct", 30)):
                    raise TradingBlocked("업종별 보유/예약 비중 제한을 초과합니다.")
                index_id = order["context"].get("index_id")
                if index_id:
                    indices = order["context"].get("index_map", {})
                    index_value = sum(
                        v for symbol, v in exposed.items() if indices.get(symbol) == index_id
                    )
                    if index_value / equity * 100 > decimal(risk.get("max_index_pct", 20)):
                        raise TradingBlocked("ETF 추종 지수별 보유/예약 비중 제한을 초과합니다.")
        elif quantity > min(old["quantity"], int(old.get("sellable_quantity", 0)) + own_remaining):
            raise TradingBlocked("매도 가능 수량을 초과합니다.")
        if order["context"].get("origin") == "automatic" and order["side"] == "sell":
            allocation = self.repo.document("allocation", order["symbol"]) or {}
            if quantity > allocation.get("managed_quantity", 0):
                raise TradingBlocked("전략에 할당되지 않은 보유 종목은 자동 매도하지 않습니다.")
        # Slow holdings/capacity calls must not age the initial target beyond its gate.
        for quote in quotes.values():
            self._fresh(quote["observed_at"], "최종 시세")
            if automatic and self.repo.mode == "kis_paper":
                self._fresh(quote["source_timestamp"], "최종 실시간 체결 시세", 60)
        self._fresh(account["observed_at"], "최종 계좌")
        return {"account": account, "quotes": quotes, "equity": str(equity), "at": self.at()}, str(
            price
        )

    def submit(self, order_id):
        order = self.repo.get_order(order_id)
        if order["state"] != "registered":
            return order
        with self.repo.execution_lock() as generation:
            order = self.repo.get_order(order_id)
            if order["state"] != "registered":
                return order
            snapshot, price = self._checks(order)
            date = self.clock.now().astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
            self.repo.transition(
                order_id,
                {"registered"},
                "sending",
                self.at(),
                valuation=snapshot,
                submitted_price=price,
                trading_date=date,
                execution_generation=generation,
            )
            self.repo.assert_generation(generation)
            payload = {
                k: order[k] for k in ("symbol", "side", "quantity", "order_type", "limit_price")
            }
            if payload["order_type"] == "limit":
                payload["limit_price"] = price
            try:
                receipt = self.broker.place_order(payload)
            except TradingBlocked:
                return self.repo.transition(
                    order_id,
                    {"sending"},
                    "rejected",
                    self.at(),
                    error="증권사가 주문을 거부했습니다.",
                )
            except Exception:
                # Even a generic transport error can occur after the broker committed the order.
                return self.repo.transition(
                    order_id,
                    {"sending"},
                    "unknown",
                    self.at(),
                    error="전송 결과 확인이 필요합니다. 다시 주문하지 말고 대조하세요.",
                )
            return self.repo.transition(
                order_id, {"sending"}, "acknowledged", self.at(), **receipt, error=None
            )

    def cancel(self, order_id, command_id, quantity, price=None):
        with self.repo.execution_lock() as generation:
            if self.preflight:
                self.preflight()
            order = self.repo.get_order(order_id)
            payload = {"order_id": order_id, "quantity": quantity, "price": price}
            prior = self.repo.document("command_result", command_id)
            if prior:
                if prior["payload"] != payload:
                    raise TradingBlocked("같은 명령 식별자의 내용이 다릅니다.")
                return self.repo.get_order(order_id)
            if (
                order["state"] in TERMINAL
                or order["state"] in {"unknown", "sending"}
                or not order.get("broker_order_id")
            ):
                raise TradingBlocked("정정/취소 가능한 접수 주문이 아닙니다.")
            if type(quantity) is not int or not 0 < quantity <= order["remaining_quantity"]:
                raise TradingBlocked("정정/취소 수량을 확인하세요.")
            if price is not None:
                if quantity != order["remaining_quantity"]:
                    raise TradingBlocked("가격 정정은 잔량 전체를 대상으로 합니다.")
                _, price = self._checks(
                    {**order, "quantity": quantity, "limit_price": price, "order_type": "limit"},
                    amendment=True,
                )
            command, fresh = self.repo.command(command_id, payload, self.at())
            if not fresh:
                return self.repo.get_order(order_id)
            self.repo.assert_generation(generation)
            try:
                target = order.get("current_active_target") or order
                receipt = self.broker.cancel_order(target, quantity, price)
            except TradingBlocked:
                self.repo.finish_command(command_id, "rejected", {})
                return self.repo.transition(
                    order_id,
                    {order["state"]},
                    order["state"],
                    self.at(),
                    command_id=command_id,
                    command_state="rejected",
                    error="정정/취소가 거부됐습니다. 원주문 상태를 확인하세요.",
                )
            except Exception:
                self.repo.finish_command(command_id, "unknown", {})
                return self.repo.transition(
                    order_id,
                    {order["state"]},
                    "unknown",
                    self.at(),
                    command_id=command_id,
                    command_state="unknown",
                    error="정정/취소 결과 확인이 필요합니다.",
                )
            values = {
                "command": "amend" if price is not None else "cancel",
                "command_id": command_id,
                "command_state": "acknowledged",
                "command_receipt": receipt,
            }
            if price is not None:
                values.update(
                    submitted_price=price, amendments=[*order.get("amendments", []), receipt]
                )
            self.repo.finish_command(command_id, "acknowledged", receipt)
            self.repo.put_document(
                "command_result", {"payload": payload, "receipt": receipt}, command_id
            )
            return self.repo.transition(
                order_id, {order["state"]}, order["state"], self.at(), **values
            )

    def reconcile(self):
        with self.repo.execution_lock():
            local = self.repo.all_orders()
            ledger = self.repo.document("position_ledger")
            today = self.clock.now().astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
            dates = [
                o.get("trading_date", o["created_at"][:10])
                for o in local
                if o["state"] not in {"registered", "expired", "rejected"}
            ]
            broker_orders = self.broker.orders(min(dates, default=today), today)
            used, unresolved, issues = set(), [], []
            for order in local:
                if order["state"] in {"registered", "expired", "rejected"}:
                    continue
                date = order.get("trading_date", order["created_at"][:10]).replace("-", "")
                candidates = [
                    o
                    for o in broker_orders
                    if str(o.get("trading_date", "")).replace("-", "") == date
                ]
                if order.get("broker_order_id"):
                    candidates = [
                        o for o in candidates if o["broker_order_id"] == order["broker_order_id"]
                    ]
                else:
                    # A missing receipt needs an unambiguous match including submission time.
                    submitted_at = datetime.fromisoformat(
                        next(
                            (e["at"] for e in order["events"] if e["state"] == "sending"),
                            order["created_at"],
                        )
                    )

                    def matches(candidate):
                        if any(
                            candidate.get(k) != order[k] for k in ("symbol", "side", "quantity")
                        ):
                            return False
                        if candidate.get("order_type") != order["order_type"]:
                            return False
                        if order["order_type"] == "limit" and decimal(
                            candidate.get("limit_price", "0")
                        ) != decimal(order.get("submitted_price") or order["limit_price"]):
                            return False
                        try:
                            received = datetime.fromisoformat(candidate["ordered_at"])
                            return abs((received - submitted_at).total_seconds()) <= 120
                        except (ValueError, KeyError, TypeError):
                            return False

                    candidates = [o for o in candidates if matches(o)]
                if len(candidates) != 1:
                    if order["state"] not in TERMINAL:
                        self.repo.transition(
                            order["id"],
                            {order["state"]},
                            "unknown",
                            self.at(),
                            error="주문내역을 유일하게 대조하지 못했습니다.",
                        )
                        unresolved.append(order["id"])
                    else:
                        issues.append("완료 주문의 브로커 이력을 확인할 수 없습니다.")
                    continue
                actual = candidates[0]
                key = (date, actual["broker_order_id"])
                if key in used:
                    issues.append("하나의 외부 주문이 여러 의도와 일치합니다.")
                    unresolved.append(order["id"])
                    continue
                used.add(key)
                commands = self.repo.commands_for(order["id"], details=True)
                graph = reconcile_order_chain(
                    [
                        row
                        for row in broker_orders
                        if str(row.get("trading_date", "")).replace("-", "") == date
                    ],
                    actual["broker_order_id"],
                    confirmed_amend_receipts=order.get("amendments", []),
                    unresolved_commands=[
                        c for c in commands if c["state"] in {"sending", "unknown"}
                    ],
                    semantics_verified=self.repo.mode == "local_simulation"
                    or bool(
                        (self.repo.document("capabilities") or {}).get(
                            "amendment_semantics_verified"
                        )
                    ),
                )
                used.update((date, identifier) for identifier in graph["chain_order_ids"])
                if graph["status"] == "unknown":
                    unresolved.append(order["id"])
                    self.repo.transition(
                        order["id"],
                        {order["state"]},
                        "unknown",
                        self.at(),
                        error="주문/정정/취소 연결의 확인이 필요합니다.",
                        chain=graph,
                    )
                    continue
                filled, cancelled = graph["filled_quantity"], graph["cancelled_quantity"]
                if filled < order["filled_quantity"] or decimal(graph["filled_amount"]) < decimal(
                    order["filled_amount"]
                ):
                    issues.append("누적 체결이 이전 대조보다 감소했습니다.")
                    continue
                state = graph["status"]
                fee = graph["fee"]
                fee_source = "broker" if fee is not None else None
                evidence = self.repo.document("order_cost", order["id"])
                if fee is None and evidence and state in TERMINAL:
                    try:
                        matches_fill = (
                            evidence.get("verified_by_user") is True
                            and type(evidence.get("filled_quantity")) is int
                            and evidence["filled_quantity"] == filled
                            and decimal(evidence.get("filled_amount"))
                            == decimal(graph["filled_amount"])
                            and bool(evidence.get("source"))
                        )
                        cost = decimal(evidence.get("fee"))
                        if matches_fill and cost >= 0:
                            fee = str(cost)
                            fee_source = "operator_evidence"
                    except (ValueError, TypeError):
                        pass
                values = {
                    "broker_order_id": actual["broker_order_id"],
                    "branch_id": actual.get("branch_id"),
                    "filled_quantity": filled,
                    "filled_amount": graph["filled_amount"],
                    "fee": fee,
                    "fee_source": fee_source,
                    "cancelled_quantity": cancelled,
                    "rejected_quantity": graph["rejected_quantity"],
                    "remaining_quantity": graph["remaining_quantity"],
                    "current_active_target": graph["current_active_target"],
                    "chain": graph,
                    "error": None,
                    "reconciled_at": self.at(),
                }
                if ledger is not None:
                    already = int(ledger["applied"].get(order["id"], 0))
                    delta = filled - already
                    if delta < 0:
                        issues.append("기록한 체결 수량보다 브로커 누적 수량이 작습니다.")
                    else:
                        symbol = order["symbol"]
                        ledger["quantities"][symbol] = ledger["quantities"].get(
                            symbol, 0
                        ) + delta * (1 if order["side"] == "buy" else -1)
                        ledger["applied"][order["id"]] = filled
                        if delta and order["context"].get("origin") == "automatic":
                            allocation = self.repo.document("allocation", symbol) or {
                                "managed_quantity": 0,
                                "opened_session": date,
                            }
                            allocation["managed_quantity"] += delta * (
                                1 if order["side"] == "buy" else -1
                            )
                            allocation.update(
                                strategy_version=order["context"].get("deployment_version"),
                                stop_price=order["context"].get(
                                    "stop_price", allocation.get("stop_price", 0)
                                ),
                            )
                            self.repo.put_document("allocation", allocation, symbol)
                if state != order["state"] or any(
                    order.get(k) != v for k, v in values.items() if k != "reconciled_at"
                ):
                    self.repo.transition(order["id"], {order["state"]}, state, self.at(), **values)
            account = self.broker.account()
            previous_account = self.repo.document("account")
            if previous_account is not None:
                self.repo.put_document("previous_account", previous_account)
            self.repo.put_document("account", account)
            if self.repo.document("baseline") is None:
                self.repo.put_document(
                    "baseline", {**account, "started_at": self.at(), "segment_id": self.at()}
                )
            actual_positions = {
                p["symbol"]: p["quantity"] for p in account["positions"] if p["quantity"]
            }
            position_uncertain = False
            if ledger is None:
                ledger = {
                    "quantities": actual_positions,
                    "applied": {o["id"]: o["filled_quantity"] for o in self.repo.all_orders()},
                }
            else:
                expected_positions = {s: q for s, q in ledger["quantities"].items() if q}
                if expected_positions != actual_positions:
                    position_uncertain = True
                    issues.append(
                        "체결 장부와 브로커 보유 수량이 다릅니다. "
                        "외부 매매/기업행동/계좌 초기화를 확인하세요."
                    )
            self.repo.put_document("position_ledger", ledger)
            position_status = "mismatch" if issues else "pending" if unresolved else "matched"
            financial = self._reconcile_money(
                account, unresolved=unresolved, position_uncertain=position_uncertain
            )
            if financial["status"] == "mismatch":
                issues.extend(financial["reasons"])
            external = [
                o
                for o in broker_orders
                if (str(o.get("trading_date", "")).replace("-", ""), o["broker_order_id"])
                not in used
            ]
            result = {
                "at": self.at(),
                "status": "mismatch" if issues else "pending" if unresolved else "matched",
                "unresolved": unresolved,
                "issues": issues,
                "external_orders": external,
                "broker_order_count": len(broker_orders),
                "source": self.repo.mode,
                "position_status": position_status,
                "financial": financial,
            }
            self.repo.put_document("reconciliation", result)
            self.repo.put_document("reconciliation_history", result, self.at())
            return result
