"""The risk engine: the only component allowed to decide a size.

Strategies say *what* and *where the idea dies*. This says *how much*, and it is
the only place in the system that reads the account balance. Keeping that
knowledge in one class is what makes the risk dial actually mean something —
there is no second code path where a strategy sizes its own position and
quietly bypasses the leverage cap.

Order of operations, and it matters:

1. Guards first. If the account is halted, nothing else runs.
2. Size from the stop distance, not from a fixed notional. Position size is
   `risk_amount / stop_distance`, so a wide stop gets a small position and the
   loss is the same either way. This is the single idea that separates surviving
   from not.
3. Clamp against every ceiling: leverage, gross exposure, per-position notional,
   available margin, venue minimums.
4. Reject rather than shrink to something meaningless.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, Decimal

from simin.core.types import (
    Direction,
    ExitReason,
    Intent,
    Position,
    Symbol,
    Trade,
    ZERO,
)
from simin.risk.dial import RiskProfile


class Halt(enum.StrEnum):
    NONE = "none"
    DAILY_LOSS = "daily_loss"
    MAX_DRAWDOWN = "max_drawdown"
    LOSS_STREAK = "loss_streak"
    KILL_SWITCH = "kill_switch"
    NO_CAPITAL = "no_capital"

    @property
    def is_halted(self) -> bool:
        return self is not Halt.NONE

    @property
    def is_permanent(self) -> bool:
        """Drawdown and kill-switch halts need a human; the others expire."""
        return self in (Halt.MAX_DRAWDOWN, Halt.KILL_SWITCH)


class Rejection(enum.StrEnum):
    NONE = "none"
    HALTED = "halted"
    NO_STOP = "no_stop"
    STOP_TOO_CLOSE = "stop_too_close"
    STOP_TOO_WIDE = "stop_too_wide"
    MAX_POSITIONS = "max_positions"
    MAX_EXPOSURE = "max_exposure"
    INSUFFICIENT_MARGIN = "insufficient_margin"
    BELOW_MIN_NOTIONAL = "below_min_notional"
    BELOW_MIN_QTY = "below_min_qty"
    DAILY_TRADE_LIMIT = "daily_trade_limit"
    COOLDOWN = "cooldown"
    ALREADY_IN_POSITION = "already_in_position"
    SHORTS_DISABLED = "shorts_disabled"
    CAPITAL_CAP = "capital_cap"


@dataclass(frozen=True, slots=True)
class Sizing:
    """The engine's answer. `qty == 0` means refused, and `reason` says why."""

    qty: Decimal
    leverage: Decimal
    stop_price: Decimal
    take_profit: Decimal | None
    risk_amount: Decimal
    notional: Decimal
    margin_required: Decimal
    liquidation_price: Decimal
    rejection: Rejection = Rejection.NONE
    note: str = ""

    @property
    def approved(self) -> bool:
        return self.qty > 0 and self.rejection is Rejection.NONE

    @classmethod
    def refuse(cls, reason: Rejection, note: str = "") -> Sizing:
        return cls(ZERO, Decimal(1), ZERO, None, ZERO, ZERO, ZERO, ZERO, reason, note)


@dataclass(slots=True)
class AccountState:
    """Everything the risk engine needs to know about the account.

    `equity` is cash plus unrealised PnL. Sizing from equity rather than cash is
    what makes the account de-risk automatically in a drawdown: lose 20% and
    every subsequent position is 20% smaller, which is the mechanism that turns
    a losing streak into a decaying curve instead of a straight line to zero.
    """

    cash: Decimal
    equity: Decimal
    peak_equity: Decimal
    positions: dict[str, Position] = field(default_factory=dict)
    #: Realised PnL today, and the equity this UTC day opened at.
    day: date = field(default_factory=lambda: datetime.now(UTC).date())
    day_start_equity: Decimal = ZERO
    day_realised: Decimal = ZERO
    trades_today: int = 0
    loss_streak: int = 0
    #: Bar index of the last exit per symbol, for the cooldown.
    last_exit_bar: dict[str, int] = field(default_factory=dict)
    halt: Halt = Halt.NONE
    halt_note: str = ""

    @property
    def drawdown(self) -> Decimal:
        if self.peak_equity <= 0:
            return ZERO
        return max(ZERO, (self.peak_equity - self.equity) / self.peak_equity)

    @property
    def gross_exposure(self) -> Decimal:
        return sum((p.notional for p in self.positions.values()), ZERO)

    @property
    def used_margin(self) -> Decimal:
        return sum((p.margin for p in self.positions.values()), ZERO)

    @property
    def free_margin(self) -> Decimal:
        return max(ZERO, self.cash - self.used_margin)

    def roll_day(self, now: datetime) -> None:
        """Start a new UTC day: reset the daily counters and lift a daily halt.

        A daily-loss halt that never lifts is a permanent one, which is not what
        the dial promises.
        """
        today = now.astimezone(UTC).date()
        if today == self.day:
            return
        self.day = today
        self.day_start_equity = self.equity
        self.day_realised = ZERO
        self.trades_today = 0
        if self.halt is Halt.DAILY_LOSS:
            self.halt = Halt.NONE
            self.halt_note = ""


