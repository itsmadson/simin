"""The only interface the trading core knows about.

Simin's core never names a venue. Venues are plugins behind this protocol, which
keeps the engine testable, keeps credentials at the edge, and means a venue going
away (or being sanctioned, frozen, or breached) is a configuration change rather
than a rewrite. See docs/04-exchanges-iran.md for why that mattered here.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from datetime import datetime

from simin.types import (
    TF,
    Balance,
    Bar,
    FeeSchedule,
    Order,
    OrderBook,
    OrderRequest,
    SymbolInfo,
    Ticker,
    Trade,
    VenueHealth,
)


class VenueError(RuntimeError):
    """Base class for venue failures."""

    retryable: bool = False


class VenueUnavailable(VenueError):
    retryable = True


class RateLimited(VenueError):
    retryable = True

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class OrderRejected(VenueError):
    """The venue refused the order. Never retried blindly."""


class ExchangeAdapter(abc.ABC):
    """Venue adapter.

    Contract every implementation must honour:

    * All timestamps returned are timezone-aware UTC.
    * ``get_ohlcv`` returns **closed bars only**, ascending by ts, no duplicates.
    * ``create_order`` is idempotent on ``client_order_id``: calling it twice with
      the same id must not create two orders. This is what makes a retry safe.
    * Money and quantities are ``Decimal``.
    * Errors are raised as ``VenueError`` subclasses, never as raw HTTP errors.
    """

    venue: str
    supports_trading: bool = False

    @abc.abstractmethod
    async def get_symbols(self) -> list[SymbolInfo]: ...

    @abc.abstractmethod
    async def get_ticker(self, symbol: str) -> Ticker: ...

    @abc.abstractmethod
    async def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBook: ...

    @abc.abstractmethod
    async def get_ohlcv(
        self, symbol: str, tf: TF, since: datetime, limit: int = 1000
    ) -> list[Bar]: ...

    @abc.abstractmethod
    def fee_schedule(self, symbol: str | None = None) -> FeeSchedule: ...

    @abc.abstractmethod
    async def health(self) -> VenueHealth: ...

    def watch_trades(self, symbols: list[str]) -> AsyncIterator[Trade]:
        """Stream trades. Adapters without a stream raise on iteration."""
        raise NotImplementedError(f"{self.venue} has no trade stream")

    # --- trading surface: only adapters with supports_trading=True implement these ---

    async def get_balance(self) -> list[Balance]:
        raise NotImplementedError(f"{self.venue} is read-only")

    async def create_order(self, req: OrderRequest) -> Order:
        raise NotImplementedError(f"{self.venue} is read-only")

    async def cancel_order(self, order_id: str, symbol: str) -> Order:
        raise NotImplementedError(f"{self.venue} is read-only")

    async def get_order(self, order_id: str, symbol: str) -> Order:
        raise NotImplementedError(f"{self.venue} is read-only")

    async def close(self) -> None:
        return None
