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

On the synthetic data shipped for the offline demo, the calibration currently
reports something like this — your own numbers on real history will differ, and
you should run it yourself:

| Level | Target/month | Measured/month | Median DD | Risk of ruin |
|------:|-------------:|---------------:|----------:|-------------:|
| 2 | +4% | +0.67% | 2.1% | 0.1% |
| 4 | +12% | +1.99% | 8.5% | 5.7% |
| 5 | +20% | −0.29% | 26.1% | 75.0% |
| 7 | +50% | +5.47% | 30.9% | 54.2% |
| 10 | +200% | −9.19% | 80.4% | 97.0% |

That is the system working correctly. A bot that reported +200% here would be
lying to you.

---

## Quick start

```bash
docker compose up
```

Open <http://localhost:3000>. It starts in **lab** mode on the **offline** venue
with generated data, so it runs with no credentials, no API keys, and no network
to any exchange. Turn the dial, press start, and watch it trade on a compressed
clock — one candle every three seconds.

Nothing in that path can spend money.

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
