"""A data source that serves candles from memory or disk.

Two jobs:

1. **Deterministic tests.** The runner and the backtester can be driven over a
   known series with no network and no clock dependence.

2. **A working demo without credentials.** `docker compose up` on a machine with
   no API keys and no reachable exchange still produces a bot you can watch
   trade, because the alternative — an empty dashboard and a connection error —
   teaches nothing about whether the system works.

The generated series is explicitly labelled synthetic everywhere it surfaces.
Nobody should ever mistake a backtest on generated data for evidence about a
real market, so `is_synthetic` is on the class and the API passes it through.
"""

from __future__ import annotations

import random
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from simin.core.types import (
    TF,
    Candle,
    MarketKind,
    Order,
    OrderStatus,
    OrderType,
    Side,
    Symbol,
)
from simin.exchanges.base import Balance, Exchange, ExchangeError, Fees, Ticker


class ReplayExchange(Exchange):
    """Read-only market data from a fixed candle series."""

    name = "replay"
    display_name = "Replay (offline data)"
    can_trade = False
    kinds = (MarketKind.SPOT, MarketKind.FUTURES)
    #: Surfaced by the API so the UI can label every result built on this.
    is_synthetic = False

    def __init__(
        self,
        series: dict[str, list[Candle]],
        tf: TF = TF.H2,
        symbols: Sequence[Symbol] | None = None,
        seconds_per_bar: float = 0.0,
    ) -> None:
        if not series:
            raise ValueError("ReplayExchange needs at least one series")
        self._series = {k: list(v) for k, v in series.items()}
        self._tf = tf
        self._symbols = list(symbols or [
            Symbol(
                base=name[:-4] or name, quote=name[-4:], venue=self.name,
                venue_symbol=name, kind=MarketKind.FUTURES,
                price_precision=2, qty_precision=6,
                min_qty=Decimal("0.0001"), min_notional=Decimal("5"), max_leverage=10,
            )
            for name in self._series
        ])
        #: How far through the series the clock has advanced. Empty means
        #: "serve everything", which is what the backtester wants.
        self._cursor: dict[str, int] = {}
        #: Wall-clock seconds per simulated bar. Zero means the series is
        #: static — correct for backtests, useless for a live demo, because a
        #: runner polling a series that never grows sees no new bar and
        #: correctly refuses to do anything at all. Setting this makes the
        #: offline venue emit bars on a compressed clock so the bot visibly
        #: trades, at the cost of being obviously not real time.
        self._seconds_per_bar = seconds_per_bar
        self._started = time.monotonic()
        self._origin = 0

    def advance(self, steps: int = 1) -> None:
        """Move the replay clock forward. Used to drive the runner in tests."""
        for name, candles in self._series.items():
            current = self._cursor.get(name, len(candles))
            self._cursor[name] = min(current + steps, len(candles))

    def seek(self, index: int) -> None:
        for name in self._series:
            self._cursor[name] = min(max(index, 0), len(self._series[name]))
        self._origin = index
        self._started = time.monotonic()

    def _cut(self, name: str) -> int:
        """How many candles are visible right now."""
        series = self._series[name]
        if self._seconds_per_bar > 0:
            elapsed = time.monotonic() - self._started
            grown = self._origin + int(elapsed / self._seconds_per_bar)
            return min(max(grown, 210), len(series))
        return self._cursor.get(name, len(series))

    async def symbols(self) -> Sequence[Symbol]:
        return self._symbols

    async def candles(
        self, symbol: str, tf: TF, limit: int = 500, end: datetime | None = None
    ) -> list[Candle]:
        series = self._series.get(symbol)
        if series is None:
            raise ExchangeError(f"replay has no series for {symbol!r}")
        window = series[: self._cut(symbol)]
        if end is not None:
            window = [c for c in window if c.ts <= end]
        return window[-limit:]

    async def ticker(self, symbol: str) -> Ticker:
        rows = await self.candles(symbol, self._tf, limit=1)
        if not rows:
            raise ExchangeError(f"replay has no data yet for {symbol}")
        last = rows[-1].close
        spread = last * Decimal("0.0002")
        return Ticker(symbol, last - spread, last + spread, last, rows[-1].ts)

    async def fees(self, symbol: str) -> Fees:
        return Fees(Decimal("0.0003"), Decimal("0.0005"), Decimal("0.0001"))

    async def balances(self) -> dict[str, Balance]:
        return {}

    async def place_order(self, *args: object, **kwargs: object) -> Order:
        raise ExchangeError(
            "ReplayExchange is a data source and cannot place orders. Wrap it in a "
            "PaperExchange to simulate an account over it."
        )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        return False

    async def get_order(self, symbol: str, order_id: str) -> Order:
        raise ExchangeError("ReplayExchange holds no orders")


