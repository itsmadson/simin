"""The v1 strategy set and the benchmarks they must beat.

Every strategy here is deliberately simple and has few tunable parameters. The
research is explicit about why: each extra knob multiplies the number of
effective trials, which inflates the best backtest you will find and deflates
its Sharpe once corrected (docs/01 §1.1, docs/03 §3).

The benchmark family exists to answer the only question that matters about any
of them — *is this better than something trivial?*
"""

from __future__ import annotations

import random
from decimal import Decimal

from simin.risk.engine import Intent
from simin.strategies.base import Strategy, StrategyContext


class TrendFollow(Strategy):
    """Long when the fast/slow spread is positive and the trend is *high quality*.

    The R² filter is what separates this from an EMA cross: a cross fires on any
    wobble, while requiring an actual linear trend keeps it out of chop, which is
    where crossover systems donate their edge to the fee schedule.
    """

    name = "trend_follow"

    def __init__(self, min_quality: float = 0.35, min_adx: float = 22.0) -> None:
        self.min_quality = min_quality
        self.min_adx = min_adx

    def generate(self, ctx: StrategyContext) -> Intent | None:
        vals = ctx.require("ema_spread", "trend_q20", "adx14", "close")
        if vals is None:
            return None
        spread, quality, adx, close = vals
        if spread <= 0 or quality < self.min_quality or adx < self.min_adx:
            return None
        entry = Decimal(str(close))
        stop = self.stop_for(ctx, 1, entry)
        if stop is None:
            return None
        return Intent(
            ts=ctx.ts, symbol=ctx.symbol, direction=1, entry=entry, stop=stop,
            strategy=self.name, regime=ctx.regime.regime,
            confidence=min(1.0, quality),
        )

    def exit_signal(self, ctx: StrategyContext) -> bool:
        vals = ctx.require("ema_spread")
        return vals is not None and vals[0] < 0


class DonchianBreakout(Strategy):
    """Buy a break of the prior N-bar high. The oldest documented systematic edge.

    Small per-trade expectancy, long right tail: most trades lose a little, a few
    pay for everything. That shape is why the risk engine's job is to survive the
    losing runs rather than to pick winners.
    """

    name = "donchian_breakout"

    def __init__(self, min_volume_z: float = 0.5) -> None:
        self.min_volume_z = min_volume_z

    def generate(self, ctx: StrategyContext) -> Intent | None:
        vals = ctx.require("close", "donchian_up20", "vol_z20")
        if vals is None:
            return None
        close, upper, vol_z = vals
        if close <= upper or vol_z < self.min_volume_z:
            return None
        entry = Decimal(str(close))
        stop = self.stop_for(ctx, 1, entry)
        if stop is None:
            return None
        return Intent(
            ts=ctx.ts, symbol=ctx.symbol, direction=1, entry=entry, stop=stop,
            strategy=self.name, regime=ctx.regime.regime, confidence=0.6,
        )


class RangeMeanReversion(Strategy):
    """Fade a stretched z-score, but only when there is no trend to be run over by."""

    name = "range_mean_reversion"
    stop_atr_multiple = 1.5

    def __init__(self, entry_z: float = -2.0, max_adx: float = 20.0) -> None:
        self.entry_z = entry_z
        self.max_adx = max_adx

    def generate(self, ctx: StrategyContext) -> Intent | None:
        vals = ctx.require("z20", "adx14", "close")
        if vals is None:
            return None
        z, adx, close = vals
        if z > self.entry_z or adx > self.max_adx:
            return None
        entry = Decimal(str(close))
        stop = self.stop_for(ctx, 1, entry)
        if stop is None:
            return None
        return Intent(
            ts=ctx.ts, symbol=ctx.symbol, direction=1, entry=entry, stop=stop,
            strategy=self.name, regime=ctx.regime.regime,
            confidence=min(1.0, abs(z) / 4.0),
        )

    def exit_signal(self, ctx: StrategyContext) -> bool:
        vals = ctx.require("z20")
        return vals is not None and vals[0] >= 0.0   # reverted to the mean: done


class VolatilityBreakout(Strategy):
    """Enter when volatility expands out of a compression. Regime change, not level."""

    name = "vol_breakout"

    def __init__(self, expansion: float = 1.5) -> None:
        self.expansion = expansion

    def generate(self, ctx: StrategyContext) -> Intent | None:
        vals = ctx.require("vol_ratio", "mom6", "close")
        if vals is None:
            return None
        ratio, mom, close = vals
        if ratio < self.expansion or mom <= 0:
            return None
        entry = Decimal(str(close))
        stop = self.stop_for(ctx, 1, entry)
        if stop is None:
            return None
        return Intent(
            ts=ctx.ts, symbol=ctx.symbol, direction=1, entry=entry, stop=stop,
            strategy=self.name, regime=ctx.regime.regime, confidence=0.5,
        )


