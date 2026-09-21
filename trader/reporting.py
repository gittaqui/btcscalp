"""Net results, time-weighted exposure, daily returns and uncertainty estimates."""

from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

from trader.models import SECOND, D
from trader.portfolio import Portfolio


def day_key(ns):
    return datetime.fromtimestamp(ns / SECOND, timezone.utc).date().isoformat()


def report(store, starting_equity=None):
    fills = store.fills()
    portfolio = Portfolio.from_fills(fills)
    trades = portfolio.trades
    orders = store.orders()
    points = store.events("equity", limit=2_000_000)
    start = float(starting_equity or store.get("initial_live_usd") or store.get("starting_equity", "10000"))
    end = float(points[-1]["equity"]) if points else start + float(portfolio.cash_change)
    net = np.array([float(t["net_pnl"]) for t in trades], dtype=float)
    wins, losses = net[net > 0], net[net < 0]
    gross_profit, gross_loss = float(wins.sum()), float(-losses.sum())
    equity = np.array([start] + [float(p["equity"]) for p in points])
    peaks = np.maximum.accumulate(equity)
    drawdowns = np.divide(peaks - equity, peaks, out=np.zeros_like(peaks), where=peaks > 0)
    daily = {}
    for point in points:
        daily[day_key(point["ts_ns"])] = float(point["equity"])
    daily_values = [start] + [daily[day] for day in sorted(daily)]
    returns = np.diff(daily_values) / np.array(daily_values[:-1]) if len(daily_values) > 1 else np.array([])
    std = float(np.std(returns, ddof=1)) if len(returns) > 1 else 0
    downside = float(np.sqrt(np.mean(np.minimum(returns, 0) ** 2))) if len(returns) else 0
    # Ratios are annualized from UTC daily equity returns, never per-trade returns.
    sharpe = float(np.mean(returns) / std * np.sqrt(365)) if std else None
    sortino = float(np.mean(returns) / downside * np.sqrt(365)) if downside else None
    latency = np.array([f.latency_ms for f in fills])
    duration = sum(max(0, b["ts_ns"] - a["ts_ns"]) for a, b in zip(points, points[1:]))
    exposure = sum(
        max(0, b["ts_ns"] - a["ts_ns"]) * (float(a["btc"]) > 0) for a, b in zip(points, points[1:])
    )
    maker_orders = [o for o in orders if o.kind == "maker"]
    days_pnl, regimes = defaultdict(float), defaultdict(list)
    for trade in trades:
        days_pnl[day_key(trade["exit_ns"])] += float(trade["net_pnl"])
        regimes[trade["regime"]].append(float(trade["net_pnl"]))
    by_regime = {
        r: {"trades": len(values), "net_pnl": sum(values), "expectancy": float(np.mean(values))}
        for r, values in regimes.items()
    }
    equity_state = store.get("risk_equity", {})
    maximum_drawdown = max(float(drawdowns.max()), float(equity_state.get("max_drawdown", 0)))
    maximum_drawdown_usd = max(float(np.max(peaks - equity)), float(equity_state.get("max_drawdown_usd", 0)))
    average_drawdown = (
        float(equity_state.get("drawdown_time_integral", 0)) / equity_state["observed_ns"]
        if equity_state.get("observed_ns", 0)
        else float(drawdowns.mean())
    )
    result = {
        "status": "NO DEPLOYABLE EDGE",
        "starting_equity": start,
        "ending_equity": end,
        "number_of_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(net) if len(net) else None,
        "average_winner": float(wins.mean()) if len(wins) else None,
        "average_loser": float(losses.mean()) if len(losses) else None,
        "expectancy": float(net.mean()) if len(net) else None,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "gross_pnl": float(portfolio.realized_gross),
        "net_profit": float(net.sum()),
        "marked_net_profit": end - start,
        "net_return_pct": (end / start - 1) * 100,
        "fees": float(portfolio.fees),
        "maker_fees": float(portfolio.maker_fees),
        "taker_fees": float(portfolio.taker_fees),
        "slippage": float(portfolio.slippage),
        "benchmark_gross_pnl": float(portfolio.realized_gross + portfolio.slippage),
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "profit_factor_note": "undefined without losing trades"
        if gross_loss == 0
        else "net trade profits / net trade losses",
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "maximum_drawdown_pct": maximum_drawdown * 100,
        "average_drawdown_pct": average_drawdown * 100,
        "recovery_factor": (end - start) / maximum_drawdown_usd if maximum_drawdown_usd > 0 else None,
        "exposure": exposure / duration if duration else 0,
        "turnover": sum(float(f.price * f.quantity) for f in fills) / start,
        "average_holding_seconds": float(np.mean([t["holding_seconds"] for t in trades])) if trades else None,
        "maker_fill_percentage": 100 * sum(o.filled > 0 for o in maker_orders) / len(maker_orders)
        if maker_orders
        else 0,
        "maker_percentage": 100 * sum(f.maker for f in fills) / len(fills) if fills else 0,
        "taker_percentage": 100 * sum(not f.maker for f in fills) / len(fills) if fills else 0,
        "cancelled_order_ratio": sum(o.status == "cancelled" for o in orders) / len(orders) if orders else 0,
        "average_latency_ms": float(latency.mean()) if len(latency) else None,
        "p50_latency_ms": float(np.percentile(latency, 50)) if len(latency) else None,
        "p95_latency_ms": float(np.percentile(latency, 95)) if len(latency) else None,
        "p99_latency_ms": float(np.percentile(latency, 99)) if len(latency) else None,
        "latency_definition": "order-intent to fill, includes maker waiting; REST latency reported in health",
        "open_btc": str(portfolio.quantity),
        "open_position_cost": str(portfolio.quantity * portfolio.entry_price),
        "regimes": by_regime,
        "daily_pnl": dict(days_pnl),
        "best_regime": max(by_regime, key=lambda r: by_regime[r]["expectancy"]) if by_regime else None,
        "worst_regime": min(by_regime, key=lambda r: by_regime[r]["expectancy"]) if by_regime else None,
        "operational_incidents": store.events("risk", limit=10000),
        "strategy_anomalies": store.events("anomaly", limit=1000),
        "accounting": "gross_pnl uses execution prices; net_pnl = gross_pnl - fees; slippage is embedded, not subtracted twice",
        "first_ns": points[0]["ts_ns"] if points else None,
        "last_ns": points[-1]["ts_ns"] if points else None,
    }
    entry_notional = sum(float(t["entry_notional"]) for t in trades)
    for name, amount in (
        ("gross_edge_bps", sum(float(t["gross_pnl"]) for t in trades)),
        ("fee_drag_bps", sum(float(t["fees"]) for t in trades)),
        ("slippage_drag_bps", sum(float(t["slippage"]) for t in trades)),
        ("net_edge_bps", float(net.sum())),
    ):
        result[name] = amount / entry_notional * 10000 if entry_notional else None
    return result


