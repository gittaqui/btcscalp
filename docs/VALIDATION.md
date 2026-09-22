# Validation record and release gates

Engineering verification updated on 2026-09-22 using Python 3.12 in an isolated environment installed with hash verification from `requirements-dev.lock`.

## Verified locally

| Check | Result |
|---|---|
| Automated suite | 114 passed; 1 opt-in sandbox test skipped |
| Ruff lint and formatting | Passed |
| Locked dependency compatibility | Passed in a clean virtual environment |
| Synthetic tape → event replay → report | Passed; zero trades without a model; NO DEPLOYABLE EDGE |
| JSONL → Parquet | Passed with source hash and instrument metadata retained |
| Full validation command | Completed chronological partitions, folds, ablations and stresses; rejected synthetic/insufficient evidence |
| Dashboard HTTP checks | Authentication enforced, origin checks enforced, confirmation required, command persisted |
| Crash recovery mechanics | Real child process killed; lock released; persisted intent/kill and transaction behavior tested |
| Configuration files / Compose structure | Parsed; model constraints and container security settings checked |
| GitHub CI Python job (initial implementation) | Passed locked install, lint/format, non-sandbox tests, CLI replay and dependency checks |
| GitHub CI container job (initial implementation) | Docker image build and container CLI startup passed |

CI evidence: https://github.com/gittaqui/btcscalp/actions/runs/35653828110 (implementation commit `a5bd7eb1032c3663aa0ef132c8f38d5c146fac90`).

Tests cover sequence gaps, duplicates, crossing/invalid levels, time reversal, trade direction, tick/quantity increments, fee discovery and HMAC payloads, unknown-order timeouts, 429/5xx errors, rejected orders, partial fills, cancel/fill races, queue position, IOC remainder cancellation, inventory mismatch, net accounting, stale/frequency/exposure/loss risk checks, live gates, label cutoff/overlap, confidence abstention, day-bootstrap reproducibility, kill persistence, separate environments, spread expansion, 5× volatility and 500 ms / 2 s / 10 s delayed execution.

The drawdown reporter also preserves adverse event-level excursions between periodic dashboard/equity snapshots. These engineering fixtures are intentionally deterministic and do not represent a profitable trading history.

## Emergency recovery follow-up, 2026-09-22

Added 26 regression cases covering rejected IOC exits, a lost response after a partial fill, unresolved exit status, transient and persistent balance outages after native-stop cancellation, cancellation delays that stale the book, best-effort cancellation across multiple orders, stale/overlapping/poorly bounded sell intents, changed execution details under a duplicate fill ID, and changed exchange order identity.

Operator-control tests include newer kill/pause commands arriving before and during reconciliation, a new risk kill during reconciliation, a stop from a separate SQLite connection, and successful explicit reset-then-resume. A newer stop wins; failed cancellation retains the kill and flatten request. Tests use the real controller, risk gate, live broker and SQLite ledger against an independently simulated venue with its own cash, inventory and order records.

Failed exit transitions now attempt fresh reconciliation before restoring native protection. Unknown IOC outcomes prohibit overlapping replacements, and unrecoverable protection is exposed as `operator_required`. This narrows tested failure paths; it does not remove the exchange's non-atomic cancel/submit interval or replace the sandbox protocol below. Python source changes invalidate earlier approval artifacts and paper evidence; obtain fresh evidence for this revision.

## Not verified here

- The Gemini public API smoke call did not complete successfully from this execution environment; it returned an unavailable/server-error outcome. Real WebSocket subscription, authenticated account permissions, native stop behavior and fee discovery must still be exercised from the intended deployment host.
- Gemini sandbox end-to-end was not run: no sandbox credentials were supplied. The opt-in test is implemented and is skipped by default.
- Docker Engine/Compose were not installed locally. GitHub CI successfully built the image and ran its CLI. The full Compose service group, persistence on an actual VPS and container-to-Gemini connectivity are still unverified.
- No real production historical dataset, independently evaluated research edge, 14-day paper run or 500 completed paper trades exists for this implementation.
- Live trading has not been enabled, no real exchange order has been sent, and no VPS has been provisioned.

**Release/strategy acceptance: NO DEPLOYABLE EDGE.** This is a complete initial source implementation for engineering review and staged validation. It is not a production certification or an assurance that a safe/profitable live deployment exists.

## Required sandbox acceptance protocol

Run on a dedicated test account with both restricted keys, a synchronized host and real alert delivery. Save timestamps, configuration/code hashes, order IDs, account snapshots and operator observations for each exercise:

1. Read symbol metadata and actual API fee rates. Verify BTC/USD spot, precision and market status.
2. Run `RUN_SANDBOX_TESTS=true python -m pytest tests/test_sandbox.py -q`; inspect that no submitted test order remains active.
3. Exercise zero fill, partial entry, multiple entry fills, cancellation with an in-flight fill, partial IOC exit, rejected orders and native stop trigger. Compare the ledger to actual exchange balances and trade history after every step.
4. Force a connection loss and verify entry-key heartbeat cancellation at the exchange. Confirm a guard-key native stop persists. Do not infer either behavior from a local log message.
5. Terminate only the strategy process while inventory exists. Verify the independent guardian detects missing heartbeats, cancels entry risk, reconciles and attempts price-bounded reduction.
6. Stop both processes. Verify exchange-native protection persists. Demonstrate a stop-limit price-gap case and the documented manual recovery path.
7. Force a timed-out submission; verify one client ID, no blind resend, an unknown journal state and reconciliation before any new order. Exercise process restart between intent persistence and the HTTP response.
8. Inject 429, 5xx, DNS/REST unavailability, a missed depth update, an old timestamp, 500 ms / 2 s / 10 s delay, spread expansion and a volatility shock. Verify no fresh entry escapes a latched stop.
9. Exercise database restart, disk-full behavior, a server reboot, unchanged file locks and restore from backup. Confirm no automatic live resumption after an unclean runtime restart.
10. Test kill, pause, resume, flatten and reset in both CLI and dashboard; verify remote exchange state, not just queued-command acknowledgements. Check alerts during network failure and reconnect.

Record defects as issues and require a fresh test/evidence run after code or execution-assumption changes. Do not reduce a risk gate merely to make this checklist pass.

## Production paper acceptance

Use a frozen model/code/config, authentic current fees, production L2 and trades, a measured hosting region and a conservative queue/execution model. Inspect profitability **after** fees, spread, latency and shortfall. Require the research gates and the minimum production-paper period/sample/regime/operational evidence in `RESEARCH.md`. If the necessary evidence never appears, retain NO DEPLOYABLE EDGE and do not enable live trading.
