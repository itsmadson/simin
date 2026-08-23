from datetime import UTC, datetime, timedelta

import pytest
from factories import bar, series

from simin.data.quality import (
    DataQualityError,
    check_bars,
    closed_only,
    dedupe,
    find_gaps,
)
from simin.types import TF


def test_clean_series_has_no_errors():
    report = check_bars(series(200))
    assert report.ok
    assert report.n_bars == 200


def test_gap_is_an_error():
    bars = series(50)
    del bars[20:23]
    report = check_bars(bars)
    kinds = {i.kind for i in report.errors}
    assert "gap" in kinds
    with pytest.raises(DataQualityError, match="gap"):
        report.raise_for_errors()


def test_duplicate_timestamp_is_an_error():
    bars = series(10)
    bars.insert(5, bars[5])
    assert "duplicate" in {i.kind for i in check_bars(bars).errors}


def test_unsorted_series_is_an_error():
    bars = series(10)
    bars[3], bars[7] = bars[7], bars[3]
    assert "unsorted" in {i.kind for i in check_bars(bars).errors}


def test_find_gaps_returns_missing_open_times():
    bars = series(10)
    removed = bars[4].ts
    del bars[4]
    gaps = find_gaps(bars, TF.H1)
    assert gaps == [(removed, removed + timedelta(hours=1))]


def test_dedupe_keeps_last_observation_and_sorts():
    bars = series(5)
    corrected = bar(bars[2].ts, close=999, spread=0.5)
    out = dedupe([*bars[::-1], corrected])  # out of order, correction last
    assert [b.ts for b in out] == sorted(b.ts for b in bars)
    assert out[2].close == corrected.close


def test_closed_only_drops_the_in_progress_bar():
    """The core anti-look-ahead guard: an unclosed bar must never be visible."""
    bars = series(5, tf=TF.H1)
    now = bars[-1].ts + timedelta(minutes=30)   # last bar still forming
    visible = closed_only(bars, now)
    assert len(visible) == 4
    assert visible[-1].ts == bars[-2].ts


def test_price_jump_is_flagged_as_warning_not_error():
    bars = series(100, step=0.0, first=100.0)
    spiked = bar(bars[50].ts, close=100_000, spread=0.5)
    bars[50] = spiked
    report = check_bars(bars)
    assert "price_jump" in {i.kind for i in report.issues}
    assert report.ok  # warnings do not block ingest; they get logged and reviewed


def test_zero_volume_run_is_flagged():
    bars = [
        bar(datetime(2024, 1, 1, tzinfo=UTC) + i * TF.H1.delta, 100,
            volume=0 if 10 <= i < 30 else 5)
        for i in range(40)
    ]
    assert "zero_volume_run" in {i.kind for i in check_bars(bars).issues}


def test_empty_series_is_handled():
    assert check_bars([]).n_bars == 0
