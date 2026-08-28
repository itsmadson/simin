"""The venue interface every exchange adapter implements.

Two hard rules, enforced here rather than trusted to each adapter:

1. **`can_trade` is explicit.** An adapter that cannot place real orders says
   so, and the trader refuses to run in REAL mode against it. There is no
   adapter that "sort of" trades.

2. **Real orders check the mode.** `place_order` on a live adapter asserts the
   process is in REAL mode. Belt and braces against a config mistake putting a
   lab session on a real key.

Adapters normalise everything into the domain types. Venue quirks — CoinEx's
string decimals, Nobitex's Rial-vs-Toman ambiguity, per-venue symbol spellings —
are dealt with at the boundary so nothing above this layer knows about them.
"""

from __future__ import annotations

import abc
import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from simin.core.types import (
    TF,
    Candle,
    MarketKind,
    Mode,
    Order,
    OrderType,
    Position,
    Side,
    Symbol,
)


class ExchangeError(RuntimeError):
    """Anything the venue rejected or could not answer."""


class RateLimited(ExchangeError):
    """Back off and retry. Carries the venue's suggested wait, when given."""

    def __init__(self, message: str, retry_after: float = 1.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class InsufficientFunds(ExchangeError):
    """Not retryable. Retrying an order the account cannot afford just burns
    rate limit and, on some venues, trips anti-abuse throttling."""


@dataclass(frozen=True, slots=True)
class Balance:
    asset: str
    free: Decimal
    locked: Decimal

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


@dataclass(frozen=True, slots=True)
class Ticker:
    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    ts: datetime

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        """Spread in basis points. The cost you pay before the trade even moves."""
        m = self.mid
        return (self.ask - self.bid) / m * 10000 if m > 0 else Decimal(0)


@dataclass(frozen=True, slots=True)
class Fees:
    """Venue fee schedule. Round-trip cost is what actually matters, and on
    every venue in scope it dwarfs the difference between good and mediocre
    indicator settings."""

    maker: Decimal
    taker: Decimal
    #: Perpetual funding, charged every 8h on futures. Signed: positive means
    #: longs pay shorts.
    funding_rate: Decimal = Decimal("0.0001")

    @property
    def round_trip_taker(self) -> Decimal:
        return self.taker * 2


class RateLimiter:
    """Token bucket. Shared per venue, because rate limits are per key, not
    per object, and constructing two adapters must not double the budget."""

    __slots__ = ("_capacity", "_tokens", "_refill_per_sec", "_last", "_lock")

    def __init__(self, requests_per_second: float, burst: int | None = None) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._capacity = float(burst if burst is not None else max(1, int(requests_per_second)))
        self._tokens = self._capacity
        self._refill_per_sec = requests_per_second
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._last) * self._refill_per_sec
                )
                self._last = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                await asyncio.sleep((cost - self._tokens) / self._refill_per_sec)


class Exchange(abc.ABC):
    """One venue."""

    #: Stable venue id, matching the credential env prefix.
    name: str = "unnamed"
    #: Display name for the UI.
    display_name: str = ""
    #: False for backtest/paper adapters. Checked before REAL mode may start.
    can_trade: bool = False
    #: Which market kinds this venue offers. Spot-only venues clamp the dial.
    kinds: tuple[MarketKind, ...] = (MarketKind.SPOT,)
    #: The currency balances are denominated in.
    quote_asset: str = "USDT"

    @property
    def supports_futures(self) -> bool:
        return MarketKind.FUTURES in self.kinds

    @property
    def supports_shorts(self) -> bool:
        return self.supports_futures

    # --- Market data ------------------------------------------------------

    @abc.abstractmethod
    async def symbols(self) -> Sequence[Symbol]:
        """Every instrument this venue lists, with its precision and minimums."""

    @abc.abstractmethod
    async def candles(
        self,
        symbol: str,
        tf: TF,
        limit: int = 500,
        end: datetime | None = None,
    ) -> list[Candle]:
        """Closed candles, oldest first.

        Adapters must drop the still-forming last candle. Returning it is the
        classic live-trading lookahead bug: the bot sees a partial bar, decides,
        and the bar then closes somewhere else entirely.
        """

    @abc.abstractmethod
    async def ticker(self, symbol: str) -> Ticker: ...

    @abc.abstractmethod
    async def fees(self, symbol: str) -> Fees: ...

    # --- Account ----------------------------------------------------------

    @abc.abstractmethod
    async def balances(self) -> dict[str, Balance]: ...

    async def positions(self) -> Sequence[Position]:
        """Open futures positions. Spot venues have none by definition."""
        return ()

    # --- Trading ----------------------------------------------------------

    @abc.abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: Side,
        type: OrderType,
        qty: Decimal,
        price: Decimal | None = None,
        stop_price: Decimal | None = None,
        reduce_only: bool = False,
        client_id: str = "",
    ) -> Order: ...

    @abc.abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool: ...

    @abc.abstractmethod
    async def get_order(self, symbol: str, order_id: str) -> Order: ...

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        """Futures venues override. Spot venues correctly do nothing."""
        if leverage > 1 and not self.supports_futures:
            raise ExchangeError(f"{self.name} is spot-only; cannot set {leverage}x leverage")

    async def close(self) -> None:
        """Release sockets. Idempotent."""

    # --- Guards -----------------------------------------------------------

    def assert_can_trade(self, mode: Mode) -> None:
        """Called before any order. The last gate before money moves."""
        if mode is Mode.REAL and not self.can_trade:
            raise ExchangeError(
                f"{self.name} adapter cannot place real orders — refusing to run REAL mode. "
                "This is a configuration error, not a transient failure."
            )

    async def __aenter__(self) -> Exchange:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


def normalise_symbol(raw: str) -> str:
    """`btc/usdt`, `BTC-USDT`, `BTC_USDT` -> `BTCUSDT`."""
    return raw.upper().replace("/", "").replace("-", "").replace("_", "").strip()
