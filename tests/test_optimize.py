"""The parameter search must be structurally incapable of peeking."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from factories import gbm_series

from simin.backtest.costs import CostModel
from simin.backtest.portfolio import PortfolioConfig
from simin.config import RiskProfile, limits_for
from simin.risk.engine import RiskEngine
from simin.strategies.swing import SwingMomentum, SwingPullback
from simin.types import FeeSchedule
from simin.validation.optimize import SweepReport, Trial, grid, split_by_date, sweep

FREE = CostModel(fees=FeeSchedule(Decimal(0), Decimal(0)), spread_bps=Decimal(0),
                 impact_coefficient=Decimal(0), latency_bps=Decimal(0))
CUTOFF = datetime(2024, 3, 1, tzinfo=UTC)


def universe(n=4, bars=2000, mu=0.0004):
    return {
        f"SYM{i}USDT": gbm_series(bars, seed=i, mu=mu, sigma=0.012, volume=500_000.0,
                                  symbol=f"SYM{i}USDT", start=datetime(2024, 1, 1, tzinfo=UTC))
        for i in range(n)
    }


def factory(p):
    return [SwingMomentum(min_momentum=p["min_momentum"]), SwingPullback()]


def run_sweep(combos=None, series=None):
    series = series or universe()
    train, holdout = split_by_date(series, CUTOFF)
    combos = combos or grid(min_momentum=[0.003, 0.01])
    return sweep(
        train, holdout, combos, factory,
        risk=RiskEngine(limits_for(RiskProfile.BALANCED)),
        config=PortfolioConfig(starting_equity=Decimal("1000000"), cost=FREE, max_hold_bars=96),
    )


def test_grid_expands_every_combination():
    combos = grid(a=[1, 2, 3], b=["x", "y"])
    assert len(combos) == 6
    assert {"a": 2, "b": "y"} in combos


def test_split_is_chronological_and_complete():
    series = universe(3)
    train, holdout = split_by_date(series, CUTOFF)
    for symbol in series:
        assert max(b.ts for b in train[symbol]) < CUTOFF
        assert min(b.ts for b in holdout[symbol]) >= CUTOFF
        assert len(train[symbol]) + len(holdout[symbol]) == len(series[symbol])


def test_the_search_never_touches_the_holdout():
    """Every trial must be scored on training data alone."""
    series = universe(3)
    train, holdout = split_by_date(series, CUTOFF)
    seen_lengths = []

    def spy_factory(p):
        return factory(p)

    def on_progress(_i, _n, trial):
        seen_lengths.append(len(trial.result.stamps))

    report = sweep(
        train, holdout, grid(min_momentum=[0.005, 0.02]), spy_factory,
        risk=RiskEngine(limits_for(RiskProfile.BALANCED)),
        config=PortfolioConfig(starting_equity=Decimal("1000000"), cost=FREE),
        on_progress=on_progress,
    )
    train_bars = len(next(iter(train.values())))
    for length in seen_lengths:
        assert length <= train_bars      # never longer than the training window
    assert report.holdout is not None
    assert report.holdout.stamps[0] >= CUTOFF


def test_trial_count_is_carried_into_the_deflated_sharpe():
    """Forty trials must deflate the result more than two do."""
    few = run_sweep(grid(min_momentum=[0.005, 0.02]))
    many = run_sweep(grid(min_momentum=[0.002, 0.004, 0.006, 0.008, 0.01, 0.02]))
    assert few.n_trials == 2
    assert many.n_trials == 6
    assert many.holdout is not None and few.holdout is not None
    assert many.holdout.metrics.deflated_sharpe <= few.holdout.metrics.deflated_sharpe


def test_winner_is_chosen_on_robustness_not_raw_return():
    """A configuration with 3 lucky trades must not beat a consistent one."""
    from simin.backtest.metrics import Metrics

    def stub(n_trades, sharpe, ret, dd=-0.1):
        metrics = Metrics(
            n_trades=n_trades, win_rate=0.5, profit_factor=1.2, expectancy=0.001,
            avg_win=0.02, avg_loss=-0.01, total_return=ret, cagr=ret, sharpe=sharpe,
            deflated_sharpe=0.5, sortino=1.0, calmar=1.0, max_drawdown=dd,
            max_drawdown_duration_bars=10, exposure=0.3, total_fees=Decimal(1),
            fee_drag=0.1, var_95=-0.01, cvar_95=-0.02, recovery_factor=1.0,
        )
        result = type("R", (), {"metrics": metrics})()
        return Trial(params={}, result=result)   # type: ignore[arg-type]

    lucky = stub(n_trades=3, sharpe=9.0, ret=5.0)
    steady = stub(n_trades=200, sharpe=1.4, ret=0.4)
    assert lucky.score == -99.0          # too few trades to be evidence
    assert steady.score > lucky.score


def test_unsurvivable_drawdown_disqualifies_a_configuration():
    from simin.backtest.metrics import Metrics

    metrics = Metrics(
        n_trades=500, win_rate=0.6, profit_factor=2.0, expectancy=0.01, avg_win=0.05,
        avg_loss=-0.02, total_return=10.0, cagr=3.0, sharpe=4.0, deflated_sharpe=0.9,
        sortino=5.0, calmar=1.0, max_drawdown=-0.80, max_drawdown_duration_bars=100,
        exposure=0.9, total_fees=Decimal(1), fee_drag=0.1, var_95=-0.1, cvar_95=-0.2,
        recovery_factor=1.0,
    )
    trial = Trial(params={}, result=type("R", (), {"metrics": metrics})())  # type: ignore[arg-type]
    assert trial.score == -99.0


def test_verdict_calls_a_failed_holdout_invalid():
    report = SweepReport(n_trials=10)
    assert "incomplete" in report.verdict()


def test_sweep_produces_a_leaderboard_and_a_verdict():
    report = run_sweep()
    assert report.trials
    assert "sharpe" in report.table()
    verdict = report.verdict()
    assert "VERDICT" in verdict
    assert "trials tried: 2" in verdict
