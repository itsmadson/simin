# سیمین · Simin

**A crypto trading bot with one control: a risk dial from 1 to 10.**
Two modes — `lab` and `real`. Intraday trading on price action, RSI, MACD,
oscillation and market structure. Persian and English.

---

## The honest part, first

You asked for a bot that returns **40% a month at risk 5** and **200% a month at
risk 10**. The dial has all ten levels and levels 5 and 10 carry exactly those
targets. Here is what you need to know about them before anything else.

**Those numbers are targets, not forecasts, and the software treats them that
way everywhere.** +200%/month compounds to about 531,000× in a year. No fund,
desk, or individual has ever sustained it. Level 10 exists because you asked for
a full 1–10 range, and it is configured to genuinely try — 5% risked per trade,
10× leverage, 5-minute candles, almost no entry filter.

**So every target ships next to its measurement.** Run `simin calibrate` and the
system walks each level forward over real history, resamples the trades several
thousand times, and writes back what the level *actually did*: median monthly
return, the 5th and 95th percentile outcomes, median and worst-case drawdown,
and the probability of hitting the drawdown halt. The UI draws both on the same
dial — the target as a hollow dashed arc, the measurement as a solid one — and
hatches the space between them. That space is the product.

**When a level has never been measured, it says so.** The API returns `null`,
not `0`, and the interface renders "not measured yet". A zero shown where a
measurement belongs is the exact failure this design exists to prevent.

Here is what that looks like. Run it yourself:

```bash
simin calibrate --level 4 --venue offline --symbols BTCUSDT ETHUSDT --bars 6000
```

```
calibrating level 4...
  target 12%/mo  measured +0.40%/mo  maxDD 7.7%  ruin 5%  gates FAILED
    failed: survives_double_costs — return at 2x costs: -2.25%
    failed: walk_forward_consistency — 40% of walk-forward windows profitable
    failed: sharpe_positive — Sharpe -0.02 (need > 0.3)
```

Target 12%, measured 0.40%, and three named reasons the configuration should not
be trusted. On the generated demo data the aggressive levels come out worse
still — level 10 measures negative with a ruin probability in the nineties.

That is the system working correctly. A bot that reported +200% here would be
lying to you. Numbers on real exchange history will differ; the machinery that
produces them will not.

---

## Quick start

```bash
cp .env.example .env    # optional; every value already has a working default
docker compose up
```

Open <http://localhost:3000>. It starts in **lab** mode on the **offline** venue
with generated data, so it runs with no credentials, no API keys, and no network
to any exchange. Turn the dial, press start, and watch it trade on a compressed
clock — one candle every three seconds.

Nothing in that path can spend money.

### Lab mode on real prices

Better than the offline demo, and still simulated money: point it at CoinEx.

```bash
SIMIN_VENUE=coinex docker compose up
```

No credentials needed — CoinEx market data is public, and lab mode always wraps
it in an adapter that reports `can_trade = False`. The status line then reads
`coinex (paper)` rather than just `paper`, so it is never ambiguous whether you
are looking at real prices or generated ones.

One thing to expect: at risk level 4 the signal timeframe is **2h**, so the bot
decides once every two hours and will sit still in between. That is correct
behaviour, not a hang — it refuses to act on a candle that has not closed. Use
the **Lab** tab if you want results immediately, or a higher dial level if you
want a faster cadence (level 7 is 15m, level 9 is 5m).

If port 8000 is already taken on your machine, set `SIMIN_API_PORT=8010` and
`SIMIN_PUBLIC_API_URL=http://localhost:8010` in `.env`.

## Which markets, and is any of it real?

Three commands, in the order the questions actually arise.

### 1. Capacity — what can this account trade?

```bash
simin universe --venue coinex --equity 10000 --position 3000
```

Walks the **real order book** for every listed market rather than trusting
turnover, because turnover is a headline one whale and a hundred bots can
manufacture, and depth is what your order actually meets. KASUSDT shows healthy
24h volume and costs 0.16% in slippage on a *thousand-dollar* order — most of a
round trip gone before the trade has an opinion.

