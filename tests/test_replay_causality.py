"""Causality tests — the ones that decide whether any backtest number is real.

Every failure mode here (peeking at an unclosed bar, reading tomorrow's price,
rewinding the clock) produces a beautiful equity curve and a losing account.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factories import series

from simin.exchanges.replay import Clock, ReplayAdapter
from simin.types import TF, Side


def run(coro):
    return asyncio.run(coro)


def make_adapter(n=100, tf=TF.H1):
    bars = series(n, tf=tf)
    clock = Clock(now=bars[0].ts)
    adapter = ReplayAdapter(clock=clock)
    adapter.load("BTCUSDT", tf, bars)
    return adapter, bars


def test_future_bars_are_invisible():
    adapter, bars = make_adapter()
    adapter.clock.advance_to(bars[10].close_time)
    visible = run(adapter.get_ohlcv("BTCUSDT", TF.H1, bars[0].ts))
    assert len(visible) == 11
    assert visible[-1].ts == bars[10].ts
    assert all(b.close_time <= adapter.clock.now for b in visible)


def test_the_bar_currently_forming_is_invisible():
    """A strategy acting mid-bar must not see that bar's close. This is the
    difference between a backtest and a fantasy."""
    adapter, bars = make_adapter()
    adapter.clock.advance_to(bars[10].ts + timedelta(minutes=59))
    visible = run(adapter.get_ohlcv("BTCUSDT", TF.H1, bars[0].ts))
    assert visible[-1].ts == bars[9].ts


def test_a_cheating_strategy_gets_nothing_not_the_future():
    """Canary: code that asks for data past 'now' receives an empty list."""
    adapter, bars = make_adapter()
    adapter.clock.advance_to(bars[10].close_time)
    peek = run(adapter.get_ohlcv("BTCUSDT", TF.H1, since=bars[50].ts))
    assert peek == []


def test_clock_cannot_rewind():
    adapter, bars = make_adapter()
    adapter.clock.advance_to(bars[10].close_time)
    with pytest.raises(ValueError, match="backwards"):
        adapter.clock.advance_to(bars[5].ts)


def test_clock_must_be_utc_aware():
    with pytest.raises(ValueError, match="UTC"):
        Clock(now=datetime(2024, 1, 1))


def test_ticker_reflects_only_visible_data_and_applies_spread():
    adapter, bars = make_adapter()
    adapter.clock.advance_to(bars[10].close_time)
    tick = run(adapter.get_ticker("BTCUSDT"))
    assert tick.last == bars[10].close
    assert tick.bid < tick.last < tick.ask
    assert tick.spread_bps == pytest.approx(float(adapter.spread_bps), rel=1e-6)


def test_ticker_raises_before_any_data_is_visible():
    adapter, bars = make_adapter()
    with pytest.raises(Exception, match="no visible data"):
        run(adapter.get_ticker("BTCUSDT"))


def test_synthetic_book_is_uncrossed_and_sweepable():
    adapter, bars = make_adapter()
    adapter.clock.advance_to(bars[10].close_time)
    book = run(adapter.get_orderbook("BTCUSDT", depth=10))
    assert book.bids[0].price < book.asks[0].price
    filled, avg = book.sweep(Side.BUY, Decimal("0.01"))
    assert filled == Decimal("0.01")
    assert avg >= book.asks[0].price


def test_loading_mismatched_bars_is_rejected():
    adapter, _ = make_adapter()
    with pytest.raises(ValueError, match="does not match"):
        adapter.load("ETHUSDT", TF.H1, series(5, symbol="BTCUSDT"))


def test_symbols_respect_point_in_time_listing():
    adapter, bars = make_adapter()
    adapter.clock.advance_to(bars[0].ts)
    listed = run(adapter.get_symbols())
    assert [s.symbol for s in listed] == ["BTCUSDT"]


def test_duplicate_bars_are_collapsed_on_load():
    tf = TF.H1
    bars = series(20, tf=tf)
    adapter = ReplayAdapter(clock=Clock(now=bars[0].ts))
    adapter.load("BTCUSDT", tf, [*bars, bars[5], bars[5]])
    adapter.clock.advance_to(bars[-1].close_time)
    assert len(run(adapter.get_ohlcv("BTCUSDT", tf, bars[0].ts))) == 20


def test_utc_only_timestamps_everywhere():
    adapter, bars = make_adapter()
    adapter.clock.advance_to(bars[5].close_time)
    for b in run(adapter.get_ohlcv("BTCUSDT", TF.H1, bars[0].ts)):
        assert b.ts.tzinfo is not None and b.ts.utcoffset() == timedelta(0)
    assert adapter.clock.now.tzinfo is UTC
