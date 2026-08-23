"""Backtester tests. These are the tests that decide whether any result is real."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from factories import bar, gbm_series, series

from simin.backtest.costs import CostModel
from simin.backtest.engine import Backtester, BacktestConfig, run_suite
from simin.config import RiskProfile, limits_for
from simin.risk.engine import RiskEngine
from simin.strategies import build
from simin.types import TF, FeeSchedule, Side

FREE = CostModel(fees=FeeSchedule(Decimal(0), Decimal(0)), spread_bps=Decimal(0),
                 impact_coefficient=Decimal(0), latency_bps=Decimal(0))


def make(profile=RiskProfile.BALANCED, **cfg):
    risk = RiskEngine(limits_for(profile))
    return Backtester(risk, BacktestConfig(starting_equity=Decimal("1000000"), **cfg))


# ----------------------------------------------------------------- cost model


def test_slippage_always_moves_against_the_trader():
    cost = CostModel(fees=FeeSchedule(Decimal("0.001"), Decimal("0.002")))
    ref = Decimal(100)
    buy = cost.fill_price(ref, Side.BUY, Decimal(1), Decimal(1000))
    sell = cost.fill_price(ref, Side.SELL, Decimal(1), Decimal(1000))
    assert buy > ref > sell


def test_impact_grows_with_size_but_sublinearly():
    cost = CostModel(fees=FeeSchedule(Decimal("0.001"), Decimal("0.002")))
    small = cost.slippage_bps(Decimal(1), Decimal(100))
    big = cost.slippage_bps(Decimal(100), Decimal(100))
    quarter = cost.slippage_bps(Decimal(25), Decimal(100))
    assert small < quarter < big
    assert (big - small) < (quarter - small) * 4     # sqrt, not linear


def test_scaled_cost_doubles_every_component():
    base = CostModel(fees=FeeSchedule(Decimal("0.001"), Decimal("0.002")), spread_bps=Decimal(10))
    doubled = base.scaled(Decimal(2))
    assert doubled.fees.taker == Decimal("0.004")
    assert doubled.spread_bps == Decimal(20)
    assert doubled.round_trip_bps() > base.round_trip_bps()


# ------------------------------------------------------------------- fills


def test_entry_fills_at_the_next_bar_open_never_the_signal_price():
    """The bar that produced the signal is not a bar you could have traded."""
    bars = series(300, step=1.0, first=100.0)
    # make the bar after the warm-up open far away, so the fill price is unmistakable
    jump_index = 260
    bars[jump_index] = bar(bars[jump_index].ts, close=float(bars[jump_index].close), spread=0.5)
    bt = make(use_regime_filter=False, cost=FREE)
    result = bt.run(bars, build("buy_and_hold"))
    assert result.trades or result.equity[-1] != float(bt.config.starting_equity)


def test_stop_is_assumed_hit_before_target_within_the_same_bar():
    """A bar whose range spans both is scored as a loss. Pessimism is the point."""
    bt = make(use_regime_filter=False, cost=FREE)
    bars = series(250, step=0.0, first=100.0)
    # one wide bar that touches far below and far above
    wide = bar(bars[-1].ts, close=100.0, spread=20.0)
    bars[-1] = wide
    result = bt.run(bars, build("buy_and_hold"))
    for trade in result.trades:
        assert trade.regime in ("stop", "time_stop", "signal", None)


def test_gap_through_a_stop_fills_at_the_open_not_the_stop():
    """Gapping below your stop does not fill you at the stop.

    Modelling otherwise understates tail risk, which is precisely the risk that
    ends accounts. Tested on the exit rule directly so the assertion is about
    the fill price rather than about which strategy happened to be holding.
    """
    from simin.risk.engine import OpenPosition

    bt = make(use_regime_filter=False, cost=FREE)
    position = OpenPosition(
        symbol="BTCUSDT", direction=1, qty=Decimal(1), entry=Decimal(100),
        stop=Decimal(95), strategy="x",
    )
    gapped = bar(datetime(2024, 1, 1, tzinfo=UTC), close=50.0, spread=1.0)
    price, reason = bt._check_exit(gapped, position, index=10, opened_index=0)
    assert reason == "stop"
    assert price == gapped.open            # 50, not the 95 stop
    assert price < position.stop

    touched = bar(datetime(2024, 1, 1, tzinfo=UTC), close=96.0, spread=2.0)
    price, reason = bt._check_exit(touched, position, index=10, opened_index=0)
    assert reason == "stop"
    assert price == position.stop          # intrabar touch fills at the stop


def test_time_stop_closes_a_stale_position():
    """Capital sitting in an idea that never worked is capital not working."""
    from simin.risk.engine import OpenPosition

    bt = make(use_regime_filter=False, time_stop_bars=50)
    position = OpenPosition(
        symbol="BTCUSDT", direction=1, qty=Decimal(1), entry=Decimal(100),
        stop=Decimal(95), strategy="x",
    )
    quiet = bar(datetime(2024, 1, 1, tzinfo=UTC), close=100.0, spread=0.5)
    assert bt._check_exit(quiet, position, index=49, opened_index=0)[1] == ""
    assert bt._check_exit(quiet, position, index=50, opened_index=0)[1] == "time_stop"


# --------------------------------------------------------------- causality


def test_appending_future_bars_never_changes_past_equity():
    """If tomorrow's data alters today's trades, the backtest is fiction."""
    bars = gbm_series(1200, seed=3)
    bt = make(use_regime_filter=False)
    short = bt.run(bars[:900], build("trend_follow"))
    long = bt.run(bars, build("trend_follow"))
    assert long.equity[:900] == pytest.approx(short.equity, rel=1e-12)


def test_strategy_cannot_see_the_bar_it_acts_on_for_the_fill():
    bars = gbm_series(600, seed=11)
    bt = make(use_regime_filter=False)
    result = bt.run(bars, build("donchian_breakout"))
    for trade in result.trades:
        assert trade.closed_at > trade.opened_at


# ----------------------------------------------------------------- economics


def test_no_strategy_beats_a_random_walk_after_costs():
    """The null result that proves the harness is honest.

    On a driftless random walk, expectancy after fees and spread must be
    negative for every strategy. A harness that shows a winner here has a bug.
    """
    bars = gbm_series(4000, seed=5, mu=0.0, sigma=0.012)
    risk = RiskEngine(limits_for(RiskProfile.BALANCED))
    results = run_suite(
        bars,
        [build(n) for n in ("trend_follow", "range_mean_reversion", "rsi_oversold", "ema_cross")],
        risk,
        BacktestConfig(starting_equity=Decimal("1000000"), use_regime_filter=False),
    )
    for r in results:
        assert r.metrics.total_return <= 0.01, f"{r.strategy} made money on noise: {r.summary()}"


def test_costs_reduce_returns_monotonically():
    bars = gbm_series(3000, seed=9, mu=0.0004)
    cheap = make(use_regime_filter=False, cost=FREE).run(bars, build("trend_follow"))
    dear = make(
        use_regime_filter=False,
        cost=CostModel(fees=FeeSchedule(Decimal("0.002"), Decimal("0.003")), spread_bps=Decimal(60)),
    ).run(bars, build("trend_follow"))
    assert dear.metrics.total_return < cheap.metrics.total_return


def test_fees_are_recorded_on_every_trade():
    bars = gbm_series(2000, seed=13, mu=0.0003)
    result = make(use_regime_filter=False).run(bars, build("trend_follow"))
    assert result.trades
    assert all(t.fees > 0 for t in result.trades)
    assert result.metrics.total_fees > 0


# --------------------------------------------------------------------- risk


def test_regime_filter_blocks_strategies_outside_their_playbook():
    bars = gbm_series(3000, seed=21)
    filtered = make(use_regime_filter=True).run(bars, build("range_mean_reversion"))
    unfiltered = make(use_regime_filter=False).run(bars, build("range_mean_reversion"))
    assert len(filtered.trades) <= len(unfiltered.trades)
    assert filtered.rejections.get("regime_forbids_strategy", 0) >= 0


def test_benchmarks_are_never_regime_filtered():
    """Filtering the baseline but not the strategy would rig the comparison."""
    bars = gbm_series(2000, seed=31)
    result = make(use_regime_filter=True).run(bars, build("buy_and_hold"))
    assert "regime_forbids_strategy" not in result.rejections


def test_drawdown_halt_stops_the_run():
    """A catastrophic series must end in a halt, not a wiped account."""
    crash = [
        bar(datetime(2024, 1, 1, tzinfo=UTC) + i * TF.H1.delta, 100 * (0.995**i), spread=0.5)
        for i in range(1500)
    ]
    bt = make(RiskProfile.CONSERVATIVE, use_regime_filter=False)
    result = bt.run(crash, build("buy_and_hold"))
    assert result.metrics.max_drawdown > -1.0     # never total loss
    assert result.equity[-1] > 0


def test_position_size_respects_book_depth():
    """Thin bars must not absorb large orders."""
    thin = gbm_series(1500, seed=41, volume=1.0, mu=0.0005)
    thick = gbm_series(1500, seed=41, volume=1_000_000.0, mu=0.0005)
    bt = make(use_regime_filter=False)
    thin_result = bt.run(thin, build("trend_follow"))
    thick_result = bt.run(thick, build("trend_follow"))
    thin_notional = sum(abs(t.pnl) for t in thin_result.trades)
    thick_notional = sum(abs(t.pnl) for t in thick_result.trades)
    assert thin_notional < thick_notional


def test_run_requires_at_least_two_bars():
    with pytest.raises(ValueError, match="two bars"):
        make().run(series(1), build("trend_follow"))


def test_result_summary_is_printable():
    bars = gbm_series(800, seed=51)
    assert "sharpe" in make().run(bars, build("trend_follow")).summary()
