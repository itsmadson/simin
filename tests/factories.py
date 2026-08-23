"""Deterministic bar builders for tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from simin.types import TF, Bar


def bar(
    ts: datetime,
    close: float | Decimal = 100,
    *,
    symbol: str = "BTCUSDT",
    tf: TF = TF.H1,
    volume: float | Decimal = 10,
    spread: float = 1.0,
) -> Bar:
    c = Decimal(str(close))
    hi = c + Decimal(str(spread))
    lo = c - Decimal(str(spread))
    return Bar(
        symbol=symbol,
        tf=tf,
        ts=ts,
        open=c,
        high=hi,
        low=lo,
        close=c,
        volume=Decimal(str(volume)),
    )


def series(
    n: int,
    *,
    tf: TF = TF.H1,
    start: datetime | None = None,
    symbol: str = "BTCUSDT",
    step: float = 1.0,
    first: float = 100.0,
) -> list[Bar]:
    start = start or datetime(2024, 1, 1, tzinfo=UTC)
    return [
        bar(start + i * tf.delta, first + i * step, symbol=symbol, tf=tf, spread=0.5)
        for i in range(n)
    ]


def hours(n: int) -> timedelta:
    return timedelta(hours=n)
