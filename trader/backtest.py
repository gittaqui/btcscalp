"""Tick/order-book event replay with a separate fill simulator and a closing window."""

import json
from pathlib import Path

from trader.config import Config
from trader.engine import Engine
from trader.market_data import read_events
from trader.models import SECOND, D, Fees, Instrument
from trader.orderbook import OrderBook
from trader.paper import PaperBroker
from trader.reporting import report, robustness
from trader.storage.db import Store
from trader.strategy import EdgeModel


def configured_fees(config, now=0):
    if config.fees.maker_bps is None or config.fees.taker_bps is None or not config.fees.source_note.strip():
        raise ValueError(
            "Set both actual account fee rates and fees.source_note, or use authenticated fee discovery"
        )
    return Fees(
        maker_bps=config.fees.maker_bps,
        taker_bps=config.fees.taker_bps,
        source=config.fees.source_note,
        fetched_ns=now,
    )


def tape_instrument(path):
    manifest = json.loads(Path(str(path) + ".manifest.json").read_text())
    if "instrument" not in manifest:
        raise ValueError("Tape manifest needs recorded exchange instrument increments")
    return Instrument.model_validate(manifest["instrument"])


async def replay(
    config: Config, events, instrument, model=None, fees=None, start_ns=0, end_ns=None, db=":memory:"
):
    store = Store(db)
    if store.orders() or store.events("equity", limit=1):
        store.close()
        raise ValueError("Backtest output database must be new")
    store.set("mode", "backtest")
    store.set("starting_equity", str(config.paper_starting_usd))
    book = OrderBook()
    broker = PaperBroker(config, store, fees or configured_fees(config), book)
    engine = Engine(config, store, broker, book, instrument, broker.fees, model or EdgeModel())
    closing_ns = (config.risk.time_stop_seconds + config.execution.simulated_latency_ms / 1000 + 5) * SECOND
    last = None
    try:
        for event in events:
            if end_ns is not None and event.recv_ns >= end_ns:
                break
            # Earlier observations warm the causal feature engine; never enter before the partition.
            engine.allow_entries = event.recv_ns >= start_ns and (
                end_ns is None or event.recv_ns < end_ns - closing_ns
            )
            if event.recv_ns >= start_ns and end_ns and event.recv_ns >= end_ns - 3 * SECOND:
                store.set("flatten_requested", True)
            await engine.event(event)
            last = event
        if last and book.valid:
            store.event(
                "equity",
                {"equity": str(broker.account().equity(book.bid)), "btc": str(broker.portfolio().quantity)},
                last.recv_ns,
            )
        result = report(store)
        result["robustness"] = robustness(result, broker.portfolio().trades)
        result["execution_assumptions"] = config.execution.model_dump()
        result["fees_used"] = broker.fees.model_dump(mode="json")
        result["config_fingerprint"] = config.fingerprint()
        return result, broker.portfolio().trades
    finally:
        store.close()


async def run_backtest(config, path, output, db=None):
    instrument = tape_instrument(path)
    model = EdgeModel.load(config.strategy.model_path)
    manifest = json.loads(Path(str(path) + ".manifest.json").read_text())
    result, _ = await replay(
        config, read_events(path), instrument, model, end_ns=manifest["end_ns"] + 1, db=db or ":memory:"
    )
    result["data_source"] = manifest
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(json.dumps(result, indent=2))
    return result


def synthetic_events(count=3000):
    """Deterministic engineering fixture; never admissible as evidence of an edge."""
    from trader.models import Event

    base = 1_700_000_000 * SECOND
    price = D("60000")
    previous_bid = previous_ask = None
    for index in range(count):
        now = base + index * SECOND
        movement = D(((index // 30) % 8) - 4) / 10
        price += movement
        bid, ask = price - D("0.5"), price + D("0.5")
        bids, asks = [(bid, D("0.5"))], [(ask, D("0.4"))]
        if index and previous_bid != bid:
            bids.insert(0, (previous_bid, D("0")))
        if index and previous_ask != ask:
            asks.insert(0, (previous_ask, D("0")))
        yield Event(
            kind="snapshot" if not index else "delta",
            ts_ns=now,
            recv_ns=now + 1_000_000,
            first=index,
            last=index,
            bids=bids,
            asks=asks,
        )
        yield Event(
            kind="trade",
            ts_ns=now + 10_000_000,
            recv_ns=now + 11_000_000,
            price=bid if index % 2 else ask,
            quantity=D("0.02"),
            aggressor="sell" if index % 2 else "buy",
            trade_id=str(index),
        )
        previous_bid, previous_ask = bid, ask
