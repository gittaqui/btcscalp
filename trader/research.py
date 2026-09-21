"""Chronological fitting, untouched test, walk-forward folds and predeclared stresses."""

import json
from collections import deque
from pathlib import Path

from trader.backtest import configured_fees, replay, tape_instrument
from trader.features import FeatureEngine
from trader.market_data import file_hash, read_events
from trader.models import SECOND, D
from trader.orderbook import OrderBook
from trader.strategy import EdgeModel


def labeled_samples(events, config):
    book, engine, pending = OrderBook(), FeatureEngine(config), deque()
    last_sample = 0
    horizon = config.strategy.label_horizon_seconds * SECOND
    for event in events:
        if event.kind == "disconnect":
            pending.clear()
            book.reset()
            engine = FeatureEngine(config)
            continue
        book.apply(event)
        if event.kind == "trade":
            engine.trade(event)
        if not book.valid:
            continue
        engine.sample(book, event.recv_ns)
        if event.recv_ns - last_sample < config.frequency.resolved().signal_interval_ms * 1_000_000:
            continue
        now = event.recv_ns
        feature = engine.calculate(book, now)
        while pending and pending[0].ts_ns + horizon <= now:
            old = pending.popleft()
            # Do not label across missing data or arbitrary future gaps.
            if now - old.ts_ns <= horizon + 2 * SECOND:
                yield old, (float(book.mid) / old.values["mid"] - 1) * 10000, now
        if feature.ready:
            pending.append(feature)
        last_sample = now


