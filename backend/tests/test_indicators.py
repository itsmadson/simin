"""Indicator maths, checked against published reference values where they exist.

Two properties are asserted everywhere and matter more than any single number:

* **Length alignment.** Every series is exactly as long as its input. A shorter
  series forces index arithmetic on every caller, and one off-by-one there is a
  strategy reading tomorrow's RSI.
* **Warm-up is None, never zero.** RSI 0 reads as maximally oversold. A
  warm-up filled with zeros makes a bot buy the first bar of every dataset.
"""

from __future__ import annotations

import math

import pytest

from simin.indicators import core as ind
from tests.conftest import make_candles

# Wilder's original worked example from "New Concepts in Technical Trading
# Systems" (1978). If this drifts, the smoothing is wrong.
WILDER = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
    45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
]


class TestRSI:
    def test_matches_wilder_reference(self) -> None:
        out = ind.rsi(WILDER, 14)
        assert out[14] == pytest.approx(70.46, abs=0.02)
        assert out[15] == pytest.approx(66.25, abs=0.02)

    def test_warmup_is_none_not_zero(self) -> None:
        out = ind.rsi(WILDER, 14)
        assert all(v is None for v in out[:14])
        assert out[14] is not None

    def test_flat_series_does_not_divide_by_zero(self) -> None:
        assert ind.rsi([10.0] * 30, 14)[-1] == 100.0

    def test_bounded(self) -> None:
        out = ind.rsi([float(c.close) for c in make_candles(400)], 14)
        assert all(0.0 <= v <= 100.0 for v in out if v is not None)

    def test_too_short_returns_all_none(self) -> None:
        assert ind.rsi([1.0, 2.0, 3.0], 14) == [None, None, None]


class TestMovingAverages:
    def test_sma(self) -> None:
        assert ind.sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]

    def test_ema_seeds_from_sma(self) -> None:
        # Seeding from the SMA rather than the first value removes a startup
        # bias that otherwise contaminates hundreds of bars.
        out = ind.ema([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 5)
        assert out[4] == pytest.approx(3.0)
        assert out[3] is None

    def test_rma_uses_wilder_alpha(self) -> None:
        # Wilder's alpha is 1/n, not 2/(n+1). Confusing them makes ATR and RSI
        # subtly too fast, and every ATR-derived stop too tight.
        values = [float(i) for i in range(1, 21)]
        rma = ind.rma(values, 10)
        ema = ind.ema(values, 10)
        assert rma[-1] is not None and ema[-1] is not None
        assert rma[-1] < ema[-1]


class TestMACD:
    def test_signal_never_precedes_macd(self) -> None:
        """The classic bug: feeding zeros into the signal EMA during MACD
        warm-up drags the first ~35 values toward zero and manufactures
        crossovers that never happened."""
        closes = [float(c.close) for c in make_candles(300)]
        line, signal, hist = ind.macd(closes)
        first_line = next(i for i, v in enumerate(line) if v is not None)
        first_signal = next(i for i, v in enumerate(signal) if v is not None)
        assert first_signal > first_line
        assert all(signal[i] is None for i in range(first_line))

    def test_histogram_is_line_minus_signal(self) -> None:
        closes = [float(c.close) for c in make_candles(300)]
        line, signal, hist = ind.macd(closes)
        for a, b, c in zip(line, signal, hist, strict=True):
            if a is None or b is None:
                assert c is None
            else:
                assert c == pytest.approx(a - b)

    def test_rejects_fast_above_slow(self) -> None:
        with pytest.raises(ValueError, match="fast < slow"):
            ind.macd([1.0] * 100, fast=26, slow=12)


class TestVolatility:
    def test_atr_is_positive(self) -> None:
        cs = make_candles(300)
        out = ind.atr(
            [float(c.high) for c in cs],
            [float(c.low) for c in cs],
            [float(c.close) for c in cs],
            14,
        )
        assert all(v > 0 for v in out if v is not None)

    def test_bollinger_percent_b(self) -> None:
        closes = [float(c.close) for c in make_candles(300)]
        mid, up, dn, pct = ind.bollinger(closes, 20, 2.0)
        for i, p in enumerate(pct):
            if p is None:
                continue
            assert up[i] is not None and dn[i] is not None
            assert up[i] > mid[i] > dn[i]
            # %B is allowed outside 0..1 — that is precisely the signal that
            # price closed outside the band.
            assert -2 < p < 3

    def test_bollinger_on_flat_series_centres(self) -> None:
        _, _, _, pct = ind.bollinger([50.0] * 60, 20, 2.0)
        assert pct[-1] == 0.5


class TestRegime:
    def test_adx_bounded(self) -> None:
        cs = make_candles(400)
        adx, pdi, mdi = ind.adx(
            [float(c.high) for c in cs],
            [float(c.low) for c in cs],
            [float(c.close) for c in cs],
        )
        for series in (adx, pdi, mdi):
            assert all(0.0 <= v <= 100.0 for v in series if v is not None)

    def test_adx_higher_in_a_trend_than_in_chop(self) -> None:
        trend = make_candles(500, seed=1, drift=0.004, vol=0.004)
        chop = make_candles(500, seed=1, drift=0.0, vol=0.004)

        def mean_adx(cs):
            out, _, _ = ind.adx(
                [float(c.high) for c in cs],
                [float(c.low) for c in cs],
                [float(c.close) for c in cs],
            )
            vals = [v for v in out[200:] if v is not None]
            return sum(vals) / len(vals)

        assert mean_adx(trend) > mean_adx(chop)

    def test_supertrend_direction_is_plus_or_minus_one(self) -> None:
        cs = make_candles(300)
        _, dirs = ind.supertrend(
            [float(c.high) for c in cs],
            [float(c.low) for c in cs],
            [float(c.close) for c in cs],
        )
        assert set(d for d in dirs if d is not None) <= {-1, 1}


class TestAlignment:
    def test_every_series_matches_input_length(self) -> None:
        cs = make_candles(300)
        h = [float(c.high) for c in cs]
        low = [float(c.low) for c in cs]
        c = [float(c.close) for c in cs]
        v = [float(x.volume) for x in cs]
        n = len(cs)

        series = [
            ind.sma(c, 20), ind.ema(c, 20), ind.rma(c, 20), ind.stdev(c, 20),
            ind.rsi(c), ind.atr(h, low, c), ind.roc(c), ind.zscore(c),
            *ind.macd(c), *ind.bollinger(c), *ind.stochastic(h, low, c),
            *ind.adx(h, low, c), *ind.keltner(h, low, c),
            ind.obv(c, v), ind.supertrend(h, low, c)[0],
        ]
        for s in series:
            assert len(s) == n

    def test_all_values_finite(self) -> None:
        cs = make_candles(300)
        c = [float(x.close) for x in cs]
        for s in (ind.rsi(c), ind.zscore(c), ind.roc(c)):
            assert all(math.isfinite(v) for v in s if v is not None)
