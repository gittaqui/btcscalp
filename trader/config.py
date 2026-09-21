"""Strict configuration: typos, missing costs, leverage and unsafe modes fail closed."""

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from trader.models import D, StrictModel


class ExchangeConfig(StrictModel):
    name: Literal["gemini"] = "gemini"
    environment: Literal["production", "sandbox"] = "production"
    rest_timeout_seconds: float = Field(default=5, gt=0, le=30)
    requests_per_second: float = Field(default=2, gt=0, le=5)
    reconciliation_seconds: float = Field(default=5, ge=2)
    heartbeat_seconds: float = Field(default=5, ge=1, le=10)
    maximum_retries: int = Field(default=3, ge=0, le=6)
    baseline_btc: D = Field(default=D("0"), ge=0)

    @property
    def rest_url(self):
        return "https://api.sandbox.gemini.com" if self.environment == "sandbox" else "https://api.gemini.com"

    @property
    def ws_url(self):
        return "wss://ws.sandbox.gemini.com" if self.environment == "sandbox" else "wss://ws.gemini.com"


PRESETS = {
    "ultra_low": (1000, 1000, 28800, 1, 3),
    "low": (500, 500, 1200, 3, 36),
    "medium": (250, 250, 360, 10, 100),
    "high": (100, 100, 120, 30, 240),
}


class Frequency(StrictModel):
    frequency_mode: Literal["ultra_low", "low", "medium", "high", "custom"] = "low"
    signal_interval_ms: int = Field(default=500, ge=50)
    decision_interval_ms: int = Field(default=500, ge=50)
    minimum_seconds_between_entries: float = Field(default=1200, ge=0)
    maximum_trades_per_hour: int = Field(default=3, ge=1, le=1000)
    maximum_trades_per_day: int = Field(default=36, ge=1, le=10000)
    cooldown_after_loss_seconds: float = Field(default=300, ge=0)
    cooldown_after_consecutive_losses_seconds: float = Field(default=3600, ge=0)

    def resolved(self):
        value = self.model_copy()
        if self.frequency_mode in PRESETS:
            keys = [
                "signal_interval_ms",
                "decision_interval_ms",
                "minimum_seconds_between_entries",
                "maximum_trades_per_hour",
                "maximum_trades_per_day",
            ]
            for key, number in zip(keys, PRESETS[self.frequency_mode]):
                setattr(value, key, number)
        return value

    @property
    def strictness(self) -> float:
        # Custom mode gets the strictness of its opportunity ceiling.
        count = self.resolved().maximum_trades_per_hour
        return 1 if count <= 3 else 1.5 if count <= 10 else 2


class StrategyConfig(StrictModel):
    family: Literal["micro_momentum", "mean_reversion"] = "micro_momentum"
    active_features: list[Literal["imbalance", "direction"]] = ["imbalance", "direction"]
    model_path: str = "models/model.json"
    minimum_net_edge_bps: float = Field(default=3, ge=0)
    confidence_threshold: float = Field(default=0.95, ge=0.5, lt=1)
    minimum_cell_samples: int = Field(default=100, ge=30)
    label_horizon_seconds: int = Field(default=30, ge=5, le=900)
    warmup_seconds: int = Field(default=900, ge=30, le=3600)
    minimum_liquidity_usd: float = Field(default=10000, gt=0)
    maximum_volatility_bps: float = Field(default=30, gt=0)
    minimum_volatility_bps: float = Field(default=0.05, ge=0)
    imbalance_threshold: float = Field(default=0.2, gt=0, lt=1)
    maximum_fee_burden: float = Field(default=0.7, gt=0, lt=1)
    permitted_regimes: list[str] = ["TRENDING_UP", "RANGING", "LOW_VOLATILITY"]
    degradation_window: int = Field(default=50, ge=20)


class ExecutionConfig(StrictModel):
    maker_timeout_ms: int = Field(default=3000, ge=100)
    maximum_requotes: int = Field(default=1, ge=0, le=5)
    maximum_price_chase_bps: float = Field(default=2, ge=0)
    minimum_fill_probability: float = Field(default=0.2, ge=0, le=1)
    maximum_spread_bps: float = Field(default=3, gt=0)
    slippage_bps: float = Field(default=2, ge=0)
    latency_cost_bps: float = Field(default=1, ge=0)
    adverse_selection_buffer_bps: float = Field(default=2, ge=0)
    simulated_latency_ms: int = Field(default=250, ge=1)
    cancel_latency_ms: int = Field(default=250, ge=1)
    queue_multiplier: float = Field(default=1.5, ge=1)
    participation_fraction: float = Field(default=0.1, gt=0, le=1)
    emergency_limit_bps: float = Field(default=50, gt=0, le=500)
    native_stop_limit_gap_bps: float = Field(default=50, gt=0, le=500)
    exit_retry_seconds: float = Field(default=2, ge=1)


