"""Average-cost ledger. A trade is a complete flat-to-flat episode, not each partial fill."""

from dataclasses import dataclass, field

from trader.models import ZERO, D, Fill


@dataclass
class Portfolio:
    quantity: D = ZERO
    entry_price: D = ZERO
    entry_ns: int = 0
    peak_price: D = ZERO
    realized_gross: D = ZERO
    fees: D = ZERO
    slippage: D = ZERO
    maker_fees: D = ZERO
    taker_fees: D = ZERO
    cash_change: D = ZERO
    trades: list[dict] = field(default_factory=list)
    current: dict | None = None

    def apply(self, fill: Fill):
        self.fees += fill.fee
        self.slippage += fill.slippage
        if fill.maker:
            self.maker_fees += fill.fee
        else:
            self.taker_fees += fill.fee
        if fill.side == "buy":
            if self.quantity == 0:
                self.entry_ns = fill.ts_ns
                self.peak_price = fill.price
                self.current = {
                    "entry_ns": fill.ts_ns,
                    "regime": fill.regime,
                    "gross_pnl": ZERO,
                    "fees": ZERO,
                    "slippage": ZERO,
                    "entry_notional": ZERO,
                    "maker_fills": 0,
                    "taker_fills": 0,
                }
            cost = self.entry_price * self.quantity + fill.price * fill.quantity
            self.quantity += fill.quantity
            self.entry_price = cost / self.quantity
            self.current["entry_notional"] += fill.price * fill.quantity
            self.cash_change -= fill.price * fill.quantity + fill.fee
        else:
            if fill.quantity > self.quantity or self.current is None:
                raise ValueError("UNTRACKED_SELL_OR_NEGATIVE_INVENTORY")
            gross = (fill.price - self.entry_price) * fill.quantity
            self.quantity -= fill.quantity
            self.realized_gross += gross
            self.current["gross_pnl"] += gross
            self.cash_change += fill.price * fill.quantity - fill.fee
        self.current["fees"] += fill.fee
        self.current["slippage"] += fill.slippage
        self.current["maker_fills" if fill.maker else "taker_fills"] += 1
        if self.quantity == 0:
            self.current.update(exit_ns=fill.ts_ns, holding_seconds=(fill.ts_ns - self.entry_ns) / 1e9)
            self.current["net_pnl"] = self.current["gross_pnl"] - self.current["fees"]
            self.current["benchmark_gross_pnl"] = self.current["gross_pnl"] + self.current["slippage"]
            self.trades.append(self.current)
            self.current = None
            self.entry_price = ZERO

    @classmethod
    def from_fills(cls, fills):
        result = cls()
        for fill in fills:
            result.apply(fill)
        return result

    def unrealized(self, bid):
        return self.quantity * (bid - self.entry_price)
