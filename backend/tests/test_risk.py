"""The risk dial and the engine that enforces it.

These are the tests that decide whether "risk level 5" means anything. If
sizing can exceed the dial's leverage cap, or a halt can be bypassed, then the
number the user picked is decoration.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from simin.core.types import Direction, Intent, Position, Signal
from simin.exchanges.base import Exchange
from simin.exchanges.registry import adapt_profile
from simin.risk.dial import (
    MAX_LEVEL,
    MIN_LEVEL,
    all_profiles,
    ladder,
    profile,
    spot_only,
)
from simin.risk.engine import AccountState, Halt, Rejection, RiskEngine


def account(equity: str = "10000") -> AccountState:
    e = Decimal(equity)
    return AccountState(cash=e, equity=e, peak_equity=e, day_start_equity=e)


def long_intent(stop: str = "98") -> Intent:
    return Intent(Signal.LONG, 0.9, stop_price=Decimal(stop), strategy="test")


class TestDial:
    def test_ten_levels_exist(self) -> None:
        assert len(all_profiles()) == 10
        assert [p.level for p in all_profiles()] == list(range(MIN_LEVEL, MAX_LEVEL + 1))

    def test_out_of_range_is_rejected(self) -> None:
        for bad in (0, 11, -1, "x", None):
            with pytest.raises(ValueError):
                profile(bad)  # type: ignore[arg-type]

    def test_risk_rises_monotonically(self) -> None:
        ps = all_profiles()
        for a, b in zip(ps, ps[1:], strict=False):
            assert b.risk_per_trade > a.risk_per_trade
            assert b.max_leverage >= a.max_leverage
            assert b.max_drawdown_halt >= a.max_drawdown_halt
            assert b.daily_loss_halt >= a.daily_loss_halt
            assert b.target_monthly_return > a.target_monthly_return

    def test_selectivity_falls_as_risk_rises(self) -> None:
        """Higher risk must mean *more* trades, which means a looser filter and
        a tighter stop. If these moved the other way the dial would be
        cosmetic."""
        ps = all_profiles()
        for a, b in zip(ps, ps[1:], strict=False):
            assert b.min_confluence <= a.min_confluence
            assert b.atr_stop_mult <= a.atr_stop_mult
            assert b.max_trades_per_day >= a.max_trades_per_day

    def test_aggressive_levels_carry_warnings(self) -> None:
        for p in all_profiles():
            if p.level >= 7:
                assert p.warnings_en, f"level {p.level} has no warning"
                assert p.warnings_fa, f"level {p.level} has no Persian warning"

    def test_headline_pairs_target_with_measurement(self) -> None:
        head = profile(10).headline()
        assert head["target_monthly_return"] == 2.0
        # Uncalibrated must be null, never zero — a zero rendered where "not
        # measured" belongs is the exact lie the design exists to prevent.
        assert head["measured_monthly_return"] is None
        assert head["calibrated"] is False

    def test_ladder_serialises(self) -> None:
        rows = ladder()
        assert len(rows) == 10
        assert all("target_monthly_return" in r for r in rows)

    def test_spot_only_clamps_and_explains(self) -> None:
        clamped = spot_only(profile(9))
        assert clamped.max_leverage == 1
        assert clamped.allow_shorts is False
        assert any("spot-only" in w for w in clamped.warnings_en)
        assert len(clamped.warnings_fa) > len(profile(9).warnings_fa)

    def test_spot_only_is_a_noop_on_unlevered_levels(self) -> None:
        assert spot_only(profile(1)) is profile(1)


class TestSizing:
    def test_risk_is_constant_regardless_of_stop_width(self, symbol) -> None:
        """The core identity. A wide stop gets a small position and the loss is
        the same either way — this is what separates surviving from not."""
        engine = RiskEngine(profile(5))
        risks = []
        for stop in ("99", "98", "95", "90"):
            sizing = engine.size(long_intent(stop), symbol, Decimal("100"), account(), 100)
            assert sizing.approved
            risks.append(sizing.risk_amount)
        for r in risks:
            assert r == pytest.approx(Decimal("150"), abs=Decimal("0.5"))

    def test_size_scales_with_the_dial(self, symbol) -> None:
        sizes = [
            RiskEngine(profile(lv))
            .size(long_intent(), symbol, Decimal("100"), account(), 100)
            .qty
            for lv in range(1, 11)
        ]
        assert sizes == sorted(sizes)
        assert sizes[-1] > sizes[0] * 10

    def test_leverage_never_exceeds_the_cap(self, symbol) -> None:
        for lv in range(1, 11):
            p = profile(lv)
            sizing = RiskEngine(p).size(
                long_intent("99.7"), symbol, Decimal("100"), account(), 100
            )
            if sizing.approved:
                assert sizing.leverage <= p.max_leverage

    def test_venue_leverage_ceiling_is_respected(self, symbol) -> None:
        from dataclasses import replace

        spot = replace(symbol, max_leverage=1)
        sizing = RiskEngine(profile(10)).size(
            long_intent("99.8"), spot, Decimal("100"), account(), 100
        )
        if sizing.approved:
            assert sizing.leverage == 1

    def test_quantity_rounds_down(self, symbol) -> None:
        """Rounding up risks more than budgeted, which is the one direction
        that must never happen."""
        sizing = RiskEngine(profile(5)).size(
            long_intent("97.3"), symbol, Decimal("100.7"), account("9999.37"), 100
        )
        assert sizing.approved
        assert sizing.risk_amount <= Decimal("9999.37") * profile(5).risk_per_trade

    def test_refuses_when_liquidation_precedes_the_stop(self, symbol) -> None:
        """If the venue liquidates before the stop is reached, the stop is
        decorative and the loss is unbounded."""
        engine = RiskEngine(profile(10))
        acct = account("100000")
        # A very tight stop forces maximum leverage.
        sizing = engine.size(
            Intent(Signal.LONG, 0.9, stop_price=Decimal("99.85")), symbol,
            Decimal("100"), acct, 100,
        )
        if sizing.approved and sizing.leverage > 1:
            assert sizing.liquidation_price < sizing.stop_price


class TestRejections:
    @pytest.mark.parametrize(
        "setup,expected",
        [
            (lambda a: setattr(a, "trades_today", 99), Rejection.DAILY_TRADE_LIMIT),
            (lambda a: a.last_exit_bar.update({"BTCUSDT": 99}), Rejection.COOLDOWN),
        ],
    )
    def test_guards(self, symbol, setup, expected) -> None:
        acct = account()
        setup(acct)
        sizing = RiskEngine(profile(5)).size(
            long_intent(), symbol, Decimal("100"), acct, 100
        )
        assert sizing.rejection is expected

    def test_no_stop_is_refused(self, symbol) -> None:
        sizing = RiskEngine(profile(5)).size(
            Intent(Signal.LONG, 0.9, stop_price=None), symbol, Decimal("100"), account(), 100
        )
        assert sizing.rejection is Rejection.NO_STOP

    def test_stop_too_close_is_refused(self, symbol) -> None:
        """A stop inside the spread is not a stop; sizing against it produces
        an absurd position."""
        sizing = RiskEngine(profile(5)).size(
            long_intent("99.99"), symbol, Decimal("100"), account(), 100
        )
        assert sizing.rejection is Rejection.STOP_TOO_CLOSE

    def test_stop_too_wide_is_refused(self, symbol) -> None:
        sizing = RiskEngine(profile(5)).size(
            long_intent("50"), symbol, Decimal("100"), account(), 100
        )
        assert sizing.rejection is Rejection.STOP_TOO_WIDE

    def test_shorts_refused_on_long_only_levels(self, symbol) -> None:
        sizing = RiskEngine(profile(2)).size(
            Intent(Signal.SHORT, 0.9, stop_price=Decimal("102")), symbol,
            Decimal("100"), account(), 100,
        )
        assert sizing.rejection is Rejection.SHORTS_DISABLED

    def test_max_positions(self, symbol) -> None:
        acct = account()
        for i in range(profile(5).max_concurrent_positions):
            acct.positions[f"X{i}"] = Position(
                symbol=f"X{i}", direction=Direction.LONG, qty=Decimal("1"),
                entry_price=Decimal("100"), stop_price=Decimal("98"),
                take_profit=None, leverage=Decimal("1"),
                opened_at=datetime.now(UTC), strategy="t", risk_level=5,
            )
        sizing = RiskEngine(profile(5)).size(
            long_intent(), symbol, Decimal("100"), acct, 100
        )
        assert sizing.rejection is Rejection.MAX_POSITIONS

    def test_already_in_position(self, symbol) -> None:
        acct = account()
        acct.positions["BTCUSDT"] = Position(
            symbol="BTCUSDT", direction=Direction.LONG, qty=Decimal("1"),
            entry_price=Decimal("100"), stop_price=Decimal("98"), take_profit=None,
            leverage=Decimal("1"), opened_at=datetime.now(UTC), strategy="t", risk_level=5,
        )
        sizing = RiskEngine(profile(5)).size(
            long_intent(), symbol, Decimal("100"), acct, 100
        )
        assert sizing.rejection is Rejection.ALREADY_IN_POSITION

    def test_capital_cap_is_independent_of_the_dial(self, symbol) -> None:
        engine = RiskEngine(profile(10), max_capital=Decimal("50"))
        sizing = engine.size(long_intent(), symbol, Decimal("100"), account(), 100)
        assert not sizing.approved or sizing.notional <= Decimal("50")


class TestHalts:
    def test_drawdown_halt(self) -> None:
        acct = account()
        acct.equity = Decimal("7900")  # 21% down; level 5 halts at 20%
        assert RiskEngine(profile(5)).check_halts(acct) is Halt.MAX_DRAWDOWN

    def test_daily_loss_halt(self) -> None:
        acct = account()
        acct.day_realised = Decimal("-450")  # 4.5%; level 5 halts at 4%
        assert RiskEngine(profile(5)).check_halts(acct) is Halt.DAILY_LOSS

    def test_loss_streak_halt(self) -> None:
        acct = account()
        acct.loss_streak = profile(5).loss_streak_halt
        assert RiskEngine(profile(5)).check_halts(acct) is Halt.LOSS_STREAK

    def test_kill_switch_beats_everything(self) -> None:
        assert RiskEngine(profile(1)).check_halts(account(), frozen=True) is Halt.KILL_SWITCH

    def test_zero_equity_halts(self) -> None:
        acct = account()
        acct.equity = Decimal("0")
        assert RiskEngine(profile(5)).check_halts(acct) is Halt.NO_CAPITAL

    def test_drawdown_halt_needs_a_human_but_daily_does_not(self) -> None:
        assert Halt.MAX_DRAWDOWN.is_permanent
        assert Halt.KILL_SWITCH.is_permanent
        assert not Halt.DAILY_LOSS.is_permanent

    def test_daily_halt_lifts_on_a_new_day(self) -> None:
        """A daily halt that never lifts is a permanent one, which is not what
        the dial promises."""
        acct = account()
        acct.day_realised = Decimal("-450")
        acct.halt = Halt.DAILY_LOSS
        acct.roll_day(datetime.now(UTC) + timedelta(days=1))
        assert acct.halt is Halt.NONE
        assert acct.day_realised == 0

    def test_recovery_reduces_size(self) -> None:
        engine = RiskEngine(profile(5))
        acct = account()
        acct.loss_streak = 4
        assert engine.effective_profile(acct).risk_per_trade < profile(5).risk_per_trade

    def test_halted_account_cannot_size(self, symbol) -> None:
        acct = account()
        acct.equity = Decimal("5000")  # deep drawdown
        sizing = RiskEngine(profile(5)).size(
            long_intent(), symbol, Decimal("100"), acct, 100
        )
        assert sizing.rejection is Rejection.HALTED


class TestPositionManagement:
    def _position(self) -> Position:
        return Position(
            symbol="BTCUSDT", direction=Direction.LONG, qty=Decimal("10"),
            entry_price=Decimal("100"), stop_price=Decimal("98"),
            take_profit=Decimal("110"), leverage=Decimal("3"),
            opened_at=datetime.now(UTC), strategy="t", risk_level=5,
            risk_amount=Decimal("20"), initial_stop=Decimal("98"),
        )

    def test_trailing_stop_only_tightens(self) -> None:
        """Loosening a trailing stop turns a winner into a loser and is the most
        common bug in this kind of code."""
        engine = RiskEngine(profile(5))
        pos = self._position()
        seen = []
        for close in ("102", "105", "103", "101"):
            c = Decimal(close)
            stop, _ = engine.manage(pos, c + 1, c - 1, c, Decimal("1"))
            pos.stop_price = stop
            seen.append(stop)
        assert seen == sorted(seen)

    def test_stop_is_checked_before_target(self) -> None:
        """Within one bar we cannot know the path. Assuming the good fill is how
        a losing system backtests profitably."""
        engine = RiskEngine(profile(5))
        pos = self._position()
        _, reason = engine.manage(pos, Decimal("115"), Decimal("97"), Decimal("100"), Decimal("1"))
        assert reason is not None and reason.value == "stop_loss"

    def test_liquidation_takes_precedence_over_the_stop(self) -> None:
        engine = RiskEngine(profile(5))
        pos = self._position()
        pos.leverage = Decimal("10")
        pos.stop_price = Decimal("80")
        liq = pos.liquidation_price()
        _, reason = engine.manage(pos, Decimal("100"), liq - 1, liq - 1, Decimal("1"))
        assert reason is not None and reason.value == "liquidation"

    def test_breakeven_arms_then_holds(self) -> None:
        engine = RiskEngine(profile(5))
        pos = self._position()
        target = pos.entry_price + Decimal("2") * profile(5).breakeven_at_r
        stop, _ = engine.manage(pos, target + 1, target - 1, target, Decimal("0.5"))
        assert pos.breakeven_armed
        assert stop >= pos.entry_price


class TestVenueAdaptation:
    def test_futures_venue_keeps_leverage(self) -> None:
        class Futures(Exchange):
            name = "f"
            kinds = __import__("simin.core.types", fromlist=["MarketKind"]).MarketKind.SPOT, \
                    __import__("simin.core.types", fromlist=["MarketKind"]).MarketKind.FUTURES

            async def symbols(self): return ()
            async def candles(self, *a, **k): return []
            async def ticker(self, s): raise NotImplementedError
            async def fees(self, s): raise NotImplementedError
            async def balances(self): return {}
            async def place_order(self, *a, **k): raise NotImplementedError
            async def cancel_order(self, *a): return False
            async def get_order(self, *a): raise NotImplementedError

        assert adapt_profile(profile(9), Futures()).max_leverage == profile(9).max_leverage
