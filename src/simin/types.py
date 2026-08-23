"""Core value types.

Deliberately stdlib-only (dataclasses + Decimal) so that the domain model and its
tests carry no heavy dependencies and can run anywhere.

Money is Decimal, never float. A float rounding error in a quantity is a rejected
order at best and a wrong position size at worst.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal


class TF(enum.Enum):
    """Bar timeframe. Value is the canonical string used in storage and APIs."""

    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def delta(self) -> timedelta:
        return _TF_DELTA[self]

    @property
    def seconds(self) -> int:
        return int(self.delta.total_seconds())

    def floor(self, ts: datetime) -> datetime:
        """Round a timestamp down to the open of the bar containing it (UTC)."""
        _require_utc(ts)
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        n = int((ts - epoch).total_seconds()) // self.seconds
        return epoch + timedelta(seconds=n * self.seconds)

    @classmethod
    def parse(cls, raw: str) -> TF:
        try:
            return cls(raw)
        except ValueError as exc:  # pragma: no cover - trivial
            raise ValueError(f"unknown timeframe {raw!r}") from exc


_TF_DELTA: dict[TF, timedelta] = {
    TF.M1: timedelta(minutes=1),
    TF.M3: timedelta(minutes=3),
    TF.M5: timedelta(minutes=5),
    TF.M15: timedelta(minutes=15),
    TF.M30: timedelta(minutes=30),
    TF.H1: timedelta(hours=1),
    TF.H4: timedelta(hours=4),
    TF.D1: timedelta(days=1),
}


class Side(enum.StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(enum.StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderStatus(enum.StrEnum):
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class RunMode(enum.StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


def _require_utc(ts: datetime) -> None:
    if ts.tzinfo is None or ts.utcoffset() != timedelta(0):
        raise ValueError(f"timestamp must be timezone-aware UTC, got {ts!r}")


@dataclass(frozen=True, slots=True)
class Bar:
    """A *closed* OHLCV bar. ``ts`` is the bar OPEN time in UTC.

    Simin never stores an unclosed bar in the research path: an in-progress bar is
    the most common accidental source of look-ahead bias.
    """

    symbol: str
    tf: TF
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal | None = None
    trades: int | None = None

    def __post_init__(self) -> None:
        _require_utc(self.ts)
        if self.ts != self.tf.floor(self.ts):
            raise ValueError(f"bar ts {self.ts.isoformat()} not aligned to {self.tf.value}")
        if self.high < self.low:
            raise ValueError("high < low")
        for name in ("open", "close"):
            px = getattr(self, name)
            if not (self.low <= px <= self.high):
                raise ValueError(f"{name} {px} outside [low, high]")
        if self.volume < 0:
            raise ValueError("negative volume")

    @property
    def close_time(self) -> datetime:
        """The instant this bar becomes known. Nothing may be acted on before it."""
        return self.ts + self.tf.delta


@dataclass(frozen=True, slots=True)
class Ticker:
    symbol: str
    ts: datetime
    bid: Decimal
    ask: Decimal
    last: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal(2)

    @property
    def spread_bps(self) -> Decimal:
        mid = self.mid
        if mid <= 0:
            raise ValueError("non-positive mid price")
        return (self.ask - self.bid) / mid * Decimal(10_000)


@dataclass(frozen=True, slots=True)
class Level:
    price: Decimal
    qty: Decimal


@dataclass(frozen=True, slots=True)
class OrderBook:
    symbol: str
    ts: datetime
    bids: tuple[Level, ...]  # descending price
    asks: tuple[Level, ...]  # ascending price

    def __post_init__(self) -> None:
        _require_utc(self.ts)
        if any(a.price <= b.price for a, b in zip(self.bids, self.bids[1:], strict=False)):
            raise ValueError("bids not strictly descending")
        if any(a.price >= b.price for a, b in zip(self.asks, self.asks[1:], strict=False)):
            raise ValueError("asks not strictly ascending")
        if self.bids and self.asks and self.bids[0].price >= self.asks[0].price:
            raise ValueError("crossed book")

    def depth_notional(self, side: Side, levels: int = 5) -> Decimal:
        book = self.asks if side is Side.BUY else self.bids
        return sum((lv.price * lv.qty for lv in book[:levels]), start=Decimal(0))

    def sweep(self, side: Side, qty: Decimal) -> tuple[Decimal, Decimal]:
        """Walk the book for ``qty``.

        Returns ``(filled_qty, avg_price)``. Partial sweeps are normal and must be
        modelled: assuming full fill at top-of-book is the classic backtest lie.
        """
        if qty <= 0:
            raise ValueError("qty must be positive")
        book = self.asks if side is Side.BUY else self.bids
        remaining, notional = qty, Decimal(0)
        for lv in book:
            take = min(remaining, lv.qty)
            notional += take * lv.price
            remaining -= take
            if remaining == 0:
                break
        filled = qty - remaining
        if filled == 0:
            return Decimal(0), Decimal(0)
        return filled, notional / filled


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    venue: str
    symbol: str
    base: str
    quote: str
    price_tick: Decimal
    qty_step: Decimal
    min_notional: Decimal
    listed_at: datetime | None = None
    delisted_at: datetime | None = None

    def is_tradeable_at(self, ts: datetime) -> bool:
        """Point-in-time listing check — the guard against survivorship bias."""
        if self.listed_at is not None and ts < self.listed_at:
            return False
        return not (self.delisted_at is not None and ts >= self.delisted_at)


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    ts: datetime
    price: Decimal
    qty: Decimal
    side: Side
    trade_id: str


@dataclass(frozen=True, slots=True)
class Balance:
    asset: str
    free: Decimal
    locked: Decimal

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    side: Side
    type: OrderType
    qty: Decimal
    price: Decimal | None = None
    stop_price: Decimal | None = None
    client_order_id: str = ""
    reduce_only: bool = False

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError("qty must be positive")
        if self.type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and self.price is None:
            raise ValueError(f"{self.type} requires a price")
        if self.type in (OrderType.STOP, OrderType.STOP_LIMIT) and self.stop_price is None:
            raise ValueError(f"{self.type} requires a stop_price")
        if not self.client_order_id:
            raise ValueError("client_order_id is mandatory (idempotent retries)")


@dataclass(frozen=True, slots=True)
class Fill:
    ts: datetime
    price: Decimal
    qty: Decimal
    fee: Decimal
    fee_asset: str
    is_maker: bool


@dataclass(slots=True)
class Order:
    client_order_id: str
    symbol: str
    side: Side
    type: OrderType
    qty: Decimal
    status: OrderStatus
    exchange_order_id: str | None = None
    price: Decimal | None = None
    filled_qty: Decimal = Decimal(0)
    avg_price: Decimal | None = None
    fills: list[Fill] = field(default_factory=list)
    created_at: datetime | None = None
    reject_reason: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Fees as fractions, e.g. Decimal('0.0015') == 0.15%."""

    maker: Decimal
    taker: Decimal

    def cost(self, notional: Decimal, *, is_maker: bool) -> Decimal:
        return notional * (self.maker if is_maker else self.taker)


@dataclass(frozen=True, slots=True)
class VenueHealth:
    venue: str
    ok: bool
    latency_p95_ms: float
    error_rate: float
    clock_skew_ms: float
    checked_at: datetime
