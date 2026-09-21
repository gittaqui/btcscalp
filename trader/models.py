"""Shared domain objects. Use Decimal for all monetary and quantity arithmetic."""

from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

D = Decimal
ZERO = D("0")
BPS = D("10000")
SECOND = 1_000_000_000


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)


class Regime(StrEnum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LIQUIDITY_SHOCK = "LIQUIDITY_SHOCK"
    ABNORMAL = "ABNORMAL"
    UNCLASSIFIED = "UNCLASSIFIED"


class Event(StrictModel):
    kind: Literal["snapshot", "delta", "trade", "disconnect"]
    symbol: Literal["btcusd"] = "btcusd"
    ts_ns: int = Field(ge=0)
    recv_ns: int = Field(ge=0)
    first: int = Field(default=0, ge=0)
    last: int = Field(default=0, ge=0)
    bids: list[tuple[Decimal, Decimal]] = Field(default_factory=list)
    asks: list[tuple[Decimal, Decimal]] = Field(default_factory=list)
    price: Decimal = Field(default=ZERO, ge=0)
    quantity: Decimal = Field(default=ZERO, ge=0)
    aggressor: Literal["buy", "sell", "unknown"] = "unknown"
    trade_id: str = ""


class Instrument(StrictModel):
    symbol: Literal["btcusd"] = "btcusd"
    price_increment: Decimal = Field(gt=0)
    quantity_increment: Decimal = Field(gt=0)
    minimum_quantity: Decimal = Field(gt=0)
    status: str = "open"

    def quantity(self, value: Decimal) -> Decimal:
        return (value / self.quantity_increment).to_integral_value(
            rounding=ROUND_DOWN
        ) * self.quantity_increment

    def price(self, value: Decimal) -> Decimal:
        return (value / self.price_increment).to_integral_value(rounding=ROUND_DOWN) * self.price_increment

    def validate_order(self, order: "Order") -> None:
        if self.status not in {"open", "limit_only", "post_only"}:
            raise ValueError("MARKET_NOT_OPEN")
        if self.status == "post_only" and order.kind != "maker":
            raise ValueError("POST_ONLY_MARKET")
        if order.quantity < self.minimum_quantity or self.quantity(order.quantity) != order.quantity:
            raise ValueError("INVALID_QUANTITY")
        if self.price(order.price) != order.price:
            raise ValueError("INVALID_PRICE_INCREMENT")


class Fees(StrictModel):
    maker_bps: Decimal = Field(ge=0, le=1000)
    taker_bps: Decimal = Field(ge=0, le=1000)
    source: str
    fetched_ns: int = Field(ge=0)

    def cost(self, price: Decimal, quantity: Decimal, maker: bool) -> Decimal:
        return price * quantity * (self.maker_bps if maker else self.taker_bps) / BPS


class Order(StrictModel):
    client_id: str
    side: Literal["buy", "sell"]
    kind: Literal["maker", "ioc", "stop"]
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    created_ns: int = Field(ge=0)
    reference_price: Decimal = Field(gt=0)
    stop_price: Decimal | None = Field(default=None, gt=0)
    exchange_id: str | None = None
    status: Literal["intent", "unknown", "open", "closed", "cancelled", "rejected"] = "intent"
    filled: Decimal = Field(default=ZERO, ge=0)
    regime: str = "UNCLASSIFIED"
    reason: str = ""

    @model_validator(mode="after")
    def check(self):
        if self.filled > self.quantity:
            raise ValueError("Overfilled order")
        if self.kind == "stop" and (
            self.side != "sell" or self.stop_price is None or self.stop_price <= self.price
        ):
            raise ValueError("Sell stop requires stop price above limit price")
        return self

    @property
    def remaining(self) -> Decimal:
        return self.quantity - self.filled


class Fill(StrictModel):
    fill_id: str
    client_id: str
    ts_ns: int
    side: Literal["buy", "sell"]
    price: Decimal = Field(gt=0)
    quantity: Decimal = Field(gt=0)
    fee: Decimal = Field(ge=0)
    maker: bool
    reference_price: Decimal = Field(gt=0)
    latency_ms: float = Field(ge=0)
    regime: str = "UNCLASSIFIED"

    @property
    def slippage(self) -> Decimal:
        # Signed implementation shortfall; passive fills can improve the reference.
        sign = 1 if self.side == "buy" else -1
        return (self.price - self.reference_price) * self.quantity * sign


class Account(StrictModel):
    usd: Decimal = Field(ge=0)
    btc: Decimal = Field(default=ZERO, ge=0)
    available_usd: Decimal = Field(ge=0)
    available_btc: Decimal = Field(default=ZERO, ge=0)
    asof_ns: int = Field(ge=0)

    def equity(self, mark: Decimal) -> Decimal:
        return self.usd + self.btc * mark


class Decision(StrictModel):
    ts_ns: int
    regime: str
    features: dict[str, float | None]
    signal: str = "NO_TRADE"
    confidence: float = 0
    expected_gross_edge_bps: float = 0
    costs: dict[str, float] = Field(default_factory=dict)
    expected_total_cost_bps: float = 0
    expected_net_edge_bps: float = 0
    fill_probability: float = 0
    decision: str = "NO_TRADE"
    reason: str = "NO_EDGE"
