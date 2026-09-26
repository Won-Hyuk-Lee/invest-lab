import json
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from invest_lab.adapters.market_data import MarketStore
from invest_lab.adapters.runtime import Runtime
from invest_lab.adapters.runtime_store import RuntimeStore
from invest_lab.adapters.simulated_broker import SimulatedBroker
from invest_lab.application.execution import ExecutionService
from invest_lab.domain.errors import TradingBlocked
from invest_lab.domain.models import Bar, Instrument, Quote, Strategy
from invest_lab.domain.strategies import StrategyRules


class Clock:
    at = datetime(2026, 9, 25, 0, tzinfo=timezone.utc)

    def now(self):
        return self.at


class Market:
    source = "synthetic_demo"

    def __init__(self, clock):
        self.clock = clock

    def instruments(self):
        return [
            Instrument("005930", "A", "kospi", "shared-sector"),
            Instrument("000660", "B", "kospi", "shared-sector"),
            Instrument("035420", "C", "kospi", "other-sector"),
            Instrument("0048K0", "ETF A", "etf_us", "ETF"),
            Instrument("360750", "ETF B", "etf_us", "ETF"),
        ]

    def quote(self, symbol):
        return Quote(symbol, Decimal("100"), self.clock.now(), self.source)

    def history(self, symbol):
        return [Bar(date(2026, 9, 24), 100, 110, 90, 100, 1000000)]


def signal(symbol, sector="shared-sector", index=None):
    return {
        "symbol": symbol,
        "sector": sector,
        "eligible": True,
        "as_of": "2026-09-24",
        "stop_distance": 1,
        "etf": {"benchmark_id": index} if index else None,
        "technical": {
            "history_count": 260,
            "close": 100,
            "sma": {"120": 90, "20": 101},
            "atr14": 1,
            "volume20": 1000000,
        },
    }


def setup_runtime(tmp_path, *, rules=None, positions=None, cash="10000", calendar=False):
    clock = Clock()
    market = Market(clock)
    workspace = SimpleNamespace(
        clock=clock, market=market, strategies=SimpleNamespace(latest=lambda: Strategy())
    )
    store = RuntimeStore(tmp_path / "automatic.db")
    store.put_document("screen", {})  # fixture has no external research dependency
    broker = SimulatedBroker(
        store, market, clock, initial={"cash": cash, "positions": positions or []}
    )
    calendar_store = None
    if calendar:
        calendar_store = MarketStore(tmp_path / "market.db")
        calendar_store.import_calendar(
            [
                {
                    "session": day,
                    "is_open": True,
                    "open_at": day + "T09:00:00+09:00",
                    "close_at": day + "T15:30:00+09:00",
                }
                for day in ("2026-09-24", "2026-09-25")
            ],
            source="fixture",
            as_of="2026-09-23T00:00:00Z",
        )
    runtime = Runtime(workspace, store, broker, market_store=calendar_store)
    config = StrategyRules(
        max_position_pct=60,
        max_total_pct=80,
        max_sector_pct=100,
        max_index_pct=100,
        risk_per_trade_pct=100,
    )
    values = {**asdict(config), **(rules or {})}
    store.put_document("deployment", {"enabled": True, "version": 1, "rules": values})
    store.put_document(
        "control",
        {
            "entries_enabled": True,
            "risk_policy": {"max_position_pct": 100, "max_invested_pct": 100},
        },
    )
    return runtime


def watch(runtime, rows, *, entries=True):
    runtime.store.put_document(
        "watchlist",
        {
            "as_of": "2026-09-24",
            "deployment_version": 1,
            "items": rows if entries else [],
            "all_signals": rows,
            "errors": [],
        },
    )


def test_automatic_buys_resize_after_each_actual_fill_and_do_not_overspend(tmp_path):
    runtime = setup_runtime(tmp_path)
    watch(runtime, [signal("005930"), signal("000660"), signal("035420", "other-sector")])
    result = runtime.auto_trade()
    orders = runtime.store.all_orders()
    assert result["submitted"] == 2
    assert {order["symbol"]: order["quantity"] for order in orders} == {"005930": 59, "000660": 20}
    account = runtime.broker.account()
    assert Decimal(account["holdings_value"]) / Decimal(account["equity"]) <= Decimal("0.8")
    assert Decimal(account["cash"]) > 0
    assert runtime.auto_trade()["submitted"] == 0


