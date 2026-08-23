"""Risk engine — the last word on every order.

Signals arrive here as *intents*. What leaves is either a sized order or a
rejection with a reason. There is deliberately no bypass: strategies, the ML
layer and manual input all pass through the same checks, because the component
most likely to want an exception is the one you least want to grant it to.

Ordering matters. Account-level halts are evaluated before sizing, so a system
in a drawdown halt cannot compute its way to a position.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from simin.config import RiskLimits


class RejectReason(enum.StrEnum):
    OK = "ok"
    KILL_SWITCH = "kill_switch"
    DRAWDOWN_HALT = "drawdown_halt"
    DAILY_LOSS_STOP = "daily_loss_stop"
    WEEKLY_LOSS_STOP = "weekly_loss_stop"
    LOSS_STREAK = "loss_streak"
    MAX_POSITIONS = "max_positions"
    ASSET_EXPOSURE = "asset_exposure"
    TOTAL_EXPOSURE = "total_exposure"
    BETA_EXPOSURE = "correlated_beta_exposure"
    VENUE_EXPOSURE = "venue_exposure"
    ALREADY_OPEN = "already_open"
    INVALID_STOP = "invalid_stop"
    SIZE_TOO_SMALL = "size_below_min_notional"
    NO_LIQUIDITY = "insufficient_depth"
    REGIME_FORBIDS = "regime_forbids_strategy"
    STALE_DATA = "stale_data"


@dataclass(frozen=True, slots=True)
class Intent:
    """What a strategy wants. Never sized — sizing is the risk engine's job."""

    ts: datetime
    symbol: str
    direction: int  # +1 long, -1 short
    entry: Decimal
    stop: Decimal
    strategy: str
    regime: str | None = None
    confidence: float = 1.0
    take_profits: tuple[Decimal, ...] = ()
    beta: float = 1.0  # exposure to the common crypto factor
    venue: str = "default"

    def __post_init__(self) -> None:
        if self.direction not in (1, -1):
            raise ValueError("direction must be +1 or -1")
        if self.entry <= 0:
            raise ValueError("entry must be positive")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")

    @property
    def stop_distance(self) -> Decimal:
        return abs(self.entry - self.stop)

    @property
    def stop_is_protective(self) -> bool:
        """A long's stop must sit below entry, a short's above. Otherwise it is
        not a stop, it is a guaranteed loss trigger."""
        return (self.stop < self.entry) if self.direction > 0 else (self.stop > self.entry)


@dataclass(frozen=True, slots=True)
class OpenPosition:
    symbol: str
    direction: int
    qty: Decimal
    entry: Decimal
    stop: Decimal
    strategy: str
    beta: float = 1.0
    venue: str = "default"
    opened_at: datetime | None = None

    def notional(self, price: Decimal | None = None) -> Decimal:
        return self.qty * (price if price is not None else self.entry)


@dataclass(slots=True)
class AccountState:
    equity: Decimal
    peak_equity: Decimal
    day_start_equity: Decimal
    week_start_equity: Decimal
    consecutive_losses: int = 0
    kill_switch: bool = False
    kill_reason: str | None = None
    positions: dict[str, OpenPosition] = field(default_factory=dict)
    last_data_ts: datetime | None = None

    @property
    def drawdown(self) -> Decimal:
        if self.peak_equity <= 0:
            return Decimal(0)
        return self.equity / self.peak_equity - Decimal(1)

    @property
    def daily_pnl_pct(self) -> Decimal:
        if self.day_start_equity <= 0:
            return Decimal(0)
        return self.equity / self.day_start_equity - Decimal(1)

    @property
    def weekly_pnl_pct(self) -> Decimal:
        if self.week_start_equity <= 0:
            return Decimal(0)
        return self.equity / self.week_start_equity - Decimal(1)

    def mark(self, equity: Decimal) -> None:
        self.equity = equity
        self.peak_equity = max(self.peak_equity, equity)

    def roll_day(self) -> None:
        self.day_start_equity = self.equity

    def roll_week(self) -> None:
        self.week_start_equity = self.equity

    def trip(self, reason: str) -> None:
        """Kill switch. Only a human clears it — see docs/03 §1."""
        self.kill_switch = True
        self.kill_reason = reason