On CoinEx futures, at $3,000 a position:

```
Scanned 223 markets at $3,000 per position (equity $10,000)
23 tradeable, 200 excluded

market           turnover/24h    range     slip  round trip   cover
BTCUSDT           133,348,518   3.22%   0.001%     0.102%    31.7
ETHUSDT            58,671,676   2.31%   0.002%     0.104%    22.3
HYPEUSDT           10,999,927   5.61%   0.011%     0.123%    45.8
DOGEUSDT            6,288,887   5.45%   0.000%     0.100%    54.5
...
Excluded:  thin 196   too_expensive 3   stable 1
```

**The answer is account-specific**, which is the whole point. A market that is
untradeable at $50,000 is perfectly fine at $500, so the position size is an
input — and "this coin is illiquid" without naming a size says nothing.

### 2. Correlation — how many bets is that really?

```bash
simin portfolio --venue coinex
```

```
22 markets, average pairwise correlation 0.55, which is 1.8 genuinely
independent bets. Holding all 22 at once hurts 3.5x more in a correlated move
than position count suggests.

  BTCUSDT carries 5 more at rho 0.79: ETHUSDT, SOLUSDT, XRPUSDT, DOGEUSDT, LINKUSDT
```

BTC, ETH, SOL, XRP, DOGE and LINK run 0.75–0.86 with each other. They are one
asset wearing six tickers. Twenty positions in that basket is one position
paying twenty sets of fees, sized as though the risk were spread — which is how
an account that looks diversified takes a six-fold hit on a bad Tuesday.
Effective breadth is `N² / ΣΣρ`, and the bot takes one position per cluster.

### 3. Significance — is any of it edge?

```bash
simin screen --venue coinex --level 4
```

This is the one that matters. Searching 22 markets for the best one is how
people find noise: **the maximum of many draws is large by construction.** Give
230 zero-edge coin-flip strategies a backtest and the luckiest will post an
annualised Sharpe near 2.8. Reporting that as a discovery is not optimism, it is
a category error.

So the screener applies a **deflated Sharpe ratio** (Bailey & López de Prado)
against the number of trials actually run, corrects for the skew and fat tails
that inflate a naive Sharpe, and separately bootstraps the same strategies over
block-shuffled returns to get an **empirical null** — what they score on data
with the structure removed.

Over 416 days of real CoinEx data at level 4:

```
Screened 22 markets on 2h, risk level 4
Correcting for 22 trials. Null Sharpe p95 = +0.08

market        trades  SR/trade   ann.SR   return   maxDD    DSR   >null  verdict
TRUMPUSDT         83     0.193     2.00   19.0%    4.6%   0.42   100%  ----
WLDUSDT           79     0.143     1.11   11.9%    4.2%   0.24   100%  ----
SUIUSDT           60     0.122     0.25    9.0%    3.9%   0.15   100%  ----
...
BTCUSDT           73    -0.001    -0.26   -1.7%    5.5%   0.03    67%  ----
PUMPUSDT          58    -0.369    -3.22  -16.8%    7.5%   0.00    27%  ----

Nothing survived. 22 configurations were tested; the best results are
consistent with luck, and none of them should be traded.
```

TRUMPUSDT looks excellent — +19%, annualised Sharpe 2.00. Deflated for having
tested 22 markets: **0.42**. Ten of 22 positive, twelve negative. That is a coin
flip, and the naive read ("TRUMP made 19%, trade TRUMP") is precisely the
expensive mistake.

**A screener that returns "nothing survived" is working.** That will be the
usual outcome and it is worth far more than a ranked list of lucky coins.

The statistics are tested rather than asserted — `tests/test_research.py`
verifies 0% false positives across 120 searches over pure noise, where an
uncorrected read fires ~100% of the time, while still detecting a genuine
0.3R-per-trade edge.

