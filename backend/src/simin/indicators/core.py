"""Indicator math.

Every function takes a list of floats and returns a list of the same length,
with `None` for every position where the indicator has not yet warmed up.

That alignment rule matters more than it looks. The alternative — returning a
shorter list — forces every caller to do index arithmetic, and one off-by-one
there is a strategy that reads tomorrow's RSI. Returning `None` makes the
warm-up explicit and makes "I accidentally used a value that did not exist yet"
a `TypeError` instead of a silent 0.0.

Floats, not Decimal: these are statistics, not money. Decimal here would be
100x slower for no correctness gain.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Series = list[float | None]


def _need(values: Sequence[float], period: int) -> None:
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")


def sma(values: Sequence[float], period: int) -> Series:
    """Simple moving average."""
    _need(values, period)
    out: Series = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def ema(values: Sequence[float], period: int) -> Series:
    """Exponential moving average, seeded with the SMA of the first `period`.

    Seeding with the SMA rather than the first value removes the long startup
    bias that otherwise contaminates the first few hundred bars.
    """
    _need(values, period)
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rma(values: Sequence[float], period: int) -> Series:
    """Wilder's smoothing (used by RSI, ATR, ADX). Alpha is 1/period, not 2/(n+1)."""
    _need(values, period)
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


def stdev(values: Sequence[float], period: int) -> Series:
    """Rolling population standard deviation."""
    _need(values, period)
    out: Series = [None] * len(values)
    s = 0.0
    sq = 0.0
    for i, v in enumerate(values):
        s += v
        sq += v * v
        if i >= period:
            old = values[i - period]
            s -= old
            sq -= old * old
        if i >= period - 1:
            mean = s / period
            var = max(sq / period - mean * mean, 0.0)
            out[i] = math.sqrt(var)
    return out


def rsi(closes: Sequence[float], period: int = 14) -> Series:
    """Relative Strength Index, Wilder's original formulation.

    Returns 0..100. A flat series has no losses, which would divide by zero;
    that case returns 100, matching every charting platform.
    """
    _need(closes, period)
    out: Series = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains = [0.0] * len(closes)
    losses = [0.0] * len(closes)
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains[i] = max(delta, 0.0)
        losses[i] = max(-delta, 0.0)
    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
    return out


def macd(
    closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[Series, Series, Series]:
    """MACD line, signal line, histogram.

    The signal EMA is computed only over the region where the MACD line exists,
    then re-aligned. Feeding zeros into the signal EMA during MACD warm-up —
    which is what naive implementations do — drags the first ~35 signal values
    toward zero and manufactures crossovers that never happened.
    """
    if not fast < slow:
        raise ValueError("MACD needs fast < slow")
    fast_e = ema(closes, fast)
    slow_e = ema(closes, slow)
    line: Series = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_e, slow_e, strict=True)
    ]
    start = next((i for i, v in enumerate(line) if v is not None), len(line))
    dense = [v for v in line[start:] if v is not None]
    sig_dense = ema(dense, signal)
    sig: Series = [None] * len(line)
    for offset, v in enumerate(sig_dense):
        sig[start + offset] = v
    hist: Series = [
        (m - s) if (m is not None and s is not None) else None
        for m, s in zip(line, sig, strict=True)
    ]
    return line, sig, hist


def true_range(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
) -> list[float]:
    tr = [highs[0] - lows[0]] if highs else []
    for i in range(1, len(highs)):
        prev = closes[i - 1]
        tr.append(max(highs[i] - lows[i], abs(highs[i] - prev), abs(lows[i] - prev)))
    return tr


def atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14
) -> Series:
    """Average True Range. The unit every stop in this system is measured in."""
    return rma(true_range(highs, lows, closes), period)


def bollinger(
    closes: Sequence[float], period: int = 20, mult: float = 2.0
) -> tuple[Series, Series, Series, Series]:
    """Middle, upper, lower, and %B position within the band."""
    mid = sma(closes, period)
    sd = stdev(closes, period)
    upper: Series = [None] * len(closes)
    lower: Series = [None] * len(closes)
    pct: Series = [None] * len(closes)
    for i, (m, s) in enumerate(zip(mid, sd, strict=True)):
        if m is None or s is None:
            continue
        u = m + mult * s
        lo = m - mult * s
        upper[i] = u
        lower[i] = lo
        width = u - lo
        pct[i] = (closes[i] - lo) / width if width > 0 else 0.5
    return mid, upper, lower, pct


def bandwidth(upper: Series, lower: Series, mid: Series) -> Series:
    """Bollinger bandwidth — the squeeze detector. Low bandwidth precedes expansion."""
    out: Series = [None] * len(mid)
    for i, (u, lo, m) in enumerate(zip(upper, lower, mid, strict=True)):
        if u is None or lo is None or m is None or m == 0:
            continue
        out[i] = (u - lo) / m
    return out


def stochastic(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    k_period: int = 14,
    d_period: int = 3,
    smooth: int = 3,
) -> tuple[Series, Series]:
    """Slow stochastic %K and %D."""
    raw: Series = [None] * len(closes)
    for i in range(k_period - 1, len(closes)):
        hh = max(highs[i - k_period + 1 : i + 1])
        ll = min(lows[i - k_period + 1 : i + 1])
        rng = hh - ll
        raw[i] = 50.0 if rng == 0 else (closes[i] - ll) / rng * 100.0
    return _smooth_sparse(raw, smooth), _smooth_sparse(_smooth_sparse(raw, smooth), d_period)


