# Installation and operations

Commands below run from the repository root on Ubuntu 24.04 / Python 3.12, or WSL2. Use the same configuration file for a service and its operator commands. Keep production and sandbox credentials, state, logs and alerts separate.

## 1. Local installation

```bash
git clone https://github.com/gittaqui/btcscalp.git
cd btcscalp
git switch main
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps -e .
cp .env.example .env
chmod 600 .env
cp config/config.example.yaml config/local.yaml
mkdir -p data models reports
python -m pytest -q
```

Edit `config/local.yaml`: set actual account fees and their source note, or configure read access for authenticated fee discovery. Never copy the fixture fee values into production merely to make startup succeed. `risk_per_trade_pct: 0.10` means **0.10 percent**, while fee and spread fields are **basis points**. The default order cap is $100, additionally limited by the other sizing controls.

## 2. Gemini sandbox setup

1. Create a test account at https://exchange.sandbox.gemini.com. This is separate from your production login.
2. Create two **account-scoped**, time-based-nonce API keys with Trader permissions and **no withdrawal/fund-management permissions**. On the entry key enable **Require Heartbeat**; on the guard key leave Require Heartbeat **disabled**. The guard key's native stop must survive entry-session loss. Master/group keys are unsupported.
3. Put the entry key in `GEMINI_SANDBOX_API_KEY` / `GEMINI_SANDBOX_API_SECRET`; put the second key in `GEMINI_SANDBOX_GUARD_KEY` / `GEMINI_SANDBOX_GUARD_SECRET`. Set `SANDBOX_TRADING_ENABLED=true`; keep `LIVE_TRADING_ENABLED=false`.
4. Copy the sandbox config. Check `exchange.baseline_btc` against the account's untouched seeded BTC allocation (the documented default is 1,000 BTC). Do not use the baseline feature to adopt trading inventory. Sandbox orders can still change test balances; the opt-in test places/cancels one minimum-size passive order.

```bash
cp config/sandbox.example.yaml config/local-sandbox.yaml
set -a
source .env
set +a
RUN_SANDBOX_TESTS=true python -m pytest tests/test_sandbox.py -q
```

Run guardian in terminal 1, then sandbox runtime in terminal 2 after the guardian is running:

```bash
python -m trader guardian --mode sandbox --config config/local-sandbox.yaml --confirm SANDBOX_BTCUSD
```

```bash
python -m trader sandbox --config config/local-sandbox.yaml --confirm SANDBOX_BTCUSD
```

Initially the strategy remains flat without a fitted model. The explicit sandbox integration test exercises order placement/cancellation independently. To validate fills/stops, use the sandbox acceptance protocol in `VALIDATION.md` with sandbox research data, a bounded test balance and operator observation. Never count sandbox profitability as market evidence.

## 3. Paper trading

```bash
python -m trader --mode paper --config config/local.yaml
```

The feed is production Gemini; all fills are simulated. Start with `low` or `ultra_low`. Record data before fitting:

```bash
python -m trader collect --config config/local.yaml --seconds 86400 --output data/day01.jsonl
python -m trader validate --config config/local.yaml --data data/day01.jsonl --output reports/research.json
```

One day normally will not provide sufficient evidence. Run a longer continuous capture spanning multiple regimes, or combine tapes as described in `RESEARCH.md`. Configuration/model/code changes invalidate a paper evaluation; start a **new flat paper database**, preserving the old one for review. Do not relabel old fills as the new strategy's performance.

## 4. Event-driven backtest

```bash
python -m trader backtest --config config/local.yaml \
  --data data/day02.jsonl --output reports/backtest-day02.json --database data/backtest-day02.sqlite
```

The output database must be new. The model refuses to predict within its training period. A closing window disables fresh entries before the tape ends; any remaining inventory is marked and causes acceptance failure. The engine does not invent a final fill at the last observed price.

## 5. Docker deployment

Install Docker Engine and Compose from the distribution/provider's supported package repository. Use an operator-owned checkout. Set up the dashboard config for the matching mode and a token:

```bash
cp config/local.yaml config/local-dashboard.yaml
python - <<'PY'
from pathlib import Path
import secrets, yaml
p = Path('config/local-dashboard.yaml')
c = yaml.safe_load(p.read_text()); c['monitoring']['host'] = '0.0.0.0'
p.write_text(yaml.safe_dump(c, sort_keys=False))
p = Path('.env')
lines = [line for line in p.read_text().splitlines() if not line.startswith('DASHBOARD_TOKEN=')]
p.write_text('\n'.join(lines + ['DASHBOARD_TOKEN=' + secrets.token_urlsafe(48)]) + '\n')
PY
sudo chown -R 10001:10001 data
chmod 600 .env
docker compose build
docker compose --profile paper up -d
docker compose logs --tail 100 paper
docker compose exec paper python -m trader health --config config/local.yaml
```