async def validate(config, path, output):
    manifest = json.loads(Path(str(path) + ".manifest.json").read_text())
    digest = file_hash(path)
    if manifest.get("sha256") != digest:
        raise ValueError("DATA_HASH_MISMATCH")
    start, end = manifest["start_ns"], manifest["end_ns"]
    if not start or not end or end <= start:
        raise ValueError("EMPTY_DATASET")
    span = end - start
    train_end, validation_end = start + int(span * 0.6), start + int(span * 0.8)
    embargo = (config.strategy.label_horizon_seconds + config.risk.time_stop_seconds) * SECOND
    if (
        min(train_end - start, validation_end - train_end, end - validation_end)
        <= embargo + config.strategy.warmup_seconds * SECOND
    ):
        raise ValueError("DATASET_TOO_SHORT_FOR_PURGED_PARTITIONS")
    instrument, fees = tape_instrument(path), configured_fees(config)
    # Baseline family is an operator choice. Only train sees labels; validation may reject it.
    model = EdgeModel.fit(labeled_samples(read_events(path), config), config, train_end, digest)
    validation, _ = await replay(
        config,
        read_events(path),
        instrument,
        model,
        fees,
        start_ns=train_end + embargo,
        end_ns=validation_end,
    )
    ablations = {}
    for removed in config.strategy.active_features:
        ablated = config.model_copy(deep=True)
        ablated.strategy.active_features = [key for key in config.strategy.active_features if key != removed]
        ablated_model = EdgeModel.fit(labeled_samples(read_events(path), ablated), ablated, train_end, digest)
        ablation, _ = await replay(
            ablated,
            read_events(path),
            instrument,
            ablated_model,
            fees,
            start_ns=train_end + embargo,
            end_ns=validation_end,
        )
        ablations[removed] = {
            "validation": ablation,
            "incremental_expectancy": (validation["expectancy"] - ablation["expectancy"])
            if validation["expectancy"] is not None and ablation["expectancy"] is not None
            else None,
        }
    test, test_trades = await replay(
        config, read_events(path), instrument, model, fees, start_ns=validation_end + embargo, end_ns=end + 1
    )
    walk_forward = []
    for train_fraction, test_fraction in ((0.3, 0.45), (0.45, 0.6), (0.6, 0.8)):
        fold_end = start + int(span * train_fraction)
        fold_model = EdgeModel.fit(labeled_samples(read_events(path), config), config, fold_end, digest)
        fold, _ = await replay(
            config,
            read_events(path),
            instrument,
            fold_model,
            fees,
            start_ns=fold_end + embargo,
            end_ns=start + int(span * test_fraction),
        )
        walk_forward.append({"train_end_ns": fold_end, "result": fold})
    stress = {}
    for name in ("fees_150pct", "slippage_200pct", "latency_500ms", "latency_2000ms", "latency_10000ms"):
        changed, adjusted_fees = config.model_copy(deep=True), fees.model_copy()
        if name == "fees_150pct":
            adjusted_fees.maker_bps *= D("1.5")
            adjusted_fees.taker_bps *= D("1.5")
        elif name == "slippage_200pct":
            changed.execution.slippage_bps *= 2
        else:
            changed.execution.simulated_latency_ms = int(name.split("_")[1].removesuffix("ms"))
        result, _ = await replay(
            changed,
            read_events(path),
            instrument,
            model,
            adjusted_fees,
            start_ns=validation_end + embargo,
            end_ns=end + 1,
        )
        stress[name] = result
    parameter_sensitivity = {}
    for factor in (0.9, 1.1):
        changed = config.model_copy(deep=True)
        changed.strategy.imbalance_threshold *= factor
        perturbed = EdgeModel.fit(labeled_samples(read_events(path), changed), changed, train_end, digest)
        result, _ = await replay(
            changed,
            read_events(path),
            instrument,
            perturbed,
            fees,
            start_ns=validation_end + embargo,
            end_ns=end + 1,
        )
        parameter_sensitivity[str(factor)] = result
    reasons = list(test["robustness"]["reasons"])
    if any(
        value["incremental_expectancy"] is None or value["incremental_expectancy"] <= 0
        for value in ablations.values()
    ):
        reasons.append("FEATURE_INCREMENTAL_VALUE_NOT_ESTABLISHED")
    if manifest.get("synthetic") or manifest.get("source") != "gemini-production":
        reasons.append("NON_PRODUCTION_DATA_NOT_EVIDENCE")
    if (validation["expectancy"] or 0) <= 0:
        reasons.append("VALIDATION_NOT_POSITIVE")
    if any((fold["result"]["expectancy"] or 0) <= 0 for fold in walk_forward):
        reasons.append("WALK_FORWARD_NOT_POSITIVE")
    # A deliberately severe 10 s latency is a failure-control experiment, not a profitability target.
    required_stresses = [value for name, value in stress.items() if name != "latency_10000ms"]
    if any(
        (r["expectancy"] or 0) <= 0 or r["maximum_drawdown_pct"] > 8 or D(r["open_btc"]) > 0
        for r in required_stresses + list(parameter_sensitivity.values())
    ):
        reasons.append("SENSITIVITY_NOT_ROBUST")
    model.save(config.strategy.model_path)
    result = {
        "schema": 1,
        "status": "RESEARCH_GATES_PASSED" if not reasons else "NO DEPLOYABLE EDGE",
        "reasons": reasons,
        "data_sha256": digest,
        "model_sha256": file_hash(config.strategy.model_path),
        "config_fingerprint": config.fingerprint(),
        "source": manifest,
        "partitions": {
            "train_end_ns": train_end,
            "validation_end_ns": validation_end,
            "test_end_ns": end,
            "embargo_ns": embargo,
        },
        "validation": validation,
        "test": test,
        "walk_forward": walk_forward,
        "stress": stress,
        "parameter_sensitivity": parameter_sensitivity,
        "feature_ablations": ablations,
        "rolling_50_trades": [
            {
                "start_trade": i,
                "trades": len(test_trades[i : i + 50]),
                "expectancy": sum(float(t["net_pnl"]) for t in test_trades[i : i + 50])
                / len(test_trades[i : i + 50]),
            }
            for i in range(0, len(test_trades), 50)
        ],
        "selection_policy": "No tuning on held-out test. Changing anything after reviewing it requires a new test period.",
    }
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(json.dumps(result, indent=2))
    return result


def frequency_advice(reports, approved_modes):
    """Advisory only; never mutates configuration or routes orders."""
    eligible = []
    for mode, result in reports.items():
        if mode not in approved_modes or not result.get("robustness", {}).get("passed"):
            continue
        span_hours = (result["last_ns"] - result["first_ns"]) / SECOND / 3600
        if span_hours <= 0 or (result["expectancy"] or 0) <= 0:
            continue
        eligible.append(
            {
                "mode": mode,
                "expected_trades_hour": result["number_of_trades"] / span_hours,
                "net_edge_bps": result["net_edge_bps"],
                "gross_edge_bps": result["gross_edge_bps"],
                "fee_drag_bps": result["fee_drag_bps"],
                "slippage_drag_bps": result["slippage_drag_bps"],
                "expected_hourly_pnl": result["net_profit"] / span_hours,
                "drawdown_pct": result["maximum_drawdown_pct"],
                "confidence_lower_expectancy": result["robustness"]["lower_expectancy_95"],
            }
        )
    eligible.sort(key=lambda x: x["expected_hourly_pnl"], reverse=True)
    return {"recommendation": eligible[0]["mode"] if eligible else "NO TRADE", "comparisons": eligible}