def _smooth_sparse(series: Series, period: int) -> Series:
    """SMA over a series that begins with Nones, preserving alignment."""
    start = next((i for i, v in enumerate(series) if v is not None), len(series))
    dense = [v for v in series[start:] if v is not None]
    smoothed = sma(dense, period)
    out: Series = [None] * len(series)
    for offset, v in enumerate(smoothed):
        out[start + offset] = v
    return out


def adx(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14
) -> tuple[Series, Series, Series]:
    """ADX, +DI, -DI. ADX above ~25 means trending; below ~20 means chop.

    This is the switch that decides whether the oscillation strategies or the
    trend strategies are allowed to fire.
    """
    n = len(closes)
    if n < 2:
        return [None] * n, [None] * n, [None] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
    tr = true_range(highs, lows, closes)
    atr_s = rma(tr, period)
    pdm_s = rma(plus_dm, period)
    mdm_s = rma(minus_dm, period)
    pdi: Series = [None] * n
    mdi: Series = [None] * n
    dx: Series = [None] * n
    for i in range(n):
        a, p, m = atr_s[i], pdm_s[i], mdm_s[i]
        if a is None or p is None or m is None or a == 0:
            continue
        pv = 100.0 * p / a
        mv = 100.0 * m / a
        pdi[i] = pv
        mdi[i] = mv
        denom = pv + mv
        dx[i] = 0.0 if denom == 0 else 100.0 * abs(pv - mv) / denom
    return _smooth_sparse(dx, period), pdi, mdi


def supertrend(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 10,
    mult: float = 3.0,
) -> tuple[Series, list[int | None]]:
    """Supertrend line and direction (+1 up, -1 down).

    The band-ratcheting rule (a band may only tighten while trend is unchanged)
    is what stops it flip-flopping on every wick.
    """
    n = len(closes)
    a = atr(highs, lows, closes, period)
    line: Series = [None] * n
    dirs: list[int | None] = [None] * n
    prev_upper = prev_lower = None
    prev_dir = 1
    for i in range(n):
        if a[i] is None:
            continue
        hl2 = (highs[i] + lows[i]) / 2
        upper = hl2 + mult * a[i]
        lower = hl2 - mult * a[i]
        if prev_upper is not None:
            upper = min(upper, prev_upper) if closes[i - 1] <= prev_upper else upper
            lower = max(lower, prev_lower) if closes[i - 1] >= prev_lower else lower
            if closes[i] > prev_upper:
                prev_dir = 1
            elif closes[i] < prev_lower:
                prev_dir = -1
        dirs[i] = prev_dir
        line[i] = lower if prev_dir == 1 else upper
        prev_upper, prev_lower = upper, lower
    return line, dirs


def vwap_session(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    session_starts: Sequence[bool],
) -> Series:
    """Volume-weighted average price, reset at each session boundary.

    A VWAP that never resets is just a slow cumulative average and carries no
    information after a few weeks; the reset is the whole point.
    """
    out: Series = [None] * len(closes)
    pv = 0.0
    vol = 0.0
    for i in range(len(closes)):
        if session_starts[i]:
            pv = vol = 0.0
        typical = (highs[i] + lows[i] + closes[i]) / 3
        pv += typical * volumes[i]
        vol += volumes[i]
        out[i] = pv / vol if vol > 0 else closes[i]
    return out


def obv(closes: Sequence[float], volumes: Sequence[float]) -> list[float]:
    """On-balance volume. Divergence against price is the useful signal."""
    out = [0.0] * len(closes)
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            out[i] = out[i - 1] + volumes[i]
        elif closes[i] < closes[i - 1]:
            out[i] = out[i - 1] - volumes[i]
        else:
            out[i] = out[i - 1]
    return out


def roc(values: Sequence[float], period: int = 10) -> Series:
    """Rate of change, as a fraction."""
    out: Series = [None] * len(values)
    for i in range(period, len(values)):
        base = values[i - period]
        if base != 0:
            out[i] = (values[i] - base) / base
    return out


def zscore(values: Sequence[float], period: int = 50) -> Series:
    """How many standard deviations from the rolling mean. The oscillation
    strategies' core measurement of 'stretched'."""
    m = sma(values, period)
    s = stdev(values, period)
    out: Series = [None] * len(values)
    for i, (mm, ss) in enumerate(zip(m, s, strict=True)):
        if mm is None or ss is None or ss == 0:
            continue
        out[i] = (values[i] - mm) / ss
    return out


def keltner(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 20,
    mult: float = 1.5,
) -> tuple[Series, Series, Series]:
    mid = ema(closes, period)
    a = atr(highs, lows, closes, period)
    up: Series = [None] * len(closes)
    dn: Series = [None] * len(closes)
    for i, (m, av) in enumerate(zip(mid, a, strict=True)):
        if m is None or av is None:
            continue
        up[i] = m + mult * av
        dn[i] = m - mult * av
    return mid, up, dn
