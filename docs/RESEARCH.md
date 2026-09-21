# Research protocol

## Current result

**NO DEPLOYABLE EDGE.** There is no authentic historical dataset, fitted validated model or extended paper record in Git. Synthetic fixtures test mechanics only. The project makes no claim of expected return or win rate.

## Baseline hypotheses

`micro_momentum` tests whether positive book imbalance plus recent positive five-second price movement predicts a positive short forward midpoint return. `mean_reversion` tests positive book imbalance after a negative short-window price z-score. These are hypotheses, not assumed alpha.

Models group non-overlapping training observations into a small number of regime/imbalance cells. Each cell stores sample size, mean gross return, standard error and an approximate confidence for a positive conditional mean. This confidence is **not a calibrated probability that a trade wins**. The lower 95% normal bound is charged with actual entry fee, conservative taker exit fee, full spread, configured slippage, latency and adverse-selection buffer. The residual must exceed the frequency-dependent minimum.

The fill-probability estimate is a conservative flow/queue heuristic. It is not an exchange queue-position measurement. Paper trades must establish actual fill and shortfall behavior; if the heuristic is poorly calibrated, reject/research rather than increasing exposure. All other calculated features are diagnostics until ablation studies justify including them in the model.

## Dataset

Use production Gemini full snapshots, differential depth and trades with both exchange and local receipt times. Record exchange instrument increments and fee schedule provenance for the period. OHLC candles and public REST order-book snapshots do not establish a defensible queue replay by themselves. No unofficial data source or scraped account data is used.

JSONL files are append-free recordings with a sidecar `.manifest.json`: schema, source, environment, start/end, event count, instrument and SHA-256. Start each recording with a full snapshot. Never sort a damaged tape or silently remove disconnects. Replays must preserve receipt chronology. Parquet files contain `recv_ns` and lossless `event_json` columns, using Zstandard compression.

For longer runs prefer one continuous capture. Concatenating files needs an explicit disconnect boundary between sessions, preserved snapshots, chronological checks and a newly generated manifest; do not concatenate raw text and keep an old hash. Current validation rejects datasets whose operational gaps prevent a safe uninterrupted evaluation. This is deliberately conservative.

## Chronology and selection

The validation command uses 60% train, 20% validation, 20% untouched test by receipt time. It purges labels at the training cutoff and inserts an embargo equal to label horizon plus maximum holding time before each evaluation segment. Training examples do not overlap within a model. No future scaler, fitted threshold or test-dependent feature is used.

Three expanding walk-forward folds train up to 30%, 45% and 60% and test up to 45%, 60% and 80%. The final 20% remains separate from these folds. Family selection is an operator research choice made before final test inspection. Removing each active feature is evaluated on validation to measure incremental expectancy. Diagnostic features are not added merely because they look attractive in-sample.

Predeclared stresses: 1.5× fees, 2× adverse slippage, 500 ms / 2 s / 10 s execution latency, and ±10% imbalance thresholds with models refitted on training only. The 10 s stress checks operational behavior, not a requirement that a scalper stay profitable at 10 s latency. Spread expansion and volatility shock are also covered as explicit risk failure tests. Requotes never erase queue position costs: replacing an order starts a new queue.

Parameter perturbation on test is an acceptance stress, not permission to select whichever test result wins. Any redesign after viewing test results consumes that holdout; collect a fresh period. Keep an experiment log listing every tried family/parameter, dataset hash, date and reason for rejection to account for multiple testing. The current automated ablation comparison is a screening gate; it is not a formal causal proof of feature value.

## Execution assumptions and accounting

- Passive orders activate after latency and join behind displayed quantity multiplied by a conservative queue factor. Only eligible aggressive trades consume queue/available volume. Cancellations and price touches alone do not fill an order.
- IOC sales walk visible depth with bounded participation and adverse price adjustment. A partial IOC cancels its remainder. End-of-tape inventory is marked, not fabricated as a closed trade.
- Maker fills can have negative signed implementation shortfall. Realized gross P&L uses actual fill prices, so fees alone are subtracted to obtain net P&L. Slippage is already embedded. Benchmark gross minus signed slippage minus fees gives the same net result; never subtract slippage twice.
- Net profit factor uses positive and negative **net** trade outcomes. Sharpe and Sortino use UTC daily marked equity returns, annualized by √365, and are undefined when the sample/variance is insufficient. Report count, expectancy, profit factor, return and drawdown alongside win rate.
- Aggregate fee/slippage drag, exposure, turnover, holding duration, maker filled-order ratio, maker/taker fill share, cancellation ratio and intent-to-fill latency are reported. Intent-to-fill latency includes passive waiting; REST RTT is a separate health measure.

## Acceptance

The test sample needs at least 500 complete flat-to-flat episodes, at least two observed regimes, positive expectancy with a positive lower 95% UTC-day bootstrap bound, net profit factor ≥1.30, mark-to-market drawdown ≤8%, positive walk-forward and validation expectancy, successful cost/parameter stresses and no unresolved inventory/operational incidents. The conservative operational risk default halts at 5% drawdown even though the research rejection threshold is 8%.

Whole-day resampling preserves within-day dependence. Fewer than five days cannot establish a positive lower confidence bound. Trade-order Monte Carlo reports drawdown sensitivity and is not a replacement for chronological loss analysis. No one day or top 5% of profitable trades may explain more than half of positive profits. Rolling 50-trade and regime summaries help identify degradation.

Next require a frozen production-feed paper run: 500+ complete trades **and** at least 14 days, multiple regimes, acceptable drawdown and operational stability. Live approval binds code/config/model hashes and actual fees. There is no automatic promotion. If an edge cannot be established, keep **NO DEPLOYABLE EDGE** and continue data collection or retire the hypothesis.
