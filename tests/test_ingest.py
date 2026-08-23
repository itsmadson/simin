"""Backfill behaviour: idempotent, resumable, gap-repairing."""

import asyncio
from datetime import UTC, datetime, timedelta

from factories import series

from simin.data.ingest import backfill, coverage, expected_bar_count, stale_by
from simin.exchanges.replay import Clock, ReplayAdapter
from simin.types import TF, Bar


class FakeRepo:
    """In-memory stand-in with the same contract as the Timescale repo."""

    def __init__(self) -> None:
        self.rows: dict[tuple[int, str, datetime], Bar] = {}
        self.quality_logs: list[tuple[int, str, int, list]] = []
        self.insert_calls = 0

    async def insert_bars(self, symbol_id: int, bars) -> int:
        self.insert_calls += 1
        for b in bars:
            self.rows[(symbol_id, b.tf.value, b.ts)] = b
        return len(bars)

    async def last_bar_ts(self, symbol_id: int, tf: TF):
        keys = [k[2] for k in self.rows if k[0] == symbol_id and k[1] == tf.value]
        return max(keys) if keys else None

    async def log_quality(self, symbol_id: int, tf: TF, n_bars: int, issues) -> None:
        self.quality_logs.append((symbol_id, tf.value, n_bars, issues))


def _adapter(bars, tf=TF.H1):
    clock = Clock(now=bars[-1].ts + tf.delta)
    adapter = ReplayAdapter(clock=clock)
    adapter.load("BTCUSDT", tf, bars)
    return adapter


def test_backfill_stores_every_closed_bar():
    bars = series(300)
    repo = FakeRepo()
    result = asyncio.run(
        backfill(
            _adapter(bars), repo, symbol_id=1, symbol="BTCUSDT", tf=TF.H1,
            start=bars[0].ts, end=bars[-1].close_time, page_limit=100,
        )
    )
    assert result.stored == 300
    assert len(repo.rows) == 300
    assert result.report.ok


def test_backfill_is_idempotent():
    """Re-running a completed backfill must not duplicate or corrupt anything."""
    bars = series(120)
    repo = FakeRepo()
    kwargs = dict(symbol_id=1, symbol="BTCUSDT", tf=TF.H1, start=bars[0].ts,
                  end=bars[-1].close_time, page_limit=50)
    asyncio.run(backfill(_adapter(bars), repo, **kwargs))
    first = dict(repo.rows)
    asyncio.run(backfill(_adapter(bars), repo, **kwargs))
    assert repo.rows == first


def test_backfill_resumes_from_the_last_stored_bar():
    bars = series(200)
    repo = FakeRepo()
    asyncio.run(
        backfill(_adapter(bars[:100]), repo, symbol_id=1, symbol="BTCUSDT", tf=TF.H1,
                 start=bars[0].ts, end=bars[99].close_time, page_limit=50)
    )
    before = repo.insert_calls
    result = asyncio.run(
        backfill(_adapter(bars), repo, symbol_id=1, symbol="BTCUSDT", tf=TF.H1,
                 start=bars[0].ts, end=bars[-1].close_time, page_limit=50)
    )
    assert repo.insert_calls > before
    assert len(repo.rows) == 200
    assert result.fetched == 100  # only the missing tail was re-downloaded


def test_backfill_reports_gaps_the_venue_never_serves():
    bars = series(100)
    holed = bars[:40] + bars[45:]
    repo = FakeRepo()
    result = asyncio.run(
        backfill(_adapter(holed), repo, symbol_id=1, symbol="BTCUSDT", tf=TF.H1,
                 start=bars[0].ts, end=bars[-1].close_time, page_limit=100)
    )
    assert result.gaps_found == 1
    assert result.gaps_repaired == 0
    assert not result.report.ok            # unrepaired gap blocks feature computation
    assert repo.quality_logs                # and it is recorded, not swallowed


def test_expected_bar_count_assumes_a_24_7_market():
    start = datetime(2024, 1, 1, tzinfo=UTC)
    assert expected_bar_count(start, start + timedelta(days=7), TF.D1) == 7
    assert expected_bar_count(start, start + timedelta(days=1), TF.H1) == 24
    assert expected_bar_count(start, start, TF.H1) == 0


def test_coverage_measures_completeness():
    bars = series(24, tf=TF.H1)
    start, end = bars[0].ts, bars[0].ts + timedelta(hours=24)
    assert coverage(bars, start, end, TF.H1) == 1.0
    assert coverage(bars[:12], start, end, TF.H1) == 0.5


def test_stale_by_detects_a_dead_feed():
    bars = series(10, tf=TF.H1)
    fresh = bars[-1].close_time
    assert stale_by(bars, fresh, TF.H1) == timedelta(0)
    assert stale_by(bars, fresh + timedelta(hours=3), TF.H1) == timedelta(hours=3)
    assert stale_by([], fresh, TF.H1) == timedelta.max


class RangeRepo(FakeRepo):
    """FakeRepo that also reports its oldest stored bar, like the real repo."""

    async def stored_range(self, symbol_id: int, tf: TF):
        keys = [k[2] for k in self.rows if k[0] == symbol_id and k[1] == tf.value]
        return (min(keys), max(keys)) if keys else (None, None)


def test_backfill_extends_history_backwards_when_asked_for_an_earlier_start():
    """Regression: asking for earlier history than is stored used to store nothing.

    Found by loading real Binance data twice with different --start values: the
    second run reported success and wrote zero rows, leaving the series starting
    years later than requested.
    """
    bars = series(400)
    repo = RangeRepo()
    late = dict(symbol_id=1, symbol="BTCUSDT", tf=TF.H1, page_limit=100)

    # first load covers only the recent half
    asyncio.run(
        backfill(_adapter(bars), repo, start=bars[200].ts, end=bars[-1].close_time, **late)
    )
    assert len(repo.rows) == 200

    # now ask for the full history
    result = asyncio.run(
        backfill(_adapter(bars), repo, start=bars[0].ts, end=bars[-1].close_time, **late)
    )
    assert len(repo.rows) == 400, "earlier history was not filled in"
    assert result.fetched == 200
    stored_ts = sorted(k[2] for k in repo.rows)
    assert stored_ts[0] == bars[0].ts


def test_backfill_fills_both_ends_in_one_pass():
    bars = series(300)
    repo = RangeRepo()
    middle = bars[100:200]
    asyncio.run(repo.insert_bars(1, middle))

    asyncio.run(
        backfill(
            _adapter(bars), repo, symbol_id=1, symbol="BTCUSDT", tf=TF.H1,
            start=bars[0].ts, end=bars[-1].close_time, page_limit=100,
        )
    )
    assert len(repo.rows) == 300


def test_backfill_without_stored_range_support_still_resumes_forward():
    """Older repo objects that only expose last_bar_ts keep working."""
    bars = series(200)
    repo = FakeRepo()
    asyncio.run(
        backfill(
            _adapter(bars[:100]), repo, symbol_id=1, symbol="BTCUSDT", tf=TF.H1,
            start=bars[0].ts, end=bars[99].close_time, page_limit=50,
        )
    )
    asyncio.run(
        backfill(
            _adapter(bars), repo, symbol_id=1, symbol="BTCUSDT", tf=TF.H1,
            start=bars[0].ts, end=bars[-1].close_time, page_limit=50,
        )
    )
    assert len(repo.rows) == 200
