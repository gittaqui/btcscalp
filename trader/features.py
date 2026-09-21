"""Causal microstructure features on fixed 100 ms samples and multiple horizons."""

import math
from collections import deque
from dataclasses import dataclass
from statistics import fmean, pstdev

from trader.config import Config
from trader.models import SECOND, Event, Regime
from trader.orderbook import OrderBook

HORIZONS = (1, 5, 15, 30, 60, 180, 300, 900)


@dataclass
class FeatureSet:
    ts_ns: int
    values: dict[str, float | None]
    regime: Regime
    ready: bool


class FeatureEngine:
    def __init__(self, config: Config):
        self.config = config
        self.prices = deque(maxlen=36020)
        self.trades = deque(maxlen=500000)
        self.trade_ids = set()
        self.trade_order = deque()
        self.last_sample_ns = 0
        self.previous_spread = None
        self.previous_volatility = 0.0
        self.ema = None
        self.previous_ema = None

    def trade(self, event: Event):
        if event.trade_id in self.trade_ids:
            return
        self.trade_ids.add(event.trade_id)
        self.trade_order.append(event.trade_id)
        if len(self.trade_order) > 100000:
            self.trade_ids.remove(self.trade_order.popleft())
        self.trades.append((event.recv_ns, float(event.price), float(event.quantity), event.aggressor))
        while self.trades and self.trades[0][0] < event.recv_ns - 901 * SECOND:
            self.trades.popleft()

    def sample(self, book: OrderBook, now: int):
        mid = float(book.mid)
        if now - self.last_sample_ns >= 100_000_000 or not self.prices:
            self.prices.append((now, mid))
            self.last_sample_ns = now
            self.previous_ema = self.ema if self.ema is not None else mid
            self.ema = mid if self.ema is None else 0.02 * mid + 0.98 * self.ema
        while self.prices and self.prices[0][0] < now - 901 * SECOND:
            self.prices.popleft()

    def calculate(self, book: OrderBook, now: int) -> FeatureSet:
        self.sample(book, now)
        mid, spread = float(book.mid), float(book.spread_bps)
        bid_q, ask_q = float(book.bids[book.bid]), float(book.asks[book.ask])
        bid_depth, ask_depth = float(book.depth("buy")), float(book.depth("sell"))
        imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)
        microprice = (float(book.ask) * bid_q + float(book.bid) * ask_q) / (bid_q + ask_q)
        values = {
            "mid": mid,
            "bid": float(book.bid),
            "ask": float(book.ask),
            "spread_bps": spread,
            "imbalance": imbalance,
            "microprice": microprice,
            "microprice_deviation_bps": (microprice / mid - 1) * 10000,
            "bid_depth_usd": bid_depth,
            "ask_depth_usd": ask_depth,
            "depth_ratio": bid_depth / ask_depth,
            "liquidity_concentration": (bid_q * float(book.bid) + ask_q * float(book.ask))
            / (bid_depth + ask_depth),
            "spread_change_bps": 0 if self.previous_spread is None else spread - self.previous_spread,
            "ema_slope_bps": (self.ema / self.previous_ema - 1) * 10000,
        }
        price_list = list(self.prices)
        for horizon in HORIZONS:
            boundary = now - horizon * SECOND
            past = next((p for timestamp, p in reversed(price_list) if timestamp <= boundary), None)
            values[f"return_{horizon}s_bps"] = None if past is None else (mid / past - 1) * 10000
            trades = [t for t in self.trades if t[0] >= boundary]
            buy = sum(t[2] for t in trades if t[3] == "buy")
            sell = sum(t[2] for t in trades if t[3] == "sell")
            volume = sum(t[2] for t in trades)
            values[f"volume_{horizon}s"] = volume
            values[f"flow_{horizon}s"] = (buy - sell) / (buy + sell) if buy + sell else 0
            values[f"sell_volume_{horizon}s"] = sell
            prices = [p for timestamp, p in price_list if timestamp >= boundary]
            returns = [math.log(b / a) * 10000 for a, b in zip(prices, prices[1:])]
            values[f"volatility_{horizon}s_bps"] = math.sqrt(sum(r * r for r in returns)) if returns else 0
        recent = [p for timestamp, p in price_list if timestamp >= now - 30 * SECOND]
        std = pstdev(recent) if len(recent) > 1 else 0
        values["zscore"] = (mid - fmean(recent)) / std if std else 0
        trades = [t for t in self.trades if t[0] >= now - 30 * SECOND]
        volume = sum(t[2] for t in trades)
        vwap = sum(t[1] * t[2] for t in trades) / volume if volume else mid
        values["vwap_deviation_bps"] = (mid / vwap - 1) * 10000
        values["volume_acceleration"] = values["volume_5s"] / max(values["volume_30s"] / 6, 1e-12)
        values["price_acceleration_bps"] = (values["return_1s_bps"] or 0) - (values["return_5s_bps"] or 0) / 5
        vol = values["volatility_30s_bps"]
        values["volatility_acceleration_bps"] = vol - self.previous_volatility
        self.previous_spread, self.previous_volatility = spread, vol
        ready = now - self.prices[0][0] >= self.config.strategy.warmup_seconds * SECOND
        return FeatureSet(now, values, classify(values, self.config) if ready else Regime.UNCLASSIFIED, ready)


def classify(f, config):
    if f["spread_bps"] > config.execution.maximum_spread_bps * 2:
        return Regime.ABNORMAL
    if min(f["bid_depth_usd"], f["ask_depth_usd"]) < config.strategy.minimum_liquidity_usd:
        return Regime.LIQUIDITY_SHOCK
    vol = f["volatility_30s_bps"]
    if vol > config.strategy.maximum_volatility_bps:
        return Regime.HIGH_VOLATILITY
    if vol < config.strategy.minimum_volatility_bps:
        return Regime.LOW_VOLATILITY
    ret = f["return_30s_bps"] or 0
    if ret > max(2, vol * 0.75):
        return Regime.TRENDING_UP
    if ret < -max(2, vol * 0.75):
        return Regime.TRENDING_DOWN
    return Regime.RANGING
