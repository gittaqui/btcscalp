"""Official Gemini v3 streams, normalized event files and provenance checks."""

import asyncio
import hashlib
import json
import random
import time
from pathlib import Path

from websockets.asyncio.client import connect

from trader.config import Config
from trader.models import Event
from trader.orderbook import OrderBook


def normalize(message: dict, received_ns: int, first_depth: bool) -> Event | None:
    # Some gateways wrap stream messages; both documented event shapes are supported.
    data = message.get("data", message)
    symbol = str(data.get("s", "btcusd")).lower()
    if symbol != "btcusd":
        raise ValueError("UNEXPECTED_SYMBOL")
    if data.get("e") == "depthUpdate":
        return Event(
            kind="snapshot" if first_depth else "delta",
            ts_ns=int(data["E"]),
            recv_ns=received_ns,
            first=int(data["U"]),
            last=int(data["u"]),
            bids=data["b"],
            asks=data["a"],
        )
    if all(key in data for key in ("t", "p", "q", "m", "E")):
        if not isinstance(data["m"], bool):
            raise ValueError("INVALID_MAKER_FLAG")
        return Event(
            kind="trade",
            ts_ns=int(data["E"]),
            recv_ns=received_ns,
            price=data["p"],
            quantity=data["q"],
            trade_id=str(data["t"]),
            aggressor="sell" if data["m"] else "buy",
        )
    return None


async def market_stream(config: Config, shutdown: asyncio.Event):
    """Reconnect always starts with a full snapshot. Loss is explicit to all consumers."""
    attempt = 0
    while not shutdown.is_set():
        try:
            async with connect(
                config.exchange.ws_url + "?snapshot=-1",
                open_timeout=10,
                ping_interval=10,
                ping_timeout=10,
                max_queue=64,
                max_size=8_000_000,
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "id": "market",
                            "method": "subscribe",
                            "params": ["btcusd@depth@100ms", "btcusd@trade"],
                        }
                    )
                )
                first, acknowledged = True, False
                guard = OrderBook()
                while not shutdown.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(3, config.risk.stale_data_ms / 1000))
                    now = time.time_ns()
                    message = json.loads(raw)
                    if message.get("id") == "market":
                        if message.get("status") != 200:
                            raise RuntimeError("SUBSCRIPTION_REJECTED")
                        acknowledged = True
                        continue
                    event = normalize(message, now, first)
                    if event is None:
                        continue
                    if abs(now - event.ts_ns) > config.risk.maximum_clock_drift_ms * 1_000_000:
                        raise RuntimeError("CLOCK_DRIFT_OR_DELAYED_FEED")
                    if event.kind in {"snapshot", "delta"}:
                        guard.apply(event)
                        first = False
                    if not acknowledged:
                        raise RuntimeError("DATA_BEFORE_SUBSCRIPTION_ACKNOWLEDGEMENT")
                    attempt = 0
                    yield event
        except asyncio.CancelledError:
            raise
        except Exception:
            # Details are intentionally not copied from network exceptions: URLs may contain secrets.
            now = time.time_ns()
            yield Event(kind="disconnect", ts_ns=now, recv_ns=now)
            attempt += 1
            try:
                await asyncio.wait_for(
                    shutdown.wait(), timeout=min(30, 2 ** min(attempt, 5)) + random.random()
                )
            except TimeoutError:
                pass


def file_hash(path: str | Path) -> str:
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


class Recorder:
    def __init__(self, path: str, config: Config, source="gemini-production"):
        self.path, self.config, self.source = Path(path), config, source
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("x")
        self.count = 0
        self.start = None
        self.end = None

    def write(self, event: Event):
        self.stream.write(event.model_dump_json() + "\n")
        self.count += 1
        self.start = self.start if self.start is not None else event.recv_ns
        self.end = event.recv_ns
        if self.count % 100 == 0:
            self.stream.flush()

    def close(self):
        self.stream.close()
        manifest = {
            "schema": 1,
            "source": self.source,
            "symbol": "btcusd",
            "events": self.count,
            "start_ns": self.start,
            "end_ns": self.end,
            "sha256": file_hash(self.path),
            "environment": self.config.exchange.environment,
            "synthetic": self.source == "synthetic",
        }
        self.path.with_suffix(self.path.suffix + ".manifest.json").write_text(json.dumps(manifest, indent=2))


def read_events(path: str | Path):
    """Replay local receipt order. Sorting a damaged tape would conceal data defects."""
    path = Path(path)
    last = -1
    if path.suffix == ".parquet":
        import pyarrow.parquet as pq

        def rows():
            for batch in pq.ParquetFile(path).iter_batches(batch_size=4096, columns=["event_json"]):
                yield from batch.column(0).to_pylist()

        source = rows()
    else:
        source = path.open()
    try:
        for line in source:
            event = Event.model_validate_json(line)
            if event.recv_ns < last:
                raise ValueError("Tape is not chronological")
            last = event.recv_ns
            yield event
    finally:
        if hasattr(source, "close"):
            source.close()


def to_parquet(source: str, output: str):
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = pa.schema([("recv_ns", pa.int64()), ("event_json", pa.string())])
    with pq.ParquetWriter(output, schema, compression="zstd") as writer:
        batch = []
        for event in read_events(source):
            batch.append({"recv_ns": event.recv_ns, "event_json": event.model_dump_json()})
            if len(batch) >= 4096:
                writer.write_table(pa.Table.from_pylist(batch, schema=schema))
                batch = []
        if batch:
            writer.write_table(pa.Table.from_pylist(batch, schema=schema))
    original = json.loads(Path(source + ".manifest.json").read_text())
    original.update({"parent_sha256": file_hash(source), "sha256": file_hash(output)})
    Path(output + ".manifest.json").write_text(json.dumps(original, indent=2))
