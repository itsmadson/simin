# Simin — Phase 1: Deep Research Report
*(سیمین — گزارش تحقیق فاز ۱)*

Date: 2026-08-23. Status: **research only, no strategy validated yet.**

---

## 0. The three findings that matter most

Everything else in this document is detail. These three change the design:

### 0.1 The IRT premium dominates every Toman pair

`BTC/IRT = (BTC/USDT) × (USDT/IRT)`

On an Iranian exchange, a "BTC" trade is really **two bets**: crypto beta and the Rial/Dollar rate. Historically the USDT/IRT leg carries a large share of the variance of BTC/IRT and is driven by news, capital controls and the free-market dollar rate — not by anything a candlestick indicator can see. A backtest on BTC/IRT that produces a smooth up-curve is often just measuring Rial devaluation, **not alpha**.

**Design consequence:** Simin must decompose every Toman pair into (crypto leg, FX leg), benchmark strategies against a `hold USDT` baseline denominated in IRT, and where possible trade `COIN/USDT` pairs on the Iranian exchange to isolate the crypto leg. Reporting PnL only in IRT is a lie by omission; the dashboard shows PnL in **both IRT and USDT**.

### 0.2 Transaction cost sets the strategy universe, not the indicators

Iranian spot fees are roughly 0.10–0.25% maker / 0.15–0.30% taker per side. Add IRT-pair spread (frequently 0.2–1.0% on non-BTC/ETH pairs) and slippage. Realistic **round-trip cost: 0.5–1.2%**.

A signal must have expected edge > round-trip cost to be tradeable at all. This kills essentially every 1m/5m scalping idea on Iranian venues. The surviving frequency band is **1h–1D**, with 15m used only for entry timing inside an already-decided position.

**This is the single most common reason retail crypto bots lose money**, and it is decided before a single indicator is chosen.

### 0.3 +200%/month is not an achievable target — it is a ruin generator

200%/month compounded = **531,441× per year (+53,144,000%)**. No fund, prop desk or public track record has ever sustained anything within two orders of magnitude of that. To reach it you need either:
- ~2% edge per trade at ~60 trades/month with zero drawdown clustering (edge that large does not exist net of the 0.5–1.2% costs above), or
- leverage high enough that a single 15% adverse move liquidates the account.

Kelly math: to target 200%/month you must risk far above full-Kelly on any realistic edge, and **betting above full Kelly makes probability of ruin → 1 as trade count grows**, even with a genuinely positive edge. That result is not opinion; it is a property of geometric growth.

**What Simin will do instead:** treat 200%/mo as a *hypothesis to be tested and reported on*, and optimize for risk-adjusted return. Realistic, still-ambitious targets for a validated systematic crypto strategy:

| Tier | Monthly return | Max DD | Deflated Sharpe | Credibility |
|---|---|---|---|---|
| Realistic good | 2–5% | 10–15% | 1.0–1.8 | achievable, hard |
| Excellent | 5–10% | 15–25% | 1.8–2.5 | rare, needs real edge |
| Suspicious | 20–50% | any | any | assume overfit until OOS proves otherwise |
| Impossible sustained | 200% | — | — | reject |

If a backtest shows +200%/month, the correct engineering response is to **hunt for the bug** (look-ahead, leakage, unrealistic fills, survivorship), because that is what it almost always is.

---

## 1. Technical indicator research

Method for ranking: each family judged on (a) regime dependence, (b) usable timeframe, (c) false-signal profile, (d) correlation with other families, (e) marginal value in combination, (f) survival after 0.5–1.2% round-trip cost, (g) overfitting surface (number of tunable params), (h) whether independent out-of-sample evidence exists.

### 1.1 Ranking (for crypto, after costs)

**Tier A — keep, as features or filters**

