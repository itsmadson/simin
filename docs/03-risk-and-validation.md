# Risk Engine, Backtest Methodology, and Go/No-Go Gates

## 1. Risk Engine (the most important component)

Hard, ordered checks. Any failure → order rejected and logged to `risk_events`. No override path exists in code.

**Sizing**
```
risk_per_trade   = base_risk × regime_mult × confidence_mult      # base 0.5–1.0% of equity
stop_distance    = k × ATR(n)          (k by regime, min tick-aware)
qty              = (equity × risk_per_trade) / stop_distance
qty              = min(qty, notional_cap, depth_cap(book, ≤10% of top-5 depth))
```
- Volatility targeting: scale the whole book so portfolio realized vol ≈ target (e.g. 25% annualized). This alone removes most blow-up scenarios.
- **Correlation-aware:** crypto is ~1 factor. Cap *summed BTC-beta exposure*, not per-symbol count. 5 alt longs is one big BTC long — the engine treats it that way.
- Fractional-Kelly cap: never exceed **½ Kelly** on the estimated edge, and estimate Kelly from OOS statistics only.

**Limits (all configurable, all enforced)**
| Limit | Default |
|---|---|
| risk per trade | 0.75% equity |
| max open positions | 5 |
| max exposure per asset | 20% equity |
| max total exposure | 60% equity (long-only venues) |
| max BTC-beta exposure | 1.0× equity |
| daily loss stop | −3% → flatten new entries for the day |
| weekly loss stop | −7% → halve risk |
| max drawdown throttle | −10% → risk ×0.5; −15% → risk ×0.25; −20% → **halt, human review** |
| consecutive losses | 6 → pause 24h |
| per-venue exposure | ≤50% of equity on any one venue (custody risk — see sanctions doc) |

**Circuit breakers (kill switch)** — trip on: spread > 3× median, data staleness > 2 bars, venue error rate > 10%, clock skew > 2s, price gap > 8% in one bar, book depth collapse, model input out of training distribution, PnL/position reconciliation mismatch vs the exchange, or any unhandled exception in the trader loop. Tripping = cancel all working orders, stop new entries, keep managing stops, page the operator. **Restart requires a human.**

**Exits:** ATR stop from entry, breakeven move after +1R, ATR trailing (Supertrend-style) in trends, partial TP (e.g. ½ at +1.5R), and a time stop — if the thesis hasn't worked within the expected holding time, exit. Time stops are underrated and remove a lot of dead capital.

## 2. Backtesting engine

Event-driven, single event queue, strict causality:
```
bar_close(t) → features(t) → regime(t) → signals(t) → risk(t) → orders queued
             → executed at t+1 open (or next tick) with modelled spread/slippage/latency
```
Models: market/limit/stop/stop-limit, partial fills against recorded book depth, maker-fill only if price actually traded through the level, per-venue fee tiers, funding costs where applicable, minimum notional and tick/step rounding, order rejection simulation.

**Anti-bias checklist enforced by tests (each has a dedicated failing-first unit test):**
- no access to `bar[t]` OHLC when deciding at `t` open
- higher-TF joins use last *closed* higher-TF bar (`merge_asof`, strict)
- indicators computed on expanding/rolling windows only — no full-history normalization (a `StandardScaler.fit` on all data is data leakage and is banned by a lint rule)
- universe is point-in-time (delisted coins present until their delisting date) → no survivorship bias
- fills never at prices better than the bar's actual range
- labels use the triple-barrier method with purge + embargo around every CV boundary
- an intentionally "perfect" future-peeking strategy must be detected by the leakage test suite

**Reference validation:** the same strategy is run through the backtester and through the paper engine on identical replayed data; the equity curves must match within tolerance. If they diverge, the simulator is wrong — this test is what makes paper→live believable.

## 3. Validation protocol (nothing is "true" until it passes this)