class RiskEngine:
    """Sizing plus circuit breakers, configured entirely by one `RiskProfile`."""

    __slots__ = ("profile", "_max_capital", "_recovering")

    #: A stop closer than this fraction of price is noise, not a stop: it will
    #: be hit by the spread. Sizing against it produces an absurd position.
    MIN_STOP_FRACTION = Decimal("0.0015")
    #: A stop further than this means the strategy has no real idea where it is
    #: wrong; the resulting position is too small to matter anyway.
    MAX_STOP_FRACTION = Decimal("0.25")
    #: Fraction of free margin a single position may post. The remainder covers
    #: entry and exit commissions and any funding accrued while the position is
    #: open — all of which are debited from the same balance the margin sits in.
    MARGIN_UTILISATION = Decimal("0.97")

    def __init__(self, profile: RiskProfile, max_capital: Decimal = ZERO) -> None:
        self.profile = profile
        self._max_capital = max_capital
        self._recovering = False

    # --- Guards -----------------------------------------------------------

    def check_halts(self, acct: AccountState, frozen: bool = False) -> Halt:
        """Evaluate every circuit breaker. Called before anything else, always."""
        if frozen:
            return Halt.KILL_SWITCH
        if acct.halt.is_permanent:
            return acct.halt
        if acct.equity <= 0:
            return Halt.NO_CAPITAL

        p = self.profile
        if acct.drawdown >= p.max_drawdown_halt:
            return Halt.MAX_DRAWDOWN

        base = acct.day_start_equity if acct.day_start_equity > 0 else acct.equity
        if base > 0 and acct.day_realised < 0:
            day_loss = -acct.day_realised / base
            if day_loss >= p.daily_loss_halt:
                return Halt.DAILY_LOSS

        if acct.loss_streak >= p.loss_streak_halt:
            return Halt.LOSS_STREAK
        return Halt.NONE

    def effective_profile(self, acct: AccountState) -> RiskProfile:
        """The profile actually in force, de-risked while recovering.

        After a loss-streak halt clears, going straight back to full size is
        how a bad day becomes a bad week. Size comes back only after the
        streak is broken by a winner.
        """
        if acct.loss_streak >= max(2, self.profile.loss_streak_halt - 1):
            return self.profile.scaled(self.profile.recovery_risk_factor)
        return self.profile

    # --- Sizing -----------------------------------------------------------

    def size(
        self,
        intent: Intent,
        symbol: Symbol,
        price: Decimal,
        acct: AccountState,
        bar_index: int,
        atr: Decimal | None = None,
        frozen: bool = False,
    ) -> Sizing:
        direction = intent.direction
        if direction is None:
            return Sizing.refuse(Rejection.NONE, "no directional intent")

        halt = self.check_halts(acct, frozen)
        if halt.is_halted:
            return Sizing.refuse(Rejection.HALTED, halt.value)

        p = self.effective_profile(acct)

        if direction is Direction.SHORT and not p.allow_shorts:
            return Sizing.refuse(Rejection.SHORTS_DISABLED, f"level {p.level} is long-only")
        if symbol.name in acct.positions:
            return Sizing.refuse(Rejection.ALREADY_IN_POSITION, symbol.name)
        if len(acct.positions) >= p.max_concurrent_positions:
            return Sizing.refuse(
                Rejection.MAX_POSITIONS, f"{len(acct.positions)}/{p.max_concurrent_positions}"
            )
        if acct.trades_today >= p.max_trades_per_day:
            return Sizing.refuse(
                Rejection.DAILY_TRADE_LIMIT, f"{acct.trades_today}/{p.max_trades_per_day}"
            )
        last_exit = acct.last_exit_bar.get(symbol.name)
        if last_exit is not None and bar_index - last_exit < p.cooldown_bars:
            return Sizing.refuse(
                Rejection.COOLDOWN, f"{bar_index - last_exit}/{p.cooldown_bars} bars"
            )

        stop = intent.stop_price
        if stop is None or stop <= 0:
            return Sizing.refuse(Rejection.NO_STOP)

        stop_distance = abs(price - stop)
        if stop_distance <= 0:
            return Sizing.refuse(Rejection.STOP_TOO_CLOSE, "stop equals entry")
        fraction = stop_distance / price
        if fraction < self.MIN_STOP_FRACTION:
            return Sizing.refuse(
                Rejection.STOP_TOO_CLOSE, f"{fraction:.4%} of price"
            )
        if fraction > self.MAX_STOP_FRACTION:
            return Sizing.refuse(Rejection.STOP_TOO_WIDE, f"{fraction:.2%} of price")

        # The core sizing identity. Everything after this only shrinks it.
        risk_amount = acct.equity * p.risk_per_trade
        qty = risk_amount / stop_distance
        notional = qty * price

        # Leverage: use only as much as this position actually needs, never
        # more than the dial permits. A position whose notional fits inside the
        # free margin does not need leverage at all, and unused leverage is
        # unused liquidation risk.
        cap_leverage = min(p.max_leverage, Decimal(max(symbol.max_leverage, 1)))
        # Pick the least leverage that still fits, which keeps the liquidation
        # price as far away as possible. "Fits" means inside the usable margin,
        # not all of it — sizing against the full balance leaves nothing to pay
        # the commission and lands the order fractionally short every time.
        usable = acct.free_margin * self.MARGIN_UTILISATION
        needed = notional / usable if usable > 0 else cap_leverage
        leverage = max(Decimal(1), min(needed, cap_leverage)).quantize(Decimal("0.01"))

        notes: list[str] = []

        # Ceiling 1: single-position notional.
        max_single = acct.equity * cap_leverage * p.max_position_notional_pct
        if notional > max_single:
            notional = max_single
            notes.append("capped by per-position limit")

        # Ceiling 2: gross exposure across everything open.
        room = acct.equity * p.max_gross_exposure - acct.gross_exposure
        if room <= 0:
            return Sizing.refuse(
                Rejection.MAX_EXPOSURE,
                f"gross {acct.gross_exposure:.0f} at limit {acct.equity * p.max_gross_exposure:.0f}",
            )
        if notional > room:
            notional = room
            notes.append("capped by gross exposure")

        # Ceiling 3: the absolute capital cap, independent of the dial.
        if self._max_capital > 0:
            allowed = self._max_capital - acct.gross_exposure
            if allowed <= 0:
                return Sizing.refuse(Rejection.CAPITAL_CAP, f"cap {self._max_capital}")
            if notional > allowed:
                notional = allowed
                notes.append("capped by SIMIN_MAX_CAPITAL")

        # Ceiling 4: margin actually available, less a buffer.
        #
        # Posting *all* free margin leaves nothing to pay the entry fee, so the
        # order is short by exactly the commission and the venue rejects it. On
        # the paper adapter that showed up as free capital of −53.34 on a
        # hundred-million-dollar account: a maximally sized position, refused by
        # the width of its own fee. A real venue rejects it too.
        margin = notional / leverage
        if margin > usable:
            notional = usable * leverage
            margin = usable
            notes.append("capped by free margin")

        if notional <= 0:
            return Sizing.refuse(Rejection.INSUFFICIENT_MARGIN, f"free={acct.free_margin:.2f}")

        qty = self._round_qty(notional / price, symbol)
        if qty <= 0 or qty < symbol.min_qty:
            return Sizing.refuse(
                Rejection.BELOW_MIN_QTY, f"{qty} < venue minimum {symbol.min_qty}"
            )
        notional = qty * price
        if symbol.min_notional > 0 and notional < symbol.min_notional:
            return Sizing.refuse(
                Rejection.BELOW_MIN_NOTIONAL, f"{notional:.2f} < {symbol.min_notional}"
            )

        margin = notional / leverage
        # Recompute the realised risk: rounding down the quantity means the
        # trade risks slightly less than the budget, which is the safe direction.
        actual_risk = qty * stop_distance

        take_profit = intent.take_profit
        if take_profit is None:
            tp_distance = stop_distance * p.take_profit_r
            take_profit = (
                price + tp_distance if direction is Direction.LONG else price - tp_distance
            )

        probe = Position(
            symbol=symbol.name,
            direction=direction,
            qty=qty,
            entry_price=price,
            stop_price=stop,
            take_profit=take_profit,
            leverage=leverage,
            opened_at=datetime.now(UTC),
            strategy=intent.strategy,
            risk_level=p.level,
            risk_amount=actual_risk,
        )
        liq = probe.liquidation_price()

        # If the venue would liquidate before our stop is reached, the stop is
        # decorative. Refuse rather than pretend the risk is bounded.
        if leverage > 1 and liq > 0:
            doomed = (
                liq >= stop if direction is Direction.LONG else (liq <= stop and liq > 0)
            )
            if doomed:
                return Sizing.refuse(
                    Rejection.INSUFFICIENT_MARGIN,
                    f"liquidation {liq:.4f} would trigger before stop {stop:.4f} "
                    f"at {leverage}x — stop would never execute",
                )

        return Sizing(
            qty=qty,
            leverage=leverage,
            stop_price=stop,
            take_profit=take_profit,
            risk_amount=actual_risk,
            notional=notional,
            margin_required=margin,
            liquidation_price=liq,
            note="; ".join(notes),
        )

    @staticmethod
    def _round_qty(qty: Decimal, symbol: Symbol) -> Decimal:
        """Round DOWN to the venue's precision. Rounding up risks more than budgeted."""
        step = Decimal(1).scaleb(-symbol.qty_precision)
        return qty.quantize(step, rounding=ROUND_DOWN)

    # --- Position management ---------------------------------------------

    def manage(
        self, pos: Position, high: Decimal, low: Decimal, close: Decimal, atr: Decimal | None
    ) -> tuple[Decimal, ExitReason | None]:
        """Update a live position's stop and decide whether it should close.

        Returns `(new_stop, exit_reason_or_None)`. Checks stop before target:
        within a single bar we cannot know which came first, so we assume the
        worse one. A backtester that assumes the good fill is a backtester that
        prints money that does not exist.
        """
        p = self.profile
        pos.update_excursions(high, low)
        stop = pos.stop_price
        long = pos.direction is Direction.LONG

        # 1. Liquidation, if leveraged. Nothing survives this.
        if pos.leverage > 1:
            liq = pos.liquidation_price()
            if liq > 0 and ((long and low <= liq) or (not long and high >= liq)):
                return stop, ExitReason.LIQUIDATION

        # 2. Stop, assumed hit first within the bar.
        if (long and low <= stop) or (not long and high >= stop):
            reason = (
                ExitReason.TRAILING_STOP
                if pos.breakeven_armed and stop != pos.initial_stop
                else ExitReason.STOP_LOSS
            )
            return stop, reason

        # 3. Target.
        if pos.take_profit is not None:
            if (long and high >= pos.take_profit) or (not long and low <= pos.take_profit):
                return stop, ExitReason.TAKE_PROFIT

        # 4. Time stop: an idea that has not worked in N bars is a dead idea
        #    holding capital hostage.
        if p.time_stop_bars > 0 and pos.bars_held >= p.time_stop_bars:
            if pos.r_multiple(close) < Decimal("0.3"):
                return stop, ExitReason.TIME_STOP

        # 5. Breakeven, then trail. Only after the trade has proven itself.
        r = pos.r_multiple(close)
        if p.breakeven_at_r > 0 and not pos.breakeven_armed and r >= p.breakeven_at_r:
            pos.breakeven_armed = True
            be = pos.entry_price
            stop = max(stop, be) if long else min(stop, be)

        if pos.breakeven_armed and p.trail_atr_mult > 0 and atr and atr > 0:
            trail = (
                close - p.trail_atr_mult * atr if long else close + p.trail_atr_mult * atr
            )
            # A trailing stop only ever tightens. Loosening it turns a winner
            # into a loser and is the most common bug in this kind of code.
            stop = max(stop, trail) if long else min(stop, trail)

        return stop, None

    def should_flip(self, pos: Position, intent: Intent) -> bool:
        """Close on a confident signal in the opposite direction."""
        d = intent.direction
        if d is None or d is pos.direction:
            return False
        return intent.confidence >= max(self.profile.min_confluence + 0.10, 0.55)

    # --- Bookkeeping ------------------------------------------------------

    def record_close(self, acct: AccountState, trade: Trade, bar_index: int) -> None:
        acct.day_realised += trade.net_pnl
        acct.last_exit_bar[trade.symbol] = bar_index
        if trade.net_pnl < 0:
            acct.loss_streak += 1
        else:
            acct.loss_streak = 0
        halt = self.check_halts(acct)
        if halt.is_halted and not acct.halt.is_halted:
            acct.halt = halt
            acct.halt_note = f"triggered after {trade.symbol} closed at {trade.net_pnl:.2f}"


def summarise_rejections(rejections: Sequence[Rejection]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rejections:
        if r is not Rejection.NONE:
            counts[r.value] = counts.get(r.value, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