| Method | Best regime | TF | Why it survives | Overfit surface |
|---|---|---|---|---|
| **ATR / realized vol** | all | all | Not a signal — a *sizing and stop* primitive. Volatility is the most persistent, most forecastable quantity in markets (vol clustering / GARCH effect). Highest-value single input in the whole system. | low (1 param) |
| **Market structure (HH/HL, swing breaks)** | trend | 1h–1D | Non-parametric, few knobs, directly encodes trend definition. | low |
| **Donchian / volatility breakout** | trend onset, expansion | 4h–1D | Oldest publicly documented profitable systematic family (Turtle lineage); edge is small but has decades of OOS. Fits crypto's fat tails. | low (1–2) |
| **Time-series momentum (12h–30d returns)** | trend | 4h–1D | Strongest cross-asset anomaly with genuine multi-decade, multi-market OOS evidence; documented in crypto cross-section too. | low |
| **Funding rate / OI / liquidations** | leverage flush, squeezes | 1h–1D | *Not* price-derived — genuinely new information. Extreme funding + rising OI is a real crowding signal. Best single "alt data" in crypto. | low–med |
| **ADX / trend-strength** | regime gate | 4h–1D | Weak as an entry, strong as a **regime switch** deciding trend-vs-mean-reversion. | low |
| **VWAP (session/anchored)** | intraday execution | 5m–1h | Execution benchmark and mean-reversion anchor; helps reduce slippage cost. | low |
| **Volume anomaly (z-score of volume)** | breakout confirmation | all | Cheap, orthogonal-ish to price, filters fake breaks. | low |

**Tier B — useful only as conditioned features, never standalone**

| Method | Verdict |
|---|---|
| RSI | Standalone RSI(14) 30/70 is one of the most thoroughly failed retail rules in crypto: in trends it sells winners early, in chop it is a coin flip after costs. As a *feature* (RSI level, RSI slope, RSI divergence) conditioned on regime, it has modest value. |
| MACD | A smoothed momentum difference — ~85–95% correlated with a plain EMA-slope feature. Redundant with momentum; keep at most one. |
| EMA/SMA crossovers | Same information as momentum but with extra lag; positive expectancy exists on 1D crypto historically, but after fees the fast variants die. Keep slow (50/200-ish) only as a regime label. |
| Bollinger Bands | Only a vol-normalized price z-score. Useful as `(close - MA)/σ` feature. Band-touch mean reversion works **only** in confirmed low-ADX ranges and blows up in trends. |
| Stochastic RSI | Double-smoothed oscillator, extremely noisy on <1h, very high false-signal rate. Highly correlated with RSI — do not use both. |
| OBV | Cumulative signed volume; conceptually fine but crypto's wash trading corrupts it on smaller venues. Prefer order-flow imbalance where available. |
| Pivot points / Fibonacci | No mechanism, arbitrary levels, huge researcher degrees of freedom. Where they "work", it's self-fulfilling clustering of orders. Keep only as *S/R candidates fed to a model*, never as rules. |
| Ichimoku | A bundle of MAs + displacement. Its cloud is a decent trend filter, but it is 5 parameters doing the job of one; the displacement is also the most common source of accidental **look-ahead bugs**. Low priority. |
| Supertrend | ATR-band trailing rule. Genuinely decent as a *trailing stop*, mediocre as an entry. Use it in the exit engine, not the signal engine. |

**Tier C — high research value, high engineering cost**

| Method | Verdict |
|---|---|
| Order-book imbalance | Real short-horizon predictive power (seconds–minutes), well documented in microstructure literature. But its horizon is shorter than Iranian venues' cost floor → **unusable for alpha here**, valuable for **execution** (when to place the limit order). |
| Volume Profile / POC | Good structural context for S/R; expensive to build correctly from trade data. Phase 2 feature. |
| Liquidation cascades | Excellent mean-reversion trigger after forced-selling flushes. Requires derivatives data (Binance/Bybit public data) even if you trade spot in Iran — the *information* is global. |

### 1.2 Correlation reality

RSI, Stoch RSI, MACD, CCI, Williams %R, ROC are all **monotone transforms of recent returns**. A PCA over a standard 30-indicator panel typically collapses to ~4–6 effective factors:

1. Trend/momentum, 2. Volatility, 3. Volume/participation, 4. Mean-reversion (price vs anchor), 5. Positioning/leverage (funding/OI), 6. Liquidity/spread.

**Design consequence:** Simin's feature engine builds **one canonical feature per factor family**, plus regime interactions. Stacking 30 correlated oscillators does not add information; it adds variance and multiple-testing burden.

### 1.3 What actually has a mechanism (why an edge could exist)