@pytest.mark.parametrize(
    "rules,rows,expected",
    [
        ({"max_positions": 1}, [signal("005930"), signal("000660")], 59),
        ({"max_sector_pct": 30}, [signal("005930"), signal("000660")], 29),
        (
            {"max_index_pct": 25},
            [signal("0048K0", "ETF", "SP500"), signal("360750", "ETF", "SP500")],
            24,
        ),
    ],
)
def test_automatic_position_sector_and_index_limits_include_previous_fills(
    tmp_path, rules, rows, expected
):
    runtime = setup_runtime(tmp_path, rules=rules)
    watch(runtime, rows)
    runtime.auto_trade()
    orders = runtime.store.all_orders()
    assert len(orders) == 1
    assert orders[0]["quantity"] == expected


def test_unfilled_buy_reserves_total_exposure_and_position_slot(tmp_path):
    runtime = setup_runtime(tmp_path, rules={"max_total_pct": 40})
    first = runtime.execution.register(
        {
            "client_request_id": str(uuid4()),
            "symbol": "005930",
            "side": "buy",
            "quantity": 20,
            "order_type": "limit",
            "limit_price": "90",
        }
    )
    runtime.execution.submit(first["id"])
    runtime.execution.reconcile()
    watch(runtime, [signal("000660")])
    runtime.auto_trade()
    orders = runtime.store.all_orders()
    assert runtime.store.get_order(first["id"])["remaining_quantity"] == 20
    assert next(order for order in orders if order["symbol"] == "000660")["quantity"] == 21
    assert 20 * 90 + 21 * 100 <= 4000


@pytest.mark.parametrize("condition", ["entries_disabled", "loss_limit", "entry_window_closed"])
def test_entry_restrictions_do_not_suppress_managed_position_exits(tmp_path, condition):
    runtime = setup_runtime(
        tmp_path,
        positions=[{"symbol": "005930", "quantity": 10, "average_price": "100"}],
        cash="9000",
        calendar=True,
    )
    runtime.store.put_document(
        "allocation",
        {"managed_quantity": 10, "opened_session": "2026-09-24", "stop_price": 80},
        "005930",
    )
    watch(runtime, [signal("005930")], entries=False)
    stored = runtime.store.document("watchlist")
    stored["all_signals"][0]["technical"]["sma"]["20"] = 90
    runtime.store.put_document("watchlist", stored)
    if condition == "entries_disabled":
        runtime.control(False)
    elif condition == "loss_limit":
        runtime.store.put_document("daily_start", {"equity": "20000"}, "2026-09-25")
    else:
        runtime.workspace.clock.at = datetime(2026, 9, 25, 4, tzinfo=timezone.utc)
    result = runtime.auto_trade()
    assert result["submitted"] == 1
    assert runtime.store.all_orders()[0]["side"] == "sell"
    assert runtime.broker.account()["positions"] == []


def paper_execution(tmp_path, *, data=None):
    clock = Clock()
    store = RuntimeStore(tmp_path / "paper-fixture.db", "kis_paper", "fixture")
    store.put_document("control", {"entries_enabled": True})
    store.put_document("deployment", {"enabled": True, "version": 1})
    market = Market(clock)
    quote = {
        "symbol": "005930",
        "price": "100",
        "observed_at": clock.now().isoformat(),
        "timestamp_basis": "exchange",
        "source_timestamp": clock.now().isoformat(),
        "halted": False,
        "source": "kis_paper_ws",
        **(data or {}),
    }
    broker = SimpleNamespace(
        quote=lambda _: {
            "symbol": "005930",
            "price": "100",
            "observed_at": clock.now().isoformat(),
            "tick_size": "1",
            "upper_limit": "130",
            "lower_limit": "70",
        }
    )
    service = ExecutionService(
        store, broker, clock, instruments=market.instruments, realtime_quote=lambda _: quote
    )
    return service, broker, clock, quote


