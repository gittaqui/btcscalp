"""Full-depth book. Never repair a sequence gap by guessing missing levels."""

from trader.models import BPS, ZERO, D, Event


class BookError(RuntimeError):
    pass


class OrderBook:
    def __init__(self):
        self.reset()

    def reset(self):
        self.bids: dict[D, D] = {}
        self.asks: dict[D, D] = {}
        self.sequence = -1
        self.last_recv_ns = 0
        self.last_exchange_ns = 0
        self.valid = False

    def apply(self, event: Event) -> bool:
        if event.kind == "disconnect":
            self.reset()
            return False
        if event.kind not in {"snapshot", "delta"}:
            return False
        try:
            if event.first > event.last:
                raise BookError("INVALID_SEQUENCE")
            if event.kind == "snapshot":
                bids, asks = {}, {}
            else:
                if not self.valid:
                    raise BookError("SNAPSHOT_REQUIRED")
                if event.last <= self.sequence:
                    return False  # Replayed frame; must not refresh freshness.
                if not event.first <= self.sequence + 1 <= event.last:
                    raise BookError("SEQUENCE_GAP")
                if event.ts_ns < self.last_exchange_ns or event.recv_ns < self.last_recv_ns:
                    raise BookError("TIME_REVERSAL")
                bids, asks = self.bids.copy(), self.asks.copy()
            for levels, updates in ((bids, event.bids), (asks, event.asks)):
                for price, quantity in updates:
                    if not price.is_finite() or not quantity.is_finite() or price <= 0 or quantity < 0:
                        raise BookError("INVALID_LEVEL")
                    if quantity == 0:
                        levels.pop(price, None)
                    else:
                        levels[price] = quantity
            if not bids or not asks or max(bids) >= min(asks):
                raise BookError("EMPTY_OR_CROSSED_BOOK")
            self.bids, self.asks = bids, asks
            self.sequence = event.last
            self.last_recv_ns, self.last_exchange_ns = event.recv_ns, event.ts_ns
            self.valid = True
            return True
        except (ValueError, BookError):
            self.reset()
            raise

    @property
    def bid(self):
        if not self.valid:
            raise BookError("INVALID_BOOK")
        return max(self.bids)

    @property
    def ask(self):
        if not self.valid:
            raise BookError("INVALID_BOOK")
        return min(self.asks)

    @property
    def mid(self):
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self):
        return (self.ask - self.bid) / self.mid * BPS

    def levels(self, side, count=10):
        levels = self.bids if side == "buy" else self.asks
        return sorted(levels.items(), reverse=side == "buy")[:count]

    def depth(self, side, count=10):
        return sum((price * quantity for price, quantity in self.levels(side, count)), ZERO)

    def stale(self, now_ns, threshold_ms):
        return not self.valid or now_ns - self.last_recv_ns > threshold_ms * 1_000_000

    def snapshot(self, now_ns):
        return Event(
            kind="snapshot",
            ts_ns=self.last_exchange_ns,
            recv_ns=now_ns,
            first=self.sequence,
            last=self.sequence,
            bids=list(self.bids.items()),
            asks=list(self.asks.items()),
        )
