"""Opt-in real integration. No production endpoint or credentials can be used here."""

import os
import time
import uuid

import pytest

from trader.config import Config
from trader.exchange.gemini import Gemini
from trader.models import Order
from trader.storage.db import Store

pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.skipif(
        os.getenv("RUN_SANDBOX_TESTS") != "true", reason="sandbox keys and explicit opt-in required"
    ),
]


async def test_sandbox_read_place_cancel_and_reconcile():
    config = Config.model_validate({"mode": "sandbox", "exchange": {"environment": "sandbox"}})
    store = Store(":memory:")
    api = Gemini(config, store)
    placed = None
    try:
        instrument, fees = await api.instrument(), await api.fees()
        assert fees.source.startswith("gemini:")
        account = await api.balances()
        assert account.available_usd >= 0
        data = await api.request("/v1/pubticker/btcusd", private=False)
        from trader.models import D

        price = instrument.price(D(data["bid"]) * D("0.5"))
        o = Order(
            client_id="bsc-test-" + uuid.uuid4().hex,
            side="buy",
            kind="maker",
            quantity=instrument.minimum_quantity,
            price=price,
            reference_price=price,
            created_ns=time.time_ns(),
        )
        placed = await api.submit(o)
        result = await api.status(o.client_id)
        assert result["client_order_id"] == o.client_id
        await api.cancel(str(placed["order_id"]))
        final = await api.status(o.client_id)
        assert not final["is_live"]
    finally:
        if placed:
            await api.cancel(str(placed["order_id"]))
        await api.close()
        store.close()
