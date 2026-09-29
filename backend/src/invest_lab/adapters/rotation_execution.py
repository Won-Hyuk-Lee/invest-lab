"""Reconcile persistent replacement pairs before submitting dependent purchases."""

from invest_lab.domain.errors import TradingBlocked
from invest_lab.domain.orders import TERMINAL
from invest_lab.domain.portfolio_policy import (
    choose_rotations,
    holding_score,
    holding_sessions,
    portfolio_risk,
    score_percentiles,
)
from invest_lab.domain.strategies import position_size_details

FINISHED = {"completed", "cancelled", "expired"}


class RotationCoordinator:
    def __init__(self, runtime, rules, signals, instruments, today, signal_session, execute):
        self.runtime, self.rules, self.signals = runtime, rules, signals
        self.store, self.instruments = runtime.store, instruments
        self.today, self.signal_session, self.execute = today, signal_session, execute

    def age(self, start, end, symbol):
        if self.runtime.market_store:
            sessions = [
                r["session"]
                for r in self.runtime.market_store.sessions(
                    start, end, as_of=self.runtime.workspace.clock.now()
                )
            ]
        else:
            sessions = [
                b.session.isoformat() for b in self.runtime.workspace.market.history(symbol)
            ]
        return holding_sessions(start, end, sessions)

    def save(self, pair, **changes):
        pair.update(changes)
        self.store.put_document("rotation_pair", pair, pair["id"])

    def orders(self, pair):
        return [
            o
            for o in self.store.all_orders()
            if o.get("context", {}).get("rotation_id") == pair["id"]
        ]

    def suitable(self, symbol, entries):
        signal = entries.get(symbol)
        if not signal or not signal.get("eligible"):
            return None
        try:
            quote = self.runtime.execution._quote(symbol, automatic=True)
        except TradingBlocked:
            return None
        price = float(quote["price"])
        if abs(price / signal["technical"]["close"] - 1) * 100 > self.rules.max_gap_pct:
            return None
        if (signal.get("stop_distance") or 0) / price * 100 > self.rules.max_initial_stop_pct:
            return None
        return quote

    def run(self, candidates, *, entries_allowed):
        entries = {s["symbol"]: dict(s) for s in candidates}
        pairs = self.store.documents("rotation_pair")
        blocked, priority = set(), []
        for pair in pairs:
            if pair["status"] in FINISHED:
                continue
            orders = self.orders(pair)
            active = [o for o in orders if o["state"] not in TERMINAL]
            exits = [o for o in orders if o["side"] == "sell"]
            buys = [o for o in orders if o["side"] == "buy"]
            positions = {p["symbol"]: p for p in self.runtime.broker.account()["positions"]}
            old, new = pair["sell_symbol"], pair["buy_symbol"]
            blocked.add(new)
            allocation = self.store.document("allocation", old) or {}
            lifecycle = allocation.get(
                "position_id", old + ":" + allocation.get("opened_session", "")
            )
            protective = self.store.document("exit_intent", lifecycle) or {}
            if protective.get("status") == "pending" and protective.get("kind") in {
                "full",
                "risk_reduction",
            }:
                self.save(pair, status="cancelled", reason="protective_exit_priority")
                continue
            if any(o["state"] in {"unknown", "sending"} for o in active):
                self.save(pair, status="reconcile_required")
                continue
            if buys:
                if any(o["state"] not in TERMINAL for o in buys):
                    self.save(pair, status="entry_submitted")
                elif sum(o["filled_quantity"] for o in buys):
                    self.save(pair, status="completed", completed_session=self.today)
                else:
                    self.save(pair, status="expired", reason="entry_not_filled")
                continue
            if old not in positions and sum(o["filled_quantity"] for o in exits):
                if any(o["state"] not in TERMINAL for o in exits):
                    self.save(pair, status="reconcile_required")
                    continue
                self.store.put_document(
                    "rotation_cooldown", {"symbol": old, "session": self.today}, old
                )
                if entries_allowed and self.suitable(new, entries):
                    self.save(pair, status="entry_recheck", exit_confirmed_at=self.runtime.at())
                    priority.append({**entries[new], "rotation_id": pair["id"]})
                else:
                    self.save(pair, status="expired", reason="entry_recheck_failed_cash_retained")
                continue
            if old not in positions:
                self.save(pair, status="cancelled", reason="holding_closed_outside_pair")
                continue
            if old in positions and not allocation.get("managed_quantity"):
                self.save(pair, status="expired", reason="manual_residual_keeps_slot")
                continue
            quote = self.suitable(new, entries) if entries_allowed else None
            if quote is None or not self.rules.rotation_enabled:
                # A cancel acknowledgement alone cannot release a slot or reservation.
                for order in active:
                    if order["side"] != "sell":
                        continue
                    if order["state"] == "registered":
                        self.store.transition(
                            order["id"], {"registered"}, "expired", self.runtime.at()
                        )
                    else:
                        self.runtime.execution.cancel(
                            order["id"],
                            f"rotation-cancel:{pair['id']}:{order['id']}",
                            order["remaining_quantity"],
                        )
                if self.runtime.execution.reconcile()["status"] != "matched":
                    self.save(pair, status="reconcile_required")
                elif any(o["state"] not in TERMINAL for o in self.orders(pair)):
                    self.save(pair, status="cancel_pending")
                else:
                    self.save(pair, status="cancelled", reason="candidate_or_entry_window_changed")
                continue
            if active:
                self.save(
                    pair,
                    status="exit_partial"
                    if sum(o["filled_quantity"] for o in exits)
                    else "exit_submitted",
                )
                continue
            if old in positions:
                if not self.submit_exit(pair, positions[old], allocation):
                    return []

        unfinished = [p for p in pairs if p["status"] not in FINISHED]
        account = self.runtime.broker.account()
        positions = {p["symbol"]: p for p in account["positions"]}
        active = [o for o in self.store.all_orders() if o["state"] not in TERMINAL]
        quota = sum(p.get("created_session") == self.today for p in pairs)
        if (
            self.rules.rotation_enabled
            and entries_allowed
            and not unfinished
            and not active
            and len(positions) >= self.rules.max_positions
            and quota < self.rules.rotation_max_per_day
        ):
            eligible_holdings = []
            for symbol, pos in positions.items():
                allocation = self.store.document("allocation", symbol) or {}
                if not allocation.get("opened_session"):
                    continue
                eligible_holdings.append(
                    {
                        "symbol": symbol,
                        "fully_managed": allocation.get("managed_quantity") == pos["quantity"],
                        "tradable": pos["sellable_quantity"] == pos["quantity"],
                        "age": self.age(allocation["opened_session"], self.signal_session, symbol),
                    }
                )
            cooldowns = {
                item["symbol"]: 10000
                - max(0, self.age(item["session"], self.today, item["symbol"]) - 1)
                for item in self.store.documents("rotation_cooldown")
            }
            scores = {
                s: holding_score(v["technical"])
                for s, v in self.signals.items()
                if (
                    self.rules.strategy_type == "kospi_pullback"
                    and self.instruments[s].kind == "kospi"
                )
                or (
                    self.rules.strategy_type == "etf_trend"
                    and self.instruments[s].kind.startswith("etf")
                )
            }
            proposed = choose_rotations(
                eligible_holdings,
                [{"symbol": s, "quantity": 1} for s in entries if s not in positions],
                score_percentiles(scores),
                self.rules,
                day_index=10000,
                cooldowns=cooldowns,
            )
            for pair in proposed[: self.rules.rotation_max_per_day - quota]:
                old, new = pair["sell_symbol"], pair["buy_symbol"]
                quote = self.suitable(new, entries)
                if quote is None:
                    continue
                remaining = {s: p for s, p in positions.items() if s != old}
                prices = {s: float(p["value"]) / p["quantity"] for s, p in remaining.items()}
                amounts = {s: float(p["value"]) for s, p in remaining.items()}
                group = (entries[new].get("etf") or {}).get("benchmark_id")
                sizing = position_size_details(
                    equity=float(account["equity"]),
                    cash=float(account.get("buying_power", account["cash"]))
                    + float(positions[old]["value"])
                    * (
                        1
                        - (
                            self.rules.commission_bps
                            + self.rules.sell_tax_bps
                            + self.rules.slippage_bps
                        )
                        / 10000
                    ),
                    price=float(quote["price"]),
                    stop_distance=entries[new]["stop_distance"],
                    total_value=sum(amounts.values()),
                    sector_value=sum(
                        v
                        for s, v in amounts.items()
                        if self.instruments[s].sector == self.instruments[new].sector
                    ),
                    index_value=sum(
                        v
                        for s, v in amounts.items()
                        if group
                        and (self.signals.get(s, {}).get("etf") or {}).get("benchmark_id") == group
                    ),
                    volume20=entries[new]["technical"].get("volume20"),
                    open_risk=portfolio_risk(
                        {
                            s: {
                                **(self.store.document("allocation", s) or {}),
                                "quantity": p["quantity"],
                            }
                            for s, p in remaining.items()
                        },
                        prices,
                    ),
                    rules=self.rules,
                )
                if not sizing["quantity"]:
                    continue
                pair.update(
                    id=f"{self.today}:{old}:{new}",
                    created_session=self.today,
                    status="planned",
                    signal_session=self.signal_session,
                    prospective_sizing=sizing,
                )
                self.save(pair)
                allocation = self.store.document("allocation", old)
                if not self.submit_exit(pair, positions[old], allocation):
                    return []
                # Buying waits for the NEXT invocation and a fresh account reconciliation.
                blocked.add(new)
                blocked.add(old)
        for item in self.store.documents("rotation_cooldown"):
            if (
                self.age(item["session"], self.today, item["symbol"]) - 1
                <= self.rules.rotation_cooldown
            ):
                blocked.add(item["symbol"])
        return priority + [s for s in candidates if s["symbol"] not in blocked]

    def submit_exit(self, pair, position, allocation):
        symbol = pair["sell_symbol"]
        try:
            quote = self.runtime.execution._quote(symbol, automatic=True)
        except TradingBlocked:
            self.save(pair, status="planned", reason="exit_quote_unavailable")
            return True
        quantity = min(position["sellable_quantity"], allocation.get("managed_quantity", 0))
        if not quantity:
            return True
        self.save(pair, status="exit_submitted")
        reconciled = self.execute(
            {
                **self.signals.get(symbol, {"symbol": symbol}),
                "rotation_id": pair["id"],
                "exit_kind": "rotation",
            },
            "sell",
            quantity,
            quote,
        )
        if not reconciled:
            self.save(pair, status="reconcile_required")
        return reconciled
