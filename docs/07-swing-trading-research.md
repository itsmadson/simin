# Intraday & Swing Trading: what the data actually says

*Question asked: "it should scalp intraday, buy and sell all day, hold at most 4 days."*

Everything below is measured on **real Binance data**: 40,701 hourly bars per symbol
(2022-01-01 → 2026-08-23) for BTC/ETH/SOL, plus 12 more symbols from 2023.

---

## 1. The central result: edge grows with holding time, cost does not

For each holding horizon *k*, the table gives the average gross return of being long when
the past-*k* return was positive — the raw material any directional strategy works with.

**BTCUSDT, hourly, 4.6 years**

| Hold | Gross edge / trade | t-stat | Trades/yr | Net @1.10% (IRT) | Net @0.20% (global) |
|---:|---:|---:|---:|---:|---:|
| 1h | −0.001% | −0.2 | 4,425 | −1.101% | −0.201% |
| 2h | +0.001% | 0.2 | 2,224 | −1.099% | −0.199% |
| 4h | +0.005% | 0.7 | 1,109 | −1.095% | −0.195% |
| 8h | +0.034% | 3.3 | 558 | −1.066% | −0.166% |
| 12h | +0.063% | 4.9 | 371 | −1.037% | −0.137% |
| 24h | +0.090% | 5.1 | 187 | −1.010% | −0.110% |
| **48h** | **+0.274%** | **11.1** | 94 | −0.826% | **+0.074%** |
| 72h | +0.313% | 10.1 | 63 | −0.787% | +0.113% |
| 96h | +0.364% | 10.1 | 47 | −0.736% | +0.164% |
| 336h | +0.911% | 12.7 | 14 | −0.189% | +0.711% |

SOL is stronger (48h: +0.479%, t=9.9; 96h: +0.823%, t=11.3), ETH weaker (48h: +0.227%, t=6.8).
The shape is identical across all three.

**Three conclusions, none of them opinions:**

1. **Below ~8 hours there is no edge to capture.** At 1-4h the t-statistic is under 1 — the
   signal is indistinguishable from zero. This is not "a small edge eaten by fees"; there is
   nothing there before fees are even considered. No execution skill rescues a zero.
2. **The edge is real and very strong at 48-96 hours** (t = 10-11 across three independent
   assets). This is the band the user's own 4-day ceiling lands in.
3. **Cost decides everything.** The same signal is profitable at a 0.20% round trip and
   deeply unprofitable at 1.10%. Below 336h of holding, nothing clears the Iranian IRT cost.

## 2. Intraday seasonality does exist — and is too small to trade alone

Independently measured, matching published findings:

| | Best hour (UTC) | Worst hour (UTC) |
|---|---|---|
| BTC | **22:00** +0.036%/bar, t=2.8 | 13:00 −0.027%, t=−1.7 |
| SOL | **22:00** +0.041%/bar, t=2.3 | 13:00 −0.043%, t=−2.1 |

Both assets independently pick the same best and worst hour, which is what a real effect looks
like. But +0.036% per bar against a 1.10% round trip is a factor of 30 short. It is usable as
**entry timing** for a position you were going to take anyway — never as a reason to trade.

## 3. So how do you trade every day? Breadth, not frequency

If one symbol can only be traded every 2-4 days, twenty symbols produce entries almost daily
while every individual position still respects the ceiling. Measured on the 15-symbol universe:

| Universe | Trades | Trades/day | Days with activity |
|---|---:|---:|---:|
| BTC only | 389 | 0.29 | 36% |
| 15 symbols | 1,911 | **1.44** | 34% |

Same signal, same holding rules, 5× the activity — without shortening holds into the zero-edge
zone. **This is the answer to "trade all day": widen the universe, not the clock.**

## 4. Four real bugs this research exposed

Found only because the strategies were run on real data at portfolio scale:

1. **Loss-streak pause never expired.** The design called for a 24-hour cool-off after a losing
   run; the code blocked permanently. On a 15-symbol run it rejected **61,370 entries** and
   effectively ended the backtest after the first bad stretch.
2. **Position limit could be exceeded by a race.** Orders queued on the same bar were not
   counted against `max_open_positions`, so a 5-position limit became 7 in practice.