@pytest.mark.parametrize(
    "data",
    [
        {"timestamp_basis": "received", "source_timestamp": None},
        {"source_timestamp": "2026-09-24T23:58:59Z"},
        {"source_timestamp": "2026-09-25T00:00:01Z"},
        {"halted": True},
        {"halted": None},
    ],
)
def test_automatic_paper_requires_exchange_clock_and_verified_trading_flag(tmp_path, data):
    service, _, _, _ = paper_execution(tmp_path, data=data)
    with pytest.raises(TradingBlocked):
        service._quote("005930", automatic=True)


def test_automatic_quote_preserves_ws_event_time_when_adding_rest_price_bands(tmp_path):
    service, _, _, quote = paper_execution(tmp_path)
    result = service._quote("005930", automatic=True)
    assert result["source_timestamp"] == quote["source_timestamp"]
    assert result["timestamp_basis"] == "exchange"
    assert result["tick_size"] == "1" and result["upper_limit"] == "130"


def test_slow_account_check_cannot_age_initial_ws_quote_past_order_gate(tmp_path):
    service, broker, clock, _ = paper_execution(tmp_path)

    def account():
        clock.at += timedelta(seconds=61)
        return {
            "cash": "10000",
            "equity": "10000",
            "holdings_value": "0",
            "positions": [],
            "observed_at": clock.now().isoformat(),
        }

    broker.account = account
    broker.capacity = lambda *args: {
        "amount": "10000",
        "quantity": 100,
        "observed_at": clock.now().isoformat(),
    }
    submitted = []
    broker.place_order = lambda payload: submitted.append(payload)
    order = service.register(
        {
            "client_request_id": str(uuid4()),
            "symbol": "005930",
            "side": "buy",
            "quantity": 1,
            "order_type": "limit",
            "limit_price": "100",
        },
        decision_context={"origin": "automatic", "deployment_version": 1},
    )
    with pytest.raises(TradingBlocked, match="최종 실시간"):
        service.submit(order["id"])
    assert submitted == []
    assert service.repo.get_order(order["id"])["state"] == "registered"


def pending_order(runtime, symbol, side, quantity, price):
    intent = runtime.execution.register(
        {
            "client_request_id": str(uuid4()),
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "order_type": "limit",
            "limit_price": price,
        }
    )
    runtime.execution.submit(intent["id"])
    runtime.execution.reconcile()
    return runtime.store.get_order(intent["id"])


def test_amend_buy_can_reuse_its_own_nearly_full_cash_reservation(tmp_path):
    runtime = setup_runtime(tmp_path)
    order = pending_order(runtime, "005930", "buy", 99, "99")
    assert Decimal(runtime.broker.account()["buying_power"]) == 199
    changed = runtime.execution.cancel(order["id"], str(uuid4()), 99, "98")
    assert changed["submitted_price"] == "98"
    assert changed["command_state"] == "acknowledged"


def test_amend_buy_cannot_release_another_orders_reservation(tmp_path):
    runtime = setup_runtime(tmp_path)
    order = pending_order(runtime, "005930", "buy", 50, "90")
    pending_order(runtime, "000660", "buy", 50, "90")
    assert Decimal(runtime.broker.account()["buying_power"]) == 1000
    with pytest.raises(TradingBlocked, match="미수 없는"):
        runtime.execution.cancel(order["id"], str(uuid4()), 50, "120")


def test_amend_sell_can_reuse_only_its_reserved_quantity(tmp_path):
    runtime = setup_runtime(
        tmp_path,
        positions=[{"symbol": "005930", "quantity": 10, "average_price": "100"}],
        cash="9000",
    )
    first = pending_order(runtime, "005930", "sell", 5, "110")
    pending_order(runtime, "005930", "sell", 3, "110")
    assert runtime.broker.account()["positions"][0]["sellable_quantity"] == 2
    changed = runtime.execution.cancel(first["id"], str(uuid4()), 5, "109")
    assert changed["command_state"] == "acknowledged"
    assert runtime.broker.account()["positions"][0]["sellable_quantity"] == 2


def test_reconciliation_keeps_broker_fees_and_does_not_invent_missing_fees(tmp_path):
    runtime = setup_runtime(tmp_path)
    order = pending_order(runtime, "005930", "buy", 1, "100")
    assert Decimal(order["fee"]) == Decimal("0.02")
    original = runtime.broker.orders
    runtime.broker.orders = lambda start, end: [
        {key: value for key, value in row.items() if key != "fee"} for row in original(start, end)
    ]
    runtime.execution.reconcile()
    assert runtime.store.get_order(order["id"])["fee"] is None


