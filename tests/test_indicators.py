"""Indicators: known answers, warm-up honesty, and causality."""

import math

import pytest
from factories import bar, series

from simin.features import indicators as ind
from simin.types import TF


def test_sma_known_answer():
    out = ind.sma([1, 2, 3, 4, 5], 3)
    assert out[:2] == [None, None]
    assert out[2:] == [2.0, 3.0, 4.0]


def test_ema_is_seeded_with_the_first_sma():
    values = [1.0] * 10
    out = ind.ema(values, 5)
    assert out[3] is None
    assert out[4] == pytest.approx(1.0)


def test_warmup_is_none_never_backfilled():
    """A back-filled warm-up value is a peek at the future with a friendly face."""
    for fn in (lambda v: ind.sma(v, 10), lambda v: ind.ema(v, 10), lambda v: ind.rsi(v, 10)):
        out = fn([float(i) for i in range(30)])
        assert out[0] is None
        assert all(v is None for v in out[:5])


def test_every_indicator_returns_input_length():
    bars = series(120)
    c = ind.closes(bars)
    for out in (
        ind.sma(c, 20), ind.ema(c, 20), ind.rsi(c, 14), ind.atr(bars, 14), ind.adx(bars, 14),
        ind.zscore(c, 20), ind.realized_vol(c, 24), ind.momentum(c, 10),
        ind.trend_quality(c, 20), ind.vwap_session(bars, 24), ind.volume_zscore(bars, 20),
    ):
        assert len(out) == len(bars)


def test_indicators_are_causal_appending_a_bar_never_changes_history():
    """The property that makes a backtest meaningful: past values are frozen."""
    bars = series(200, step=0.4)
    c = ind.closes(bars)
    before = [ind.rsi(c, 14), ind.atr(bars, 14), ind.adx(bars, 14), ind.zscore(c, 20)]
    extra = bar(bars[-1].ts + TF.H1.delta, close=999, spread=0.5)
    bars2 = [*bars, extra]
    c2 = ind.closes(bars2)
    after = [ind.rsi(c2, 14), ind.atr(bars2, 14), ind.adx(bars2, 14), ind.zscore(c2, 20)]
    for old, new in zip(before, after, strict=False):
        assert old == new[: len(old)]


def test_rsi_saturates_on_a_monotone_series():
    assert ind.rsi([float(i) for i in range(1, 60)], 14)[-1] == 100.0
    assert ind.rsi([float(i) for i in range(60, 1, -1)], 14)[-1] == 0.0


def test_true_range_accounts_for_gaps():
    bars = [bar(series(2)[0].ts, 100, spread=1)]
    gapped = bar(bars[0].ts + TF.H1.delta, 150, spread=1)
    tr = ind.true_range([bars[0], gapped])
    assert tr[1] == pytest.approx(51.0)  # 151 - 100, not the 2.0 intrabar range


def test_atr_of_constant_range_equals_that_range():
    bars = series(60, step=0.0)
    assert ind.atr(bars, 14)[-1] == pytest.approx(1.0)


def test_donchian_excludes_the_current_bar():
    """A channel including today can never be broken by today."""
    bars = series(30, step=1.0)
    upper, _ = ind.donchian(bars, 20)
    assert upper[25] == pytest.approx(float(bars[24].high))
    assert upper[25] < float(bars[25].high)


def test_trend_quality_signs_by_direction():
    up = [float(i) for i in range(50)]
    down = list(reversed(up))
    assert ind.trend_quality(up, 20)[-1] == pytest.approx(1.0)
    assert ind.trend_quality(down, 20)[-1] == pytest.approx(-1.0)


def test_trend_quality_is_low_in_chop():
    chop = [100 + (5 if i % 2 else -5) for i in range(60)]
    assert abs(ind.trend_quality(chop, 20)[-1]) < 0.2


def test_zscore_of_a_flat_series_is_zero_not_none():
    assert ind.zscore([100.0] * 40, 20)[-1] == 0.0


def test_realized_vol_scales_with_noise():
    calm = [100 + math.sin(i) * 0.1 for i in range(200)]
    wild = [100 + math.sin(i) * 5.0 for i in range(200)]
    assert ind.realized_vol(wild, 24)[-1] > ind.realized_vol(calm, 24)[-1] * 5


def test_momentum_known_answer():
    assert ind.momentum([100.0, 105.0, 110.0], 2)[2] == pytest.approx(0.10)


def test_zero_or_negative_period_is_rejected():
    with pytest.raises(ValueError, match="period"):
        ind.sma([1.0, 2.0], 0)


def test_short_series_returns_all_none_rather_than_crashing():
    assert ind.ema([1.0, 2.0], 50) == [None, None]
    assert all(v is None for v in ind.adx(series(5), 14))
