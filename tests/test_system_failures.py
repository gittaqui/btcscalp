import subprocess
import sys
import time

import pytest

from tests.conftest import NOW, order
from trader.engine import Engine
from trader.execution import Controller
from trader.models import SECOND, D, Event
from trader.paper import PaperBroker
from trader.risk import RiskEngine, RiskRejected
from trader.runtime import bind_database
from trader.storage.db import Store, process_lock
from trader.strategy import EdgeModel


async def test_complete_entry_and_exit_uses_same_risk_and_accounting(
    cfg, store, fees, book, instrument, decision
):
    cfg.frequency.maximum_trades_per_hour = 3
    broker = PaperBroker(cfg, store, fees, book)
    broker.now = NOW
    controller = Controller(cfg, store, broker, instrument, fees, book)
    await controller.place(order(), decision)
    broker.advance(
        Event(
            kind="trade",
            ts_ns=NOW + SECOND,
            recv_ns=NOW + SECOND,
            price="60000",
            quantity="3",
            aggressor="sell",
            trade_id="1",
        )
    )
    assert broker.portfolio().quantity == D("0.001")
    now = NOW + 2 * SECOND
    book.apply(Event(kind="delta", ts_ns=now, recv_ns=now, first=11, last=11))
    broker.now = now
    await controller.flatten(now)
    broker.advance(
        Event(
            kind="trade",
            ts_ns=now + SECOND,
            recv_ns=now + SECOND,
            price="60000",
            quantity="1",
            aggressor="sell",
            trade_id="2",
        )
    )
    portfolio = broker.portfolio()
    assert portfolio.quantity == 0
    assert len(portfolio.trades) == 1
    assert portfolio.trades[0]["net_pnl"] < 0
    assert broker.account().usd == cfg.paper_starting_usd + portfolio.trades[0]["net_pnl"]


async def test_partial_entry_remainder_is_cancelled(cfg, store, fees, book, instrument, decision):
    broker = PaperBroker(cfg, store, fees, book)
    o = order(status="open", filled=D("0.0005"))
    store.put_order(o, new=True)
    broker.now = NOW + SECOND
    controller = Controller(cfg, store, broker, instrument, fees, book)
    await controller.manage_makers(decision, NOW + SECOND)
    assert o.client_id in broker.cancels


async def test_disconnect_with_position_keeps_inventory_and_kills(cfg, store, fees, book, instrument):
    broker = PaperBroker(cfg, store, fees, book)
    await broker.submit(order())
    broker.advance(
        Event(
            kind="trade",
            ts_ns=NOW + SECOND,
            recv_ns=NOW + SECOND,
            price="60000",
            quantity="3",
            aggressor="sell",
            trade_id="1",
        )
    )
    engine = Engine(cfg, store, broker, book, instrument, fees, EdgeModel())
    await engine.event(Event(kind="disconnect", ts_ns=NOW + 2 * SECOND, recv_ns=NOW + 2 * SECOND))
    assert broker.portfolio().quantity == D("0.001")
    assert not book.valid
    assert store.get("killed")["reason"] == "MARKET_DATA_DISCONNECTED"


@pytest.mark.parametrize("shock", ["spread", "volatility"])
def test_spread_expansion_and_fivefold_volatility_stop_entries(
    cfg, store, fees, book, instrument, account, decision, shock
):
    from trader.portfolio import Portfolio

    cfg.frequency.maximum_trades_per_hour = 3
    if shock == "spread":
        # The initial 2.1 bps becomes 3.15 bps, crossing the configured 3 bps gate.
        book.asks = {D("60018.9"): D("2")}
    else:
        decision.features["volatility_30s_bps"] = 8 * 5
    with pytest.raises(RiskRejected):
        RiskEngine(cfg, store).authorize(order(), account, Portfolio(), instrument, book, NOW, decision)


def test_crash_releases_process_lock_without_erasing_kill(tmp_path):
    lock = str(tmp_path / "runtime.lockfile")
    ready = tmp_path / "ready"
    program = "from trader.storage.db import process_lock; from pathlib import Path; import sys,time\nwith process_lock(sys.argv[1]):\n Path(sys.argv[2]).write_text('ready')\n time.sleep(30)\n"
    child = subprocess.Popen([sys.executable, "-c", program, lock, str(ready)])
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        with pytest.raises(RuntimeError):
            with process_lock(lock):
                pass
        child.kill()
        child.wait(timeout=3)
        with process_lock(lock):
            pass
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)


def test_mode_database_isolation(cfg, store):
    store.set("mode", "live")
    with pytest.raises(ValueError, match="shared between modes"):
        bind_database(store, cfg)


def test_sqlite_atomic_rollback_and_wal_reopen(tmp_path):
    path = str(tmp_path / "ledger.sqlite")
    store = Store(path)
    store.put_order(order(), new=True)
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.set("paused", True)
            raise RuntimeError("simulated application crash inside transaction")
    assert store.get("paused") is None
    store.close()
    recovered = Store(path)
    assert recovered.order("test-order").status == "intent"
    assert recovered.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    recovered.close()


async def test_backtest_refuses_inconsistent_history_without_fake_recovery(cfg, instrument):
    from trader.backtest import replay

    events = [
        Event(
            kind="snapshot",
            ts_ns=NOW,
            recv_ns=NOW,
            first=1,
            last=1,
            bids=[("60000", "2")],
            asks=[("60001", "1")],
        ),
        Event(kind="delta", ts_ns=NOW + SECOND, recv_ns=NOW + SECOND, first=3, last=3),
    ]
    result, _ = await replay(cfg, events, instrument)
    assert result["operational_incidents"][0]["reason"] == "SEQUENCE_GAP"
    assert result["status"] == "NO DEPLOYABLE EDGE"


def test_model_and_code_evidence_cannot_be_relabelled(cfg, store):
    from trader.models import Fill

    store.put_order(order(), new=True)
    store.fill(
        Fill(
            fill_id="1",
            client_id="test-order",
            ts_ns=NOW,
            side="buy",
            price="60000",
            quantity="0.001",
            fee="0.06",
            maker=True,
            reference_price="60000",
            latency_ms=1,
        )
    )
    store.set("code_sha256", "old-code")
    with pytest.raises(ValueError, match="Code/model changed"):
        bind_database(store, cfg)


def test_drawdown_between_snapshot_points_is_preserved(cfg, store):
    from trader.reporting import report

    risk = RiskEngine(cfg, store)
    risk.observe_equity(D("10000"), NOW)
    risk.observe_equity(D("9900"), NOW + SECOND)
    risk.observe_equity(D("10000"), NOW + 2 * SECOND)
    result = report(store)
    assert result["maximum_drawdown_pct"] == 1