```
In-sample research  →  Purged K-fold CV  →  Walk-forward (rolling & anchored)
   →  Out-of-sample holdout (never touched during research)
   →  Monte Carlo stress  →  Paper trading  →  Go/No-Go  →  tiny LIVE
```
- **Walk-forward:** train 12–18 months → validate 3 months → test 3 months, roll by 3 months across 2019→2026 (must include the 2021 bull, 2022 bear, 2024–2026 regimes). Report **every** window, not the average. A strategy that only works in one window is a curve fit.
- **The holdout is opened once.** If you open it, tune, and reopen it, it is no longer out-of-sample. Simin enforces this with an experiment ledger — the OOS set is locked behind a flag that records every access.
- **Multiple-testing correction:** every trial is logged in MLflow, and the final Sharpe is reported as a **Deflated Sharpe Ratio** given the number of trials, plus **PBO** (probability of backtest overfitting) via combinatorially symmetric CV. Raw Sharpe from 500 Optuna trials is meaningless; DSR is the number that goes in the report.
- **Monte Carlo (≥10,000 paths):** block-bootstrap trade ordering, return perturbation, ±50% slippage/fee perturbation, execution delay, parameter jitter, and random omission of the best 5% of trades (the "was it one lucky trade?" test). Output: P(profit), P(ruin), expected/95th-pct drawdown, and percentile bands on monthly return.
- **Stress scenarios:** −10/−20/−40% BTC shocks, flash crash + recovery, sudden pump, exchange downtime mid-position, missing/duplicate candles, rejected orders, partial fills, 3× spread widening, 2× fees, stale feed, and a full venue-freeze (withdrawals halted). Survival criterion: bounded, recoverable loss and correct state on restart — not profit.

## 4. Benchmarks every strategy must beat (net of cost, same period, same universe)
Buy & hold BTC · buy & hold equal-weight basket · **hold USDT (in IRT terms)** ← the one most Iranian "profitable" bots actually lose to · RSI(14) 30/70 · MACD cross · EMA 50/200 cross · random-entry-with-same-sizing (100 seeds — this isolates whether the *sizing* is doing the work, not the signal) · simple 30d time-series momentum.

If a random-entry strategy with the same risk engine achieves a similar Sharpe, **the signal has no edge** and the report must say so.

## 5. Go / No-Go criteria for LIVE

LIVE stays hard-disabled until **all** are true, verified by an automated `gates` report:

| # | Gate | Threshold |
|---|---|---|
| 1 | Walk-forward windows profitable | ≥70% of windows, no window worse than −15% |
| 2 | Deflated Sharpe (OOS) | ≥ 1.0 |
| 3 | PBO | ≤ 0.3 |
| 4 | Monte Carlo P(ruin at 50% DD) | ≤ 1% |
| 5 | Strategy survives 2× cost model | still positive expectancy |
| 6 | Beats every benchmark in §4 incl. hold-USDT | yes, OOS |
| 7 | Paper trading duration | ≥ 60 consecutive days, live data |
| 8 | Paper trades | ≥ 200 closed trades |
| 9 | Paper vs backtest divergence | realized Sharpe within 1σ of backtest expectation; slippage within 1.5× modelled |
| 10 | Paper max drawdown | ≤ backtest MC 95th percentile |
| 11 | Ops | 30 days with no unhandled exception, no reconciliation mismatch, no double-order incident |
| 12 | Human approval | explicit signed approval recorded in `runs` |

Then LIVE starts at **≤2% of intended capital**, with a hard per-day notional cap, and scales only after 30 more days of matched performance. Any gate turning red auto-reverts to PAPER.

## 6. Metrics reported everywhere
CAGR, monthly returns table, Sharpe, **Deflated Sharpe**, Sortino, Calmar, max DD + duration, ulcer index, profit factor, win rate, avg win/loss, expectancy per trade, expectancy per $ risked, trade count, exposure %, turnover, total fees paid (as % of gross PnL — if fees > 30% of gross, the strategy is a broker-enrichment scheme), realized vs modelled slippage, recovery factor, VaR/CVaR (95/99), P(ruin), and **PnL in both IRT and USDT**.
