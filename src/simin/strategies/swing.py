"""Swing strategies: frequent entries, short holds, hard 4-day ceiling.

Designed against a measured result rather than a preference. On four years of
hourly BTC/ETH/SOL data the conditional edge of a directional signal is:

    holding    gross edge per trade    t-stat
       1h            ~0.00%             0.2      <- indistinguishable from zero
       4h             0.01%             0.7
      12h             0.06%             4.9
      48h             0.27%            11.1
      96h             0.36%            10.1

Cost is charged once per round trip no matter how long the position is held, so
the edge has to grow past the cost before a trade is worth taking. At a 0.20%
round trip that happens around 48 hours. Below ~12 hours there is nothing to
capture on any venue — the signal itself is zero, so no amount of execution
skill rescues it.

The response to "trade more often" is therefore breadth, not frequency: run the
same 2-4 day signal across many symbols and entries arrive daily while every
individual position still respects the ceiling.
"""

from __future__ import annotations

import math
from decimal import Decimal

from simin.risk.engine import Intent
from simin.strategies.base import Strategy, StrategyContext

HOURS_PER_YEAR = 8760

#: Hours the position may live before it is closed regardless of P&L. The user
#: constraint (4 days) and the measured edge curve agree here, which is lucky:
#: past ~96h the marginal edge per extra hour flattens out.
MAX_HOLD_HOURS = 96


def horizon_stop(
    ctx: StrategyContext, entry: Decimal, hold_hours: int, k: float = 1.5
) -> Decimal | None:
    """Stop distance scaled to the *intended holding time*, not to one bar.

    This is the correction that makes a multi-day strategy coherent. A stop set
    at 2x the hourly ATR while aiming to hold for four days is a contradiction:
    price wanders several hourly ATRs within a single day, so the position is
    removed long before the thesis has a chance to be right or wrong. Measured
    on real hourly data, that mismatch pinned average holding time at ~10 hours
    against a 96-hour ceiling.

    Volatility scales with the square root of time, so the expected move over
    the holding window is ``annual_vol * sqrt(hours / 8760)``. The stop is a
    multiple of that.
    """
    vol = ctx.feature("vol24")
    if vol is None or vol <= 0:
        return None
    sigma = vol * math.sqrt(hold_hours / HOURS_PER_YEAR)
    distance = Decimal(str(sigma * k)) * entry
    if distance <= 0:
        return None
    return entry - distance


class SwingMomentum(Strategy):
    """Enter on multi-day momentum confirmed by trend quality; exit fast.

    Deliberately *not* an intraday strategy. Entries are frequent across a
    portfolio because many symbols are scanned, not because any one symbol is
    traded repeatedly within a day.
    """

    name = "swing_momentum"
    warmup = 200
    #: Stop distance in standard deviations of the expected 4-day move.
    stop_sigma = 1.5

    def __init__(
        self,
        lookback_feature: str = "mom48",
        min_momentum: float = 0.005,
        min_quality: float = 0.20,
        max_rsi: float = 82.0,
        prefer_hours: tuple[int, ...] = (),
    ) -> None:
        self.lookback_feature = lookback_feature
        self.min_momentum = min_momentum
        self.min_quality = min_quality
        # Chasing a vertical move is how a momentum system buys the top tick.
        self.max_rsi = max_rsi
        #: Optional UTC entry hours. On real data 22:00 UTC is the strongest
        #: hour for BTC (+0.036%/bar, t=2.8) and SOL (+0.041%, t=2.3), and 13:00
        #: is the weakest for both. Empty means no time filter.
        self.prefer_hours = prefer_hours

    def generate(self, ctx: StrategyContext) -> Intent | None:
        vals = ctx.require(self.lookback_feature, "trend_q20", "rsi14", "close")
        if vals is None:
            return None
        momentum, quality, rsi, close = vals
        if self.prefer_hours and ctx.ts.hour not in self.prefer_hours:
            return None
        if momentum < self.min_momentum or quality < self.min_quality:
            return None
        if rsi > self.max_rsi:
            return None
        entry = Decimal(str(close))
        stop = horizon_stop(ctx, entry, MAX_HOLD_HOURS, self.stop_sigma)
        if stop is None:
            return None
        # Confidence scales with how far past the threshold the signal is, so
        # the risk engine sizes a marginal signal smaller than a decisive one.
        strength = min(1.0, momentum / (self.min_momentum * 6))
        return Intent(
            ts=ctx.ts, symbol=ctx.symbol, direction=1, entry=entry, stop=stop,
            strategy=self.name, regime=ctx.regime.regime,
            confidence=max(0.2, strength * min(1.0, quality * 2)),
        )

    def exit_signal(self, ctx: StrategyContext) -> bool:
        """Leave when the reason for being there is decisively gone.

        The thresholds are not cosmetic. Exiting on any dip below zero closed
        positions after ~8 hours on real hourly data, which sits in the part of
        the horizon curve where the edge is statistically zero — the strategy
        was paying the round trip to capture nothing. Requiring a *decisive*
        flip keeps the average hold in the 2-4 day band where the edge lives.
        """
        vals = ctx.require(self.lookback_feature, "trend_q20")
        if vals is None:
            return False
        momentum, quality = vals
        return momentum < -self.exit_momentum or quality < -self.exit_quality

    #: How far the signal must reverse before the position is abandoned.
    exit_momentum: float = 0.02
    exit_quality: float = 0.25


class SwingPullback(Strategy):
    """Buy a dip inside an established uptrend, exit into strength.

    Complements SwingMomentum: it fires when momentum is positive but price has
    pulled back, so the two rarely enter on the same bar and together they
    produce entries on more days than either alone.
    """

    name = "swing_pullback"
    warmup = 200
    stop_sigma = 1.2

    def __init__(self, min_trend: float = 0.01, entry_z: float = -1.0, max_z: float = -0.2) -> None:
        self.min_trend = min_trend
        self.entry_z = entry_z
        self.max_z = max_z

    def generate(self, ctx: StrategyContext) -> Intent | None:
        vals = ctx.require("ema_spread", "z20", "mom48", "close")
        if vals is None:
            return None
        trend, z, momentum, close = vals
        if trend < self.min_trend or momentum < 0:
            return None
        # Pulled back, but not collapsing: a z below entry_z is a broken trend,
        # not a dip, and buying it is how "buy the dip" becomes "catch the knife".
        if not (self.entry_z <= z <= self.max_z):
            return None
        entry = Decimal(str(close))
        # A pullback entry starts closer to its invalidation level, so it can
        # afford a tighter horizon multiple than a momentum entry.
        stop = horizon_stop(ctx, entry, MAX_HOLD_HOURS, self.stop_sigma)
        if stop is None:
            return None
        return Intent(
            ts=ctx.ts, symbol=ctx.symbol, direction=1, entry=entry, stop=stop,
            strategy=self.name, regime=ctx.regime.regime,
            confidence=min(1.0, abs(z) * 0.6 + min(momentum, 0.1) * 4),
        )

    def exit_signal(self, ctx: StrategyContext) -> bool:
        vals = ctx.require("z20", "mom48")
        if vals is None:
            return False
        z, momentum = vals
        # Take the bounce into strength, or leave when the trend it was a dip
        # inside has actually broken.
        return z > 1.5 or momentum < -0.03
