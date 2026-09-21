import base64
import hashlib
import hmac
import json

import httpx
import pytest

from tests.conftest import order
from trader.exchange.base import ExchangeError, Rejected, UnknownSubmission
from trader.exchange.gemini import Gemini
from trader.models import D


def api(cfg, store, handler, monkeypatch):
    monkeypatch.setenv("GEMINI_PROD_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_PROD_API_SECRET", "test-secret")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.gemini.com")
    return Gemini(cfg, store, client=client)


async def test_hmac_signature_nonce_and_post_only_payload(cfg, store, monkeypatch):
    def handler(request):
        payload = request.headers["X-GEMINI-PAYLOAD"]
        assert (
            request.headers["X-GEMINI-SIGNATURE"]
            == hmac.new(b"test-secret", payload.encode(), hashlib.sha384).hexdigest()
        )
        decoded = json.loads(base64.b64decode(payload))
        assert decoded["request"] == "/v1/order/new"
        assert decoded["options"] == ["maker-or-cancel"]
        assert decoded["margin_order"] is False
        assert decoded["client_order_id"] == "test-order"
        assert request.content == b""
        return httpx.Response(200, json={"order_id": "123"})

    exchange = api(cfg, store, handler, monkeypatch)
    await exchange.submit(order())
    await exchange.close()


async def test_actual_symbol_fee_rates_are_used(cfg, store, monkeypatch):
    def handler(request):
        body = json.loads(base64.b64decode(request.headers["X-GEMINI-PAYLOAD"]))
        assert body["symbol"] == "btcusd"
        return httpx.Response(200, json={"api_maker_fee_bps": 17, "api_taker_fee_bps": 29})

    exchange = api(cfg, store, handler, monkeypatch)
    result = await exchange.fees()
    assert result.maker_bps == D("17") and result.taker_bps == D("29")
    await exchange.close()


@pytest.mark.parametrize("code", [429, 500, 502, 503])
async def test_new_order_http_errors_never_retry(cfg, store, monkeypatch, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(code, json={"message": "unavailable"})

    exchange = api(cfg, store, handler, monkeypatch)
    with pytest.raises(UnknownSubmission):
        await exchange.submit(order())
    assert len(calls) == 1
    await exchange.close()


async def test_new_order_timeout_is_unknown(cfg, store, monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("timeout", request=request)

    exchange = api(cfg, store, handler, monkeypatch)
    with pytest.raises(UnknownSubmission):
        await exchange.submit(order())
    await exchange.close()


async def test_reject_does_not_echo_credentials(cfg, store, monkeypatch):
    exchange = api(
        cfg,
        store,
        lambda request: httpx.Response(400, json={"reason": "InvalidPrice", "message": "test-secret"}),
        monkeypatch,
    )
    with pytest.raises(Rejected, match="^InvalidPrice$"):
        await exchange.submit(order())
    await exchange.close()


async def test_instrument_uses_actual_increments_and_rejects_swap(cfg, store, monkeypatch):
    row = {
        "symbol": "BTCUSD",
        "base_currency": "BTC",
        "quote_currency": "USD",
        "product_type": "spot",
        "tick_size": "0.00000001",
        "quote_increment": "0.01",
        "min_order_size": "0.00001",
        "status": "open",
    }
    exchange = api(cfg, store, lambda request: httpx.Response(200, json=row), monkeypatch)
    instrument = await exchange.instrument()
    assert instrument.quantity(D("0.001000009")) == D("0.00100000")
    row["product_type"] = "swap"
    with pytest.raises(ExchangeError, match="NOT_BTC_USD_SPOT"):
        await exchange.instrument()
    await exchange.close()


def test_sandbox_credentials_never_use_production(cfg, store, monkeypatch):
    monkeypatch.setenv("GEMINI_PROD_API_KEY", "production-key")
    monkeypatch.setenv("GEMINI_PROD_API_SECRET", "production-secret")
    cfg.exchange.environment = "sandbox"
    exchange = Gemini(cfg, store)
    assert exchange.key == "" and "sandbox" in cfg.exchange.rest_url


def test_nonce_persists_across_db_restart(tmp_path):
    from trader.storage.db import Store

    path = str(tmp_path / "state.sqlite")
    first = Store(path)
    n = first.nonce("test")
    first.close()
    second = Store(path)
    assert second.nonce("test") > n
    second.close()
