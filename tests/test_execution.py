from unittest.mock import AsyncMock

import pytest

from tests.conftest import NOW, order
from trader.exchange.base import ExchangeError, UnknownSubmission
from trader.execution import Controller, LiveBroker
from trader.models import SECOND, D, Event
from trader.paper import PaperBroker


def trade(time_ms=500, quantity="3", price="60000", identity="1", side="sell"):
    return Event(
        kind="trade",
        ts_ns=NOW + time_ms * 1_000_000,
        recv_ns=NOW + time_ms * 1_000_000,
        price=price,
        quantity=quantity,
        aggressor=side,
        trade_id=identity,
    )


async def test_touch_without_trade_never_fills(cfg, store, fees, book):
    broker = PaperBroker(cfg, store, fees, book)
    await broker.submit(order())
    broker.advance(
        Event(
            kind="delta",
            ts_ns=NOW + SECOND,
            recv_ns=NOW + SECOND,
            first=11,
            last=11,
            bids=[("60000", "0.001")],
        )
    )
    assert not store.fills()


async def test_latency_queue_partial_duplicate_cancel(cfg, store, fees, book):
    cfg.paper_starting_usd = D("100000")
    broker = PaperBroker(cfg, store, fees, book)
    await broker.submit(order(quantity=D("1")))
    broker.advance(trade(100, "10", identity="early"))
    assert not store.fills()
    broker.advance(trade(500, "2.4", identity="partial"))
    assert broker.portfolio().quantity == D("0.4")
    broker.advance(trade(500, "2.4", identity="partial"))
    assert broker.portfolio().quantity == D("0.4")
    await broker.cancel(store.order("test-order"))
    broker.advance(trade(600, "0.1", identity="race"))
    assert broker.portfolio().quantity == D("0.5")
    broker.advance(trade(800, "10", identity="after-cancel"))
    assert broker.portfolio().quantity == D("0.5")
    assert store.order("test-order").status == "cancelled"


async def test_zero_fill_and_duplicate_submission(cfg, store, fees, book):
    broker = PaperBroker(cfg, store, fees, book)
    await broker.submit(order())
    await broker.submit(order())
    assert len(store.orders()) == 1
    broker.advance(trade(500, "1"))
    assert not store.fills()


async def test_ioc_partial_cancels_remainder_and_no_double_count_slippage(cfg, store, fees, book):
    broker = PaperBroker(cfg, store, fees, book)
    await broker.submit(order(quantity=D("0.01")))
    broker.advance(trade(500, "3"))
    # Smaller displayed exit depth produces a partial sale, never a fabricated full fill.
    book.bids[D("60000")] = D("0.003")
    exit_order = order(
        "sell", client_id="exit", created_ns=NOW + SECOND, quantity=D("0.01"), price=D("59900")
    )
    await broker.submit(exit_order)
    broker.advance(trade(1500, "0.001", identity="exit-event"))
    assert store.order("exit").filled == D("0.003")
    assert store.order("exit").status == "cancelled"
    assert broker.portfolio().quantity == D("0.007")


async def test_maker_crossing_is_cancelled(cfg, store, fees, book):
    broker = PaperBroker(cfg, store, fees, book)
    await broker.submit(order(price=D("60001")))
    broker.advance(trade())
    assert store.order("test-order").status == "cancelled"
    assert not store.fills()


@pytest.mark.parametrize("latency", [500, 2000, 10000])
async def test_delayed_orders_cannot_fill_early(cfg, store, fees, book, latency):
    cfg.execution.simulated_latency_ms = latency
    broker = PaperBroker(cfg, store, fees, book)
    await broker.submit(order())
    broker.advance(trade(latency - 1, "100"))
    assert not store.fills()


def status_payload(filled="0", trades=None, live=False, cancelled=True):
    return {
        "client_order_id": "test-order",
        "symbol": "btcusd",
        "side": "buy",
        "original_amount": "0.001",
        "order_id": "123",
        "executed_amount": filled,
        "is_live": live,
        "is_cancelled": cancelled,
        "trades": trades or [],
    }


async def test_unknown_submit_never_retries(cfg, store):
    api = AsyncMock()
    api.submit.side_effect = UnknownSubmission("timeout")
    api.status.return_value = status_payload()
    broker = LiveBroker(cfg, store, api, api)
    with pytest.raises(UnknownSubmission):
        await broker.submit(order())
    assert store.order("test-order").status == "unknown"
    assert store.get("killed")
    await broker.submit(order())
    assert api.submit.await_count == 1
    assert api.status.await_count == 1


def test_status_cancelled_can_be_fully_filled_idempotently(cfg, store):
    api = AsyncMock()
    broker = LiveBroker(cfg, store, api, api)
    o = order()
    store.put_order(o, new=True)
    fill = {
        "tid": 33,
        "timestampms": NOW // 1_000_000 + 1,
        "price": "60000",
        "amount": "0.001",
        "fee_currency": "USD",
        "fee_amount": "0.06",
        "aggressor": False,
    }
    response = status_payload("0.001", [fill])
    broker.apply_status(o, response)
    broker.apply_status(store.order(o.client_id), response)
    assert len(store.fills()) == 1
    assert broker.portfolio().quantity == D("0.001")


def test_incomplete_or_broken_fills_abort_transaction(cfg, store):
    api = AsyncMock()
    broker = LiveBroker(cfg, store, api, api)
    store.put_order(order(), new=True)
    with pytest.raises(ExchangeError, match="INCOMPLETE_FILL"):
        broker.apply_status(order(), status_payload("0.001"))
    assert store.order("test-order").filled == 0


async def test_balance_mismatch_halts_reconciliation(cfg, store, account):
    api = AsyncMock()
    api.open_orders.return_value = []
    api.past_trades.return_value = []
    account.btc = D("1")
    api.balances.return_value = account
    broker = LiveBroker(cfg, store, api, api)
    with pytest.raises(ExchangeError, match="INVENTORY"):
        await broker.reconcile()


async def test_controller_requires_risk_even_for_paper(cfg, store, fees, book, instrument):
    broker = PaperBroker(cfg, store, fees, book)
    broker.now = NOW
    control = Controller(cfg, store, broker, instrument, fees, book)
    with pytest.raises(RuntimeError, match="DECISION"):
        await control.place(order())
    assert store.orders() == []
