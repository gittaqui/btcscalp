import pytest

from tests.conftest import NOW, order
from trader.config import Config, Frequency
from trader.models import SECOND, D
from trader.portfolio import Portfolio
from trader.risk import RiskEngine, RiskRejected


@pytest.mark.parametrize(
    "field,value",
    [
        ("leverage", 2),
        ("averaging_down", True),
        ("maximum_open_positions", 2),
        ("risk_per_trade_pct", -1),
        ("maximum_position_pct", 101),
    ],
)
def test_invalid_risk_configuration(field, value):
    with pytest.raises(ValueError):
        Config.model_validate({"risk": {field: value}})


def test_unknown_config_key_rejected():
    with pytest.raises(ValueError):
        Config.model_validate({"fees": {"make_bps": 10}})


@pytest.mark.parametrize("mode,maximum", [("ultra_low", 1), ("low", 3), ("medium", 10), ("high", 30)])
def test_frequency_presets(mode, maximum):
    assert Frequency(frequency_mode=mode).resolved().maximum_trades_per_hour == maximum


def test_position_size_respects_all_caps(cfg, store, account, instrument, fees):
    cfg.risk.maximum_order_usd = D("30")
    risk = RiskEngine(cfg, store)
    quantity = risk.size(account, instrument, D("60000"), D("500000"), 1, fees)
    assert quantity * 60000 <= 30
    account.available_usd = D("10")
    quantity = risk.size(account, instrument, D("60000"), D("500000"), 1, fees)
    assert quantity * 60000 * D("1.001") <= 10


def test_kill_latches_after_recovery(cfg, store):
    risk = RiskEngine(cfg, store)
    risk.observe_equity(D("10000"), NOW)
    risk.observe_equity(D("9700"), NOW + SECOND)
    assert store.get("killed")
    risk.observe_equity(D("10100"), NOW + 2 * SECOND)
    assert store.get("killed")


@pytest.mark.parametrize(
    "failure", ["stale", "pause", "kill", "position", "order", "no_edge", "funds", "limit", "hourly"]
)
def test_entries_fail_closed(cfg, store, book, instrument, account, decision, failure):
    portfolio, now, o = Portfolio(), NOW, order()
    if failure == "stale":
        now += 3 * SECOND
    if failure == "pause":
        store.set("paused", True)
    if failure == "kill":
        store.kill("test")
    if failure == "position":
        portfolio.quantity = D("0.001")
    if failure == "order":
        store.put_order(order(client_id="other"), new=True)
    if failure == "no_edge":
        decision.expected_net_edge_bps = 0
    if failure == "funds":
        account.available_usd = D("1")
    if failure == "limit":
        o.quantity = D("0.01")
    if failure == "hourly":
        store.set("entries", [NOW - SECOND] * 30)
    with pytest.raises(RiskRejected):
        RiskEngine(cfg, store).authorize(o, account, portfolio, instrument, book, now, decision)


def test_emergency_reducing_exit_allowed_when_killed(cfg, store, book, instrument, account):
    store.kill("test")
    portfolio = Portfolio(quantity=D("0.001"))
    account.btc = account.available_btc = D("0.001")
    RiskEngine(cfg, store).authorize(order("sell"), account, portfolio, instrument, book, NOW)
    with pytest.raises(RiskRejected, match="EXCEEDS"):
        RiskEngine(cfg, store).authorize(
            order("sell", quantity=D("0.002")), account, portfolio, instrument, book, NOW
        )


def test_consecutive_losses_stop_entries(cfg, store, book, instrument, account, decision):
    portfolio = Portfolio(trades=[{"net_pnl": D("-1"), "exit_ns": NOW - 10000 * SECOND}] * 5)
    with pytest.raises(RiskRejected, match="CONSECUTIVE_LOSSES"):
        RiskEngine(cfg, store).authorize(order(), account, portfolio, instrument, book, NOW, decision)
    assert store.get("killed")


def test_higher_volatility_never_increases_size(cfg, store, account, instrument, fees):
    risk = RiskEngine(cfg, store)
    low = risk.size(account, instrument, D("60000"), D("500000"), 1, fees)
    high = risk.size(account, instrument, D("60000"), D("500000"), 1000, fees)
    assert high <= low
