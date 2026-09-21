"""Common deterministic loop for historical and real-time events."""

from trader.execution import Controller
from trader.features import FeatureEngine
from trader.models import SECOND
from trader.orderbook import BookError
from trader.paper import PaperBroker
from trader.strategy import Strategy


class Engine:
    def __init__(self, config, store, broker, book, instrument, fees, model):
        self.config, self.store, self.broker, self.book = config, store, broker, book
        self.features = FeatureEngine(config)
        self.strategy = Strategy(config, model, fees)
        self.controller = Controller(config, store, broker, instrument, fees, book)
        self.last_signal = self.last_decision = self.last_equity = 0
        self.latest = None
        self.latest_feature = None
        self.last_fill_count = len(store.fills())
        self.started_ns = None
        self.allow_entries = True

    async def event(self, event):
        now = event.recv_ns
        if self.started_ns is None:
            self.started_ns = now
        if isinstance(self.broker, PaperBroker):
            self.broker.advance(event)
        if event.kind == "disconnect":
            self.store.kill("MARKET_DATA_DISCONNECTED", now)
            await self.controller.cancel_all()
            self.book.reset()
            self.features = FeatureEngine(self.config)
            self.store.set("websocket", "disconnected")
            return
        try:
            changed = self.book.apply(event)
        except BookError as exc:
            self.store.kill(str(exc), now)
            await self.controller.cancel_all()
            return
        if event.kind == "trade":
            self.features.trade(event)
        if not self.book.valid:
            return
        if event.kind in {"snapshot", "delta"} and not changed:
            return
        if self.book.stale(now, self.config.risk.stale_data_ms):
            self.store.kill("STALE_DATA", now)
            await self.controller.cancel_all()
            return
        self.store.set("websocket", "connected")
        self.features.sample(self.book, now)
        freq = self.config.frequency.resolved()
        if self.latest_feature is None or now - self.last_signal >= freq.signal_interval_ms * 1_000_000:
            self.latest_feature = self.features.calculate(self.book, now)
            self.latest = self.strategy.decide(
                self.latest_feature,
                float(self.book.bids[self.book.bid]) * self.config.execution.queue_multiplier,
            )
            self.last_signal = now
        portfolio = self.broker.portfolio()
        mark_equity = self.broker.account().equity(self.book.bid)
        self.controller.risk.observe_equity(mark_equity, now)
        if (
            now - self.last_equity >= self.config.database.snapshot_interval_seconds * SECOND
            or not self.last_equity
        ):
            self.store.event(
                "equity",
                {"equity": str(mark_equity), "btc": str(portfolio.quantity), "mid": str(self.book.mid)},
                now,
            )
            self.last_equity = now
        if now - self.last_decision >= freq.decision_interval_ms * 1_000_000:
            self.last_decision = now
            await self.controller.manage_makers(self.latest, now)
            if portfolio.quantity:
                reason = self.controller.risk.exit_reason(
                    portfolio, self.book, self.latest_feature, now, self.strategy.fees
                )
                # Persist trailing high watermark across callbacks and process restarts.
                peak = self.store.get("position_peak", {})
                if peak.get("entry_ns") == portfolio.entry_ns:
                    from trader.models import D

                    portfolio.peak_price = max(portfolio.peak_price, D(peak["price"]))
                    reason = self.controller.risk.exit_reason(
                        portfolio, self.book, self.latest_feature, now, self.strategy.fees
                    )
                self.store.set(
                    "position_peak", {"entry_ns": portfolio.entry_ns, "price": str(portfolio.peak_price)}
                )
                if self.store.get("killed") or self.store.get("flatten_requested"):
                    reason = "EMERGENCY_RISK_REDUCTION"
                if reason:
                    await self.controller.flatten(now, reason)
                else:
                    await self.controller.protect(now)
            elif self.allow_entries and not self.store.get("killed") and not self.store.get("paused", False):
                await self.controller.decide(self.latest, now)
            else:
                await self.controller.cancel_all()
                self.latest.decision = "NO_TRADE"
                self.latest.reason = "KILLED_OR_PAUSED" if self.allow_entries else "EVALUATION_CLOSING_WINDOW"
            if (
                self.store.get("flatten_requested")
                and not self.broker.portfolio().quantity
                and not self.store.orders(True)
            ):
                self.store.set("flatten_requested", False)
            self.store.event("decision", self.latest.model_dump(mode="json"), now)
        self.store.set(
            "snapshot",
            {
                "ts_ns": now,
                "btc_price": str(self.book.mid),
                "bid": str(self.book.bid),
                "ask": str(self.book.ask),
                "spread_bps": str(self.book.spread_bps),
                "imbalance": self.latest_feature.values["imbalance"],
                "regime": str(self.latest_feature.regime),
                "position_btc": str(portfolio.quantity),
                "unrealized_pnl": str(portfolio.unrealized(self.book.bid)),
                "equity": str(mark_equity),
                "last_signal": self.latest.model_dump(mode="json"),
            },
        )
        if self.config.database.record_market_events:
            self.store.event("market", event.model_dump(mode="json"), now)
