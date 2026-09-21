import pytest

from tests.conftest import NOW
from trader.market_data import normalize, read_events
from trader.models import D, Event
from trader.orderbook import BookError


def test_v3_initial_snapshot_and_delta(book):
    msg = {
        "e": "depthUpdate",
        "E": NOW + 1,
        "s": "BTCUSD",
        "U": 11,
        "u": 12,
        "b": [["60000", "0"], ["59999", "3"]],
        "a": [],
    }
    event = normalize(msg, NOW + 100, False)
    assert book.apply(event)
    assert book.bid == D("59999") and book.sequence == 12


def test_gap_invalidates_until_snapshot(book):
    with pytest.raises(BookError, match="SEQUENCE_GAP"):
        book.apply(Event(kind="delta", ts_ns=NOW + 1, recv_ns=NOW + 1, first=13, last=14))
    assert not book.valid
    with pytest.raises(BookError, match="SNAPSHOT_REQUIRED"):
        book.apply(Event(kind="delta", ts_ns=NOW + 2, recv_ns=NOW + 2, first=15, last=15))
    book.apply(
        Event(
            kind="snapshot",
            ts_ns=NOW + 3,
            recv_ns=NOW + 3,
            first=30,
            last=30,
            bids=[("10", "1")],
            asks=[("11", "1")],
        )
    )
    assert book.valid


def test_duplicate_does_not_refresh_stale_clock(book):
    assert not book.apply(Event(kind="delta", ts_ns=NOW + 999, recv_ns=NOW + 5_000_000_000, first=9, last=10))
    assert book.last_recv_ns == NOW
    assert book.stale(NOW + 5_000_000_000, 1000)


@pytest.mark.parametrize(
    "bids,asks", [([("60002", "1")], []), ([("60000", "-1")], []), ([("60000", "0")], [])]
)
def test_invalid_or_crossed_book_fails_closed(book, bids, asks):
    with pytest.raises(BookError):
        book.apply(
            Event(kind="delta", ts_ns=NOW + 1, recv_ns=NOW + 1, first=11, last=11, bids=bids, asks=asks)
        )
    assert not book.valid


def test_old_event_time_stops_book(book):
    with pytest.raises(BookError, match="TIME_REVERSAL"):
        book.apply(Event(kind="delta", ts_ns=NOW - 1, recv_ns=NOW + 1, first=11, last=11))


def test_trade_aggressor_direction_and_nan_rejection():
    raw = {"E": NOW, "s": "btcusd", "t": 12, "p": "60000", "q": "0.1", "m": True}
    assert normalize(raw, NOW + 1, False).aggressor == "sell"
    raw["m"] = False
    assert normalize(raw, NOW + 1, False).aggressor == "buy"
    raw["p"] = "NaN"
    with pytest.raises(ValueError):
        normalize(raw, NOW, False)


def test_wrong_symbol_and_unknown_event():
    with pytest.raises(ValueError, match="UNEXPECTED_SYMBOL"):
        normalize({"s": "ethusd"}, NOW, False)
    assert normalize({"e": "new_future_event"}, NOW, False) is None


def test_replay_rejects_out_of_order_tape(tmp_path):
    path = tmp_path / "tape.jsonl"
    events = [Event(kind="disconnect", ts_ns=t, recv_ns=t) for t in [NOW + 1, NOW]]
    path.write_text("\n".join(e.model_dump_json() for e in events))
    with pytest.raises(ValueError, match="chronological"):
        list(read_events(path))


def test_snapshot_on_reconnect_replaces_every_level(book):
    book.apply(Event(kind="disconnect", ts_ns=NOW + 1, recv_ns=NOW + 1))
    book.apply(
        Event(
            kind="snapshot",
            ts_ns=NOW + 2,
            recv_ns=NOW + 2,
            first=1,
            last=1,
            bids=[("10", "3")],
            asks=[("11", "2")],
        )
    )
    assert list(book.bids) == [D("10")]
