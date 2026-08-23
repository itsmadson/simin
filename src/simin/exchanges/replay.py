"""Deterministic replay adapter used by the backtester.

This is where look-ahead bias is structurally prevented rather than merely
avoided by convention: the adapter holds a clock, and it is *incapable* of
returning data whose close time is in the future relative to that clock. A
strategy that tries to peek gets an empty list, not tomorrow's price.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from simin.data.quality import dedupe
from simin.exchanges.base import ExchangeAdapter, VenueError
from simin.types import (
    TF,
    Bar,
    FeeSchedule,
    Level,
    OrderBook,
    SymbolInfo,
    Ticker,
    VenueHealth,
)


@dataclass(slots=True)
class Clock:
    """Simulated wall clock. The backtest event loop owns it; nothing else moves it."""

    now: datetime

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            raise ValueError("clock must be timezone-aware UTC")

    def advance_to(self, ts: datetime) -> None:
        if ts < self.now:
            raise ValueError(f"clock cannot go backwards: {ts} < {self.now}")
        self.now = ts


@dataclass(slots=True)
class ReplayAdapter(ExchangeAdapter):
    """Serves a fixed bar history as of ``clock.now``."""

    clock: Clock
    bars: dict[tuple[str, TF], list[Bar]] = field(default_factory=dict)
    symbols: dict[str, SymbolInfo] = field(default_factory=dict)
    fees: FeeSchedule = field(
        default_factory=lambda: FeeSchedule(maker=Decimal("0.0015"), taker=Decimal("0.0025"))
    )
    spread_bps: Decimal = Decimal("20")
    venue: str = "replay"
    supports_trading: bool = False

    def load(self, symbol: str, tf: TF, bars: Iterable[Bar]) -> None:
        series = dedupe(bars)
        for bar in series:
            if bar.symbol != symbol or bar.tf is not tf:
                raise ValueError("bar does not match the series it is loaded into")
        self.bars[(symbol, tf)] = series
        self.symbols.setdefault(
            symbol,
            SymbolInfo(
                venue=self.venue,
                symbol=symbol,
                base=symbol[:-4] if len(symbol) > 4 else symbol,
                quote=symbol[-4:] if len(symbol) > 4 else "",
                price_tick=Decimal("0.00000001"),
                qty_step=Decimal("0.00000001"),
                min_notional=Decimal("0"),
                listed_at=series[0].ts if series else None,
            ),
        )

    def _visible(self, symbol: str, tf: TF) -> list[Bar]:
        """Bars whose close time has already passed. The causality boundary."""
        now = self.clock.now
        return [b for b in self.bars.get((symbol, tf), []) if b.close_time <= now]

    async def get_symbols(self) -> list[SymbolInfo]:
        return [s for s in self.symbols.values() if s.is_tradeable_at(self.clock.now)]

    async def get_ohlcv(self, symbol: str, tf: TF, since: datetime, limit: int = 1000) -> list[Bar]:
        visible = [b for b in self._visible(symbol, tf) if b.ts >= since]
        # forward paging from `since`, matching real venue APIs: the caller walks
        # the cursor. Returning the tail instead would silently skip history.
        return visible[:limit]

    def last_bar(self, symbol: str, tf: TF) -> Bar | None:
        visible = self._visible(symbol, tf)
        return visible[-1] if visible else None

    async def get_ticker(self, symbol: str) -> Ticker:
        bar = self.last_bar(symbol, TF.M1) or self.last_bar(symbol, TF.H1)
        if bar is None:
            raise VenueError(f"replay: no visible data for {symbol} at {self.clock.now}")
        half = bar.close * self.spread_bps / Decimal(20_000)
        return Ticker(
            symbol=symbol,
            ts=self.clock.now,
            bid=bar.close - half,
            ask=bar.close + half,
            last=bar.close,
        )

    async def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBook:
        """Synthetic book around the last close.

        Explicitly an approximation: depth decays linearly away from the touch.
        Real recorded books replace this once order-book capture is running — a
        synthetic book flatters large orders, so backtest sizes are capped
        against it conservatively.
        """
        ticker = await self.get_ticker(symbol)
        bar = self.last_bar(symbol, TF.M1) or self.last_bar(symbol, TF.H1)
        assert bar is not None
        unit = (bar.volume / Decimal(100)) or Decimal(1)
        step = ticker.mid * Decimal("0.0005")
        bids = tuple(
            Level(ticker.bid - step * i, unit * (Decimal(depth - i) / Decimal(depth)))
            for i in range(depth)
        )
        asks = tuple(
            Level(ticker.ask + step * i, unit * (Decimal(depth - i) / Decimal(depth)))
            for i in range(depth)
        )
        return OrderBook(symbol=symbol, ts=self.clock.now, bids=bids, asks=asks)

    def fee_schedule(self, symbol: str | None = None) -> FeeSchedule:
        return self.fees

    async def health(self) -> VenueHealth:
        return VenueHealth(
            venue=self.venue,
            ok=True,
            latency_p95_ms=0.0,
            error_rate=0.0,
            clock_skew_ms=0.0,
            checked_at=self.clock.now or datetime.now(UTC),
        )