If you screen several risk levels and report the best, pass `--extra-trials` so
the bar rises accordingly. Searching more and correcting for less is how the
correction gets defeated.

### What a wider, honestly-corrected search found

Four (timeframe, risk level) combinations over ~18 markets is ~70 trials, not
18 — so the search was run with `--extra-trials 4` and the bar raised to match.
Searching more while correcting for less is how the correction gets defeated.

| Timeframe | Level | Positive out-of-sample | Best DSR | Survivors |
|---|---|---|---|---|
| 4h | 4 | 7 / 17 | 0.17 | **0** |
| 2h | 7 | 1 / 17 | 0.02 | **0** |
| 1h | 5 | 0 / 18 | 0.01 | **0** |
| 15m | 7 | 0 / 18 | 0.00 | **0** |

Nothing survived anywhere. But look at the second column, because it is the most
useful number in this document: **the degradation is perfectly monotonic with
timeframe.** 4h is roughly a coin flip. 15m is uniformly negative across every
market tested.

That is fee drag, and it has a direct and uncomfortable implication for the risk
dial: **levels 7 through 10 trade the fastest candles, and on this venue the
fastest candles are measurably the worst.** The aggressive end of the dial is not
merely more volatile in pursuit of a higher return — on this evidence it has a
*lower* expected return than the slow end, and pays for the privilege in
liquidation risk.

Funding offers no consolation either: CoinEx funding rates across the liquid
markets were 0.0000% at the time of writing, so there is no carry to harvest to
offset the friction.

None of this says trading is impossible. It says these six generic
indicator strategies, on this venue, over this year, do not have an edge that
survives being looked for — and that the honest thing to do with that finding is
print it rather than keep searching until something looks good.

## What it trades on

Six strategies, each built for a specific market condition and muted when the
market is not in it. A mean-reversion system screaming "buy" in a freight-train
downtrend must not be allowed to open the trade.

| Strategy | Regime | Idea |
|---|---|---|
| `oscillation` | range | Fades stretched price back to the mean: Bollinger extreme + RSI + z-score + a rejection candle at a tested level |
| `trend_pullback` | trend | Buys dips inside a confirmed uptrend — EMA stack, ATR-measured pullback depth, RSI reset, structure intact |
| `macd_momentum` | trend | MACD crossovers filtered by the 200 EMA and by whether the histogram is actually expanding |
| `breakout` | any | Break of market structure out of a Bollinger-inside-Keltner squeeze, confirmed by volume |
| `rsi_divergence` | any | Price makes a new extreme, RSI does not — but only enters on a change of character |
| `stoch_scalp` | range | Fast stochastic reversals for the low-timeframe levels, with a hard cost gate |

They vote. Agreement produces a confidence score; disagreement lowers it; the
dial's `min_confluence` decides whether what remains clears the bar. That
threshold is the entire link between the risk number you pick and how often the
bot trades.

Under them sits a full price-action layer: fractal swing points, market
structure (BOS and CHoCH), clustered support/resistance, and thirteen scored
candlestick patterns — scored rather than boolean, because a 4:1 pin bar at a
four-times-tested level is not the same trade as a 2:1 pin bar in the middle of
nowhere.

## What the dial actually changes

Picking a level fixes twenty-odd parameters at once:

| | L1 Vault | L4 Balanced | L7 Aggressive | L10 Ruin Or Riches |
|---|---|---|---|---|
| Risk per trade | 0.25% | 1.0% | 2.5% | 5.0% |
| Max leverage | 1× | 2× | 5× | 10× |
| Signal timeframe | 4h | 2h | 15m | 5m |
| Entry threshold | 0.80 | 0.60 | 0.46 | 0.34 |
| Trades per day | 1 | 4 | 14 | 40 |
| Stop | 3.0 × ATR | 2.2 × ATR | 1.6 × ATR | 1.0 × ATR |
| Drawdown halt | 6% | 15% | 30% | 50% |
| Shorts | no | yes | yes | yes |