def change_simulated_cash(runtime, amount):
    with runtime.store.connect() as con:
        row = con.execute(
            "SELECT payload FROM simulated_accounts WHERE namespace=?",
            (runtime.store.namespace,),
        ).fetchone()
        account = json.loads(row[0])
        account["cash"] = str(Decimal(account["cash"]) + Decimal(amount))
        con.execute(
            "UPDATE simulated_accounts SET payload=? WHERE namespace=?",
            (json.dumps(account), runtime.store.namespace),
        )


@pytest.mark.parametrize("cash_change", ["1000", "-2000"])
def test_cash_only_external_change_blocks_even_if_holdings_match(tmp_path, cash_change):
    runtime = setup_runtime(tmp_path)
    assert runtime.execution.reconcile()["financial"]["status"] == "baseline"
    previous = runtime.store.document("account")
    change_simulated_cash(runtime, cash_change)
    result = runtime.execution.reconcile()
    assert result["status"] == "mismatch"
    assert result["position_status"] == "matched"
    assert result["financial"]["difference"] == cash_change
    assert result["financial"]["cumulative_status"] == "mismatch"
    assert runtime.store.document("previous_account") == previous
    assert runtime.store.document("monetary_ledger")["account"] == previous
    assert runtime.execution.reconcile()["status"] == "mismatch"
    change_simulated_cash(runtime, str(-Decimal(cash_change)))
    assert runtime.execution.reconcile()["status"] == "mismatch"


def test_ui_account_refresh_cannot_erase_monetary_checkpoint(tmp_path):
    runtime = setup_runtime(tmp_path)
    runtime.execution.reconcile()
    change_simulated_cash(runtime, "500")
    runtime.account()  # account display can refresh separately from reconciliation
    result = runtime.execution.reconcile()
    assert result["financial"]["difference"] == "500"
    assert result["status"] == "mismatch"


def test_buy_sell_cash_changes_reconcile_actual_fills_and_fees_once(tmp_path):
    runtime = setup_runtime(tmp_path)
    bought = pending_order(runtime, "005930", "buy", 2, "100")
    first = runtime.store.document("reconciliation")["financial"]
    assert first["status"] == "matched"
    assert Decimal(first["fill_cash_delta"]) == -200
    assert Decimal(first["fee_delta"]) == Decimal(bought["fee"])
    sold = pending_order(runtime, "005930", "sell", 2, "100")
    second = runtime.store.document("reconciliation")["financial"]
    assert second["status"] == "matched"
    assert Decimal(second["fill_cash_delta"]) == 200
    assert Decimal(second["fee_delta"]) == Decimal(sold["fee"])
    repeated = runtime.execution.reconcile()["financial"]
    assert repeated["status"] == "matched"
    assert Decimal(repeated["fill_cash_delta"]) == Decimal(repeated["fee_delta"]) == 0


def test_missing_fill_fee_preserves_separate_financial_uncertainty(tmp_path):
    runtime = setup_runtime(tmp_path)
    original = runtime.broker.orders
    runtime.broker.orders = lambda start, end: [
        {key: value for key, value in row.items() if key != "fee"} for row in original(start, end)
    ]
    pending_order(runtime, "005930", "buy", 1, "100")
    result = runtime.store.document("reconciliation")
    assert result["status"] == result["position_status"] == "matched"
    assert result["financial"]["status"] == "unverified"
    assert result["financial"]["fee_delta"] is None
    assert result["financial"]["expected_economic_cash"] is None
    repeated = runtime.execution.reconcile()
    assert repeated["financial"]["status"] == "unverified"
    assert repeated["financial"]["cumulative_status"] == "unverified"


