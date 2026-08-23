from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factories import bar, series

from simin.types import TF, Bar, Level, OrderBook, OrderRequest, OrderType, Side, SymbolInfo, Ticker


def test_tf_floor_aligns_to_bar_open():
    ts = datetime(2024, 5, 3, 14, 37, 12, tzinfo=UTC)
    assert TF.H4.floor(ts) == datetime(2024, 5, 3, 12, tzinfo=UTC)
    assert TF.D1.floor(ts) == datetime(2024, 5, 3, tzinfo=UTC)
    assert TF.M15.floor(ts) == datetime(2024, 5, 3, 14, 30, tzinfo=UTC)


def test_bar_rejects_naive_timestamp():
    with pytest.raises(ValueError, match="UTC"):
        bar(datetime(2024, 1, 1))


def test_bar_rejects_misaligned_timestamp():
    with pytest.raises(ValueError, match="not aligned"):
        bar(datetime(2024, 1, 1, 0, 30, tzinfo=UTC), tf=TF.H1)


def test_bar_rejects_impossible_ohlc():
    with pytest.raises(ValueError, match="outside"):
        Bar(
            symbol="X", tf=TF.H1, ts=datetime(2024, 1, 1, tzinfo=UTC),
            open=Decimal(200), high=Decimal(110), low=Decimal(90),
            close=Decimal(100), volume=Decimal(1),
        )


def test_close_time_is_the_moment_a_bar_becomes_actionable():
    b = bar(datetime(2024, 1, 1, tzinfo=UTC), tf=TF.H4)
    assert b.close_time == datetime(2024, 1, 1, 4, tzinfo=UTC)


def test_ticker_spread_bps():
    t = Ticker("BTCUSDT", datetime(2024, 1, 1, tzinfo=UTC),
               bid=Decimal("99.5"), ask=Decimal("100.5"), last=Decimal(100))
    assert t.mid == Decimal(100)
    assert t.spread_bps == Decimal(100)


def test_orderbook_rejects_crossed_book():
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="crossed"):
        OrderBook("X", ts,
                  bids=(Level(Decimal(101), Decimal(1)),),
                  asks=(Level(Decimal(100), Decimal(1)),))


def test_sweep_is_partial_when_book_is_thin():
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    book = OrderBook(
        "X", ts,
        bids=(Level(Decimal(99), Decimal(1)),),
        asks=(Level(Decimal(100), Decimal(1)), Level(Decimal(101), Decimal(1))),
    )
    filled, avg = book.sweep(Side.BUY, Decimal(5))
    assert filled == Decimal(2)                  # not 5: the book ran out
    assert avg == Decimal("100.5")               # and the average price walked up


def test_order_request_requires_idempotency_key():
    with pytest.raises(ValueError, match="client_order_id"):
        OrderRequest("X", Side.BUY, OrderType.MARKET, Decimal(1))


def test_point_in_time_listing_blocks_survivorship_bias():
    info = SymbolInfo(
        venue="v", symbol="DEADUSDT", base="DEAD", quote="USDT",
        price_tick=Decimal("0.01"), qty_step=Decimal("0.01"), min_notional=Decimal(10),
        listed_at=datetime(2022, 1, 1, tzinfo=UTC),
        delisted_at=datetime(2023, 1, 1, tzinfo=UTC),
    )
    assert not info.is_tradeable_at(datetime(2021, 6, 1, tzinfo=UTC))
    assert info.is_tradeable_at(datetime(2022, 6, 1, tzinfo=UTC))
    assert not info.is_tradeable_at(datetime(2023, 6, 1, tzinfo=UTC))


def test_series_factory_is_contiguous():
    bars = series(10)
    assert bars[-1].ts - bars[0].ts == timedelta(hours=9)
