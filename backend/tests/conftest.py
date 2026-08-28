"""Shared fixtures.

Every test that touches settings gets a clean environment. `simin.config`
caches its `Settings` for the process, which is right in production and lethal
in a test suite — one test setting `SIMIN_MODE=real` would silently change the
mode for every test that ran after it.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import random
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from simin.config import reset_settings_cache
from simin.core.types import TF, Candle, MarketKind, Symbol
from simin.exchanges.costs import CostModel
from simin.indicators.features import FeatureFrame


def pytest_collection_modifyitems(items) -> None:
    """Mark async tests so the runner below picks them up."""
    for item in items:
        if inspect.iscoroutinefunction(getattr(item, "function", None)):
            item.add_marker("asyncio")


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Run `async def` tests without requiring pytest-asyncio.

    The plugin is a dev dependency and is present in CI and in the image, but
    the suite should not be un-runnable in an environment that happens not to
    have it — these tests are the safety net for a system that moves money, and
    "I couldn't run them" is not an acceptable state.
    """
    func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(func):
        return None
    kwargs = {
        name: pyfuncitem.funcargs[name]
        for name in pyfuncitem._fixtureinfo.argnames
    }
    asyncio.run(func(**kwargs))
    return True


@pytest.fixture(autouse=True)
def clean_env(tmp_path) -> Iterator[None]:
    saved = {k: v for k, v in os.environ.items() if k.startswith("SIMIN_")}
    for key in saved:
        del os.environ[key]
    os.environ["SIMIN_DATA_DIR"] = str(tmp_path / "data")
    reset_settings_cache()
    yield
    for key in [k for k in os.environ if k.startswith("SIMIN_")]:
        del os.environ[key]
    os.environ.update(saved)
    reset_settings_cache()


def make_candles(
    n: int = 800,
    seed: int = 7,
    drift: float = 0.0004,
    vol: float = 0.010,
    start: float = 100.0,
    tf: TF = TF.H2,
) -> list[Candle]:
    """A deterministic OHLC series. Same seed, same bars, every run."""
    rng = random.Random(seed)
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    out: list[Candle] = []
    price = start
    for i in range(n):
        o = price
        price = max(0.5, price * (1 + rng.gauss(drift, vol)))
        hi = max(o, price) * (1 + abs(rng.gauss(0, vol * 0.5)))
        lo = min(o, price) * (1 - abs(rng.gauss(0, vol * 0.5)))
        out.append(
            Candle(
                ts=t0 + timedelta(seconds=tf.seconds * i),
                open=Decimal(str(round(o, 4))),
                high=Decimal(str(round(hi, 4))),
                low=Decimal(str(round(lo, 4))),
                close=Decimal(str(round(price, 4))),
                volume=Decimal(str(round(rng.uniform(50, 500), 2))),
            )
        )
    return out


@pytest.fixture
def candles() -> list[Candle]:
    return make_candles()


@pytest.fixture
def frame(candles: list[Candle]) -> FeatureFrame:
    return FeatureFrame("BTCUSDT", TF.H2, candles)


@pytest.fixture
def symbol() -> Symbol:
    return Symbol(
        base="BTC",
        quote="USDT",
        venue="test",
        venue_symbol="BTCUSDT",
        kind=MarketKind.FUTURES,
        price_precision=2,
        qty_precision=6,
        min_qty=Decimal("0.0001"),
        min_notional=Decimal("5"),
        max_leverage=10,
    )


@pytest.fixture
def costs() -> CostModel:
    return CostModel()
