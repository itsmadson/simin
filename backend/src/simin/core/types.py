"""Core domain types.

Stdlib only (dataclasses + Decimal) so the domain model carries no heavy
dependencies and can be exercised anywhere.

Money and quantities are Decimal, never float. A float rounding error in a
quantity is a rejected order at best and a wrong position size at worst.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

ZERO = Decimal("0")


def utcnow() -> datetime:
    return datetime.now(UTC)


def require_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("naive datetime; every timestamp must be timezone-aware UTC")
    return ts.astimezone(UTC)


class TF(enum.Enum):
    """Bar timeframe. The value is the canonical string used in storage and APIs."""

    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    H6 = "6h"
    H12 = "12h"
    D1 = "1d"

    @property
    def delta(self) -> timedelta:
        return _TF_DELTA[self]

    @property
    def seconds(self) -> int:
        return int(self.delta.total_seconds())

    @property
    def minutes(self) -> int:
        return self.seconds // 60

    @property
    def per_day(self) -> float:
        return 86400.0 / self.seconds

    def floor(self, ts: datetime) -> datetime:
        """Round a timestamp down to the open of the bar containing it."""
        ts = require_utc(ts)
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        n = int((ts - epoch).total_seconds()) // self.seconds
        return epoch + timedelta(seconds=n * self.seconds)

    def is_closed(self, bar_open: datetime, now: datetime) -> bool:
        """True once the bar that opened at `bar_open` can no longer change."""
        return require_utc(now) >= require_utc(bar_open) + self.delta

    @classmethod
    def parse(cls, raw: str) -> TF:
        try:
            return cls(raw.strip().lower())
        except ValueError as exc:
            raise ValueError(f"unknown timeframe {raw!r}") from exc


_TF_DELTA: dict[TF, timedelta] = {
    TF.M1: timedelta(minutes=1),
    TF.M3: timedelta(minutes=3),
    TF.M5: timedelta(minutes=5),
    TF.M15: timedelta(minutes=15),
    TF.M30: timedelta(minutes=30),
    TF.H1: timedelta(hours=1),
    TF.H2: timedelta(hours=2),
    TF.H4: timedelta(hours=4),
    TF.H6: timedelta(hours=6),
    TF.H12: timedelta(hours=12),
    TF.D1: timedelta(days=1),
}


class Mode(enum.StrEnum):
    """The two operating modes the whole system is built around.

    LAB never touches a venue that can move money. REAL does, and every code
    path that can place a real order asserts this enum first.
    """

    LAB = "lab"
    REAL = "real"


class Side(enum.StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> Decimal:
        return Decimal(1) if self is Side.BUY else Decimal(-1)


class Direction(enum.StrEnum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> Decimal:
        return Decimal(1) if self is Direction.LONG else Decimal(-1)

    @property
    def entry_side(self) -> Side:
        return Side.BUY if self is Direction.LONG else Side.SELL

    @property
    def exit_side(self) -> Side:
        return self.entry_side.opposite


class OrderType(enum.StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop_market"
    TAKE_PROFIT_MARKET = "take_profit_market"


class OrderStatus(enum.StrEnum):
    PENDING = "pending"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED)


class MarketKind(enum.StrEnum):
    SPOT = "spot"
    FUTURES = "futures"


class ExitReason(enum.StrEnum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    SIGNAL_FLIP = "signal_flip"
    TIME_STOP = "time_stop"
    LIQUIDATION = "liquidation"
    KILL_SWITCH = "kill_switch"
    MANUAL = "manual"
    SESSION_END = "session_end"


@dataclass(frozen=True, slots=True)
class Candle:
    """One OHLCV bar. `ts` is the bar OPEN time, always UTC."""

    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        if self.high < self.low:
            raise ValueError(f"candle at {self.ts}: high {self.high} < low {self.low}")
        if not (self.low <= self.open <= self.high):
            raise ValueError(f"candle at {self.ts}: open {self.open} outside [low, high]")
        if not (self.low <= self.close <= self.high):
            raise ValueError(f"candle at {self.ts}: close {self.close} outside [low, high]")
        if self.volume < 0:
            raise ValueError(f"candle at {self.ts}: negative volume")

    @property
    def is_bull(self) -> bool:
        return self.close > self.open

    @property
    def body(self) -> Decimal:
        return abs(self.close - self.open)

    @property
    def range(self) -> Decimal:
        return self.high - self.low

    @property
    def upper_wick(self) -> Decimal:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> Decimal:
        return min(self.open, self.close) - self.low

    @property
    def hlc3(self) -> Decimal:
        return (self.high + self.low + self.close) / 3


@dataclass(frozen=True, slots=True)
class Symbol:
    """A tradeable instrument on one venue.

    `base`/`quote` are the canonical asset codes; `venue_symbol` is whatever
    string that particular exchange wants on the wire.
    """

    base: str
    quote: str
    venue: str
    venue_symbol: str
    kind: MarketKind = MarketKind.SPOT
    price_precision: int = 2
    qty_precision: int = 6
    min_qty: Decimal = Decimal("0")
    min_notional: Decimal = Decimal("0")
    max_leverage: int = 1
    contract_size: Decimal = Decimal("1")

    @property
    def name(self) -> str:
        return f"{self.base}{self.quote}"

    def __str__(self) -> str:
        return f"{self.venue}:{self.name}:{self.kind}"


@dataclass(frozen=True, slots=True)
class Order:
    id: str
    symbol: str
    side: Side
    type: OrderType
    qty: Decimal
    price: Decimal | None = None
    stop_price: Decimal | None = None
    reduce_only: bool = False
    client_id: str = ""
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: Decimal = ZERO
    avg_price: Decimal = ZERO
    fee: Decimal = ZERO
    created_at: datetime = field(default_factory=utcnow)
    venue_order_id: str = ""

    @property
    def remaining(self) -> Decimal:
        return self.qty - self.filled_qty


@dataclass(frozen=True, slots=True)
class Fill:
    order_id: str
    symbol: str
    side: Side
    qty: Decimal
    price: Decimal
    fee: Decimal
    ts: datetime
    is_maker: bool = False


@dataclass(slots=True)
class Position:
    """An open position. Mutable — it is updated bar by bar as price moves."""

    symbol: str
    direction: Direction
    qty: Decimal
    entry_price: Decimal
    stop_price: Decimal
    take_profit: Decimal | None
    leverage: Decimal
    opened_at: datetime
    strategy: str
    risk_level: int
    #: Notional risked at entry, in quote currency. Defines 1R for this trade.
    risk_amount: Decimal = ZERO
    initial_stop: Decimal = ZERO
    fees_paid: Decimal = ZERO
    funding_paid: Decimal = ZERO
    bars_held: int = 0
    max_favorable: Decimal = ZERO
    max_adverse: Decimal = ZERO
    breakeven_armed: bool = False
    partial_taken: bool = False
    meta: dict[str, object] = field(default_factory=dict)

    @property
    def notional(self) -> Decimal:
        return self.qty * self.entry_price

    @property
    def margin(self) -> Decimal:
        return self.notional / self.leverage if self.leverage else self.notional

    def unrealized(self, price: Decimal) -> Decimal:
        return (price - self.entry_price) * self.qty * self.direction.sign

    def r_multiple(self, price: Decimal) -> Decimal:
        """Profit measured in units of the risk taken. The only honest scale."""
        if self.risk_amount <= 0:
            return ZERO
        return self.unrealized(price) / self.risk_amount

    def liquidation_price(self, maintenance_margin_rate: Decimal = Decimal("0.005")) -> Decimal:
        """Price at which the venue force-closes this position.

        Isolated-margin approximation: the position dies when the loss eats the
        posted margin minus the maintenance requirement.
        """
        if self.leverage <= 1:
            return ZERO if self.direction is Direction.LONG else Decimal("Infinity")
        adverse = self.entry_price * (1 / self.leverage - maintenance_margin_rate)
        price = self.entry_price - adverse * self.direction.sign
        return max(price, ZERO)

    def update_excursions(self, high: Decimal, low: Decimal) -> None:
        best = high if self.direction is Direction.LONG else low
        worst = low if self.direction is Direction.LONG else high
        self.max_favorable = max(self.max_favorable, self.unrealized(best))
        self.max_adverse = min(self.max_adverse, self.unrealized(worst))


@dataclass(frozen=True, slots=True)
class Trade:
    """A closed round trip. This is what performance is computed from."""

    symbol: str
    direction: Direction
    qty: Decimal
    entry_price: Decimal
    exit_price: Decimal
    opened_at: datetime
    closed_at: datetime
    gross_pnl: Decimal
    fees: Decimal
    funding: Decimal
    net_pnl: Decimal
    r_multiple: Decimal
    reason: ExitReason
    strategy: str
    risk_level: int
    leverage: Decimal
    bars_held: int
    max_favorable: Decimal = ZERO
    max_adverse: Decimal = ZERO

    @property
    def is_win(self) -> bool:
        return self.net_pnl > 0

    @property
    def duration(self) -> timedelta:
        return self.closed_at - self.opened_at


@dataclass(frozen=True, slots=True)
class EquityPoint:
    ts: datetime
    equity: Decimal
    cash: Decimal
    exposure: Decimal
    open_positions: int
    drawdown: Decimal


class Signal(enum.StrEnum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"
    CLOSE = "close"


@dataclass(frozen=True, slots=True)
class Intent:
    """A strategy's opinion. It never carries a size — sizing is the risk
    engine's job, and only the risk engine knows the account."""

    signal: Signal
    #: 0..1 confluence score. The risk dial decides how high this must be.
    confidence: float
    #: Where the idea stops being right. Sizing is derived from this distance.
    stop_price: Decimal | None = None
    take_profit: Decimal | None = None
    strategy: str = ""
    reasons: tuple[str, ...] = ()

    @property
    def is_entry(self) -> bool:
        return self.signal in (Signal.LONG, Signal.SHORT)

    @property
    def direction(self) -> Direction | None:
        if self.signal is Signal.LONG:
            return Direction.LONG
        if self.signal is Signal.SHORT:
            return Direction.SHORT
        return None


FLAT = Intent(signal=Signal.FLAT, confidence=0.0)