def synthetic_series(
    symbol: str,
    bars: int = 4000,
    tf: TF = TF.H2,
    start_price: float = 30000.0,
    seed: int = 0,
    end: datetime | None = None,
) -> list[Candle]:
    """Generate a plausible crypto series: regime-switching drift with fat tails.

    Deliberately *not* a clean uptrend. A generator that only produces bull
    markets makes every trend strategy look brilliant, which is worse than
    useless — it is misleading. This alternates trending and ranging regimes and
    injects occasional gaps, so a strategy that only works in one condition is
    visibly exposed.
    """
    rng = random.Random(seed if seed else hash(symbol) & 0xFFFF)
    finish = end or datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start_ts = tf.floor(finish - timedelta(seconds=tf.seconds * bars))

    candles: list[Candle] = []
    price = start_price
    regime_bars = 0
    drift = 0.0
    vol = 0.006

    for i in range(bars):
        if regime_bars <= 0:
            kind = rng.choices(["bull", "bear", "range"], weights=[0.35, 0.25, 0.40])[0]
            drift = {"bull": 0.0011, "bear": -0.0009, "range": 0.0}[kind]
            vol = {"bull": 0.009, "bear": 0.012, "range": 0.006}[kind]
            regime_bars = rng.randint(60, 400)
        regime_bars -= 1

        shock = 0.0
        if rng.random() < 0.002:
            # Fat tail. Crypto does this, and a generator without it lets stops
            # look far safer than they are.
            shock = rng.gauss(0, 0.06)

        o = price
        price = max(price * 0.5, price * (1 + rng.gauss(drift, vol) + shock))
        span = abs(price - o)
        high = max(o, price) + abs(rng.gauss(0, vol * 0.6)) * o
        low = min(o, price) - abs(rng.gauss(0, vol * 0.6)) * o
        low = max(low, 0.01)
        volume = abs(rng.gauss(1000, 300)) * (1 + span / max(o, 1e-9) * 20)

        candles.append(
            Candle(
                ts=start_ts + timedelta(seconds=tf.seconds * i),
                open=Decimal(str(round(o, 2))),
                high=Decimal(str(round(high, 2))),
                low=Decimal(str(round(low, 2))),
                close=Decimal(str(round(price, 2))),
                volume=Decimal(str(round(volume, 2))),
            )
        )
    return candles


def synthetic_exchange(
    symbols: Sequence[str] = ("BTCUSDT", "ETHUSDT", "SOLUSDT"),
    bars: int = 4000,
    tf: TF = TF.H2,
    seed: int = 20260828,
    seconds_per_bar: float = 0.0,
) -> ReplayExchange:
    """A ready-made offline venue. Always flagged synthetic.

    `seconds_per_bar` turns on the compressed demo clock: pass ~3 and the
    offline bot prints a new candle every three seconds, so a level-9
    configuration shows you a month of its behaviour over a coffee instead of
    over a month.
    """
    prices = {"BTCUSDT": 42000.0, "ETHUSDT": 2300.0, "SOLUSDT": 98.0}
    ex = ReplayExchange(
        {
            name: synthetic_series(
                name, bars, tf, prices.get(name, 100.0), seed + i
            )
            for i, name in enumerate(symbols)
        },
        tf=tf,
        seconds_per_bar=seconds_per_bar,
    )
    if seconds_per_bar > 0:
        ex.seek(int(bars * 0.6))
    ex.is_synthetic = True
    ex.display_name = "Offline demo (synthetic data)"
    return ex
