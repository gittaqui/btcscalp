"""Central pre-order controls. Protective sales may reduce risk while entries are stopped."""

from datetime import datetime, timezone

from trader.config import Config
from trader.models import BPS, SECOND, ZERO, Account, D, Decision, Fees, Instrument, Order
from trader.portfolio import Portfolio
from trader.storage.db import Store


class RiskRejected(RuntimeError):
    pass


class RiskEngine:
    def __init__(self, config: Config, store: Store):
        self.config, self.store = config, store
        self.frequency = config.frequency.resolved()

    def observe_equity(self, equity: D, now: int):
        if equity <= 0:
            self.store.kill("NONPOSITIVE_EQUITY", now)
            return
        day = datetime.fromtimestamp(now / SECOND, timezone.utc).date().isoformat()
        state = self.store.get("risk_equity", {})
        if not state:
            state = {"session_start": str(equity), "peak": str(equity), "day": day, "day_start": str(equity)}
        if state["day"] != day:
            state.update(day=day, day_start=state.get("last_equity", str(equity)))
        peak = max(D(state["peak"]), equity)
        state["peak"] = str(peak)
        drawdown = (peak - equity) / peak
        duration = max(0, now - state.get("last_ns", now))
        state["max_drawdown"] = str(max(D(state.get("max_drawdown", "0")), drawdown))
        state["max_drawdown_usd"] = str(max(D(state.get("max_drawdown_usd", "0")), peak - equity))
        state["drawdown_time_integral"] = str(
            D(state.get("drawdown_time_integral", "0")) + D(state.get("last_drawdown", "0")) * duration
        )
        state["observed_ns"] = state.get("observed_ns", 0) + duration
        state["last_ns"], state["last_equity"], state["last_drawdown"] = now, str(equity), str(drawdown)
        self.store.set("risk_equity", state)
        tests = [
            (D(state["day_start"]), self.config.risk.maximum_daily_loss_pct, "DAILY_LOSS"),
            (D(state["session_start"]), self.config.risk.maximum_session_loss_pct, "SESSION_LOSS"),
            (peak, self.config.risk.maximum_drawdown_pct, "DRAWDOWN"),
        ]
        for start, limit, reason in tests:
            if (start - equity) / start * 100 >= limit:
                self.store.kill(reason, now)

    def size(
        self, account: Account, instrument: Instrument, bid: D, liquidity: D, volatility: float, fees: Fees
    ):
        r = self.config.risk
        equity = account.equity(bid)
        stop = max(r.hard_stop_bps, D(str(volatility)) * r.volatility_stop_multiplier)
        budget_bps = stop + fees.maker_bps + fees.taker_bps + D(str(self.config.execution.slippage_bps))
        risk_notional = equity * r.risk_per_trade_pct / 100 * BPS / budget_bps
        available = account.available_usd / (1 + fees.maker_bps / BPS)
        notional = min(
            risk_notional,
            equity * r.maximum_position_pct / 100,
            r.maximum_order_usd,
            r.maximum_usd_exposure,
            r.maximum_btc_exposure * bid,
            liquidity * r.liquidity_fraction,
            available,
        )
        quantity = instrument.quantity(notional / bid)
        return quantity if quantity >= instrument.minimum_quantity else ZERO

    def authorize(
        self,
        order: Order,
        account: Account,
        portfolio: Portfolio,
        instrument: Instrument,
        book,
        now: int,
        decision: Decision | None = None,
    ):
        instrument.validate_order(order)
        if order.side == "sell":
            # Account availability and the local reservation ledger must both permit
            # a reduction; an unresolved IOC may still fill after a timeout.
            reserved = sum((o.remaining for o in self.store.orders(True) if o.side == "sell"), ZERO)
            if order.quantity > min(account.available_btc, portfolio.quantity):
                raise RiskRejected("EXIT_EXCEEDS_AVAILABLE_INVENTORY")
            if order.quantity + reserved > portfolio.quantity:
                raise RiskRejected("EXIT_OVERLAPS_PENDING_SELL")
            if order.kind not in {"ioc", "stop"}:
                raise RiskRejected("EXIT_TYPE_NOT_APPROVED")
            if (
                now < account.asof_ns
                or now - account.asof_ns > max(10, self.config.exchange.reconciliation_seconds * 3) * SECOND
            ):
                raise RiskRejected("STALE_ACCOUNT")
            if order.kind == "ioc":
                if book.stale(now, self.config.risk.stale_data_ms):
                    raise RiskRejected("STALE_DATA")
                floor = instrument.price(
                    book.bid * (1 - D(str(self.config.execution.emergency_limit_bps)) / BPS)
                )
                if order.price < floor:
                    raise RiskRejected("EXIT_PRICE_OUTSIDE_APPROVED_BOUND")
            return
        c, s = self.config, self.store
        if order.kind != "maker":
            raise RiskRejected("ENTRY_MUST_BE_MAKER")
        if s.get("killed") or s.get("paused", False):
            raise RiskRejected("KILLED_OR_PAUSED")
        if book.stale(now, c.risk.stale_data_ms):
            raise RiskRejected("STALE_DATA")
        if now - account.asof_ns > max(10, c.exchange.reconciliation_seconds * 3) * SECOND:
            raise RiskRejected("STALE_ACCOUNT")
        if portfolio.quantity > 0 or s.orders(active_only=True):
            raise RiskRejected("EXISTING_POSITION_OR_PENDING_ORDER")
        if decision is None or decision.decision != "BUY" or decision.ts_ns > now:
            raise RiskRejected("NO_VALID_DECISION")
        if now - decision.ts_ns > c.risk.stale_data_ms * 1_000_000:
            raise RiskRejected("STALE_DECISION")
        strictness = c.frequency.strictness
        if (
            decision.expected_net_edge_bps <= c.strategy.minimum_net_edge_bps * strictness
            or decision.confidence <= c.strategy.confidence_threshold
        ):
            raise RiskRejected("NO_EDGE")
        if book.spread_bps >= D(str(c.execution.maximum_spread_bps / strictness)):
            raise RiskRejected("SPREAD_TOO_WIDE")
        if decision.features["volatility_30s_bps"] > c.strategy.maximum_volatility_bps / strictness:
            raise RiskRejected("VOLATILITY_TOO_HIGH")
        liquidity = min(book.depth("buy"), book.depth("sell"))
        if liquidity < D(str(c.strategy.minimum_liquidity_usd * strictness)):
            raise RiskRejected("LIQUIDITY_TOO_LOW")
        if order.quantity * order.price > c.risk.maximum_order_usd:
            raise RiskRejected("ORDER_SIZE_LIMIT")
        equity = account.equity(book.bid)
        exposure = order.quantity * order.price
        if (
            order.quantity > c.risk.maximum_btc_exposure
            or exposure > c.risk.maximum_usd_exposure
            or exposure > equity * c.risk.maximum_position_pct / 100
        ):
            raise RiskRejected("EXPOSURE_LIMIT")
        fees = D(str(decision.costs["entry_fee_bps"]))
        if exposure * (1 + fees / BPS) > account.available_usd:
            raise RiskRejected("INSUFFICIENT_BUYING_POWER")
        entries = [x for x in s.get("entries", []) if x > now - 86400 * SECOND]
        f = self.frequency
        if entries and now - entries[-1] < f.minimum_seconds_between_entries * SECOND:
            raise RiskRejected("COOLDOWN")
        if sum(x > now - 3600 * SECOND for x in entries) >= f.maximum_trades_per_hour:
            raise RiskRejected("HOURLY_LIMIT")
        if len(entries) >= f.maximum_trades_per_day:
            raise RiskRejected("DAILY_TRADE_LIMIT")
        trades = portfolio.trades
        losses = 0
        for trade in reversed(trades):
            if trade["net_pnl"] >= 0:
                break
            losses += 1
        if losses >= c.risk.maximum_consecutive_losses:
            s.kill("CONSECUTIVE_LOSSES", now)
            raise RiskRejected("CONSECUTIVE_LOSSES")
        if losses:
            cooldown = (
                f.cooldown_after_loss_seconds if losses == 1 else f.cooldown_after_consecutive_losses_seconds
            )
            if now - trades[-1]["exit_ns"] < cooldown * SECOND:
                raise RiskRejected("LOSS_COOLDOWN")
        window = c.strategy.degradation_window
        if len(trades) >= window and sum(t["net_pnl"] for t in trades[-window:]) <= 0:
            s.set("paused", True)
            raise RiskRejected("STRATEGY_DEGRADATION")
        if len(trades) >= window // 2 and sum(t["net_pnl"] for t in trades[-window // 2 :]) <= 0:
            if entries and now - entries[-1] < max(f.minimum_seconds_between_entries * 2, 60) * SECOND:
                raise RiskRejected("DEGRADATION_REDUCED_FREQUENCY")

    def record_entry(self, now):
        entries = [x for x in self.store.get("entries", []) if x > now - 86400 * SECOND]
        self.store.set("entries", entries + [now])

    def exit_reason(self, portfolio, book, feature, now, fees):
        if portfolio.quantity == 0:
            return None
        r = self.config.risk
        portfolio.peak_price = max(portfolio.peak_price, book.bid)
        ret = (book.bid / portfolio.entry_price - 1) * BPS
        stop = max(
            r.hard_stop_bps, D(str(feature.values["volatility_30s_bps"])) * r.volatility_stop_multiplier
        )
        if ret <= -stop:
            return "HARD_OR_VOLATILITY_STOP"
        if now - portfolio.entry_ns >= r.time_stop_seconds * SECOND:
            return "TIME_STOP"
        if ret >= r.take_profit_bps + fees.maker_bps + fees.taker_bps:
            return "TAKE_PROFIT"
        peak_ret = (portfolio.peak_price / portfolio.entry_price - 1) * BPS
        if peak_ret >= r.break_even_trigger_bps + fees.maker_bps + fees.taker_bps:
            if ret <= fees.maker_bps + fees.taker_bps + D(str(self.config.execution.slippage_bps)):
                return "BREAK_EVEN_ADJUSTMENT"
            if (portfolio.peak_price - book.bid) / portfolio.peak_price * BPS >= r.trailing_stop_bps:
                return "TRAILING_STOP"
        if feature.values["imbalance"] < -0.5 or feature.regime in {
            "TRENDING_DOWN",
            "LIQUIDITY_SHOCK",
            "ABNORMAL",
        }:
            return "MICROSTRUCTURE_REVERSAL"
        return None