def test_pending_order_recovery_does_not_advance_monetary_anchor(tmp_path):
    runtime = setup_runtime(tmp_path)
    runtime.execution.reconcile()
    anchor = runtime.store.document("monetary_ledger")
    intent = runtime.execution.register(
        {
            "client_request_id": str(uuid4()),
            "symbol": "005930",
            "side": "buy",
            "quantity": 1,
            "order_type": "market",
            "limit_price": None,
        }
    )
    runtime.broker.timeout_after_accept = True
    runtime.execution.submit(intent["id"])
    original = runtime.broker.orders
    runtime.broker.orders = lambda *_: []
    result = runtime.execution.reconcile()
    assert result["financial"]["status"] == "unverified"
    assert runtime.store.document("monetary_ledger") == anchor
    runtime.broker.orders = original
    recovered = runtime.execution.reconcile()
    assert recovered["financial"]["status"] == "matched"
    assert Decimal(recovered["financial"]["fill_cash_delta"]) == -100


def test_fill_between_order_history_and_account_read_retries_money_comparison(tmp_path):
    runtime = setup_runtime(tmp_path)
    order = pending_order(runtime, "005930", "buy", 2, "90")
    anchor = runtime.store.document("monetary_ledger")
    original = runtime.broker.account

    def intervening_fill():
        runtime.broker.fill_order(order["broker_order_id"], 2)
        runtime.broker.account = original
        return original()

    runtime.broker.account = intervening_fill
    raced = runtime.execution.reconcile()
    assert raced["position_status"] == "mismatch"
    assert raced["financial"]["status"] == "unverified"
    assert runtime.store.document("monetary_ledger") == anchor
    recovered = runtime.execution.reconcile()
    assert recovered["status"] == "matched"
    assert recovered["financial"]["status"] == "matched"
    assert Decimal(recovered["financial"]["fill_cash_delta"]) == -180


def test_operator_cost_evidence_is_exact_fill_bound_and_broker_fee_wins(tmp_path):
    runtime = setup_runtime(tmp_path)
    order = pending_order(runtime, "005930", "buy", 1, "100")
    evidence = {
        "filled_quantity": 1,
        "filled_amount": "100.00",
        "fee": "5.00",
        "source": "fixture statement",
        "verified_by_user": True,
        "at": runtime.execution.at(),
    }
    runtime.store.put_document("order_cost", evidence, order["id"])
    runtime.execution.reconcile()
    assert runtime.store.get_order(order["id"])["fee"] == order["fee"]
    assert runtime.store.get_order(order["id"])["fee_source"] == "broker"
    original = runtime.broker.orders
    runtime.broker.orders = lambda start, end: [
        {key: value for key, value in row.items() if key != "fee"} for row in original(start, end)
    ]
    runtime.execution.reconcile()
    assert runtime.store.get_order(order["id"])["fee"] == "5.00"
    assert runtime.store.get_order(order["id"])["fee_source"] == "operator_evidence"
    runtime.store.put_document("order_cost", {**evidence, "filled_quantity": 2}, order["id"])
    runtime.execution.reconcile()
    assert runtime.store.get_order(order["id"])["fee"] is None
    assert runtime.store.get_order(order["id"])["fee_source"] is None
    runtime.store.put_document("order_cost", {**evidence, "filled_amount": "101"}, order["id"])
    runtime.execution.reconcile()
    assert runtime.store.get_order(order["id"])["fee"] is None


def test_late_cost_evidence_verifies_original_cash_change_without_double_fee(tmp_path):
    runtime = setup_runtime(tmp_path)
    original = runtime.broker.orders
    runtime.broker.orders = lambda start, end: [
        {key: value for key, value in row.items() if key != "fee"} for row in original(start, end)
    ]
    order = pending_order(runtime, "005930", "buy", 1, "100")
    assert runtime.store.document("reconciliation")["financial"]["status"] == "unverified"
    assert Decimal(runtime.store.document("monetary_ledger")["account"]["cash"]) == 10000
    runtime.store.put_document(
        "order_cost",
        {
            "filled_quantity": 1,
            "filled_amount": "100",
            "fee": "0.02",
            "source": "fixture exact statement",
            "verified_by_user": True,
            "at": runtime.execution.at(),
        },
        order["id"],
    )
    result = runtime.execution.reconcile()["financial"]
    assert result["status"] == "matched"
    assert result["cumulative_status"] == "verified"
    assert Decimal(result["fill_cash_delta"]) == -100
    assert Decimal(result["fee_delta"]) == Decimal("0.02")
    assert Decimal(result["difference"]) == 0
    result = runtime.execution.reconcile()["financial"]
    assert result["status"] == "matched"
    assert Decimal(result["fee_delta"]) == 0
