"""Portfolio backtester and the swing strategies built on the measured edge curve."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from factories import gbm_series

from simin.backtest.costs import CostModel
from simin.backtest.portfolio import PortfolioBacktester, PortfolioConfig
from simin.config import RiskProfile, limits_for
from simin.features.engine import build_features
from simin.features.regime import REGIME_PLAYBOOK, Regime
from simin.risk.engine import RiskEngine
from simin.strategies import ALL_STRATEGIES, build
from simin.strategies.swing import MAX_HOLD_HOURS
from simin.types import FeeSchedule

FREE = CostModel(fees=FeeSchedule(Decimal(0), Decimal(0)), spread_bps=Decimal(0),
                 impact_coefficient=Decimal(0), latency_bps=Decimal(0))


def universe(n_symbols=4, bars=1500, seed=0, mu=0.0003):
    return {
        f"SYM{i}USDT": gbm_series(
            bars, seed=seed + i, mu=mu, sigma=0.012, volume=500_000.0,
            symbol=f"SYM{i}USDT", start=datetime(2024, 1, 1, tzinfo=UTC),
        )
        for i in range(n_symbols)
    }


def runner(**cfg):
    risk = RiskEngine(limits_for(RiskProfile.BALANCED))
    config = PortfolioConfig(starting_equity=Decimal("1000000"), **cfg)
    return PortfolioBacktester(risk, config)


def swings():
    return [build("swing_momentum"), build("swing_pullback")]


# ------------------------------------------------------------- swing strategies


def test_swing_strategies_are_registered_and_permitted_by_the_regime_playbook():
    assert "swing_momentum" in ALL_STRATEGIES
    assert "swing_pullback" in ALL_STRATEGIES
    assert "swing_momentum" in REGIME_PLAYBOOK[Regime.STRONG_BULL]
    # and still blocked where both families bleed
    assert REGIME_PLAYBOOK[Regime.SIDEWAYS_HIGH_VOL] == ()
    assert REGIME_PLAYBOOK[Regime.PANIC] == ()


def test_swing_momentum_needs_momentum_quality_and_a_non_extended_rsi():
    from simin.features.regime import RegimeState
    from simin.strategies.base import StrategyContext

    rows = build_features(gbm_series(600, seed=3, mu=0.001))
    strategy = build("swing_momentum")
    regime = RegimeState(Regime.STRONG_BULL, 30.0, 0.5, "test")
    fired = 0
    for i in range(strategy.warmup, len(rows)):
        ctx = StrategyContext(ts=rows[i].ts, symbol="X", row=rows[i], regime=regime,
                              position=None, bar_index=i)
        intent = strategy.generate(ctx)
        if intent is not None:
            fired += 1
            assert intent.stop < intent.entry
            assert 0 < intent.confidence <= 1
            assert rows[i].get("rsi14") <= strategy.max_rsi
            assert rows[i].get("mom48") >= strategy.min_momentum
    assert fired > 0


def test_max_hold_matches_the_four_day_constraint():
    assert MAX_HOLD_HOURS == 96


# ------------------------------------------------------------------- portfolio


def test_portfolio_holds_several_symbols_at_once():
    result = runner(cost=FREE).run(universe(5, mu=0.0006), swings())
    assert result.metrics.n_trades > 0
    assert len(result.symbols) == 5


def test_no_position_ever_exceeds_the_holding_ceiling():
    """The hard constraint: four days, no exceptions, whatever the P&L."""
    result = runner(cost=FREE, max_hold_bars=96).run(universe(4, mu=0.0005), swings())
    assert result.trades
    assert result.max_hold_bars_seen <= 96


def test_shorter_ceiling_is_respected_too():
    result = runner(cost=FREE, max_hold_bars=24).run(universe(4, mu=0.0005), swings())
    assert result.max_hold_bars_seen <= 24


def test_breadth_produces_more_trades_than_a_single_symbol():
    """The core claim: frequency comes from more symbols, not shorter holds."""
    single = runner(cost=FREE).run(universe(1, mu=0.0005), swings())
    many = runner(cost=FREE).run(universe(8, mu=0.0005), swings())
    assert many.metrics.n_trades > single.metrics.n_trades
    assert many.trades_per_day > single.trades_per_day


def test_holding_time_does_not_grow_when_symbols_are_added():
    """Breadth must not be bought by holding longer — that would be cheating."""
    single = runner(cost=FREE).run(universe(1, mu=0.0005), swings())
    many = runner(cost=FREE).run(universe(8, mu=0.0005), swings())
    if single.trades and many.trades:
        assert many.avg_hold_bars <= single.avg_hold_bars * 1.5


def test_a_shorter_holding_ceiling_produces_a_worse_net_outcome():
    """The measured edge curve, expressed as a test.

    Edge accrues with holding time while cost is charged once per round trip, so
    cutting the ceiling from four days to six hours must not improve the result.
    """
    expensive = CostModel(fees=FeeSchedule(Decimal("0.002"), Decimal("0.0025")),
                          spread_bps=Decimal(60))
    patient = runner(cost=expensive, max_hold_bars=96).run(universe(6, mu=0.0004), swings())
    churny = runner(cost=expensive, max_hold_bars=6).run(universe(6, mu=0.0004), swings())
    assert churny.metrics.total_return <= patient.metrics.total_return
    assert churny.metrics.expectancy <= patient.metrics.expectancy


def test_portfolio_respects_max_open_positions():
    limits = limits_for(RiskProfile.BALANCED)
    result = runner(cost=FREE).run(universe(10, mu=0.0006), swings())
    # reconstruct concurrency from the trade log
    events = []
    for t in result.trades:
        events.append((t.opened_at, 1))
        events.append((t.closed_at, -1))
    events.sort()
    concurrent = peak = 0
    for _, delta in events:
        concurrent += delta
        peak = max(peak, concurrent)
    assert peak <= limits.max_open_positions


def test_portfolio_reports_daily_activity():
    result = runner(cost=FREE).run(universe(6, mu=0.0005), swings())
    assert result.total_days > 0
    assert 0.0 <= result.day_coverage <= 1.0
    assert len(result.daily_returns) == result.total_days


def test_equity_is_marked_every_bar():
    result = runner(cost=FREE).run(universe(3), swings())
    assert len(result.equity) == len(result.stamps)
    assert all(v > 0 for v in result.equity)


def test_empty_universe_is_rejected():
    with pytest.raises(ValueError, match="no symbols"):
        runner().run({}, swings())


def test_portfolio_never_beats_a_random_walk_after_real_costs():
    """Same null test as the single-symbol engine, at portfolio level."""
    expensive = CostModel(fees=FeeSchedule(Decimal("0.002"), Decimal("0.0025")),
                          spread_bps=Decimal(60))
    result = runner(cost=expensive).run(universe(8, seed=50, mu=0.0), swings())
    assert result.metrics.total_return <= 0.02