3. **Stop distance did not match the holding horizon.** A 2×ATR(1h) stop with a 4-day target is
   incoherent: price wanders several hourly ATRs within one day, so positions were removed long
   before the thesis could resolve. Average hold was pinned at ~10h against a 96h ceiling.
   Stops are now scaled by `annual_vol × sqrt(hold_hours / 8760)`, which moved the average hold
   to 19-23h with the ceiling actually being reached.
4. **Trailing stop converted a swing strategy into an intraday one** by ratcheting from the
   first bar. It now engages only after the trade is up 1R, and trails by the original risk
   distance.

## 5. Where this leaves the strategy

`swing_momentum` and `swing_pullback` (in `simin/strategies/swing.py`) implement the version the
data supports: 48-hour momentum with a trend-quality filter, horizon-scaled stops, a hard 96-hour
ceiling, exits on a decisive signal reversal.

Current honest status on the 15-symbol universe, 2024-01-01 → 2026-08-23:

| Cost tier | Return | Trades/day | Avg hold |
|---|---:|---:|---:|
| Iranian IRT (1.10%) | −27.0% | 1.44 | 19h |
| Global (0.20%) | ~breakeven on BTC | 0.30 | 23h |

**It is not yet profitable, and it is not being shipped as if it were.** The parameters have not
been walk-forward validated, and tuning them until the backtest looks good is exactly the
failure mode the whole validation stack exists to prevent. The next honest step is the Lab:
optimise on 2022-2024 only, then look at 2025-2026 once.

## 6. The holdout was opened. The result did not survive.

Twelve configurations were searched on **2022-01-01 → 2024-12-31 only**, across 15 symbols at a
0.20% round trip. The holdout (2025-01-01 → 2026-08-23) was then evaluated exactly once, with
the trial count carried into the deflated Sharpe.

**Training leaderboard (top 4 of 12):**

| Score | Return | Sharpe | MaxDD | Trades | Parameters |
|---:|---:|---:|---:|---:|---|
| 0.38 | +7.18% | 0.38 | −11.3% | 2,458 | mom=0.02 quality=0.25 trend=0.02 |
| 0.33 | +6.54% | 0.33 | −12.5% | 2,504 | mom=0.003 quality=0.25 trend=0.02 |
| 0.28 | +5.06% | 0.28 | −11.3% | 2,451 | mom=0.02 quality=0.1 trend=0.02 |
| 0.28 | +5.45% | 0.28 | −11.9% | 2,972 | mom=0.003 quality=0.25 trend=0.005 |

Every single configuration was profitable in training. That is exactly the pattern that should
raise suspicion rather than confidence.

**Holdout, opened once:**

| | Trades | Win rate | Profit factor | Return | Sharpe | MaxDD |
|---|---:|---:|---:|---:|---:|---:|
| Train (2022-2024) | 2,458 | 42.0% | 1.11 | **+7.2%** | 0.38 | −11.3% |
| Holdout (2025-2026) | 1,279 | 35.2% | 0.91 | **−3.2%** | −0.40 | −8.2% |

Deflated Sharpe on the holdout: **0.01**. Sharpe retention: **−104%** — the sign flipped.

**VERDICT: INVALID.** The parameters fitted the training window. The profit factor fell from
1.11 to 0.91 and the win rate from 42% to 35%, which is what a fitted edge looks like when the
data it was fitted to runs out.

This is the system working as designed. Twelve profitable-looking backtests, one honest test,
and the honest test says no. Publishing the 7.2% figure and calling it a strategy would have
been trivially easy and completely wrong.

**What this does not invalidate:** the horizon curve in §1. That is a property of the data
measured directly across three assets with t-statistics of 10-11, not a fitted parameter set.
The edge at 48-96 hours is real. What failed is this particular way of *harvesting* it.

Reproduce with:

```bash
docker compose exec api python -m simin.cli sweep --cutoff 2025-01-01
```

Every holdout access is written to `risk_events` with the trial count and chosen parameters.

## 7. Conclusions

What the research has already settled, and what no amount of tuning will change:

- an intraday version of this cannot work on any venue, because the underlying signal is zero;
- a 2-4 day version has a strong, repeatable signal across assets;
- whether that signal survives depends almost entirely on the round-trip cost of the venue —
  which is a choice about *where* you trade, not *how* you trade.
