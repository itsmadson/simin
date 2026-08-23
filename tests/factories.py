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


def gbm_series(
    n: int,
    *,
    seed: int = 0,
    tf: TF = TF.H1,
    start: datetime | None = None,
    symbol: str = "BTCUSDT",
    first: float = 100.0,
    sigma: float = 0.01,
    mu: float = 0.0,
    volume: float = 10_000.0,
) -> list[Bar]:
    """Geometric random walk with a realistic intrabar range.

    A random walk is the right null hypothesis: a strategy that makes money on
    it, after costs, is finding a bug rather than an edge.
    """
    import math
    import random

    rng = random.Random(seed)
    start = start or datetime(2024, 1, 1, tzinfo=UTC)
    price = first
    out: list[Bar] = []
    for i in range(n):
        shock = rng.gauss(mu, sigma)
        close = price * math.exp(shock)
        high = max(price, close) * (1 + abs(rng.gauss(0, sigma / 2)))
        low = min(price, close) * (1 - abs(rng.gauss(0, sigma / 2)))
        out.append(
            Bar(
                symbol=symbol,
                tf=tf,
                ts=start + i * tf.delta,
                open=Decimal(str(round(price, 6))),
                high=Decimal(str(round(high, 6))),
                low=Decimal(str(round(low, 6))),
                close=Decimal(str(round(close, 6))),
                volume=Decimal(str(round(volume * (0.5 + rng.random()), 4))),
            )
        )
        price = close
    return out
