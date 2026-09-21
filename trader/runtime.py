"""Async service lifecycle, durable operator controls, separate watchdog process."""

import asyncio
import contextlib
import json
import signal
import time
from pathlib import Path

from trader.backtest import configured_fees
from trader.engine import Engine
from trader.exchange.gemini import Gemini
from trader.execution import Controller, LiveBroker
from trader.market_data import Recorder, file_hash, market_stream
from trader.models import SECOND, D, Event
from trader.monitoring import Alerts
from trader.orderbook import OrderBook
from trader.paper import PaperBroker
from trader.safety import authorize_start, code_hash
from trader.storage.db import Store, process_lock
from trader.strategy import EdgeModel


async def process_commands(engine):
    store = engine.store
    for command in store.pending_commands():
        action, now = command["action"], time.time_ns()
        try:
            if action in {"pause", "cancel-all", "kill"}:
                store.set("paused", True)
                await engine.controller.cancel_all()
                if action == "kill":
                    store.kill("OPERATOR_KILL")
                    store.set("flatten_requested", True)
            elif action == "flatten":
                store.set("paused", True)
                store.set("flatten_requested", True)
                await engine.controller.flatten(now)
            elif action == "resume":
                if store.get("killed"):
                    raise ValueError("Reset kill explicitly after reconciliation")
                if engine.book.stale(now, engine.config.risk.stale_data_ms):
                    raise ValueError("Fresh market data required")
                await engine.broker.reconcile()
                store.set("paused", False)
            elif action == "reset-kill":
                await engine.broker.reconcile()
                if engine.broker.portfolio().quantity or store.orders(True):
                    raise ValueError("Must be flat with no outstanding orders before reset")
                if engine.book.stale(now, engine.config.risk.stale_data_ms):
                    raise ValueError("Fresh synchronized book required")
                store.set("killed", None)
                store.set("paused", True)
                store.set("order_rejections", 0)
                # Loss baselines deliberately survive reset; a breached loss stop will re-latch.
            store.finish_command(command["id"], "Applied; resume remains explicit after kill reset")
        except Exception as exc:
            store.finish_command(command["id"], type(exc).__name__ + ": " + str(exc)[:160], success=False)


def bind_database(store, config):
    existing = store.get("mode")
    if existing and existing != config.mode:
        raise ValueError("Database cannot be shared between modes")
    fingerprint = config.fingerprint()
    previous = store.get("config_fingerprint")
    if previous and previous != fingerprint and (store.orders(True) or store.fills()):
        raise ValueError("Configuration changed: use a new flat evaluation database")
    model_digest = (
        file_hash(config.strategy.model_path) if Path(config.strategy.model_path).exists() else None
    )
    if store.fills() and (
        store.get("code_sha256") != code_hash() or store.get("model_sha256") != model_digest
    ):
        raise ValueError(
            "Code/model changed: prior evidence cannot be relabeled; reconcile and start a new flat evaluation"
        )
    store.set("mode", config.mode)
    store.set("config_fingerprint", fingerprint)
    store.set(
        "source", "gemini-production" if config.exchange.environment == "production" else "gemini-sandbox"
    )
    store.set("code_sha256", code_hash())
    store.set("starting_equity", str(config.paper_starting_usd))
    store.set("model_sha256", model_digest)


