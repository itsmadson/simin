"""Indicator primitives.

Two deliberate constraints:

* **Causal by construction.** Every function returns a list the same length as its
  input, where element ``i`` uses only inputs ``0..i``. Warm-up positions are
  ``None`` rather than a back-filled guess — a silently back-filled warm-up is a
  peek at the future dressed up as a number.
* **float, not Decimal.** Indicators are statistics, not money. Money stays
  Decimal all the way through execution; mixing the two in a rolling mean just
  makes it slow without making it more correct.

Only one indicator per factor family is implemented. Stacking ten oscillators
that are all transforms of recent returns adds variance, not information
(docs/01-research.md §1.2).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence

from simin.types import Bar

Series = list[float | None]


def closes(bars: Sequence[Bar]) -> list[float]:
    return [float(b.close) for b in bars]


def highs(bars: Sequence[Bar]) -> list[float]:
    return [float(b.high) for b in bars]


def lows(bars: Sequence[Bar]) -> list[float]:
    return [float(b.low) for b in bars]


def volumes(bars: Sequence[Bar]) -> list[float]:
    return [float(b.volume) for b in bars]


def sma(values: Sequence[float], period: int) -> Series:
    _check_period(period)
    out: Series = [None] * len(values)
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values: Sequence[float], period: int) -> Series:
    """Exponential moving average, seeded with the first SMA.

    Seeding with values[0] instead would make the first hundred bars depend on a
    single print, which is how a strategy ends up with different signals
    depending on where the backtest happened to start.
    """
    _check_period(period)
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def wilder_smooth(values: Sequence[float], period: int) -> Series:
    """Wilder's smoothing (used by ATR, RSI, ADX). Alpha = 1/period."""
    _check_period(period)
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


def true_range(bars: Sequence[Bar]) -> list[float]:
    """max(high-low, |high-prev_close|, |low-prev_close|) — gap-aware range."""
    out: list[float] = []
    prev_close: float | None = None
    for b in bars:
        h, low_, c = float(b.high), float(b.low), float(b.close)
        if prev_close is None:
            out.append(h - low_)
        else:
            out.append(max(h - low_, abs(h - prev_close), abs(low_ - prev_close)))
        prev_close = c
    return out


def atr(bars: Sequence[Bar], period: int = 14) -> Series:
    """Average True Range — the sizing and stop primitive the whole risk engine rests on."""
    return wilder_smooth(true_range(bars), period)


def rsi(values: Sequence[float], period: int = 14) -> Series:
    _check_period(period)
    gains: list[float] = [0.0]
    losses: list[float] = [0.0]
    for prev, cur in itertools.pairwise(values):
        change = cur - prev
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = wilder_smooth(gains[1:], period)
    avg_loss = wilder_smooth(losses[1:], period)
    out: Series = [None] * len(values)
    for i in range(len(avg_gain)):
        g, loss = avg_gain[i], avg_loss[i]
        if g is None or loss is None:
            continue
        out[i + 1] = 100.0 if loss == 0 else 100.0 - 100.0 / (1 + g / loss)
    return out


def adx(bars: Sequence[Bar], period: int = 14) -> Series:
    """Trend *strength* (direction-agnostic).

    Used as a regime gate, not an entry: it says whether trend-following or
    mean-reversion is the right family right now (docs/01 §1.1).
    """
    _check_period(period)
    n = len(bars)
    out: Series = [None] * n
    if n < period * 2:
        return out
    plus_dm: list[float] = [0.0]
    minus_dm: list[float] = [0.0]
    for prev, cur in itertools.pairwise(bars):
        up = float(cur.high) - float(prev.high)
        down = float(prev.low) - float(cur.low)
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    tr = true_range(bars)
    tr_s = wilder_smooth(tr[1:], period)
    plus_s = wilder_smooth(plus_dm[1:], period)
    minus_s = wilder_smooth(minus_dm[1:], period)
    dx: list[float] = []
    first_dx_index: int | None = None
    for i in range(len(tr_s)):
        t, p, m = tr_s[i], plus_s[i], minus_s[i]
        if t is None or p is None or m is None or t == 0:
            continue
        pdi, mdi = 100 * p / t, 100 * m / t
        denom = pdi + mdi
        dx.append(0.0 if denom == 0 else 100 * abs(pdi - mdi) / denom)
        if first_dx_index is None:
            first_dx_index = i + 1
    if first_dx_index is None:
        return out
    smoothed = wilder_smooth(dx, period)
    for i, v in enumerate(smoothed):
        idx = first_dx_index + i
        if v is not None and idx < n:
            out[idx] = v
    return out


def rolling_std(values: Sequence[float], period: int) -> Series:
    _check_period(period)
    out: Series = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        out[i] = math.sqrt(sum((v - mean) ** 2 for v in window) / period)
    return out