class FeeConfig(StrictModel):
    maker_bps: D | None = Field(default=None, ge=0, le=1000)
    taker_bps: D | None = Field(default=None, ge=0, le=1000)
    source_note: str = ""
    refresh_seconds: int = Field(default=1800, ge=60, le=86400)


class RiskConfig(StrictModel):
    risk_per_trade_pct: D = Field(default=D("0.10"), gt=0, le=1)
    maximum_position_pct: D = Field(default=D("5"), gt=0, le=100)
    maximum_daily_loss_pct: D = Field(default=D("2"), gt=0, le=20)
    maximum_session_loss_pct: D = Field(default=D("2"), gt=0, le=20)
    maximum_drawdown_pct: D = Field(default=D("5"), gt=0, le=30)
    maximum_consecutive_losses: int = Field(default=5, ge=1)
    maximum_open_positions: Literal[1] = 1
    leverage: Literal[1] = 1
    averaging_down: Literal[False] = False
    maximum_order_usd: D = Field(default=D("100"), gt=0)
    maximum_btc_exposure: D = Field(default=D("0.01"), gt=0)
    maximum_usd_exposure: D = Field(default=D("500"), gt=0)
    liquidity_fraction: D = Field(default=D("0.01"), gt=0, le=D("0.1"))
    hard_stop_bps: D = Field(default=D("20"), gt=0)
    volatility_stop_multiplier: D = Field(default=D("3"), ge=1)
    take_profit_bps: D = Field(default=D("40"), gt=0)
    trailing_stop_bps: D = Field(default=D("15"), gt=0)
    break_even_trigger_bps: D = Field(default=D("25"), gt=0)
    time_stop_seconds: int = Field(default=120, ge=1)
    stale_data_ms: int = Field(default=2000, ge=100)
    maximum_latency_ms: int = Field(default=1000, ge=100)
    maximum_clock_drift_ms: int = Field(default=1000, ge=100)
    maximum_api_errors: int = Field(default=3, ge=1)
    maximum_order_rejections: int = Field(default=3, ge=1)
    watchdog_timeout_seconds: int = Field(default=15, ge=5, le=60)
    automatic_kill_reset: Literal[False] = False


class DatabaseConfig(StrictModel):
    path: str = "data/paper.sqlite"
    record_market_events: bool = True
    snapshot_interval_seconds: float = Field(default=5, ge=1)


class MonitoringConfig(StrictModel):
    host: Literal["127.0.0.1", "0.0.0.0"] = "127.0.0.1"
    port: int = Field(default=8080, ge=1024, le=65535)


class NotificationConfig(StrictModel):
    discord_enabled: bool = False


class LoggingConfig(StrictModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class Config(StrictModel):
    exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)
    symbol: Literal["btcusd"] = "btcusd"
    mode: Literal["paper", "sandbox", "live", "backtest"] = "paper"
    frequency: Frequency = Field(default_factory=Frequency)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    fees: FeeConfig = Field(default_factory=FeeConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    paper_starting_usd: D = Field(default=D("10000"), gt=0)
    evidence_path: str = "reports/evidence.json"

    @model_validator(mode="after")
    def consistency(self):
        if self.mode == "sandbox" and self.exchange.environment != "sandbox":
            raise ValueError("Sandbox mode requires sandbox endpoints")
        if self.mode in {"paper", "live"} and self.exchange.environment != "production":
            raise ValueError("Paper/live use production market data")
        if self.mode == "live" and self.exchange.baseline_btc != 0:
            raise ValueError("Live account must start with zero BTC and exclusive bot ownership")
        if self.risk.maximum_session_loss_pct > self.risk.maximum_drawdown_pct:
            raise ValueError("Session loss limit must not exceed drawdown limit")
        return self

    def fingerprint(self) -> str:
        value = self.model_dump(mode="json", include={"symbol", "frequency", "strategy", "execution", "risk"})
        value["strategy"].pop("model_path")
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def model_fingerprint(self) -> str:
        keys = {
            "active_features",
            "family",
            "imbalance_threshold",
            "minimum_cell_samples",
            "label_horizon_seconds",
            "warmup_seconds",
            "minimum_liquidity_usd",
            "maximum_volatility_bps",
            "minimum_volatility_bps",
            "permitted_regimes",
        }
        value = self.strategy.model_dump(mode="json", include=keys)
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def load_config(path: str | Path, mode: str | None = None) -> Config:
    with Path(path).open() as stream:
        data = yaml.safe_load(stream) or {}
    if mode:
        data["mode"] = mode
    return Config.model_validate(data)