Open http://127.0.0.1:8080 and enter the locally stored token. Remote access uses an SSH tunnel:

```bash
ssh -L 8080:127.0.0.1:8080 trader@YOUR_VPS_IP
```

The HTTP and metrics endpoints require a bearer token; the HTML login page contains no account data. Do not publish port 8080 on a public interface. Containers run as UID 10001 with dropped capabilities, a read-only root filesystem, resource limits, and bounded Docker logs. Docker profiles prevent live services starting with the paper command. Do not run paper and live against the same database.

To stop paper:

```bash
docker compose --profile paper stop
```

For sandbox, prepare `config/local-sandbox.yaml`, set the sandbox dashboard database, and use `docker compose --profile sandbox up -d`. Guardian health must pass before the runtime starts.

## 6. Production VPS procedure

Choose a persistent Ubuntu 24.04 VPS, initially 2 vCPU, 2 GB RAM and 20–60 GB SSD. Measure several regions instead of assuming one is fastest. No particular provider, price or region is claimed optimal here.

As the provisioned administrator:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates git python3.12-venv chrony ufw unattended-upgrades docker.io docker-compose-v2
sudo systemctl enable --now chrony docker
sudo adduser --disabled-password --gecos '' trader
sudo install -d -m 700 -o trader -g trader /home/trader/.ssh
sudo install -m 600 -o trader -g trader ~/.ssh/authorized_keys /home/trader/.ssh/authorized_keys
sudo ufw allow OpenSSH
sudo ufw --force enable
sudo install -m 600 /dev/null /etc/ssh/sshd_config.d/60-btcscalp.conf
sudo tee /etc/ssh/sshd_config.d/60-btcscalp.conf >/dev/null <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
sudo sshd -t
sudo systemctl reload ssh
sudo systemctl enable --now unattended-upgrades
```

Verify a **second SSH session** using the new key before closing the administrator session. Adjust the authorized-key source to the actual administrator account if it is not `~/.ssh/authorized_keys`. Do not change SSH/firewall rules blindly on an existing managed server. Docker group membership gives root-equivalent host control; prefer explicit `sudo docker ...` or a properly managed rootless deployment.

As `trader`, perform installation steps 1 and 5. For Docker commands use `sudo` if the account is not configured for rootless Docker. Verify clock and latency:

```bash
timedatectl show -p NTPSynchronized --value
chronyc tracking
python -m trader latency --config config/local.yaml
python -m trader collect --config config/local.yaml --seconds 300 --output data/region-latency.jsonl
```

Inspect exchange/receipt lag, API p50/p95, reconnect frequency, CPU, disk growth and operation under simulated failures. Logs are UTC. The runtime compares exchange and receipt timestamps; this detects delay/drift but does not replace host NTP.

Keep a bounded retention policy for raw market data. The SQLite event log is durable but not an unlimited archive. Stop entries before maintenance, export historical market tapes to Parquet, back up the database with the supported online backup, and retain orders/fills/risk history:

```bash
python -m trader pause --config config/local.yaml
python scripts/backup.py data/paper.sqlite data/paper-backup.sqlite
```

Store backups securely off-host. Do not copy only the main SQLite file while its WAL is active. Verify restore on an isolated copy. Do not auto-reboot a live host for updates; perform updates while flat and paused.

## 7. Change frequency

Edit `frequency.frequency_mode` to `ultra_low`, `low`, `medium`, `high` or `custom`. Presets set the five cadence/ceiling values; explicit cadence fields apply in `custom` mode. All modes retain configured loss cooldowns.

```yaml
frequency:
  frequency_mode: custom
  signal_interval_ms: 500
  decision_interval_ms: 500
  minimum_seconds_between_entries: 1200
  maximum_trades_per_hour: 3
  maximum_trades_per_day: 36
  cooldown_after_loss_seconds: 300
  cooldown_after_consecutive_losses_seconds: 3600
```

Pause, flatten, verify zero inventory/no outstanding orders, stop the service, and change configuration. Research and paper evaluation must be rerun under the new fingerprint. Limits are ceilings, never quotas. Strictness increases with configured hourly capacity: more net edge/liquidity/fill probability, less spread/volatility/fee burden. Increasing frequency never authorizes a taker entry.

## 8. Activate and deactivate live trading

Do not proceed until all items in `VALIDATION.md` have real evidence. You need positive held-out and walk-forward results, stressed-cost viability, at least **500 complete production-feed paper trades**, **14 days**, multiple regimes and stable operations. A high win rate alone cannot pass.

Prepare `config/local-live.yaml` from `live.example.yaml`, copying the exact validated strategy, frequency, execution and risk settings. Its database must be `data/live.sqlite`; keep the same model and evidence paths. Create separate production entry/guard keys with the same restrictions as sandbox. Fund only the dedicated risk-approved spot account; begin with zero BTC.

Create the approval artifact while still using the validated paper config:

```bash
python -m trader approve --config config/local.yaml --research reports/research.json \
  --database data/paper.sqlite --operator 'YOUR_NAME' --confirm APPROVE_LIVE_RESEARCH