Higher risk means a looser filter, tighter stops, faster candles and more
trades. That is what "more risk" mechanically *is*, and the tests assert the
curve moves monotonically in both directions.

## Position sizing

One identity does the real work:

```
position size = (equity × risk_per_trade) / distance to stop
```

A wide stop gets a small position; a tight stop gets a large one; the loss if
the stop is hit is the same either way. Sizing from *equity* rather than cash
means the account de-risks automatically in a drawdown — lose 20% and every
subsequent position is 20% smaller, which turns a losing streak into a decaying
curve instead of a straight line to zero.

Leverage is used only when a position's notional exceeds free margin, never
because the dial permits it. And if the venue would liquidate before the stop is
reached, the trade is **refused** — a stop that can never execute is decoration,
and the loss behind it is unbounded.

## Circuit breakers

| Breaker | Behaviour |
|---|---|
| Daily loss halt | Stops opening positions; lifts at the next UTC day |
| Drawdown halt | Flattens everything and stops; needs a human to clear |
| Loss streak | Halves position size until a winner breaks the streak |
| Kill switch | Closes all positions, disables trading until restart |
| Capital cap | `SIMIN_MAX_CAPITAL` bounds notional independently of the dial |

## Lab mode

```bash
simin backtest --level 7 --venue coinex --symbols BTCUSDT ETHUSDT --bars 8000
simin calibrate --level 0     # measure every level
simin dial                    # print target beside measurement
simin explain 7               # everything one level does
```

Every backtest reports the return, **buy-and-hold over the same window**, and
**the same strategy at doubled costs** — because a return means nothing until
you know what simply holding would have done, and a strategy that dies when fees
double was never profitable, only lucky about liquidity.

Then thirteen validation gates run, and they can fail a run that made money:
sample size, profit factor, expectancy, drawdown within the dial, survival at 2×
costs, cost drag, beating buy-and-hold, walk-forward consistency, in-sample
degradation, ruin risk, and median Monte Carlo outcome.

### Why the backtest is trustworthy

The bar loop is strictly ordered, and the order is the point:

1. Fill entries decided on the **previous** bar, at **this** bar's open.
2. Mark open positions against this bar's high/low/close; exit if hit.
3. Evaluate strategies against this bar's **close**.
4. Queue any entry for the **next** bar.

That one-bar delay is what a real bot experiences — you cannot see a candle
close and simultaneously trade at that close. Within a bar, when both stop and
target were touched, the **stop is assumed to have come first**; OHLC does not
record the intrabar path, and assuming the favourable one is how a losing system
backtests profitably. A stop that gapped through fills at the open, not at the
stop price, which is the difference between a modelled −1R and a real −4R.

`tests/test_causality.py` proves it: it replays history, replaces everything
after bar 1200 with a violent crash, and asserts every trade that closed before
the cutoff is byte-identical. That test has already caught one real bug —
support/resistance levels were being computed over the whole dataset and handed
to every bar, letting a strategy at bar 500 place its stop against a level that
would not form until bar 1800.

## Real mode

Real trading is behind **five** separate deliberate acts:

1. `SIMIN_MODE=real`
2. Real API credentials for the venue
3. `SIMIN_REAL_MODE_ACKNOWLEDGED=1` — no default, ever
4. `SIMIN_MAX_CAPITAL` greater than zero
5. Typing `I understand this trades real money` verbatim in the UI

Any adapter that cannot place real orders reports `can_trade = False`, and the
runner refuses to start real mode against it. Lab mode *always* returns a
simulated adapter regardless of which venue you pick.

```bash
# .env — never commit this
SIMIN_MODE=real
SIMIN_VENUE=coinex
SIMIN_COINEX_KEY=...
SIMIN_COINEX_SECRET=...
SIMIN_REAL_MODE_ACKNOWLEDGED=1
SIMIN_MAX_CAPITAL=500
```

Credentials are read from the environment only. They are never sent to the
browser, never written to disk, and never logged — the settings endpoint reports
only *whether* a venue is configured.