async def run(config, confirmation=None, seconds=None):
    authorize_start(config, confirmation)
    with process_lock(config.database.path + ".runtime.lockfile"):
        store = Store(config.database.path)
        bind_database(store, config)
        entry, guard = Gemini(config, store), Gemini(config, store, guard=True)
        shutdown, wakeup = asyncio.Event(), asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, shutdown.set)
        tasks = []
        alerts = Alerts(config.notifications.discord_enabled, store)
        tasks.append(asyncio.create_task(alerts.run()))
        engine = None
        try:
            instrument = await entry.instrument()
            if config.mode == "paper":
                fees = await entry.fees() if entry.key else configured_fees(config, time.time_ns())
                broker = PaperBroker(config, store, fees, OrderBook())
                if store.orders(True):
                    store.kill("PAPER_RESTART_QUEUE_POSITION_UNKNOWN")
                    broker.advance(Event(kind="disconnect", ts_ns=time.time_ns(), recv_ns=time.time_ns()))
            else:
                if not entry.key or not guard.key or entry.key == guard.key:
                    raise ValueError("Separate entry and protection keys are required")
                fees = await entry.fees()
                authorize_start(config, confirmation, fees)
                broker = LiveBroker(config, store, entry, guard)
                await broker.reconcile()
                # A prior process death latches a kill; no implicit restart into trading.
                if store.get("runtime_was_running"):
                    store.kill("UNCLEAN_PROCESS_RESTART")
                heartbeat = store.get("guardian_heartbeat_ns", 0)
                if time.time_ns() - heartbeat > config.risk.watchdog_timeout_seconds * SECOND:
                    raise ValueError("Start the independent guardian before live/sandbox runtime")
                tasks.append(asyncio.create_task(entry.order_notifications(wakeup, shutdown)))
            store.set("fees", fees.model_dump(mode="json"))
            store.set("instrument", instrument.model_dump(mode="json"))
            book = broker.book if isinstance(broker, PaperBroker) else OrderBook()
            engine = Engine(
                config, store, broker, book, instrument, fees, EdgeModel.load(config.strategy.model_path)
            )
            queue = asyncio.Queue(maxsize=256)

            async def receive():
                async for event in market_stream(config, shutdown):
                    try:
                        queue.put_nowait(event)
                    except asyncio.QueueFull:
                        store.kill("MARKET_DATA_QUEUE_OVERFLOW")
                        shutdown.set()
                        return

            tasks.append(asyncio.create_task(receive()))
            started = time.monotonic()
            last_reconcile = last_heartbeat = last_fee = 0.0
            last_fill_count = len(store.fills())
            last_trade_count = len(broker.portfolio().trades)
            errors = 0
            store.set("runtime_was_running", True)
            alerts.send("STARTUP", config.mode)
            while not shutdown.is_set():
                for task in tasks:
                    if task.done() and not task.cancelled() and task.exception() is not None:
                        raise RuntimeError("BACKGROUND_SERVICE_FAILED") from task.exception()
                if seconds is not None and time.monotonic() - started >= seconds:
                    break
                now, mono = time.time_ns(), time.monotonic()
                try:
                    # The same nonblocking cross-process lock serializes all order mutations with guardian.
                    with process_lock(config.database.path + ".execution.lockfile"):
                        if config.mode != "paper":
                            if mono - last_heartbeat >= config.exchange.heartbeat_seconds:
                                await entry.heartbeat()
                                last_heartbeat = mono
                            if (
                                wakeup.is_set()
                                or mono - last_reconcile >= config.exchange.reconciliation_seconds
                            ):
                                await broker.reconcile()
                                store.set("exchange_status", "reconciled")
                                wakeup.clear()
                                last_reconcile = mono
                                await engine.controller.protect(time.time_ns())
                            if (
                                max(entry.last_latency_ms, guard.last_latency_ms)
                                > config.risk.maximum_latency_ms
                            ):
                                store.kill("ABNORMAL_REST_LATENCY")
                            if (
                                now - store.get("guardian_heartbeat_ns", 0)
                                > config.risk.watchdog_timeout_seconds * SECOND
                            ):
                                store.kill("GUARDIAN_UNAVAILABLE")
                        if mono - last_fee >= config.fees.refresh_seconds:
                            if entry.key:
                                fees = await entry.fees()
                                if config.mode != "paper":
                                    authorize_start(config, confirmation, fees)
                                engine.strategy.fees = engine.controller.fees = fees
                                if isinstance(broker, PaperBroker):
                                    broker.fees = fees
                                store.set("fees", fees.model_dump(mode="json"))
                            last_fee = mono
                        await process_commands(engine)
                        try:
                            event = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            event = None
                        if event:
                            if now - event.recv_ns > config.risk.stale_data_ms * 1_000_000:
                                store.kill("PROCESSING_BACKLOG")
                            await engine.event(event)
                        elif book.valid and book.stale(time.time_ns(), config.risk.stale_data_ms):
                            store.kill("STALE_DATA")
                            await engine.controller.cancel_all()
                        if store.get("killed"):
                            alerts.send("KILL_SWITCH", store.get("killed")["reason"])
                            await engine.controller.cancel_all()
                            if broker.portfolio().quantity:
                                await engine.controller.flatten(time.time_ns(), "KILL_SWITCH")
                        count = len(store.fills())
                        if count > last_fill_count:
                            alerts.send("TRADE", f"{count - last_fill_count} reconciled fills")
                            last_fill_count = count
                        portfolio = broker.portfolio()
                        if len(portfolio.trades) > last_trade_count:
                            risk_budget = (
                                broker.account().equity(book.bid) * config.risk.risk_per_trade_pct / 100
                                if book.valid
                                else D("0")
                            )
                            if any(t["net_pnl"] < -risk_budget for t in portfolio.trades[last_trade_count:]):
                                alerts.send(
                                    "LARGE_LOSS", "Closed loss exceeded the configured per-trade risk budget"
                                )
                            last_trade_count = len(portfolio.trades)
                        store.set("runtime_heartbeat_ns", time.time_ns())
                        errors = 0
                except RuntimeError as exc:
                    if str(exc) == "Another process owns this runtime":
                        await asyncio.sleep(0.1)
                        continue
                    errors += 1
                    store.kill("EXECUTION_OR_RECONCILIATION_ERROR")
                    store.set("exchange_status", "unavailable_or_inconsistent")
                    alerts.send("API_OR_RECONCILIATION_ERROR", type(exc).__name__)
                    if config.mode != "paper":
                        with contextlib.suppress(Exception):
                            await entry.cancel_session()
                except Exception as exc:
                    errors += 1
                    store.kill("UNEXPECTED_RUNTIME_ERROR")
                    alerts.send("RUNTIME_ERROR", type(exc).__name__)
                if errors >= config.risk.maximum_api_errors:
                    shutdown.set()
                if queue.empty():
                    await asyncio.sleep(0.02)
        finally:
            shutdown.set()
            if engine:
                with contextlib.suppress(Exception):
                    with process_lock(config.database.path + ".execution.lockfile"):
                        await engine.controller.cancel_all()
                        # Leave native protection intact for a non-flat graceful shutdown.
                        if engine.broker.portfolio().quantity:
                            store.kill("SHUTDOWN_WITH_POSITION")
                            await engine.controller.protect(time.time_ns())
                        else:
                            store.set("runtime_was_running", False)
            alerts.send("SHUTDOWN", config.mode)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await entry.close()
            await guard.close()
            store.close()


