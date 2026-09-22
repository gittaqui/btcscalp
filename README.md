# BTCScalp

Cost-aware **BTC/USD spot** research, streaming paper execution and manually gated Gemini execution.

**Current acceptance result: NO DEPLOYABLE EDGE.** No fitted profitable model, historical Gemini dataset, completed sandbox certification or 500-trade paper evaluation is included. Passing the engineering tests does not establish an investment edge or production readiness. Live mode fails closed without matching research and paper evidence plus operator approval.

## Start here

Use Python 3.12+ on Linux or Windows WSL2. All monetary calculations use `Decimal`.

```bash
git clone https://github.com/gittaqui/btcscalp.git
cd btcscalp
git switch main
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps -e .
cp .env.example .env
cp config/config.example.yaml config/local.yaml
python -m pytest -q
```

For runtime-only installation, use `requirements.lock`. Dependencies and hashes are pinned; `uv.lock` is included for reproducibility. The Linux process lock is intentional: native Windows execution is unsupported; use WSL2 or Docker.

Before paper trading, supply `fees.maker_bps`, `fees.taker_bps` and `fees.source_note` from **your actual API account fee schedule**, or provide a production key that can read `/v1/notionalvolume`. If a key is present, the API rate is used, including symbol-specific fee promotions. Unknown fees are an error, never zero. `config/test-fixture.yaml` contains explicitly synthetic cost assumptions and must not be used as account fee evidence.

```bash
set -a
source .env
set +a
python -m trader --mode paper --config config/local.yaml
```

Paper mode uses production WebSocket data and simulated execution. It places no exchange orders. Without a model it records `NO_MODEL_EVIDENCE` and stays flat. A fitted model is produced by the research command; failed research may still be studied in paper mode but cannot be approved for live trading.

## What is implemented

| Area | Behavior |
|---|---|
| Market data | Official Gemini v3 differential depth and trades, full snapshot on each connection, nanosecond exchange and receipt times, gap detection, reconnect backoff |
| Features | Causal 100 ms price sampling, eight horizons from 1 second to 15 minutes, book imbalance, microprice, depth, flow, returns, volatility, VWAP, z-score and regimes |
| Research | Two selectable, interpretable baseline families; purged chronological partitions; walk-forward folds; validation feature ablations; fee, slippage, latency and parameter stresses; UTC-day bootstrap; trade-order Monte Carlo |
| Simulator | Delayed submissions/cancellations, queue ahead, explicit aggressive trade volume, partial/missed fills, adverse IOC slippage, maker/taker fees, exchange increments |
| Risk | Spot only; one position; no averaging down; equity/size/exposure/frequency/cooldown gates; stale/latency/rejection/API and loss stops; persistent kill latch |
| Execution | Maker-or-cancel entries, bounded re-quotes, IOC protective sales, separate-key native stop limits, idempotent intent journal, exchange reconciliation |
| Recovery | Persisted fills, strict account ownership, unknown submission resolution by client ID, heartbeat entry cancellation and separate guardian process |
| Operations | Authenticated dashboard, queued operator controls, JSON logs, optional Discord alerts, metrics endpoint, SQLite online backup, Docker profiles and CI |

The exchange protocol in `trader/exchange/base.py` is the extension boundary for future Coinbase, Kraken and Robinhood adapters. Only Gemini is implemented. The optional supervisor is read-only and has no order or configuration mutation interface. No LLM runs in the execution path.

## Engineering smoke test, without exchange credentials

```bash
python -m trader synthetic --seconds 1200 --output data/synthetic.jsonl
python -m trader backtest --config config/test-fixture.yaml \
  --data data/synthetic.jsonl --output reports/synthetic.json
```

Expected result without a fitted model: zero trades and **NO DEPLOYABLE EDGE**. Synthetic fixtures exercise behavior; they are rejected as research/live approval evidence.

## Research and operator commands

```bash
# Record production L2/trades. Use a new filename for each recording.
python -m trader collect --config config/local.yaml --seconds 86400 --output data/day01.jsonl

# Optional compressed research archive.
python -m trader parquet --data data/day01.jsonl --output data/day01.parquet

# Fit on train, evaluate validation/test and walk-forward, run all stresses.
python -m trader validate --config config/local.yaml --data data/day01.parquet --output reports/research.json

# Replay a fresh dataset with the frozen model and assumptions.
python -m trader backtest --config config/local.yaml --data data/day02.jsonl --output reports/day02.json

# Operator monitoring (same config/database as the running service).
python -m trader status --config config/local.yaml
python -m trader report --config config/local.yaml --output reports/daily.json
python -m trader pause --config config/local.yaml
python -m trader resume --config config/local.yaml --confirm RESUME
python -m trader cancel-all --config config/local.yaml
python -m trader flatten --config config/local.yaml --confirm FLATTEN
python -m trader kill --config config/local.yaml --confirm KILL
python -m trader reset-kill --config config/local.yaml --confirm RESET-KILL
```

`cancel-all` cancels entry orders and retains exchange-native protection. `flatten` cancels all managed orders and attempts bounded IOC sales, re-arming protection on residual inventory. `kill` latches the stop and requests the same risk reduction. Commands are queued, not success acknowledgements: check `status` for `done`/`failed` and exchange balances. Network outages, market gaps and minimum sizes can prevent flattening.

The complete ten-part procedure, including sandbox, Docker, VPS, frequency and live enable/disable commands, is in [docs/RUNBOOK.md](docs/RUNBOOK.md). Read [docs/RESEARCH.md](docs/RESEARCH.md), [docs/SECURITY.md](docs/SECURITY.md) and [docs/VALIDATION.md](docs/VALIDATION.md) before operating.

## Layout

| Path | Responsibility |
|---|---|
| `trader/models.py`, `config.py` | Validated domain objects, presets, config fingerprints |
| `market_data.py`, `orderbook.py`, `features.py` | Streaming, tapes, causal feature calculation |
| `strategy.py`, `research.py`, `backtest.py` | Forecasts, chronological experiments and replay |
| `risk.py`, `execution.py`, `paper.py`, `portfolio.py` | Risk controls, broker state, simulation, accounting |
| `exchange/` | Gemini public/private APIs and exchange protocol |
| `storage/` | Schema, WAL database, commands, durable latches and locks |
| `runtime.py`, `safety.py` | Service/guardian lifecycle and live evidence gate |
| `dashboard.*`, `monitoring.py`, `reporting.py` | UI, alerts, telemetry and metrics |
| `tests/`, `scripts/`, `docs/` | Engineering checks and operator procedures |

## Important execution limits

- Gemini stop-limit orders do not guarantee a fill through a gap. There is an unavoidable interval between an entry fill and installation of its stop; Gemini REST does not make this application an atomic bracket order.
- Entry heartbeat cancellation and protective stops use **different account-scoped API keys**. Do not enable heartbeat cancellation on the protection key. Never grant withdrawal permissions.
- The watchdog is a separate process, but a single VPS is still a shared failure domain. Native stops remain at the exchange; they cannot guarantee liquidation during an exchange outage.
- REST reconciliation and Python on a small VPS are not co-located HFT. Frequency settings are opportunity ceilings; actual sustainable latency must be measured. Busy queues halt trading.
- Unknown fills, account changes, unsupported fee currencies, broken trades and below-minimum dust require operator reconciliation. Nothing silently adopts an external position.
- The implementation targets a dedicated account. Production starts from zero BTC; the sandbox supports an explicitly configured untouched BTC baseline to accommodate its seeded test balance.

Official API references and verification date are recorded in [docs/API_REFERENCES.md](docs/API_REFERENCES.md).
