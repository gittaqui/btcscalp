"""Fault injection around the non-atomic native-stop to IOC transition.

The venue below owns its own orders, cash and inventory. The real LiveBroker,
Controller, risk gate and SQLite ledger must recover that external state.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import NOW, order
from trader.exchange.base import ExchangeError, Rejected, UnknownSubmission
from trader.execution import Controller, LiveBroker
from trader.models import SECOND, ZERO, Account, D, Fill
from trader.portfolio import Portfolio
from trader.risk import RiskEngine, RiskRejected
from trader.runtime import process_commands


class FaultVenue:
    def __init__(self, clock):
        self.clock = clock
        self.orders = {}
        self.submitted = []
        self.cancelled = []
        self.ioc_outcome = "fill"
        self.balance_failures = 0
        self.cancel_delay_ns = 0
        self.ioc_status_unavailable = False
        self.cash = D("10000")
        self.btc = ZERO

    def accept(self, o, executed=ZERO):
        identity = str(len(self.orders) + 1)
        row = {
            "client_order_id": o.client_id,
            "symbol": "btcusd",
            "side": o.side,
            "original_amount": str(o.quantity),
            "order_id": identity,
            "executed_amount": str(executed),
            "is_live": o.kind != "ioc" and executed < o.quantity,
            "is_cancelled": o.kind == "ioc" and executed < o.quantity,
            "trades": [],
            "kind": o.kind,
        }
        if executed:
            price = D("60000") if o.side == "buy" else D("59999")
            fee = price * executed * D("0.001")
            sign = 1 if o.side == "buy" else -1
            self.btc += sign * executed
            self.cash -= sign * price * executed + fee
            row["trades"].append(
                {
                    "tid": identity,
                    "order_id": identity,
                    "timestampms": self.clock[0] // 1_000_000,
                    "price": str(price),
                    "amount": str(executed),
                    "fee_currency": "USD",
                    "fee_amount": str(fee),
                    "aggressor": o.kind == "ioc",
                }
            )
        self.orders[o.client_id] = row
        return row

    async def submit(self, o):
        self.submitted.append(o.model_copy(deep=True))
        if o.kind == "ioc":
            if self.ioc_outcome == "reject":
                raise Rejected("INJECTED_REJECTION")
            amount = D("0.0004") if self.ioc_outcome == "partial_timeout" else o.quantity
            row = self.accept(o, amount)
            if self.ioc_outcome == "partial_timeout":
                raise UnknownSubmission("INJECTED_LOST_RESPONSE")
            return row
        return self.accept(o)

    async def status(self, client_id):
        row = self.orders[client_id]
        if self.ioc_status_unavailable and row["kind"] == "ioc":
            raise ExchangeError("INJECTED_STATUS_OUTAGE")
        return row

    async def cancel(self, exchange_id):
        row = next(row for row in self.orders.values() if row["order_id"] == exchange_id)
        row["is_live"], row["is_cancelled"] = False, True
        self.cancelled.append(row["client_order_id"])
        self.clock[0] += self.cancel_delay_ns
        return row

    async def open_orders(self):
        return [row for row in self.orders.values() if row["is_live"]]

    async def past_trades(self, since):
        return [trade for row in self.orders.values() for trade in row["trades"]]

    async def balances(self):
        if self.cancelled and self.balance_failures:
            self.balance_failures -= 1
            raise ExchangeError("INJECTED_BALANCE_OUTAGE")
        reserved = sum(
            (
                D(row["original_amount"]) - D(row["executed_amount"])
                for row in self.orders.values()
                if row["is_live"] and row["side"] == "sell"
            ),
            ZERO,
        )
        return Account(
            usd=self.cash,
            available_usd=self.cash,
            btc=self.btc,
            available_btc=self.btc - reserved,
            asof_ns=self.clock[0],
        )


@pytest.fixture
async def protected_position(cfg, store, fees, book, instrument, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr("trader.execution.time.time_ns", lambda: clock[0])
    api = FaultVenue(clock)
    broker = LiveBroker(cfg, store, api, api)
    store.set("initial_live_usd", "10000")
    entry = order()
    store.put_order(entry, new=True)
    broker.apply_status(entry, api.accept(entry, entry.quantity))
    await broker.reconcile()
    controller = Controller(cfg, store, broker, instrument, fees, book)
    await controller.protect(NOW)
    assert sum(o.remaining for o in store.orders(True)) == D("0.001")
    return SimpleNamespace(api=api, broker=broker, controller=controller, clock=clock, store=store)


def assert_protection(state, quantity):
    stops = [o for o in state.store.orders(True) if o.kind == "stop"]
    assert sum((o.remaining for o in stops), ZERO) == D(quantity)
    assert all(o.status == "open" for o in stops)
    assert state.broker.portfolio().quantity == state.api.btc == D(quantity)


async def test_rejected_ioc_restores_native_stop_and_latches_kill(protected_position):
    state = protected_position
    state.api.ioc_outcome = "reject"
    with pytest.raises(Rejected):
        await state.controller.flatten(NOW)
    assert_protection(state, "0.001")
    assert state.store.get("killed")
    assert state.store.get("flatten_requested")
    assert state.store.get("protection_recovery")["status"] == "protected"
    assert len([o for o in state.api.submitted if o.kind == "ioc"]) == 1


async def test_lost_ioc_response_reconciles_partial_fill_without_resubmission(protected_position):
    state = protected_position
    state.api.ioc_outcome = "partial_timeout"
    with pytest.raises(UnknownSubmission):
        await state.controller.flatten(NOW)
    assert_protection(state, "0.0006")
    assert len(state.store.fills()) == 2
    assert len([o for o in state.api.submitted if o.kind == "ioc"]) == 1
    assert state.store.get("protection_recovery")["status"] == "protected"


async def test_unknown_ioc_cannot_be_covered_using_cached_inventory(protected_position):
    state = protected_position
    state.api.ioc_outcome = "partial_timeout"
    state.api.ioc_status_unavailable = True
    with pytest.raises(UnknownSubmission):
        await state.controller.flatten(NOW)
    assert state.store.get("protection_recovery")["status"] == "operator_required"
    assert not [o for o in state.store.orders(True) if o.kind == "stop"]
    assert len([o for o in state.api.submitted if o.kind == "ioc"]) == 1
    # Even a direct protection request must refuse the unresolved sale.
    with pytest.raises(ExchangeError, match="UNRESOLVED_EXIT"):
        await state.controller.protect(NOW)


@pytest.mark.parametrize("failures,expected", [(1, "protected"), (100, "operator_required")])
async def test_balance_outage_after_stop_cancel_has_explicit_recovery(protected_position, failures, expected):
    state = protected_position
    state.api.balance_failures = failures
    with pytest.raises(ExchangeError, match="BALANCE_OUTAGE"):
        await state.controller.flatten(NOW)
    assert state.store.get("killed")
    assert state.store.get("protection_recovery")["status"] == expected
    assert not [o for o in state.api.submitted if o.kind == "ioc"]
    if expected == "protected":
        assert_protection(state, "0.001")
    else:
        assert state.store.events("risk")[-1]["reason"] == "PROTECTION_RECOVERY_UNCONFIRMED"


async def test_stop_cancel_that_outlasts_book_freshness_rearms_without_ioc(protected_position):
    state = protected_position
    state.api.cancel_delay_ns = 3 * SECOND
    await state.controller.flatten(NOW)
    assert_protection(state, "0.001")
    assert not [o for o in state.api.submitted if o.kind == "ioc"]


async def test_successful_reduction_finishes_flat_without_rearming(protected_position):
    state = protected_position
    await state.controller.flatten(NOW)
    assert_protection(state, "0")
    assert not state.store.orders(True)
    assert len(state.broker.portfolio().trades) == 1


async def test_cancel_all_attempts_remaining_orders_after_one_failure(cfg, store, book, instrument, fees):
    broker = AsyncMock()
    broker.cancel.side_effect = [ExchangeError("FIRST_CANCEL_FAILED"), None]
    store.put_order(order(client_id="first", status="open"), new=True)
    store.put_order(order(client_id="second", status="open"), new=True)
    controller = Controller(cfg, store, broker, instrument, fees, book)
    with pytest.raises(ExchangeError, match="CANCELLATION_INCOMPLETE"):
        await controller.cancel_all()
    assert broker.cancel.await_count == 2


@pytest.mark.parametrize("failure", ["stale_book", "stale_account", "price", "pending_sell"])
def test_exit_gate_rejects_stale_or_overlapping_reductions(cfg, store, book, instrument, account, failure):
    account.btc = account.available_btc = D("0.001")
    portfolio = Portfolio(quantity=D("0.001"))
    o, now = order("sell"), NOW
    if failure == "stale_book":
        now += 3 * SECOND
    elif failure == "stale_account":
        account.asof_ns -= 60 * SECOND
    elif failure == "price":
        o.price = D("50000")
    else:
        store.put_order(order("sell", client_id="unresolved", status="unknown"), new=True)
    with pytest.raises(RiskRejected):
        RiskEngine(cfg, store).authorize(o, account, portfolio, instrument, book, now)


@pytest.mark.parametrize("field,value", [("fee", D("1")), ("price", D("59990"))])
def test_conflicting_duplicate_fill_never_rewrites_accounting(store, field, value):
    store.put_order(order(), new=True)
    fill = Fill(
        fill_id="exchange-fill-1",
        client_id="test-order",
        ts_ns=NOW,
        side="buy",
        price="60000",
        quantity="0.001",
        fee="0.06",
        maker=True,
        reference_price="60000.5",
        latency_ms=0,
    )
    assert store.fill(fill)
    assert not store.fill(fill.model_copy())
    changed = fill.model_copy(update={field: value})
    with pytest.raises(ValueError, match="CONFLICTING_DUPLICATE_FILL"):
        store.fill(changed)
    assert store.fills() == [fill]


def test_order_exchange_identity_cannot_change(cfg, store):
    from tests.test_execution import status_payload

    broker = LiveBroker(cfg, store, AsyncMock(), AsyncMock())
    o = order(exchange_id="original-exchange-id")
    store.put_order(o, new=True)
    with pytest.raises(ExchangeError, match="ORDER_IDENTITY_MISMATCH"):
        broker.apply_status(o, status_payload())
    assert store.order(o.client_id).exchange_id == "original-exchange-id"


@pytest.fixture
def control_engine(cfg, store, book, monkeypatch):
    monkeypatch.setattr("trader.runtime.time.time_ns", lambda: NOW)
    broker = SimpleNamespace(reconcile=AsyncMock(), portfolio=lambda: Portfolio())
    return SimpleNamespace(config=cfg, store=store, book=book, broker=broker, controller=AsyncMock())


@pytest.mark.parametrize("command", ["resume", "reset-kill"])
@pytest.mark.parametrize("timing", ["already_queued", "during_reconcile"])
async def test_newer_kill_supersedes_resume_and_reset(control_engine, command, timing):
    engine = control_engine
    engine.store.set("paused", True)
    if command == "reset-kill":
        engine.store.kill("ORIGINAL_STOP", NOW)
    identity = engine.store.command(command)
    if timing == "already_queued":
        engine.store.command("kill")
    else:
        engine.broker.reconcile.side_effect = lambda: engine.store.command("kill")
    await process_commands(engine)
    row = engine.store.db.execute("SELECT * FROM commands WHERE id=?", (identity,)).fetchone()
    assert row["status"] == "failed"
    assert engine.store.get("killed")["reason"] == "OPERATOR_KILL"
    assert engine.store.get("paused")


async def test_newer_pause_during_reconcile_cannot_be_cleared(control_engine):
    engine = control_engine
    engine.store.set("paused", True)
    engine.store.command("resume")
    engine.broker.reconcile.side_effect = lambda: engine.store.command("pause")
    await process_commands(engine)
    assert engine.store.get("paused")


@pytest.mark.parametrize("command", ["resume", "reset-kill"])
async def test_new_risk_kill_during_reconciliation_supersedes_control(control_engine, command):
    engine = control_engine
    engine.store.set("paused", True)
    engine.store.command(command)
    engine.broker.reconcile.side_effect = lambda: engine.store.kill("NEW_RISK_EVENT", NOW)
    await process_commands(engine)
    assert engine.store.get("killed")["reason"] == "NEW_RISK_EVENT"
    assert engine.store.get("paused")


async def test_stop_from_separate_database_connection_wins(control_engine, tmp_path):
    from trader.storage.db import Store

    runtime = Store(str(tmp_path / "controls.sqlite"))
    dashboard = Store(str(tmp_path / "controls.sqlite"))
    try:
        control_engine.store = runtime
        runtime.set("paused", True)
        runtime.command("resume")
        control_engine.broker.reconcile.side_effect = lambda: dashboard.command("kill")
        await process_commands(control_engine)
        assert runtime.get("killed")["reason"] == "OPERATOR_KILL"
        assert runtime.get("paused")
    finally:
        dashboard.close()
        runtime.close()


async def test_resume_rechecks_freshness_after_network_await(control_engine):
    engine = control_engine
    engine.store.set("paused", True)
    engine.store.command("resume")
    engine.broker.reconcile.side_effect = lambda: setattr(engine.book, "last_recv_ns", NOW - 3 * SECOND)
    await process_commands(engine)
    assert engine.store.get("paused")


async def test_kill_failure_leaves_durable_flatten_request(control_engine):
    engine = control_engine
    engine.store.command("kill")
    engine.controller.cancel_all.side_effect = ExchangeError("OUTAGE")
    await process_commands(engine)
    assert engine.store.get("killed")
    assert engine.store.get("flatten_requested")


async def test_deliberate_reset_then_resume_still_works(control_engine):
    engine = control_engine
    engine.store.kill("RESOLVED_FAULT", NOW)
    engine.store.command("reset-kill")
    await process_commands(engine)
    assert engine.store.get("killed") is None
    assert engine.store.get("paused")
    engine.store.command("resume")
    await process_commands(engine)
    assert not engine.store.get("paused")