@dataclass(frozen=True, slots=True)
class Decision:
    approved: bool
    qty: Decimal
    reason: RejectReason
    risk_fraction: Decimal = Decimal(0)
    detail: str = ""

    @property
    def rejected(self) -> bool:
        return not self.approved


class RiskEngine:
    def __init__(self, limits: RiskLimits, *, min_notional: Decimal = Decimal(0)) -> None:
        self.limits = limits
        self.min_notional = min_notional

    # ------------------------------------------------------------------ sizing

    def risk_fraction(self, state: AccountState, intent: Intent) -> Decimal:
        """Fraction of equity to risk, after drawdown throttles and confidence.

        Throttling risk *inside* a drawdown is what converts a losing streak from
        a spiral into a plateau: each subsequent loss is smaller than the last.
        """
        base = self.limits.risk_per_trade
        dd = abs(state.drawdown)
        if dd >= self.limits.dd_throttle_quarter:
            base *= Decimal("0.25")
        elif dd >= self.limits.dd_throttle_half:
            base *= Decimal("0.5")
        if state.weekly_pnl_pct <= -self.limits.weekly_loss_stop:
            base *= Decimal("0.5")
        confidence = Decimal(str(max(0.0, min(1.0, intent.confidence))))
        # Half-Kelly-style scaling by confidence, floored so a mildly confident
        # signal is small rather than absent.
        return base * (Decimal("0.5") + Decimal("0.5") * confidence) * self.limits.kelly_fraction

    def position_size(
        self, state: AccountState, intent: Intent, risk_fraction: Decimal
    ) -> Decimal:
        distance = intent.stop_distance
        if distance <= 0:
            return Decimal(0)
        return (state.equity * risk_fraction) / distance

    # ------------------------------------------------------------------ checks

    def evaluate(
        self,
        state: AccountState,
        intent: Intent,
        *,
        available_depth: Decimal | None = None,
        now: datetime | None = None,
        max_staleness: timedelta = timedelta(hours=2),
    ) -> Decision:
        """Approve or reject, and size if approved. Order of checks is deliberate."""
        if state.kill_switch:
            return Decision(
                False, Decimal(0), RejectReason.KILL_SWITCH, detail=state.kill_reason or ""
            )

        if abs(state.drawdown) >= self.limits.dd_halt:
            state.trip(f"drawdown {float(state.drawdown):.1%} breached halt")
            return Decision(False, Decimal(0), RejectReason.DRAWDOWN_HALT)

        if state.daily_pnl_pct <= -self.limits.daily_loss_stop:
            return Decision(False, Decimal(0), RejectReason.DAILY_LOSS_STOP)

        if state.weekly_pnl_pct <= -self.limits.weekly_loss_stop * Decimal(2):
            return Decision(False, Decimal(0), RejectReason.WEEKLY_LOSS_STOP)

        if state.consecutive_losses >= self.limits.max_consecutive_losses:
            return Decision(False, Decimal(0), RejectReason.LOSS_STREAK)

        if (
            state.last_data_ts is not None
            and now is not None
            and now - state.last_data_ts > max_staleness
        ):
            return Decision(False, Decimal(0), RejectReason.STALE_DATA)

        if not intent.stop_is_protective or intent.stop_distance <= 0:
            return Decision(False, Decimal(0), RejectReason.INVALID_STOP)

        if intent.symbol in state.positions:
            return Decision(False, Decimal(0), RejectReason.ALREADY_OPEN)

        if len(state.positions) >= self.limits.max_open_positions:
            return Decision(False, Decimal(0), RejectReason.MAX_POSITIONS)

        fraction = self.risk_fraction(state, intent)
        qty = self.position_size(state, intent, fraction)
        if qty <= 0:
            return Decision(False, Decimal(0), RejectReason.INVALID_STOP)

        notional = qty * intent.entry

        asset_cap = state.equity * self.limits.max_exposure_per_asset
        if notional > asset_cap:
            qty = asset_cap / intent.entry
            notional = asset_cap

        used = self.gross_exposure(state)
        total_cap = state.equity * self.limits.max_total_exposure
        if used + notional > total_cap:
            room = total_cap - used
            if room <= 0:
                return Decision(False, Decimal(0), RejectReason.TOTAL_EXPOSURE)
            qty = room / intent.entry
            notional = room

        # Correlation cap: five alt longs are one large BTC long. Capping the
        # count of positions without capping summed beta is not risk management.
        beta_used = self.beta_exposure(state)
        beta_cap = state.equity * self.limits.max_btc_beta_exposure
        beta_add = notional * Decimal(str(intent.beta))
        if beta_used + beta_add > beta_cap:
            room = beta_cap - beta_used
            if room <= 0 or intent.beta <= 0:
                return Decision(False, Decimal(0), RejectReason.BETA_EXPOSURE)
            qty = (room / Decimal(str(intent.beta))) / intent.entry
            notional = qty * intent.entry

        venue_used = self.venue_exposure(state, intent.venue)
        venue_cap = state.equity * self.limits.max_venue_exposure
        if venue_used + notional > venue_cap:
            room = venue_cap - venue_used
            if room <= 0:
                return Decision(False, Decimal(0), RejectReason.VENUE_EXPOSURE)
            qty = room / intent.entry
            notional = room

        if available_depth is not None and notional > available_depth:
            # Never take more than the book can absorb at a sane price.
            qty = available_depth / intent.entry
            notional = available_depth

        if notional < self.min_notional or qty <= 0:
            return Decision(False, Decimal(0), RejectReason.SIZE_TOO_SMALL)

        return Decision(True, qty, RejectReason.OK, risk_fraction=fraction)

    # ------------------------------------------------------------- aggregates

    @staticmethod
    def gross_exposure(state: AccountState, prices: Mapping[str, Decimal] | None = None) -> Decimal:
        return sum(
            (
                p.notional(prices.get(p.symbol) if prices else None)
                for p in state.positions.values()
            ),
            start=Decimal(0),
        )

    @staticmethod
    def beta_exposure(state: AccountState, prices: Mapping[str, Decimal] | None = None) -> Decimal:
        return sum(
            (
                p.notional(prices.get(p.symbol) if prices else None) * Decimal(str(p.beta))
                for p in state.positions.values()
            ),
            start=Decimal(0),
        )

    @staticmethod
    def venue_exposure(state: AccountState, venue: str) -> Decimal:
        return sum(
            (p.notional() for p in state.positions.values() if p.venue == venue), start=Decimal(0)
        )

    # ------------------------------------------------------- circuit breakers

    def check_circuit_breakers(
        self,
        state: AccountState,
        *,
        spread_bps: Decimal | None = None,
        median_spread_bps: Decimal | None = None,
        venue_error_rate: float | None = None,
        clock_skew_ms: float | None = None,
        bar_gap_pct: float | None = None,
        reconciliation_mismatch: bool = False,
    ) -> str | None:
        """Trip on conditions that mean the world no longer matches our model.

        Each of these has ended real accounts: trading into a blown-out spread,
        acting on a stale clock, or holding a position the exchange says you do
        not have.
        """
        if reconciliation_mismatch:
            state.trip("position reconciliation mismatch vs venue")
        elif spread_bps is not None and median_spread_bps and spread_bps > median_spread_bps * 3:
            state.trip(f"spread {spread_bps}bps > 3x median {median_spread_bps}bps")
        elif venue_error_rate is not None and venue_error_rate > 0.10:
            state.trip(f"venue error rate {venue_error_rate:.0%}")
        elif clock_skew_ms is not None and abs(clock_skew_ms) > 2000:
            state.trip(f"clock skew {clock_skew_ms:.0f}ms")
        elif bar_gap_pct is not None and abs(bar_gap_pct) > 0.08:
            state.trip(f"price gap {bar_gap_pct:.1%} in one bar")
        return state.kill_reason if state.kill_switch else None


def new_account(starting_equity: Decimal, *, ts: datetime | None = None) -> AccountState:
    _ = ts or datetime.now(UTC)
    return AccountState(
        equity=starting_equity,
        peak_equity=starting_equity,
        day_start_equity=starting_equity,
        week_start_equity=starting_equity,
    )


def trailing_stop(
    direction: int, entry: Decimal, best: Decimal, atr: Decimal, multiple: Decimal = Decimal(3)
) -> Decimal:
    """ATR trailing stop from the best price reached — never loosens.

    Ratcheting only in the favourable direction is the whole point: a stop that
    can move away from price is not a stop.
    """
    if direction > 0:
        return max(best - atr * multiple, entry - atr * multiple)
    return min(best + atr * multiple, entry + atr * multiple)
