"""Journaled live execution, reconciliation and common order controller."""

import time
import uuid

from trader.config import Config
from trader.exchange.base import ExchangeError, Rejected, UnknownSubmission
from trader.models import BPS, SECOND, ZERO, D, Fill, Order
from trader.portfolio import Portfolio
from trader.risk import RiskEngine, RiskRejected
from trader.storage.db import Store


class LiveBroker:
    def __init__(self, config, store, entry_api, guard_api):
        self.config, self.store = config, store
        self.entry_api, self.guard_api = entry_api, guard_api
        self.last_account = None

    def portfolio(self):
        return Portfolio.from_fills(self.store.fills())

    def account(self):
        if self.last_account is None:
            raise ExchangeError("RECONCILIATION_REQUIRED")
        return self.last_account

    def api_for(self, order):
        return self.entry_api if order.side == "buy" else self.guard_api

    def apply_status(self, order, response):
        if (
            str(response.get("client_order_id", order.client_id)) != order.client_id
            or str(response["symbol"]).lower() != "btcusd"
            or response["side"] != order.side
            or D(str(response["original_amount"])) != order.quantity
        ):
            raise ExchangeError("ORDER_IDENTITY_MISMATCH")
        filled = D(str(response["executed_amount"]))
        if filled < order.filled or filled > order.quantity:
            raise ExchangeError("FILL_QUANTITY_REGRESSION")
        order.exchange_id = str(response["order_id"])
        trades = response.get("trades", [])
        with self.store.transaction():
            # Save ID first for crash recovery; all fill insertions are atomic with the status.
            for row in trades:
                if row.get("break"):
                    raise ExchangeError("TRADE_BREAK_REQUIRES_OPERATOR")
                if str(row["fee_currency"]).upper() != "USD":
                    raise ExchangeError("UNSUPPORTED_FEE_CURRENCY")
                fill = Fill(
                    fill_id=f"gemini:{order.exchange_id}:{row['tid']}",
                    client_id=order.client_id,
                    ts_ns=int(row["timestampms"]) * 1_000_000,
                    side=order.side,
                    price=row["price"],
                    quantity=row["amount"],
                    fee=row["fee_amount"],
                    maker=not row["aggressor"],
                    reference_price=order.reference_price,
                    latency_ms=max(0, (int(row["timestampms"]) * 1_000_000 - order.created_ns) / 1e6),
                    regime=order.regime,
                )
                self.store.fill(fill)
            booked = sum((f.quantity for f in self.store.fills() if f.client_id == order.client_id), ZERO)
            if booked != filled:
                raise ExchangeError("INCOMPLETE_FILL_DETAILS")
            order.filled = filled
            # IOC can be cancelled and fully filled: inventory always follows fills.
            order.status = (
                "open" if response["is_live"] else "cancelled" if response["is_cancelled"] else "closed"
            )
            self.store.put_order(order)
        return order

    async def submit(self, order):
        existing = self.store.order(order.client_id)
        if existing:
            response = await self.api_for(existing).status(existing.client_id)
            return self.apply_status(existing, response)
        self.store.put_order(order, new=True)
        try:
            response = await self.api_for(order).submit(order)
            # /new may omit trade details; fetch status before updating inventory.
            order.exchange_id = str(response["order_id"])
            self.store.put_order(order)
            return self.apply_status(order, await self.api_for(order).status(order.client_id))
        except Rejected:
            # A rejection of the status request after accepted submission is ambiguous too.
            if order.exchange_id:
                order.status = "unknown"
                self.store.put_order(order)
                self.store.kill("ACCEPTED_ORDER_STATUS_UNAVAILABLE")
                raise UnknownSubmission("ACCEPTED_ORDER_STATUS_UNAVAILABLE")
            order.status = "rejected"
            self.store.put_order(order)
            count = self.store.get("order_rejections", 0) + 1
            self.store.set("order_rejections", count)
            if count >= self.config.risk.maximum_order_rejections:
                self.store.kill("REPEATED_ORDER_REJECTIONS")
            raise
        except Exception:
            order.status = "unknown"
            self.store.put_order(order)
            self.store.kill("AMBIGUOUS_ORDER_SUBMISSION")
            raise

    async def cancel(self, order):
        if order.exchange_id is None:
            self.apply_status(order, await self.api_for(order).status(order.client_id))
        if order.status == "open":
            await self.api_for(order).cancel(order.exchange_id)
        return self.apply_status(order, await self.api_for(order).status(order.client_id))

    async def reconcile(self):
        # Re-read active intents AND recently finalized orders to catch fill/cancel races.
        last_check = self.store.get("reconcile_ns", 0)
        for order in self.store.orders():
            if order.status == "rejected":
                continue
            if order.status in {"intent", "unknown", "open"} or order.created_ns >= last_check - 60 * SECOND:
                self.apply_status(order, await self.api_for(order).status(order.client_id))
        remote = await self.guard_api.open_orders()
        known = {o.client_id for o in self.store.orders()}
        if any(str(row.get("client_order_id", "")) not in known for row in remote):
            raise ExchangeError("EXTERNAL_OPEN_ORDERS_IN_DEDICATED_ACCOUNT")
        since = self.store.get("trade_history_since_ms", time.time_ns() // 1_000_000 - 60000)
        history = await self.guard_api.past_trades(since)
        known_exchange = {o.exchange_id for o in self.store.orders()}
        if any(str(row["order_id"]) not in known_exchange or row.get("break") for row in history):
            raise ExchangeError("EXTERNAL_OR_BROKEN_TRADE")
        account = await self.guard_api.balances()
        portfolio = self.portfolio()
        if abs(account.btc - portfolio.quantity) > D("0.00000000001"):
            raise ExchangeError("INVENTORY_RECONCILIATION_MISMATCH")
        initial_cash = self.store.get("initial_live_usd")
        if initial_cash is None:
            if portfolio.quantity or portfolio.trades:
                raise ExchangeError("INITIAL_CASH_BASELINE_MISSING")
            initial_cash = str(account.usd - portfolio.cash_change)
            self.store.set("initial_live_usd", initial_cash)
        if abs(account.usd - (D(initial_cash) + portfolio.cash_change)) > D("0.01"):
            raise ExchangeError("CASH_RECONCILIATION_MISMATCH")
        if history:
            self.store.set("trade_history_since_ms", max(int(t["timestampms"]) for t in history) - 10000)
        self.store.set("reconcile_ns", time.time_ns())
        self.store.set("account", account.model_dump(mode="json"))
        self.last_account = account
        return account


class Controller:
    def __init__(self, config: Config, store: Store, broker, instrument, fees, book):
        self.config, self.store, self.broker = config, store, broker
        self.instrument, self.fees, self.book = instrument, fees, book
        self.risk = RiskEngine(config, store)
        self.last_exit_ns = 0

    async def place(self, order, decision=None):
        # Both paper and live routes share this gate. The guardian also uses it.
        now = time.time_ns() if isinstance(self.broker, LiveBroker) else order.created_ns
        self.risk.authorize(
            order, self.broker.account(), self.broker.portfolio(), self.instrument, self.book, now, decision
        )
        if order.side == "buy":
            self.risk.record_entry(now)
        result = await self.broker.submit(order)
        self.store.event(
            "orders",
            {"client_id": order.client_id, "side": order.side, "kind": order.kind, "reason": order.reason},
            now,
        )
        return result

    async def cancel_all(self, include_protection=False):
        for order in self.store.orders(True):
            if include_protection or order.kind != "stop":
                await self.broker.cancel(order)

    async def protect(self, now):
        if not isinstance(self.broker, LiveBroker):
            return
        portfolio = self.broker.portfolio()
        covered = sum((o.remaining for o in self.store.orders(True) if o.kind == "stop"), ZERO)
        missing = self.instrument.quantity(max(ZERO, portfolio.quantity - covered))
        if missing < self.instrument.minimum_quantity:
            if portfolio.quantity > 0 and covered == 0:
                self.store.kill("UNPROTECTABLE_DUST_POSITION", now)
            return
        trigger = self.instrument.price(portfolio.entry_price * (1 - self.config.risk.hard_stop_bps / BPS))
        price = self.instrument.price(
            trigger * (1 - D(str(self.config.execution.native_stop_limit_gap_bps)) / BPS)
        )
        order = Order(
            client_id="bsg-" + uuid.uuid4().hex,
            side="sell",
            kind="stop",
            quantity=missing,
            price=price,
            stop_price=trigger,
            created_ns=now,
            reference_price=portfolio.entry_price,
            reason="NATIVE_PROTECTION",
        )
        await self.place(order)
        await self.broker.reconcile()

    async def flatten(self, now, reason="OPERATOR_FLATTEN"):
        self.store.set("paused", True) if reason == "OPERATOR_FLATTEN" else None
        if now - self.last_exit_ns < self.config.execution.exit_retry_seconds * SECOND:
            return
        if not self.book.valid or self.book.stale(now, self.config.risk.stale_data_ms):
            await self.cancel_all(include_protection=False)
            await self.protect(now)
            return
        await self.cancel_all(include_protection=True)
        await self.broker.reconcile()
        # For simulated cancellation delay, wait for the tape to acknowledge cancellation.
        if self.store.orders(True):
            return
        portfolio, account = self.broker.portfolio(), self.broker.account()
        if portfolio.quantity <= 0:
            return
        if not self.book.valid or self.book.stale(now, self.config.risk.stale_data_ms):
            # Fresh book is needed for a price-bounded emergency sale. Re-arm native protection.
            await self.protect(now)
            return
        quantity = self.instrument.quantity(min(portfolio.quantity, account.available_btc))
        if quantity < self.instrument.minimum_quantity:
            self.store.kill("DUST_REQUIRES_OPERATOR", now)
            return
        price = self.instrument.price(
            self.book.bid * (1 - D(str(self.config.execution.emergency_limit_bps)) / BPS)
        )
        order = Order(
            client_id="bsg-" + uuid.uuid4().hex,
            side="sell",
            kind="ioc",
            quantity=quantity,
            price=price,
            reference_price=self.book.mid,
            created_ns=now,
            reason=reason,
        )
        self.last_exit_ns = now
        await self.place(order)
        await self.broker.reconcile()
        await self.protect(now)

    async def manage_makers(self, decision, now):
        c = self.config
        active = self.store.orders(True)
        for order in active:
            if order.side == "buy":
                stale = now - order.created_ns > c.execution.maker_timeout_ms * 1_000_000
                adverse = abs(self.book.bid / order.price - 1) * BPS > D(
                    str(c.execution.maximum_price_chase_bps)
                )
                if stale or adverse or decision.decision != "BUY" or order.filled > 0:
                    await self.broker.cancel(order)

    async def decide(self, decision, now):
        c = self.config
        await self.manage_makers(decision, now)
        portfolio = self.broker.portfolio()
        if portfolio.quantity:
            await self.protect(now)
            return
        if self.store.orders(True) or decision.decision != "BUY":
            return
        # A limited re-quote is still a new risk-authorized opportunity, never a blind retry.
        chain = self.store.get("quote_chain")
        if chain and now - chain["started_ns"] <= c.execution.maker_timeout_ms * 1_000_000 * (
            c.execution.maximum_requotes + 2
        ):
            if chain["count"] > c.execution.maximum_requotes or abs(
                self.book.bid / D(chain["price"]) - 1
            ) * BPS > D(str(c.execution.maximum_price_chase_bps)):
                return
        else:
            chain = {"started_ns": now, "price": str(self.book.bid), "count": 0}
        quantity = self.risk.size(
            self.broker.account(),
            self.instrument,
            self.book.bid,
            min(self.book.depth("buy"), self.book.depth("sell")),
            decision.features["volatility_30s_bps"],
            self.fees,
        )
        if not quantity:
            decision.decision, decision.reason = "NO_TRADE", "BELOW_MINIMUM_SIZE"
            return
        order = Order(
            client_id="bsc-" + uuid.uuid4().hex,
            side="buy",
            kind="maker",
            quantity=quantity,
            price=self.instrument.price(self.book.bid),
            reference_price=self.book.mid,
            created_ns=now,
            regime=decision.regime,
            reason=decision.reason,
        )
        try:
            await self.place(order, decision)
            chain["count"] += 1
            self.store.set("quote_chain", chain)
        except RiskRejected as exc:
            decision.decision, decision.reason = "NO_TRADE", str(exc)