def zscore(values: Sequence[float], period: int) -> Series:
    """Price distance from its own mean in sigma units.

    This *is* the Bollinger signal, without the three redundant lines. A
    band-touch is simply |z| >= 2.
    """
    means = sma(values, period)
    stds = rolling_std(values, period)
    out: Series = [None] * len(values)
    for i, (m, s) in enumerate(zip(means, stds, strict=False)):
        if m is None or s is None:
            continue
        # zero dispersion (a flat price or a constant-volume stretch) is a
        # z-score of exactly zero, not an undefined value: dropping the row
        # instead would quietly delete every quiet period from the training set.
        out[i] = 0.0 if s == 0 else (values[i] - m) / s
    return out


def log_returns(values: Sequence[float]) -> Series:
    out: Series = [None]
    for prev, cur in itertools.pairwise(values):
        out.append(math.log(cur / prev) if prev > 0 and cur > 0 else None)
    return out


def realized_vol(values: Sequence[float], period: int = 24,
    bars_per_year: int = 24 * 365) -> Series:
    """Annualized realized volatility from log returns.

    Volatility, not direction, is the forecastable quantity in markets — this
    feeds position sizing, the regime classifier and the ML target scaling.
    """
    rets = log_returns(values)
    out: Series = [None] * len(values)
    for i in range(period, len(values)):
        window = [r for r in rets[i - period + 1 : i + 1] if r is not None]
        if len(window) < period:
            continue
        mean = sum(window) / len(window)
        var = sum((r - mean) ** 2 for r in window) / len(window)
        out[i] = math.sqrt(var) * math.sqrt(bars_per_year)
    return out


def momentum(values: Sequence[float], period: int) -> Series:
    """Simple lookback return — the single most evidenced cross-asset anomaly."""
    _check_period(period)
    out: Series = [None] * len(values)
    for i in range(period, len(values)):
        base = values[i - period]
        if base > 0:
            out[i] = values[i] / base - 1.0
    return out


def donchian(bars: Sequence[Bar], period: int = 20) -> tuple[Series, Series]:
    """Highest high / lowest low of the **prior** ``period`` bars.

    Excludes the current bar on purpose: a channel that includes today can never
    be broken by today, which is the classic off-by-one that turns a breakout
    system into a nonsense system.
    """
    _check_period(period)
    n = len(bars)
    upper: Series = [None] * n
    lower: Series = [None] * n
    hs, ls = highs(bars), lows(bars)
    for i in range(period, n):
        upper[i] = max(hs[i - period : i])
        lower[i] = min(ls[i - period : i])
    return upper, lower


def trend_quality(values: Sequence[float], period: int = 20) -> Series:
    """R² of a rolling linear fit, signed by slope.

    Answers "is this a trend or a drift?" more honestly than a moving-average
    slope, which is happy to look strong in pure noise.
    """
    _check_period(period)
    out: Series = [None] * len(values)
    xs = list(range(period))
    x_mean = sum(xs) / period
    sxx = sum((x - x_mean) ** 2 for x in xs)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        y_mean = sum(window) / period
        sxy = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, window, strict=False))
        syy = sum((y - y_mean) ** 2 for y in window)
        if sxx == 0 or syy == 0:
            out[i] = 0.0
            continue
        r2 = (sxy**2) / (sxx * syy)
        out[i] = r2 if sxy >= 0 else -r2
    return out


def volume_zscore(bars: Sequence[Bar], period: int = 20) -> Series:
    return zscore(volumes(bars), period)


def vwap_session(bars: Sequence[Bar], period: int = 24) -> Series:
    """Rolling volume-weighted average price — an execution benchmark and a
    mean-reversion anchor that respects where the volume actually traded."""
    _check_period(period)
    out: Series = [None] * len(bars)
    for i in range(period - 1, len(bars)):
        window = bars[i - period + 1 : i + 1]
        vol = sum(float(b.volume) for b in window)
        if vol <= 0:
            continue
        typical = sum(
            (float(b.high) + float(b.low) + float(b.close)) / 3 * float(b.volume) for b in window
        )
        out[i] = typical / vol
    return out


def macd(values: Sequence[float], fast: int = 12, slow: int = 26,
    signal: int = 9) -> tuple[Series, Series]:
    """Kept for the benchmark suite only.

    MACD is a smoothed difference of EMAs and is ~90% correlated with a plain
    momentum feature; it earns its place in this codebase as something the real
    strategies must *beat*, not as an input to them.
    """
    fast_e, slow_e = ema(values, fast), ema(values, slow)
    line: Series = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(fast_e, slow_e, strict=False)
    ]
    defined = [(i, v) for i, v in enumerate(line) if v is not None]
    sig: Series = [None] * len(values)
    if defined:
        start = defined[0][0]
        smoothed = ema([v for _, v in defined], signal)
        for offset, v in enumerate(smoothed):
            sig[start + offset] = v
    return line, sig


def _check_period(period: int) -> None:
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
