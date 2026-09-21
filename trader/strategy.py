"""Small, interpretable conditional-return model. No shipped model or invented edge."""

import json
import math
from pathlib import Path
from statistics import NormalDist, fmean, stdev

from trader.config import Config
from trader.features import FeatureSet
from trader.models import Decision, Fees


def cell_key(features: FeatureSet, config: Config) -> str | None:
    v = features.values
    if not features.ready or features.regime not in config.strategy.permitted_regimes:
        return None
    threshold = config.strategy.imbalance_threshold
    use_imbalance = "imbalance" in config.strategy.active_features
    use_direction = "direction" in config.strategy.active_features
    if config.strategy.family == "micro_momentum":
        if (use_imbalance and v["imbalance"] < threshold) or (
            use_direction and (v["return_5s_bps"] or 0) <= 0
        ):
            return None
    else:
        if (use_direction and v["zscore"] > -1) or (use_imbalance and v["imbalance"] < threshold):
            return None
    return str(features.regime) + (
        (":strong" if v["imbalance"] >= 0.6 else ":moderate") if use_imbalance else ":all"
    )


class EdgeModel:
    def __init__(self, cells=None, metadata=None):
        self.cells = cells or {}
        self.metadata = metadata or {}

    @classmethod
    def load(cls, path):
        if not Path(path).exists():
            return cls()  # Deliberately abstain without evidence.
        raw = json.loads(Path(path).read_text())
        if raw.get("schema") != 1:
            raise ValueError("Unsupported model schema")
        for cell in raw["cells"].values():
            if cell["n"] < 1 or any(
                not math.isfinite(float(cell[key])) for key in ("mean", "se", "confidence")
            ):
                raise ValueError("Invalid model values")
            if cell["se"] < 0 or not 0 <= cell["confidence"] <= 1:
                raise ValueError("Invalid uncertainty")
        return cls(raw["cells"], raw["metadata"])

    def save(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps({"schema": 1, "cells": self.cells, "metadata": self.metadata}, indent=2)
        )

    @classmethod
    def fit(cls, samples, config, train_end_ns, source_hash):
        buckets = {}
        last_end = -1
        for feature, gross_return, label_end in samples:
            # Labels crossing the training cutoff are never admitted; samples do not overlap.
            if label_end >= train_end_ns or feature.ts_ns <= last_end:
                continue
            key = cell_key(feature, config)
            if key:
                buckets.setdefault(key, []).append(float(gross_return))
                last_end = label_end
        cells = {}
        for key, values in buckets.items():
            if len(values) < config.strategy.minimum_cell_samples:
                continue
            mean = fmean(values)
            se = stdev(values) / math.sqrt(len(values))
            confidence = NormalDist().cdf(mean / se) if se else (1 if mean > 0 else 0)
            cells[key] = {"n": len(values), "mean": mean, "se": se, "confidence": confidence}
        return cls(
            cells,
            {
                "train_end_ns": train_end_ns,
                "source_sha256": source_hash,
                "family": config.strategy.family,
                "fingerprint": config.model_fingerprint(),
                "confidence_definition": "normal approximation for positive conditional mean, not win probability",
            },
        )

    def predict(self, feature, config):
        if self.metadata.get("fingerprint") != config.model_fingerprint():
            return None
        if feature.ts_ns <= self.metadata.get("train_end_ns", feature.ts_ns):
            return None
        cell = self.cells.get(cell_key(feature, config))
        if not cell or cell["n"] < config.strategy.minimum_cell_samples:
            return None
        return cell


class Strategy:
    def __init__(self, config: Config, model: EdgeModel, fees: Fees):
        self.config, self.model, self.fees = config, model, fees

    def decide(self, feature: FeatureSet, queue_ahead: float) -> Decision:
        v, c = feature.values, self.config
        result = Decision(ts_ns=feature.ts_ns, regime=str(feature.regime), features=v)
        multiplier = c.frequency.strictness
        if not feature.ready:
            result.reason = "WARMUP"
            return result
        if v["spread_bps"] >= c.execution.maximum_spread_bps / multiplier:
            result.reason = "SPREAD_TOO_WIDE"
            return result
        if min(v["bid_depth_usd"], v["ask_depth_usd"]) < c.strategy.minimum_liquidity_usd * multiplier:
            result.reason = "LIQUIDITY_TOO_LOW"
            return result
        if v["volatility_30s_bps"] > c.strategy.maximum_volatility_bps / multiplier:
            result.reason = "VOLATILITY_TOO_HIGH"
            return result
        prediction = self.model.predict(feature, c)
        if prediction is None:
            result.reason = "NO_MODEL_EVIDENCE"
            return result
        result.signal = c.strategy.family
        result.confidence = prediction["confidence"]
        # Charge the full spread and taker exit; no assumed maker exit discount.
        result.costs = {
            "entry_fee_bps": float(self.fees.maker_bps),
            "exit_fee_bps": float(self.fees.taker_bps),
            "spread_bps": v["spread_bps"],
            "slippage_bps": c.execution.slippage_bps,
            "latency_bps": c.execution.latency_cost_bps,
            "adverse_selection_bps": c.execution.adverse_selection_buffer_bps,
        }
        result.expected_gross_edge_bps = prediction["mean"]
        result.expected_total_cost_bps = sum(result.costs.values())
        # Lower confidence bound on expected gross edge, not the point estimate, must clear costs.
        lower = prediction["mean"] - 1.96 * prediction["se"]
        result.expected_net_edge_bps = lower - result.expected_total_cost_bps
        sell_rate = v["sell_volume_30s"] / 30
        flow = sell_rate * c.execution.maker_timeout_ms / 1000 * c.execution.participation_fraction
        result.fill_probability = max(0, min(1, 1 - math.exp(-flow / max(queue_ahead, 1e-12))))
        if result.expected_net_edge_bps <= c.strategy.minimum_net_edge_bps * multiplier:
            result.reason = "NO_EDGE"
        elif result.confidence <= c.strategy.confidence_threshold:
            result.reason = "LOW_CONFIDENCE"
        elif (
            float(self.fees.maker_bps + self.fees.taker_bps) / max(lower, 1e-12)
            > c.strategy.maximum_fee_burden / multiplier
        ):
            result.reason = "FEE_BURDEN"
        elif result.fill_probability < min(0.95, c.execution.minimum_fill_probability * multiplier):
            result.reason = "LOW_FILL_PROBABILITY"
        else:
            result.decision, result.reason = "BUY", "EDGE_AFTER_COSTS"
        return result
