import pytest

from trader.config import Config
from trader.models import SECOND, Account, D, Decision, Event, Fees, Instrument, Order
from trader.orderbook import OrderBook
from trader.storage.db import Store

NOW = 1_700_000_000 * SECOND


@pytest.fixture
def cfg():
    c = Config()
    c.fees.maker_bps, c.fees.taker_bps = D("10"), D("20")
    c.fees.source_note = "TEST FIXTURE, NOT CURRENT ACCOUNT RATES"
    c.frequency.frequency_mode = "custom"
    c.frequency.minimum_seconds_between_entries = 0
    c.frequency.maximum_trades_per_hour = 30
    c.frequency.maximum_trades_per_day = 240
    c.execution.simulated_latency_ms = 250
    c.execution.cancel_latency_ms = 250
    c.execution.queue_multiplier = 1
    c.execution.participation_fraction = 1
    c.database.record_market_events = False
    return c


@pytest.fixture
def store():
    value = Store(":memory:")
    yield value
    value.close()


@pytest.fixture
def book():
    value = OrderBook()
    value.apply(
        Event(
            kind="snapshot",
            ts_ns=NOW,
            recv_ns=NOW,
            first=10,
            last=10,
            bids=[("60000", "2")],
            asks=[("60001", "2")],
        )
    )
    return value


@pytest.fixture
def instrument():
    return Instrument(price_increment="0.01", quantity_increment="0.00000001", minimum_quantity="0.00001")


@pytest.fixture
def fees():
    return Fees(maker_bps="10", taker_bps="20", source="test", fetched_ns=NOW)


@pytest.fixture
def account():
    return Account(usd="10000", available_usd="10000", asof_ns=NOW)


@pytest.fixture
def decision():
    return Decision(
        ts_ns=NOW,
        regime="RANGING",
        features={"volatility_30s_bps": 1},
        confidence=0.99,
        expected_net_edge_bps=100,
        decision="BUY",
        costs={"entry_fee_bps": 10},
    )


def order(side="buy", **kwargs):
    values = dict(
        client_id="test-order",
        side=side,
        kind="maker" if side == "buy" else "ioc",
        quantity=D("0.001"),
        price=D("60000"),
        reference_price=D("60000.5"),
        created_ns=NOW,
    )
    values.update(kwargs)
    return Order(**values)