# --------------------------------------------------------------------- baselines


class BuyAndHold(Strategy):
    """Enter once, never exit. The benchmark most bots quietly lose to."""

    name = "buy_and_hold"
    warmup = 1

    def generate(self, ctx: StrategyContext) -> Intent | None:
        if ctx.bar_index != self.warmup or ctx.position is not None:
            return None
        close = ctx.feature("close")
        if close is None:
            return None
        entry = Decimal(str(close))
        return Intent(
            ts=ctx.ts, symbol=ctx.symbol, direction=1, entry=entry,
            stop=entry * Decimal("0.01"),   # nominal: this strategy never stops out
            strategy=self.name, regime=ctx.regime.regime, confidence=1.0,
        )


class RsiOversold(Strategy):
    """Textbook RSI(14) < 30. Included to be beaten, not to be traded."""

    name = "rsi_oversold"

    def generate(self, ctx: StrategyContext) -> Intent | None:
        vals = ctx.require("rsi14", "close")
        if vals is None:
            return None
        rsi, close = vals
        if rsi >= 30:
            return None
        entry = Decimal(str(close))
        stop = self.stop_for(ctx, 1, entry)
        if stop is None:
            return None
        return Intent(
            ts=ctx.ts, symbol=ctx.symbol, direction=1, entry=entry, stop=stop,
            strategy=self.name, regime=ctx.regime.regime, confidence=0.5,
        )

    def exit_signal(self, ctx: StrategyContext) -> bool:
        vals = ctx.require("rsi14")
        return vals is not None and vals[0] > 70


class EmaCross(Strategy):
    """EMA 50/200 cross — the most-quoted rule in retail crypto."""

    name = "ema_cross"

    def generate(self, ctx: StrategyContext) -> Intent | None:
        vals = ctx.require("ema_spread", "close")
        if vals is None:
            return None
        spread, close = vals
        if spread <= 0:
            return None
        entry = Decimal(str(close))
        stop = self.stop_for(ctx, 1, entry)
        if stop is None:
            return None
        return Intent(
            ts=ctx.ts, symbol=ctx.symbol, direction=1, entry=entry, stop=stop,
            strategy=self.name, regime=ctx.regime.regime, confidence=0.5,
        )

    def exit_signal(self, ctx: StrategyContext) -> bool:
        vals = ctx.require("ema_spread")
        return vals is not None and vals[0] < 0


class RandomEntry(Strategy):
    """Random entries with identical sizing and stops.

    The most informative benchmark in the suite: if a strategy cannot beat coin
    flips run through the same risk engine, its *signal* adds nothing and the
    performance belongs to the position sizing.
    """

    name = "random_entry"

    def __init__(self, probability: float = 0.02, seed: int = 0) -> None:
        self.probability = probability
        self._rng = random.Random(seed)

    def generate(self, ctx: StrategyContext) -> Intent | None:
        if ctx.feature("close") is None or self._rng.random() > self.probability:
            return None
        entry = Decimal(str(ctx.feature("close")))
        stop = self.stop_for(ctx, 1, entry)
        if stop is None:
            return None
        return Intent(
            ts=ctx.ts, symbol=ctx.symbol, direction=1, entry=entry, stop=stop,
            strategy=self.name, regime=ctx.regime.regime, confidence=0.5,
        )


ALL_STRATEGIES: dict[str, type[Strategy]] = {
    TrendFollow.name: TrendFollow,
    DonchianBreakout.name: DonchianBreakout,
    RangeMeanReversion.name: RangeMeanReversion,
    VolatilityBreakout.name: VolatilityBreakout,
}

BENCHMARKS: dict[str, type[Strategy]] = {
    BuyAndHold.name: BuyAndHold,
    RsiOversold.name: RsiOversold,
    EmaCross.name: EmaCross,
    RandomEntry.name: RandomEntry,
}


def build(name: str, **kwargs: object) -> Strategy:
    registry = {**ALL_STRATEGIES, **BENCHMARKS}
    try:
        cls = registry[name]
    except KeyError as exc:
        raise KeyError(f"unknown strategy {name!r}; known: {sorted(registry)}") from exc
    return cls(**kwargs)  # type: ignore[arg-type]
