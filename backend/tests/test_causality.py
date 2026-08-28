"""The tests that matter most: proving nothing reads the future.

A backtester with lookahead does not fail loudly — it produces excellent
results, which is the worst possible failure mode because it is indistinguishable
from success until real money is involved.

The central test here mutates the future and asserts the past is byte-identical.
It has already caught one real bug in this codebase: support/resistance levels
were computed over the whole dataset and handed to every bar, so a strategy at
bar 500 could place its stop against a level that would not form until bar 1800.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from simin.core.types import TF, Candle
from simin.exchanges.costs import CostModel
from simin.indicators.features import FeatureFrame
from simin.lab.backtest import Backtester, _align
from simin.priceaction.structure import find_levels, find_swings
from simin.risk.dial import profile
from simin.strategies.base import build_many
from simin.strategies.library import strategies_for_level
from tests.conftest import make_candles

CUTOFF = 1200


def _mutated_future(base: list[Candle]) -> list[Candle]:
    """The same history to `CUTOFF`, then a violently different continuation."""
    out = list(base[:CUTOFF])
    crash = make_candles(
        len(base) - CUTOFF, seed=999, drift=-0.02, vol=0.05,
        start=float(base[CUTOFF - 1].close),
    )
    t0 = base[CUTOFF - 1].ts
    for i, c in enumerate(crash):
        out.append(
            Candle(
                ts=t0 + timedelta(seconds=TF.H2.seconds * (i + 1)),
                open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume,
            )
        )
    return out


@pytest.fixture(scope="module")
def histories() -> tuple[list[Candle], list[Candle]]:
    base = make_candles(1800, seed=42)
    return base, _mutated_future(base)


@pytest.mark.parametrize("level", [2, 4, 5, 7, 9])
def test_replacing_the_future_does_not_change_the_past(histories, symbol, level) -> None:
    base, mutated = histories
    costs = CostModel()
    prof = profile(level)

    def run(candles: list[Candle]):
        return Backtester(
            prof, build_many(strategies_for_level(level)), costs, Decimal("10000")
        ).run({"BTCUSDT": FeatureFrame("BTCUSDT", TF.H2, candles)}, {"BTCUSDT": symbol}, TF.H2)

    a = run(base)
    b = run(mutated)
    edge = base[CUTOFF - 10].ts
    ta = [t for t in a.trades if t.closed_at < edge]
    tb = [t for t in b.trades if t.closed_at < edge]

    assert len(ta) == len(tb), f"level {level}: trade counts diverged before the cutoff"
    for x, y in zip(ta, tb, strict=True):
        assert x.opened_at == y.opened_at
        assert x.entry_price == y.entry_price
        assert x.exit_price == y.exit_price
        assert x.net_pnl == y.net_pnl
        assert x.reason is y.reason


def test_levels_are_computed_as_of_each_bar(histories) -> None:
    """The specific regression: level sets must not depend on future candles."""
    base, mutated = histories
    fa = FeatureFrame("X", TF.H2, base)
    fb = FeatureFrame("X", TF.H2, mutated)
    for i in range(220, CUTOFF - 10):
        assert fa.row(i).levels == fb.row(i).levels, f"level set differs at bar {i}"


def test_find_levels_ignores_unconfirmed_swings() -> None:
    candles = make_candles(600, seed=3)
    swings = find_swings(candles, 2, 2)
    as_of = 300
    levels = find_levels(candles, swings, as_of=as_of)
    # Reconstructing from only the swings visible at `as_of` must give the same
    # answer as asking for `as_of` directly.
    visible = [s for s in swings if s.confirmed_at <= as_of]
    assert levels == find_levels(candles, visible, as_of=as_of)


def test_swings_are_confirmed_after_they_occur() -> None:
    swings = find_swings(make_candles(300), left=2, right=2)
    assert swings
    for s in swings:
        assert s.confirmed_at == s.index + 2
        assert s.confirmed_at > s.index


def test_higher_timeframe_alignment_never_uses_an_open_bar() -> None:
    """Using the 4h bar that *contains* the current 15m bar is lookahead one
    timeframe up — the subtlest version, because it yields a backtest that looks
    merely very good rather than impossible."""
    fast = make_candles(600, seed=11, tf=TF.H2)
    slow = [
        Candle(
            ts=fast[i].ts,
            open=fast[i].open,
            high=max(c.high for c in fast[i : i + 6]),
            low=min(c.low for c in fast[i : i + 6]),
            close=fast[min(i + 5, len(fast) - 1)].close,
            volume=Decimal("100"),
        )
        for i in range(0, len(fast) - 6, 6)
    ]
    frame = FeatureFrame("X", TF.H12, slow)
    for c in fast[100:]:
        j = _align(frame, c.ts)
        if j is not None:
            assert frame.candles[j].ts + TF.H12.delta <= c.ts


def test_entries_fill_at_the_next_bar_open(symbol) -> None:
    candles = make_candles(1500, seed=5)
    costs = CostModel()
    result = Backtester(
        profile(5), build_many(strategies_for_level(5)), costs, Decimal("10000")
    ).run({"BTCUSDT": FeatureFrame("BTCUSDT", TF.H2, candles)}, {"BTCUSDT": symbol}, TF.H2)

    by_ts = {c.ts: c for c in candles}
    assert result.trades, "no trades produced; the assertion below would be vacuous"
    for t in result.trades:
        bar = by_ts[t.opened_at]
        drift = float(bar.open) * float(costs.half_spread + costs.slippage)
        assert abs(float(t.entry_price) - float(bar.open)) <= drift * 1.02 + 1e-9


def test_stop_exits_stay_inside_the_bar(symbol) -> None:
    """A stop cannot fill better than the bar allowed. Modelling a gap-through
    as filling at the stop price is the difference between −1R and −4R."""
    candles = make_candles(1500, seed=5)
    result = Backtester(
        profile(6), build_many(strategies_for_level(6)), CostModel(), Decimal("10000")
    ).run({"BTCUSDT": FeatureFrame("BTCUSDT", TF.H2, candles)}, {"BTCUSDT": symbol}, TF.H2)

    by_ts = {c.ts: c for c in candles}
    for t in result.trades:
        if t.reason.value not in ("stop_loss", "trailing_stop"):
            continue
        bar = by_ts[t.closed_at]
        if t.direction.value == "long":
            assert float(t.exit_price) <= float(bar.high) + 1e-9
        else:
            assert float(t.exit_price) >= float(bar.low) - 1e-9


def test_feature_row_refuses_partial_data(frame) -> None:
    """A strategy either has everything it needs or does nothing. Silently
    treating a warming-up indicator as 0.0 manufactures signals."""
    assert frame.row(3).require("rsi", "adx", "macd") is None
    assert frame.row(len(frame) - 1).require("rsi", "adx", "macd") is not None
