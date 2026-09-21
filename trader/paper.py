"""Queue-aware event simulator, shared by replay and real-time paper trading."""

from trader.config import Config
from trader.models import BPS, ZERO, Account, D, Event, Fees, Fill, Order
from trader.portfolio import Portfolio
from trader.storage.db import Store


class PaperBroker:
    def __init__(self, config: Config, store: Store, fees: Fees, book):
        self.config, self.store, self.fees, self.book = config, store, fees, book
        self.pending = {}
        self.queues = {}
        self.cancels = {}
        self.last_trade_ids = set()
        self.now = 0
        self.fill_counter = len(store.fills())

    def portfolio(self):
        return Portfolio.from_fills(self.store.fills())

    def account(self):
        portfolio = self.portfolio()
        cash = self.config.paper_starting_usd + portfolio.cash_change
        reserved_usd, reserved_btc = ZERO, ZERO
        for order in self.store.orders(True):
            if order.side == "buy":
                reserved_usd += order.remaining * order.price * (1 + self.fees.maker_bps / BPS)
            else:
                reserved_btc += order.remaining
        return Account(
            usd=cash,
            btc=portfolio.quantity,
            available_usd=max(ZERO, cash - reserved_usd),
            available_btc=max(ZERO, portfolio.quantity - reserved_btc),
            asof_ns=self.now,
        )

    async def submit(self, order):
        if self.store.order(order.client_id):
            return self.store.order(order.client_id)
        self.store.put_order(order, new=True)
        self.pending[order.client_id] = (
            order.created_ns + self.config.execution.simulated_latency_ms * 1_000_000
        )
        return order

    async def cancel(self, order):
        if order.status in {"intent", "open", "unknown"}:
            self.cancels.setdefault(
                order.client_id, self.now + self.config.execution.cancel_latency_ms * 1_000_000
            )

    async def reconcile(self):
        return self.account()

    def fill(self, order: Order, quantity: D, price: D, maker: bool, event_ns: int):
        quantity = min(quantity, order.remaining)
        portfolio = self.portfolio()
        if order.side == "sell":
            quantity = min(quantity, portfolio.quantity)
        else:
            cash = self.config.paper_starting_usd + portfolio.cash_change
            quantity = min(
                quantity, cash / (price * (1 + (self.fees.maker_bps if maker else self.fees.taker_bps) / BPS))
            )
        if quantity <= 0:
            return
        self.fill_counter += 1
        fill = Fill(
            fill_id=f"sim-{self.fill_counter}",
            client_id=order.client_id,
            ts_ns=event_ns,
            side=order.side,
            quantity=quantity,
            price=price,
            fee=self.fees.cost(price, quantity, maker),
            maker=maker,
            reference_price=order.reference_price,
            regime=order.regime,
            latency_ms=max(0, (event_ns - order.created_ns) / 1e6),
        )
        with self.store.transaction():
            self.store.fill(fill)
            order.filled += quantity
            order.status = "closed" if order.remaining == 0 else "open"
            self.store.put_order(order)

    def cross(self, order, event_ns):
        # Walk displayed depth; participation and adverse slippage are charged explicitly.
        levels = self.book.levels(
            "sell" if order.side == "buy" else "buy",
            len(self.book.asks if order.side == "buy" else self.book.bids),
        )
        for price, size in levels:
            if order.remaining == 0:
                break
            adjusted = price * (
                1 + D(str(self.config.execution.slippage_bps)) / BPS * (1 if order.side == "buy" else -1)
            )
            if (order.side == "buy" and adjusted > order.price) or (
                order.side == "sell" and adjusted < order.price
            ):
                break
            self.fill(
                order,
                min(order.remaining, size * D(str(self.config.execution.participation_fraction))),
                adjusted,
                False,
                event_ns,
            )
        if order.remaining > 0:
            order.status = "cancelled"
            self.store.put_order(order)

    def advance(self, event: Event):
        self.now = event.recv_ns
        if event.kind == "disconnect":
            for order in self.store.orders(True):
                order.status = "cancelled"
                self.store.put_order(order)
            self.pending.clear()
            self.cancels.clear()
            return
        for client_id, when in list(self.cancels.items()):
            if when <= event.recv_ns:
                order = self.store.order(client_id)
                if order.status in {"intent", "open", "unknown"}:
                    order.status = "cancelled"
                    self.store.put_order(order)
                self.cancels.pop(client_id)
                self.pending.pop(client_id, None)
        for client_id, when in list(self.pending.items()):
            if when > event.recv_ns or not self.book.valid:
                continue
            order = self.store.order(client_id)
            self.pending.pop(client_id)
            crosses = (order.side == "buy" and order.price >= self.book.ask) or (
                order.side == "sell" and order.price <= self.book.bid
            )
            if order.kind == "maker" and crosses:
                order.status = "cancelled"
                self.store.put_order(order)
                continue
            order.status = "open"
            self.store.put_order(order)
            if order.kind == "ioc":
                self.cross(order, event.recv_ns)
            else:
                level = (self.book.bids if order.side == "buy" else self.book.asks).get(order.price, ZERO)
                self.queues[client_id] = level * D(str(self.config.execution.queue_multiplier))
        if event.kind != "trade" or event.trade_id in self.last_trade_ids:
            return
        self.last_trade_ids.add(event.trade_id)
        # Bounded IDs; trade replay also checks monotonically ordered input and source IDs.
        if len(self.last_trade_ids) > 200000:
            self.last_trade_ids = {event.trade_id}
        volume = event.quantity * D(str(self.config.execution.participation_fraction))
        for order in self.store.orders(True):
            if (
                order.status != "open"
                or event.ts_ns < order.created_ns + self.config.execution.simulated_latency_ms * 1_000_000
            ):
                continue
            eligible = (order.side == "buy" and event.aggressor == "sell" and event.price <= order.price) or (
                order.side == "sell" and event.aggressor == "buy" and event.price >= order.price
            )
            if not eligible:
                continue
            ahead = self.queues.get(order.client_id, ZERO)
            consumed = min(ahead, volume)
            self.queues[order.client_id] = ahead - consumed
            volume -= consumed
            if volume > 0:
                quantity = min(volume, order.remaining)
                self.fill(order, quantity, order.price, True, event.recv_ns)
                volume -= quantity
