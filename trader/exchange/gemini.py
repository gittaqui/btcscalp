"""HMAC-SHA384 REST adapter and authenticated order event notifications."""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import random
import time

import httpx
from websockets.asyncio.client import connect

from trader.config import Config
from trader.exchange.base import ExchangeError, Rejected, UnknownSubmission
from trader.models import ZERO, Account, D, Fees, Instrument, Order
from trader.storage.db import Store


def credentials(config, guard=False):
    prefix = "GEMINI_SANDBOX" if config.exchange.environment == "sandbox" else "GEMINI_PROD"
    key = os.getenv(prefix + ("_GUARD_KEY" if guard else "_API_KEY"), "")
    secret = os.getenv(prefix + ("_GUARD_SECRET" if guard else "_API_SECRET"), "")
    return key, secret


class Gemini:
    def __init__(self, config: Config, store: Store, guard=False, client=None):
        self.config, self.store, self.guard = config, store, guard
        self.key, self.secret = credentials(config, guard)
        self.client = client or httpx.AsyncClient(
            base_url=config.exchange.rest_url,
            timeout=config.exchange.rest_timeout_seconds,
            follow_redirects=False,
        )
        self.lock = asyncio.Lock()
        self.last_request = 0.0
        self.last_latency_ms = 0.0
        self.nonce_namespace = hashlib.sha256(self.key.encode()).hexdigest()[:20]

    async def close(self):
        await self.client.aclose()

    def headers(self, path, body):
        if not self.key or not self.secret:
            raise ExchangeError("MISSING_ENVIRONMENT_CREDENTIALS")
        payload = {**body, "request": path, "nonce": self.store.nonce(self.nonce_namespace)}
        encoded = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode())
        signature = hmac.new(self.secret.encode(), encoded, hashlib.sha384).hexdigest()
        return {
            "X-GEMINI-APIKEY": self.key,
            "X-GEMINI-PAYLOAD": encoded.decode(),
            "X-GEMINI-SIGNATURE": signature,
            "Content-Type": "text/plain",
            "Cache-Control": "no-cache",
        }

    async def request(self, path, body=None, private=True, creates_order=False):
        attempts = 1 if creates_order else self.config.exchange.maximum_retries + 1
        for attempt in range(attempts):
            async with self.lock:
                interval = 1 / self.config.exchange.requests_per_second
                await asyncio.sleep(max(0, self.last_request + interval - time.monotonic()))
                self.last_request = time.monotonic()
                headers = self.headers(path, body or {}) if private else {}
                start = time.monotonic()
                try:
                    response = await self.client.request(
                        "POST" if private else "GET", path, headers=headers, content=b"" if private else None
                    )
                    self.last_latency_ms = (time.monotonic() - start) * 1000
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if creates_order:
                        raise UnknownSubmission("AMBIGUOUS_NETWORK_SUBMISSION") from exc
                    if attempt + 1 == attempts:
                        raise ExchangeError("REST_UNAVAILABLE") from exc
                    response = None
            if response is not None:
                if response.status_code == 429 or response.status_code >= 500:
                    if creates_order:
                        raise UnknownSubmission("AMBIGUOUS_HTTP_SUBMISSION")
                    if attempt + 1 == attempts:
                        raise ExchangeError("RATE_LIMIT_OR_SERVER_ERROR")
                elif response.status_code >= 400:
                    # Log a bounded reason code only; never response bodies or signed headers.
                    try:
                        reason = str(response.json().get("reason", "HTTP_ERROR"))[:80]
                    except ValueError:
                        reason = "HTTP_ERROR"
                    raise Rejected(reason)
                else:
                    try:
                        result = json.loads(response.text, parse_float=str)
                    except ValueError as exc:
                        if creates_order:
                            raise UnknownSubmission("INVALID_ORDER_RESPONSE") from exc
                        raise ExchangeError("INVALID_JSON") from exc
                    if isinstance(result, dict) and result.get("result") == "error":
                        raise Rejected(str(result.get("reason", "API_ERROR"))[:80])
                    return result
            delay = min(30, 2**attempt) + random.random()
            if response is not None:
                try:
                    delay = max(delay, min(60, float(response.headers.get("Retry-After", "0"))))
                except ValueError:
                    pass
            await asyncio.sleep(delay)
        raise ExchangeError("RETRIES_EXHAUSTED")

    async def instrument(self):
        data = await self.request("/v1/symbols/details/btcusd", private=False)
        if (
            data.get("product_type") != "spot"
            or data.get("base_currency") != "BTC"
            or data.get("quote_currency") != "USD"
        ):
            raise ExchangeError("NOT_BTC_USD_SPOT")
        return Instrument(
            price_increment=data["quote_increment"],
            quantity_increment=data["tick_size"],
            minimum_quantity=data["min_order_size"],
            status=data["status"],
        )

    async def fees(self):
        data = await self.request("/v1/notionalvolume", {"symbol": "btcusd"})
        return Fees(
            maker_bps=data["api_maker_fee_bps"],
            taker_bps=data["api_taker_fee_bps"],
            source="gemini:/v1/notionalvolume:btcusd",
            fetched_ns=time.time_ns(),
        )

    async def balances(self):
        rows = await self.request("/v1/balances")
        assets = {str(row["currency"]).upper(): row for row in rows if row.get("type") == "exchange"}
        if "USD" not in assets:
            raise ExchangeError("USD_EXCHANGE_BALANCE_MISSING")
        # Comparing the full balance to the fill ledger detects deposits, external activity and missing fills.
        baseline = self.config.exchange.baseline_btc
        btc = D(str(assets.get("BTC", {}).get("amount", "0"))) - baseline
        available_btc = max(ZERO, D(str(assets.get("BTC", {}).get("available", "0"))) - baseline)
        return Account(
            usd=assets["USD"]["amount"],
            btc=btc,
            available_usd=assets["USD"]["available"],
            available_btc=available_btc,
            asof_ns=time.time_ns(),
        )

    async def submit(self, order: Order):
        payload = {
            "client_order_id": order.client_id,
            "symbol": "btcusd",
            "side": order.side,
            "amount": str(order.quantity),
            "price": str(order.price),
            "margin_order": False,
            "type": "exchange stop limit" if order.kind == "stop" else "exchange limit",
        }
        if order.kind == "stop":
            payload["stop_price"] = str(order.stop_price)
        else:
            payload["options"] = ["maker-or-cancel" if order.kind == "maker" else "immediate-or-cancel"]
        return await self.request("/v1/order/new", payload, creates_order=True)

    async def status(self, client_id):
        return await self.request("/v1/order/status", {"client_order_id": client_id, "include_trades": True})

    async def cancel(self, exchange_id):
        return await self.request("/v1/order/cancel", {"order_id": int(exchange_id)})

    async def open_orders(self):
        return await self.request("/v1/orders")

    async def heartbeat(self):
        return await self.request("/v1/heartbeat")

    async def cancel_session(self):
        return await self.request("/v1/order/cancel/session")

    async def past_trades(self, since_ms):
        # Refuse a truncated page rather than silently skipping unaccounted fills.
        result = await self.request(
            "/v1/mytrades", {"symbol": "btcusd", "timestamp": since_ms, "limit_trades": 500}
        )
        if len(result) >= 500:
            raise ExchangeError("TRADE_HISTORY_PAGE_FULL_REQUIRES_OFFLINE_RECONCILIATION")
        return result

    def websocket_headers(self):
        nonce = str(int(time.time()))
        payload = base64.b64encode(nonce.encode())
        return {
            "X-GEMINI-APIKEY": self.key,
            "X-GEMINI-NONCE": nonce,
            "X-GEMINI-PAYLOAD": payload.decode(),
            "X-GEMINI-SIGNATURE": hmac.new(self.secret.encode(), payload, hashlib.sha384).hexdigest(),
        }

    async def order_notifications(self, wakeup, shutdown):
        while not shutdown.is_set():
            try:
                async with connect(
                    self.config.exchange.ws_url,
                    additional_headers=self.websocket_headers(),
                    ping_interval=10,
                    ping_timeout=10,
                    max_queue=64,
                ) as ws:
                    await ws.send(
                        json.dumps({"id": "orders", "method": "subscribe", "params": ["orders@account"]})
                    )
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("id") == "orders" and msg.get("status") != 200:
                            raise ExchangeError("PRIVATE_SUBSCRIPTION_REJECTED")
                        if msg.get("e") == "orderUpdate":
                            # REST include_trades is authoritative, so WS deltas cannot double-count fills.
                            wakeup.set()
                        if shutdown.is_set():
                            return
            except asyncio.CancelledError:
                raise
            except Exception:
                self.store.event("health", {"reason": "PRIVATE_WS_DISCONNECTED"})
                wakeup.set()
                try:
                    await asyncio.wait_for(shutdown.wait(), 3)
                except TimeoutError:
                    pass
