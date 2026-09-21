import pytest

from tests.conftest import NOW
from trader.backtest import configured_fees, replay, synthetic_events
from trader.features import FeatureEngine, FeatureSet
from trader.models import SECOND, D, Event, Fill, Regime
from trader.portfolio import Portfolio
from trader.reporting import uncertainty
from trader.safety import authorize_start
from trader.strategy import EdgeModel, Strategy


def feature(now=NOW):
    return FeatureSet(
        now,
        {
            "mid": 60000,
            "imbalance": 0.5,
            "return_5s_bps": 1,
            "zscore": -2,
            "spread_bps": 0.1,
            "bid_depth_usd": 100000,
            "ask_depth_usd": 100000,
            "volatility_30s_bps": 1,
            "sell_volume_30s": 100,
        },
        Regime.RANGING,
        True,
    )


def test_no_model_means_no_trade(cfg, fees):
    result = Strategy(cfg, EdgeModel(), fees).decide(feature(), 1)
    assert result.decision == "NO_TRADE" and result.reason == "NO_MODEL_EVIDENCE"


def test_fee_burden_rejects_high_win_probability(cfg, fees):
    model = EdgeModel(
        {"RANGING:moderate": {"n": 500, "mean": 20, "se": 1, "confidence": 0.999}},
        {"train_end_ns": NOW - 1, "fingerprint": cfg.model_fingerprint()},
    )
    result = Strategy(cfg, model, fees).decide(feature(), 1)
    assert result.confidence > 0.99
    assert result.expected_net_edge_bps < 0
    assert result.decision == "NO_TRADE"


def test_model_never_predicts_inside_training_range(cfg):
    model = EdgeModel(
        {"RANGING:moderate": {"n": 500, "mean": 200, "se": 1, "confidence": 0.999}},
        {"train_end_ns": NOW, "fingerprint": cfg.model_fingerprint()},
    )
    assert model.predict(feature(), cfg) is None


def test_training_labels_crossing_cutoff_are_excluded(cfg):
    cfg.strategy.minimum_cell_samples = 30
    samples = [(feature(NOW + i * 10 * SECOND), 2, NOW + (i * 10 + 5) * SECOND) for i in range(40)]
    samples.append((feature(NOW + 398 * SECOND), 1000000, NOW + 410 * SECOND))
    model = EdgeModel.fit(samples, cfg, NOW + 400 * SECOND, "test-data")
    assert model.cells["RANGING:moderate"]["mean"] == 2
    assert model.cells["RANGING:moderate"]["n"] == 40


def test_training_samples_do_not_overlap(cfg):
    cfg.strategy.minimum_cell_samples = 30
    samples = [(feature(NOW + i * SECOND), 2, NOW + (i + 30) * SECOND) for i in range(40)]
    assert EdgeModel.fit(samples, cfg, NOW + 100 * SECOND, "test-data").cells == {}


def test_features_are_causal(cfg, book):
    engine = FeatureEngine(cfg)
    engine.trade(
        Event(
            kind="trade", ts_ns=NOW, recv_ns=NOW, price="60000", quantity="2", aggressor="buy", trade_id="1"
        )
    )
    before = engine.calculate(book, NOW)
    saved = before.values.copy()
    engine.trade(
        Event(
            kind="trade",
            ts_ns=NOW + SECOND,
            recv_ns=NOW + SECOND,
            price="70000",
            quantity="20",
            aggressor="sell",
            trade_id="2",
        )
    )
    assert before.values == saved
    assert before.values["return_5s_bps"] is None


def fill(identity, side, quantity, price, fee, ts):
    return Fill(
        fill_id=identity,
        client_id=identity,
        ts_ns=ts,
        side=side,
        quantity=quantity,
        price=price,
        fee=fee,
        maker=side == "buy",
        reference_price="100",
        latency_ms=250,
    )


def test_partial_round_trip_fees_and_slippage_are_not_double_counted():
    portfolio = Portfolio()
    for f in [
        fill("1", "buy", "1", "100", "1", NOW),
        fill("2", "buy", "1", "102", "1", NOW + 1),
        fill("3", "sell", "0.5", "110", "0.5", NOW + 2),
        fill("4", "sell", "1.5", "108", "1.5", NOW + 3),
    ]:
        portfolio.apply(f)
    assert portfolio.quantity == 0
    assert len(portfolio.trades) == 1
    trade = portfolio.trades[0]
    assert trade["gross_pnl"] == D("15")
    assert trade["net_pnl"] == D("11")
    assert portfolio.cash_change == D("11")
    assert trade["benchmark_gross_pnl"] - trade["slippage"] - trade["fees"] == trade["net_pnl"]


def test_short_or_same_day_sample_cannot_be_statistically_approved():
    trades = [{"net_pnl": D("1"), "exit_ns": NOW + i * SECOND} for i in range(600)]
    assert uncertainty(trades)["lower_expectancy_95"] is None


def test_bootstrap_reproducibility():
    trades = [
        {"net_pnl": D("1") if i % 3 else D("-0.2"), "exit_ns": NOW + i * 86400 * SECOND} for i in range(30)
    ]
    assert uncertainty(trades, repetitions=20) == uncertainty(trades, repetitions=20)


async def test_end_to_end_replay_abstains_and_reports_no_edge(cfg, instrument):
    result, trades = await replay(cfg, synthetic_events(100), instrument)
    assert trades == []
    assert result["status"] == "NO DEPLOYABLE EDGE"
    assert not result["robustness"]["passed"]
    assert result["net_profit"] == 0


def test_missing_fees_never_defaults_to_zero(cfg):
    cfg.fees.maker_bps = None
    with pytest.raises(ValueError, match="actual account"):
        configured_fees(cfg)


@pytest.mark.parametrize(
    "env,confirmation", [(None, None), ("true", None), (None, "LIVE_BTCUSD"), ("false", "LIVE_BTCUSD")]
)
def test_live_requires_environment_and_confirmation(cfg, monkeypatch, env, confirmation):
    cfg.mode = "live"
    if env is not None:
        monkeypatch.setenv("LIVE_TRADING_ENABLED", env)
    else:
        monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    with pytest.raises(ValueError, match="LIVE_TRADING_ENABLED"):
        authorize_start(cfg, confirmation)


def test_enabling_live_without_evidence_still_fails(cfg, monkeypatch, tmp_path):
    cfg.mode = "live"
    cfg.evidence_path = str(tmp_path / "absent.json")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    with pytest.raises(FileNotFoundError):
        authorize_start(cfg, "LIVE_BTCUSD")


def test_kill_survives_database_restart(tmp_path):
    from trader.storage.db import Store

    path = str(tmp_path / "restart.sqlite")
    store = Store(path)
    store.kill("TEST_CRASH")
    store.close()
    reopened = Store(path)
    assert reopened.get("killed")["reason"] == "TEST_CRASH"
    assert reopened.get("paused") is True
    reopened.close()