async def guardian(config, confirmation):
    """Independent OS process. Native stops remain if both this process and strategy fail."""
    authorize_start(config, confirmation)
    if config.mode not in {"live", "sandbox"}:
        raise ValueError("Guardian operates on live/sandbox accounts only")
    with process_lock(config.database.path + ".guardian.lockfile"):
        store = Store(config.database.path)
        # Use GUARD key for all guardian calls; never keeps the entry heartbeat session alive.
        api = Gemini(config, store, guard=True)
        broker = LiveBroker(config, store, api, api)
        shutdown = asyncio.Event()
        alerts = Alerts(config.notifications.discord_enabled, store)
        alert_task = asyncio.create_task(alerts.run())
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, shutdown.set)
        try:
            instrument, fees = await api.instrument(), await api.fees()
            authorize_start(config, confirmation, fees)
            book = OrderBook()
            controller = Controller(config, store, broker, instrument, fees, book)

            async def receive():
                async for event in market_stream(config, shutdown):
                    book.apply(event)

            task = asyncio.create_task(receive())
            try:
                while not shutdown.is_set():
                    now = time.time_ns()
                    store.set("guardian_heartbeat_ns", now)
                    heartbeat = store.get("runtime_heartbeat_ns", 0)
                    stale = (
                        heartbeat
                        and store.get("runtime_was_running")
                        and now - heartbeat > config.risk.watchdog_timeout_seconds * SECOND
                    )
                    needs_help = (stale or store.get("killed")) and bool(
                        store.orders(True) or broker.portfolio().quantity
                    )
                    if task.done():
                        raise RuntimeError("GUARDIAN_MARKET_FEED_STOPPED")
                    if needs_help:
                        try:
                            with process_lock(config.database.path + ".execution.lockfile"):
                                store.kill("WATCHDOG_OR_LATCHED_STOP")
                                alerts.send("WATCHDOG", "Cancelling entry risk; reconciling inventory")
                                await broker.reconcile()
                                await controller.cancel_all()
                                await controller.flatten(time.time_ns(), "WATCHDOG")
                                await controller.protect(time.time_ns())
                        except Exception as exc:
                            alerts.send("WATCHDOG_FAILURE", type(exc).__name__)
                    try:
                        await asyncio.wait_for(shutdown.wait(), 1)
                    except TimeoutError:
                        pass
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        finally:
            alert_task.cancel()
            await asyncio.gather(alert_task, return_exceptions=True)
            await api.close()
            store.close()


async def collect(config, path, seconds):
    store = Store(":memory:")
    api = Gemini(config, store)
    recorder = None
    try:
        instrument = await api.instrument()
        recorder = Recorder(
            path,
            config,
            "gemini-production" if config.exchange.environment == "production" else "gemini-sandbox",
        )
        shutdown = asyncio.Event()

        async def stop_later():
            await asyncio.sleep(seconds)
            shutdown.set()

        timer = asyncio.create_task(stop_later())
        try:
            async for event in market_stream(config, shutdown):
                recorder.write(event)
        finally:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
            recorder.close()
            manifest_path = Path(path + ".manifest.json")
            data = json.loads(manifest_path.read_text())
            data["instrument"] = instrument.model_dump(mode="json")
            manifest_path.write_text(json.dumps(data, indent=2))
    finally:
        await api.close()
        store.close()