An edge needs a *reason*, not a chart. Candidate mechanisms in crypto:
1. **Slow information diffusion / underreaction** → time-series momentum.
2. **Forced liquidation cascades** → short-horizon overshoot and reversion.
3. **Leverage crowding** → funding-rate extremes precede squeezes.
4. **Retail flow seasonality / weekend liquidity** → intraday & weekday effects, weak but real.
5. **Local segmentation of the Iranian market** → IRT premium mean-reversion, cross-venue price dispersion (Section 4 in `04-exchanges-iran.md`). *This is the most underexploited and most Iran-specific edge available to this project.*

Simin ranks (5) and (1)/(3) as its highest-prior hypotheses.

---

## 2. ML / AI research

### 2.1 Verdict up front

**For tabular, engineered financial features with low signal-to-noise, gradient-boosted trees (LightGBM/XGBoost/CatBoost) are the correct default.** Recent comparative work on crypto repeatedly finds boosting matching or beating LSTM out-of-sample, and deep sequence models only pull ahead with very large data and careful regularization ([Springer 2025 comparative study](https://link.springer.com/article/10.1007/s44163-025-00519-y)). Deep nets are also far easier to leak data into.

Ranking for this project:

| Rank | Approach | Role | Notes |
|---|---|---|---|
| 1 | **LightGBM meta-labeler** | decide *whether to take* a rule-based signal, and with what size | López de Prado's meta-labeling: primary model sets direction, ML sets P(win). Improves precision without needing the ML to predict price. Lowest-risk, highest-value ML in the whole system. |
| 2 | **LightGBM/HMM regime classifier** | choose which strategy is live | Small label space, robust, interpretable. |
| 3 | **Volatility forecaster (GARCH or GBM on realized vol)** | position sizing | Vol is genuinely predictable; this is where ML pays reliably. |
| 4 | Logistic regression + strong features | baseline that must be beaten | If LightGBM cannot beat well-regularized logistic OOS, the features are noise. |
| 5 | Ensembles (bagged seeds/folds) | variance reduction | cheap, always worth it |
| 6 | Temporal CNN / GRU / TFT | phase-7 experiment only | Only after tabular pipeline is validated. Must beat GBM on the *same* purged CV. |
| 7 | Transformer for price | low priority | Attention on 1D price series with ~2k samples/asset overfits trivially. |
| 8 | **Reinforcement Learning (PPO/SAC/DQN)** | **not in v1** | RL needs a faithful market simulator including market impact. With a wrong simulator, RL learns to exploit the simulator, not the market. It is the single most over-claimed technique in retail algo trading. Revisit only after the event-driven backtester is validated against live paper fills. |

**Explicit anti-hype commitment:** if the LightGBM meta-labeler does not beat the plain rule-based strategy on out-of-sample deflated Sharpe, Simin ships with ML **off** and the dashboard says so. No fake AI.

### 2.2 Target design (more important than model choice)

- **Do not predict next-bar return.** Signal-to-noise is near zero, and MSE-optimal models learn to predict ~0.
- Use the **triple-barrier method**: label each event by which barrier it hits first (profit target / stop / time limit), with barriers scaled by ATR. This produces a target aligned with how the strategy actually exits.
- **Sample weighting by uniqueness** (overlapping labels double-count information) and **purged, embargoed K-fold CV** — standard k-fold on time series leaks and is the #1 cause of fantasy backtests.
- Meta-labeling target = "did the primary signal make money after costs?" → binary, balanced-ish, and directly usable as a probability for sizing.

### 2.3 Feature set (v1, ~40–60 features, not 300)

Per symbol, per bar, all computed from **closed bars only**:
- returns over {1,3,6,12,24,72,168} bars; vol-normalized versions
- realized vol (multi-window), ATR, vol-of-vol, Parkinson/Garman-Klass estimators
- distance to MA(20/50/200) in σ units; Bollinger z-score
- ADX, trend-slope, R² of a rolling linear fit (trend quality)
- volume z-score, dollar volume, Amihud illiquidity
- higher-TF context features (1D regime injected into 1h rows, correctly lagged)
- BTC-relative beta & residual momentum (crypto is one factor + noise)
- funding rate, OI change, long/short ratio, liquidation volume (from global venues)
- **IRT-specific:** USDT/IRT premium vs implied global rate, premium z-score, cross-venue dispersion
- calendar: hour-of-day, day-of-week (Iranian market has a distinct weekend pattern)

---

## 3. Market regime detection

**Recommended: a 2-layer hybrid, not a single classifier.**

- **Layer 1 — deterministic, auditable state machine (v1):** axes = trend direction (slow MA slope + market structure), trend strength (ADX / rolling R²), volatility bucket (realized vol percentile over 1y), and stress flag (drawdown speed, liquidation spike, spread blowout). This produces the 12 requested regimes as combinations, is fully explainable on the dashboard, and cannot silently fail.
- **Layer 2 — HMM (2–4 states) on returns+vol, plus a LightGBM classifier**, run in parallel and *compared* to Layer 1. Ship it only if it beats the state machine on downstream strategy PnL, not on classification accuracy.

Key subtlety: **regime labels must be causal**. Fitting an HMM on the full history and reading its smoothed states is look-ahead. Use filtered (online) state estimates with rolling re-fit only.

Regime → strategy map (initial prior, to be validated):

| Regime | Action |
|---|---|
| Strong bull / strong bear w/ high ADX | trend following + breakout, wider stops, trail |
| Weak trend | reduced size, trend only on higher TF confirmation |
| Sideways + low vol | mean reversion at band extremes, tight targets |
| Sideways + high vol | **stand down** (worst regime for both families) |
| Breakout / vol expansion | volatility breakout, size by inverse vol |
| Panic / crash | flatten, no new longs, optionally fade only after liquidation-flush trigger |
| Unusual volume / accumulation / distribution | context flags feeding the ML, not standalone triggers |

---

## 4. Timeframe architecture

Given the cost floor (§0.2), the recommended stack:

```
1D   → regime + asset selection (which coins are tradeable this week)
4h   → primary signal generation (trend / breakout / MR)   ← the money TF
1h   → confirmation + risk state, position management
15m  → entry timing and stop placement only
5m/1m→ execution slicing only (never signal generation)
```

Rules enforced in code:
- Higher-TF features are joined to lower-TF bars **as of the last *closed* higher-TF bar** (`merge_asof` with strict backward direction). This is the #1 multi-timeframe look-ahead bug.
- Everything is timestamped in UTC internally; Tehran time only at the presentation layer (and IRT market has DST-adjacent quirks — never do TZ math on the data path).
- No indicator may read `bar[t]` values when acting at `bar[t]` open. Signals from bar `t` execute at bar `t+1` open, with slippage.

---

## 5. Asset universe

Static list is wrong. Simin scores a candidate universe daily on:

`tradeable = (median 30d IRT volume > X) AND (median spread < 0.3%) AND (listed > 90d) AND (no delisting/maintenance flag)`

then ranks by a liquidity-adjusted expected-edge. BTC/ETH always in universe as regime anchors even if not traded. **Survivorship handling:** the universe snapshot is stored per-day, so backtests only ever see what was actually listed and liquid on that day.

---

## 6. Cost model (must be built before any strategy)

```
cost_per_trade = taker_fee + spread/2 + slippage(size, depth) + latency_drift
```
- fees from a per-exchange config (see `04-exchanges-iran.md`)
- spread & depth measured from real recorded order books, not assumed
- slippage modelled as a function of order size vs top-N book depth, with a square-root impact term for larger orders
- default backtest assumption is **pessimistic**: taker fills, 1.5× measured spread, +1 bar latency

Simin reports every strategy result twice: at modelled cost, and at **2× modelled cost**. A strategy that dies at 2× cost is not deployable.

---

## Sources

- [Bailey & López de Prado, *The Deflated Sharpe Ratio*](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551) — selection bias / multiple-testing correction, PBO
- [Bailey et al., *Statistical Overfitting and Backtest Performance*](https://sdm.lbl.gov/oapapers/ssrn-id2507040-bailey.pdf)
- [Deflated Sharpe ratio — overview](https://en.wikipedia.org/wiki/Deflated_Sharpe_ratio)
- [ML approaches to cryptocurrency trading optimization: comparative analysis (Discover AI, 2025)](https://link.springer.com/article/10.1007/s44163-025-00519-y) — GBM/XGBoost ≥ SVR/LSTM
- [Evaluating ML models for crypto price forecasting (PMC, 2025)](https://pmc.ncbi.nlm.nih.gov/articles/PMC12571449/)
- [Machine learning for cryptocurrency market prediction and trading (ScienceDirect)](https://www.sciencedirect.com/science/article/pii/S2405918822000174) — LSTM/GRU long-short OOS Sharpe after costs
- [Spurious Predictability in Financial Machine Learning (arXiv)](https://arxiv.org/pdf/2604.15531)