```

This refuses insufficient, synthetic, stale or mismatched evidence. Approval expires after seven days and is bound to Python source, configuration and model hashes. Actual live API fees must be no greater than the **lower** of research and paper assumptions. Editing a private JSON approval file is not a substitute for evidence; OS-level control of those files remains an operator trust boundary.

Set `LIVE_TRADING_ENABLED=true` in `.env` only after approval, load it, start the guardian and then start live execution with explicit confirmations:

```bash
set -a
source .env
set +a
python -m trader guardian --mode live --config config/local-live.yaml --confirm LIVE_BTCUSD
```

```bash
python -m trader live --config config/local-live.yaml --confirm LIVE_BTCUSD
```

For approved Docker operation, use the live dashboard config and run `docker compose --profile live up -d`. The environment flag and evidence gate are still mandatory. An unclean runtime restart latches a kill; it cannot silently resume entries after a crash.

To deactivate, first request flatten and verify acknowledgement, fills and zero inventory:

```bash
python -m trader flatten --config config/local-live.yaml --confirm FLATTEN
python -m trader status --config config/local-live.yaml
```

Then stop the runtime/guardian or `docker compose --profile live stop`, and set `LIVE_TRADING_ENABLED=false`. Changing the environment file alone does **not** terminate an already running process. During an unresolved position keep native protection and the guardian available, and use Gemini's official interface for manual remediation if the API is unavailable.

## 9. Kill switch and recovery

```bash
python -m trader kill --config config/local-live.yaml --confirm KILL
python -m trader status --config config/local-live.yaml
```

The command writes the kill latch immediately; cancellation/flattening is asynchronous. If the strategy is dead, the guardian observes the latch and attempts cancellation/reconciliation/reduction. Check actual exchange orders and balances. Neither a command response nor an exchange stop is a fill guarantee.

If an exit fails after protection was canceled, the controller latches `EMERGENCY_EXIT_FAILED`, retains a flatten request, and attempts to reconcile before re-arming protection for the remaining inventory. `status` includes the most recent `protection_recovery` result and timestamp: `flat`, `protected`, or `operator_required`. This is an observation at that time, not a continuing fill or coverage guarantee. An unresolved IOC is never retried under a new ID or covered using stale inventory. `operator_required` / `PROTECTION_RECOVERY_UNCONFIRMED` means the exchange outcome or protection could not be verified; inspect Gemini's official order/balance interface and retain the ledger for recovery.

Cancellation attempts continue across all targeted orders even if one fails. Entry risk is canceled before native protection is removed; book freshness is checked again after REST calls. A newer pause, flatten or kill supersedes an older resume/reset request, including commands received from another dashboard/CLI connection during reconciliation. The older request is marked failed; inspect the new stop before explicitly requesting recovery again.

Recovery requires the underlying fault to be fixed, a fresh synchronized book, successful reconciliation, zero bot inventory and no outstanding orders:

```bash
python -m trader reset-kill --config config/local-live.yaml --confirm RESET-KILL
python -m trader status --config config/local-live.yaml
python -m trader resume --config config/local-live.yaml --confirm RESUME
```

Reset leaves entries paused. It does not erase equity loss baselines or rewrite losses. Breached equity limits will re-latch. Preserve the ledger and investigate; never delete the database to hide a loss or an unknown order. A canceled entry remainder can have filled during cancellation; reconcile before any replacement or sale.

## 10. Evaluate a defensible edge

Run the full `validate` command on production L2/trade data with authentic fee assumptions. Read **all** output: sample count, net expectancy/day-bootstrap interval, net profit factor, mark-to-market drawdown, open inventory, regimes, fold stability, feature ablations, parameter perturbations, cost/latency stresses and concentration. Predeclare parameters; do not use test results to select the next candidate and then reuse the same test.

Then freeze model/code/config and run production-feed paper trading through at least 500 complete position episodes and 14 days. Archive the decision, fee, fill, health and risk records. Compare empirical maker fill rates and shortfall against simulated assumptions. If any criterion fails, the result is **NO DEPLOYABLE EDGE**. Software can be useful for collecting/rejecting hypotheses without having a deployable trading strategy.
