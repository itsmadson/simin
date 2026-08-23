"""Feature assembly, the multi-timeframe leak canary, and regime behaviour."""

from datetime import UTC, datetime, timedelta

import pytest
from factories import bar, series

from simin.features.engine import BARS_PER_YEAR, asof_join, build_features, feature_matrix
from simin.features.regime import Regime, RegimeConfig, classify, classify_series, percentile_rank
from simin.types import TF


def test_features_are_produced_for_every_bar():
    bars = series(300, step=0.3)
    rows = build_features(bars)
    assert len(rows) == len(bars)
    assert rows[-1].get("atr_pct") is not None
    assert rows[0].get("atr_pct") is None       # warm-up stays empty


def test_derived_features_are_unit_free():
    """Raw levels teach a model the price; ratios teach it the behaviour."""
    cheap = series(300, first=10.0, step=0.03)
    dear = series(300, first=10_000.0, step=30.0)
    a = build_features(cheap)[-1]
    b = build_features(dear)[-1]
    assert a.get("ema_spread") == pytest.approx(b.get("ema_spread"), rel=0.05)


def test_asof_join_never_reveals_an_unclosed_higher_timeframe_bar():
    """THE canary. A 1h row inside today's 1d bar must not see today's values."""
    h1 = series(72, tf=TF.H1, start=datetime(2024, 1, 1, tzinfo=UTC), step=0.5)
    d1 = series(3, tf=TF.D1, start=datetime(2024, 1, 1, tzinfo=UTC), step=100.0)
    base = build_features(h1, TF.H1)
    higher = build_features(d1, TF.D1)
    joined = asof_join(base, higher, TF.D1)

    by_ts = {r.ts: r for r in joined}
    # 23:00 on day 1: the day-1 daily bar has NOT closed yet
    assert by_ts[datetime(2024, 1, 1, 23, tzinfo=UTC)].get("1d_close") is None
    # 00:00 on day 2: day-1 daily bar has just closed and is now legitimate
    assert by_ts[datetime(2024, 1, 2, 0, tzinfo=UTC)].get("1d_close") == pytest.approx(
        float(d1[0].close)
    )
    # and mid-day-2 still sees only day 1, never day 2
    assert by_ts[datetime(2024, 1, 2, 12, tzinfo=UTC)].get("1d_close") == pytest.approx(
        float(d1[0].close)
    )


def test_asof_join_is_stable_when_higher_tf_extends():
    """Appending future higher-TF bars must not alter already-joined history."""
    h1 = series(48, tf=TF.H1, start=datetime(2024, 1, 1, tzinfo=UTC))
    d1 = series(2, tf=TF.D1, start=datetime(2024, 1, 1, tzinfo=UTC))
    base = build_features(h1, TF.H1)
    first = asof_join(base, build_features(d1, TF.D1), TF.D1)
    extended = series(5, tf=TF.D1, start=datetime(2024, 1, 1, tzinfo=UTC))
    second = asof_join(base, build_features(extended, TF.D1), TF.D1)
    assert [r.values for r in first] == [r.values for r in second]


def test_asof_join_with_no_higher_data_yields_nulls_not_a_crash():
    base = build_features(series(10, tf=TF.H1))
    joined = asof_join(base, [], TF.D1)
    assert [r.values for r in joined] == [r.values for r in base]


def test_feature_matrix_drops_incomplete_rows_instead_of_imputing():
    rows = build_features(series(300, step=0.3))
    names = ["atr_pct", "rsi14", "adx14", "ema_spread"]
    stamps, matrix = feature_matrix(rows, names)
    assert len(stamps) == len(matrix)
    assert all(len(r) == len(names) for r in matrix)
    assert len(matrix) < len(rows)          # warm-up excluded, not filled in


def test_bars_per_year_is_defined_for_every_timeframe():
    assert set(BARS_PER_YEAR) == set(TF)


def test_percentile_rank():
    assert percentile_rank([1, 2, 3, 4], 2.5) == 0.5
    assert percentile_rank([], 1.0) == 0.5


def test_uptrend_classifies_as_bull():
    rows = build_features(series(400, step=0.5))
    state = classify(rows, len(rows) - 1)
    assert state.regime in (Regime.STRONG_BULL, Regime.WEAK_BULL, Regime.BREAKOUT)
    assert state.allows_new_risk


def test_downtrend_classifies_as_bear():
    rows = build_features(series(400, first=400.0, step=-0.5))
    state = classify(rows, len(rows) - 1)
    assert state.regime in (Regime.STRONG_BEAR, Regime.WEAK_BEAR)


def test_warmup_is_unknown_and_forbids_risk():
    rows = build_features(series(400, step=0.5))
    state = classify(rows, 5)
    assert state.regime is Regime.UNKNOWN
    assert not state.allows_new_risk


def test_crash_is_classified_as_panic_and_blocks_all_strategies():
    calm = series(300, step=0.0, first=100.0)
    crash = [
        bar(calm[-1].ts + (i + 1) * TF.H1.delta, 100 * (0.96**(i + 1)), spread=0.5)
        for i in range(8)
    ]
    rows = build_features([*calm, *crash])
    state = classify(rows, len(rows) - 1, RegimeConfig(panic_vol_pct=0.5))
    assert state.regime is Regime.PANIC
    assert not state.allows_new_risk
    assert not state.allows("trend_follow")


def test_regime_is_causal():
    """Classifying bar i must not depend on bars after i."""
    bars = series(400, step=0.4)
    rows = build_features(bars)
    truncated = build_features(bars[:300])
    assert classify(rows, 299).regime is classify(truncated, 299).regime


def test_classify_series_covers_every_row():
    rows = build_features(series(300, step=0.2))
    states = classify_series(rows)
    assert len(states) == len(rows)


def test_playbook_permissions_are_strict():
    rows = build_features(series(400, step=0.5))
    state = classify(rows, len(rows) - 1)
    assert not state.allows("definitely_not_a_strategy")


def test_hours_helper_sanity():
    assert TF.H4.delta == timedelta(hours=4)
