"""Explicit commands; live and sandbox starts require separate startup confirmations."""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from trader.backtest import run_backtest, synthetic_events
from trader.config import load_config
from trader.market_data import Recorder, to_parquet
from trader.models import Instrument
from trader.monitoring import configure_logging
from trader.reporting import report
from trader.storage.db import Store


def parser():
    p = argparse.ArgumentParser(prog="python -m trader")
    p.add_argument(
        "command",
        nargs="?",
        choices=[
            "paper",
            "sandbox",
            "live",
            "backtest",
            "validate",
            "collect",
            "status",
            "health",
            "report",
            "pause",
            "resume",
            "cancel-all",
            "flatten",
            "kill",
            "reset-kill",
            "guardian",
            "dashboard",
            "approve",
            "parquet",
            "synthetic",
            "frequency-advice",
            "supervise",
            "latency",
        ],
    )
    p.add_argument("--mode", choices=["paper", "sandbox", "live"])
    p.add_argument("--config", default="config/config.example.yaml")
    p.add_argument("--confirm")
    p.add_argument("--data")
    p.add_argument("--output")
    p.add_argument("--seconds", type=float)
    p.add_argument("--database")
    p.add_argument("--research")
    p.add_argument("--operator")
    return p


async def dispatch(args):
    command = args.command or args.mode
    if not command:
        raise ValueError("Choose a command or --mode")
    if args.mode and args.command and args.mode != args.command and args.command != "guardian":
        raise ValueError("Conflicting command and --mode")
    config = load_config(args.config, command if command in {"paper", "live", "sandbox"} else args.mode)
    configure_logging(config.logging.level)
    if command in {"paper", "sandbox", "live", "guardian", "collect"}:
        from trader.runtime import collect, guardian, run

        if command == "guardian":
            return await guardian(config, args.confirm)
        if command == "collect":
            return await collect(config, args.output or "data/ticks.jsonl", args.seconds or 3600)
        return await run(config, args.confirm, args.seconds)
    if command == "backtest":
        if not args.data:
            raise ValueError("--data is required")
        result = await run_backtest(config, args.data, args.output or "reports/backtest.json", args.database)
        return {
            "status": result["status"],
            "trades": result["number_of_trades"],
            "expectancy": result["expectancy"],
            "profit_factor": result["profit_factor"],
            "report": args.output or "reports/backtest.json",
        }
    if command == "validate":
        from trader.research import validate

        if not args.data:
            raise ValueError("--data is required")
        result = await validate(config, args.data, args.output or "reports/research.json")
        return {
            "status": result["status"],
            "reasons": result["reasons"],
            "report": args.output or "reports/research.json",
        }
    if command == "approve":
        from trader.safety import create_evidence

        if (
            args.confirm != "APPROVE_LIVE_RESEARCH"
            or not args.operator
            or not args.database
            or not args.research
        ):
            raise ValueError(
                "Require --confirm APPROVE_LIVE_RESEARCH --operator NAME --database PAPER_DB --research REPORT"
            )
        return create_evidence(config, args.research, args.database, args.operator)
    if command == "dashboard":
        from trader.dashboard import serve

        return serve(config)
    if command == "parquet":
        if not args.data or not args.output:
            raise ValueError("--data and --output required")
        to_parquet(args.data, args.output)
        return {"output": args.output}
    if command == "synthetic":
        path = args.output or "data/synthetic.jsonl"
        recorder = Recorder(path, config, "synthetic")
        for event in synthetic_events(int(args.seconds or 3000)):
            recorder.write(event)
        recorder.close()
        manifest_path = Path(path + ".manifest.json")
        manifest = json.loads(manifest_path.read_text())
        manifest["instrument"] = Instrument(
            price_increment="0.01", quantity_increment="0.00000001", minimum_quantity="0.00001"
        ).model_dump(mode="json")
        manifest_path.write_text(json.dumps(manifest, indent=2))
        return {"output": path, "source": "synthetic engineering fixture, never research evidence"}
    if command == "frequency-advice":
        from trader.research import frequency_advice

        if not args.data:
            raise ValueError("--data must point to a JSON map of approved frequency modes to reports")
        reports = json.loads(Path(args.data).read_text())
        return frequency_advice(reports, list(reports))
    if command == "latency":
        import numpy as np

        from trader.exchange.gemini import Gemini

        store = Store(":memory:")
        api = Gemini(config, store)
        values = []
        try:
            for _ in range(10):
                await api.instrument()
                values.append(api.last_latency_ms)
            return {
                "rest_samples": 10,
                "p50_ms": float(np.percentile(values, 50)),
                "p95_ms": float(np.percentile(values, 95)),
                "note": "Also collect a tape; compare recv_ns - ts_ns only with a synchronized clock.",
            }
        finally:
            await api.close()
            store.close()
    path = args.database or config.database.path
    if not Path(path).exists():
        raise ValueError("Database does not exist; start the configured runtime first")
    store = Store(path)
    try:
        if command in {"pause", "resume", "cancel-all", "flatten", "kill", "reset-kill"}:
            if command in {"resume", "flatten", "kill", "reset-kill"} and args.confirm != command.upper():
                raise ValueError("Require --confirm " + command.upper())
            return {
                "command_id": store.command(command),
                "status": "queued; verify acknowledgement with status",
            }
        if command in {"status", "health"}:
            age = (time.time_ns() - store.get("runtime_heartbeat_ns", 0)) / 1e9
            healthy = age < config.risk.watchdog_timeout_seconds and not store.get("killed")
            result = {
                "healthy": healthy,
                "heartbeat_age_seconds": age,
                "killed": store.get("killed"),
                "paused": store.get("paused", False),
                "snapshot": store.get("snapshot"),
                "active_orders": len(store.orders(True)),
                "commands": [
                    dict(row) for row in store.db.execute("SELECT * FROM commands ORDER BY id DESC LIMIT 10")
                ],
            }
            if command == "health" and not healthy:
                print(json.dumps(result, indent=2))
                raise SystemExit(1)
            return result
        result = report(store)
        if command == "supervise":
            # Read-only summary: no arbitrary order or configuration interface is exposed to an LLM.
            return {
                "recommendation": "PAUSE_AND_RESEARCH"
                if (result["expectancy"] or 0) <= 0
                else "CONTINUE_OBSERVATION",
                "net_edge_bps": result["net_edge_bps"],
                "fee_drag_bps": result["fee_drag_bps"],
                "slippage_drag_bps": result["slippage_drag_bps"],
                "incidents": result["operational_incidents"],
            }
        if args.output:
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(json.dumps(result, indent=2))
        return result
    finally:
        store.close()


def main():
    from trader.exchange.base import ExchangeError

    args = parser().parse_args()
    try:
        result = asyncio.run(dispatch(args))
        if result is not None:
            print(json.dumps(result, indent=2, default=str))
    except (ValueError, OSError, ExchangeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