## Venues

| Venue | Futures | Leverage | Quote | Round-trip cost |
|---|---|---|---|---|
| `offline` | yes | 10× | USDT | synthetic — demo only |
| `paper` | yes | 10× | USDT | 0.20% |
| `coinex` | yes | 10× | USDT | 0.20% |
| `nobitex` | **no** | 1× | IRT | 0.96% |
| `wallex` | **no** | 1× | IRT | 1.10% |

**Iranian venues are spot only.** Running a level-9 profile there would be a
completely different bot, so the dial is *clamped* — leverage forced to 1×,
shorts disabled — and a visible warning is attached. The bot never pretends
leverage was applied.

**Toman profit is not necessarily profit.** The Rial has lost value against the
dollar for years, and turning 100M Toman into 130M during a 30% devaluation is
not a gain. The Iranian adapter exposes the USDT/IRT rate so PnL can be read in
both. It also converts Rial to Toman at the boundary — the API quotes Rial, the
country thinks in Toman, and mixing them is a 10× error in every price on screen.

Note the cost column. At 0.96% round trip on Nobitex, a strategy needs to clear
almost 1% per trade before it has made anything at all, which is why the
low-timeframe dial levels are close to unusable on Iranian venues. The software
will let you run them and will show you the measurement.

## The interface

Next.js 15, Morabba typeface, Persian-first with real RTL and an English toggle.

The design is one instrument: brushed silver on near-black, with a single
variable — `--heat` — driven by the dial position. Turn the risk up and the
entire interface warms from silver-cyan through amber to crimson. It is not
decoration; it is the primary signal that the machine has been made more
dangerous, applied everywhere at once so it cannot be dismissed as one red label.

Two things it does that most trading UIs do not:

**The honesty gap.** The target and measured arcs are drawn on the same track
with the difference hatched between them. You see the exaggeration rather than
reading it out of a table.

**The position risk axis.** Liquidation, stop, entry, current price and target
are placed *to scale* on one line. At 8× leverage the liquidation price is 12%
away and the stop is 2% away — as four numbers in a row those look similar; on a
shared axis the difference is immediate.

The live view also shows what the ensemble decided for every symbol on the last
bar, **including the ones it chose not to trade and why**. A bot that only shows
its trades is a bot you cannot supervise.

## Layout

```
backend/src/simin/
  core/types.py          Decimal money, TF, Candle, Position, Trade
  config.py              settings; real mode has no default
  risk/dial.py           THE DIAL — ten profiles, every parameter
  risk/engine.py         sizing, guards, circuit breakers, trailing stops
  indicators/core.py     RSI, MACD, ATR, Bollinger, ADX, Stochastic, Supertrend…
  indicators/features.py 35 features per bar, warm-up as None
  priceaction/           swings, BOS/CHoCH, S/R levels, 13 scored patterns
  strategies/            six strategies + the confluence ensemble
  exchanges/             base, paper, replay, coinex, iranian, registry, costs
  lab/                   backtester, metrics, walk-forward, Monte Carlo, gates
  execution/runner.py    the live bot loop
  api/app.py             FastAPI + WebSocket
frontend/
  app/                   dial · live · lab · markets · settings
  components/            RiskDial, PositionCard, Shell
```

## Development

```bash
cd backend && pip install -e ".[dev]" && pytest      # 119 tests
cd frontend && npm install && npm run dev
```

## Licence

MIT for the code. The **Morabba** typeface in `frontend/public/fonts/` is
proprietary — see `moraba/FontLicense.txt` and <https://fontiran.com>. It is
bundled here for your own use; check the licence before redistributing.

## Final word

Trading is not a solved problem, and this software will not solve it for you.
What it will do is refuse to lie to you about what it is doing: it measures
instead of promising, it counts every fee, it assumes the bad fill, and when a
configuration fails validation it says so on the screen while it runs.

Use lab mode for a long time first.