def uncertainty(trades, seed=20260921, repetitions=1000):
    if len(trades) < 2:
        return {
            "lower_expectancy_95": None,
            "upper_expectancy_95": None,
            "monte_carlo_drawdown_p95_usd": None,
        }
    rng = np.random.default_rng(seed)
    values = np.array([float(t["net_pnl"]) for t in trades])
    # Resample entire UTC days; within-day dependence and trade clustering are preserved.
    grouped = defaultdict(list)
    for trade in trades:
        grouped[day_key(trade["exit_ns"])].append(float(trade["net_pnl"]))
    groups = list(grouped.values())
    if len(groups) < 5:
        lower, upper = None, None
    else:
        means = []
        for _ in range(repetitions):
            sample = [value for index in rng.integers(0, len(groups), len(groups)) for value in groups[index]]
            means.append(np.mean(sample))
        lower, upper = [float(x) for x in np.quantile(means, [0.025, 0.975])]
    drawdowns = []
    for _ in range(repetitions):
        curve = np.r_[0.0, np.cumsum(rng.permutation(values))]
        drawdowns.append(float(np.max(np.maximum.accumulate(curve) - curve)))
    return {
        "lower_expectancy_95": lower,
        "upper_expectancy_95": upper,
        "bootstrap_unit": "UTC day",
        "bootstrap_days": len(groups),
        "seed": seed,
        "monte_carlo_drawdown_p95_usd": float(np.percentile(drawdowns, 95)),
    }


def robustness(result, trades, minimum_trades=500):
    ci = uncertainty(trades)
    reasons = []
    if result["number_of_trades"] < minimum_trades:
        reasons.append("INSUFFICIENT_TRADES")
    if result["profit_factor"] is None or result["profit_factor"] < 1.30:
        reasons.append("PROFIT_FACTOR_BELOW_TARGET_OR_UNDEFINED")
    if result["maximum_drawdown_pct"] > 8:
        reasons.append("DRAWDOWN_TOO_HIGH")
    if ci["lower_expectancy_95"] is None or ci["lower_expectancy_95"] <= 0:
        reasons.append("EXPECTANCY_NOT_STATISTICALLY_POSITIVE")
    if len(result["regimes"]) < 2:
        reasons.append("INSUFFICIENT_REGIMES")
    if D(result["open_btc"]) > 0:
        reasons.append("UNRESOLVED_OPEN_INVENTORY")
    positives = sorted((float(t["net_pnl"]) for t in trades if t["net_pnl"] > 0), reverse=True)
    total_positive = sum(positives)
    if total_positive and sum(positives[: max(1, len(positives) // 20)]) > 0.5 * total_positive:
        reasons.append("PROFIT_CONCENTRATED_IN_TOP_TRADES")
    daily = list(result["daily_pnl"].values())
    positive_days = sum(max(0, x) for x in daily)
    if positive_days and max(daily) > 0.5 * positive_days:
        reasons.append("PROFIT_CONCENTRATED_IN_ONE_DAY")
    if result["operational_incidents"]:
        reasons.append("OPERATIONAL_INCIDENTS_REQUIRE_REVIEW")
    return {"passed": not reasons, "reasons": reasons, **ci}
