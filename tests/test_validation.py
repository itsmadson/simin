"""Walk-forward, Monte Carlo and the Go/No-Go gates."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factories import gbm_series

from simin.backtest.engine import BacktestConfig
from simin.backtest.metrics import Metrics, TradeStat
from simin.config import RiskProfile, limits_for
from simin.risk.engine import RiskEngine
from simin.strategies import build
from simin.validation.gates import (
    GateReport,
    PaperRecord,
    evaluate_gates,
    initial_live_allocation,
    target_feasibility,
)
from simin.validation.montecarlo import MonteCarloReport, simulate, stress_scenarios
from simin.validation.walkforward import (
    split_holdout,
    walk_forward,
)


def risk():
    return RiskEngine(limits_for(RiskProfile.BALANCED))


def cfg():
    return BacktestConfig(starting_equity=Decimal("1000000"), use_regime_filter=False)


# ------------------------------------------------------------- walk forward


def test_walk_forward_produces_disjoint_test_windows():
    bars = gbm_series(4000, seed=3, mu=0.0002)
    report = walk_forward(
        bars, lambda: build("trend_follow"), risk(),
        train_bars=1000, test_bars=500, config=cfg(),
    )
    assert report.n_windows >= 4
    for a, b in zip(report.windows, report.windows[1:], strict=False):
        assert b.test_start > a.test_start
        assert a.test_end <= b.test_start


def test_optimizer_never_receives_the_test_slice():
    """If the optimiser can see the test data, the test is not a test."""
    seen: list[datetime] = []

    def optimizer(train_bars):
        seen.append(train_bars[-1].ts)
        return build("trend_follow")

    bars = gbm_series(3000, seed=5)
    report = walk_forward(
        bars, lambda: build("trend_follow"), risk(),
        train_bars=800, test_bars=400, config=cfg(), optimizer=optimizer,
    )
    for window, last_train_ts in zip(report.windows, seen, strict=False):
        assert last_train_ts < window.test_start


def test_walk_forward_reports_every_window_not_just_the_average():
    bars = gbm_series(3500, seed=7)
    report = walk_forward(
        bars, lambda: build("donchian_breakout"), risk(),
        train_bars=1000, test_bars=500, config=cfg(),
    )
    table = report.table()
    assert table.count("\n") >= report.n_windows
    assert "consistency" in table


def test_consistency_and_worst_window_are_derived_correctly():
    bars = gbm_series(3000, seed=11)
    report = walk_forward(
        bars, lambda: build("trend_follow"), risk(),
        train_bars=800, test_bars=400, config=cfg(),
    )
    assert 0.0 <= report.consistency <= 1.0
    assert report.worst_window <= max(w.total_return for w in report.windows)


def test_holdout_split_is_chronological():
    bars = gbm_series(1000, seed=13)
    research, holdout = split_holdout(bars, 0.2)
    assert len(holdout) == 200
    assert research[-1].ts < holdout[0].ts


def test_holdout_fraction_is_validated():
    with pytest.raises(ValueError, match="holdout_fraction"):
        split_holdout(gbm_series(100), 1.5)


# -------------------------------------------------------------- monte carlo


def trades(returns):
    base = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        TradeStat(base + timedelta(hours=i), base + timedelta(hours=i + 1), "BTC", "s",
                  Decimal(str(r)), r, Decimal(0))
        for i, r in enumerate(returns)
    ]


def test_monte_carlo_flags_a_losing_edge():
    report = simulate(trades([-0.02] * 50 + [0.01] * 50), n_simulations=500, seed=1)
    assert report.probability_of_profit < 0.2


def test_monte_carlo_reports_a_worse_tail_than_median():
    report = simulate(trades([0.03, -0.01] * 100), n_simulations=1000, seed=2)
    assert report.p05_return < report.median_return < report.p95_return
    assert report.p95_max_drawdown <= report.median_max_drawdown


def test_dropping_the_best_trades_exposes_luck_dependence():
    """One trade carrying the whole result should show up as fragility."""
    lucky = trades([0.0] * 99 + [5.0])
    report = simulate(lucky, n_simulations=500, drop_best_fraction=0.05, seed=3)
    assert report.median_return < 5.0


def test_probability_of_ruin_is_reported():
    report = simulate(trades([-0.5, 0.4] * 50), n_simulations=500, seed=4)
    assert 0.0 <= report.probability_of_ruin <= 1.0


def test_empty_trade_list_is_maximally_pessimistic():
    report = simulate([], n_simulations=10)
    assert report.probability_of_ruin == 1.0
    assert report.probability_of_profit == 0.0


def test_stress_scenarios_cover_the_documented_set():
    scenarios = stress_scenarios()
    for name in ("btc_crash_40", "flash_crash_recover", "fees_2x", "venue_outage"):
        assert name in scenarios


# --------------------------------------------------------------------- gates


def metrics(**kw):
    defaults = dict(
        n_trades=250, win_rate=0.5, profit_factor=1.4, expectancy=0.002, avg_win=0.02,
        avg_loss=-0.01, total_return=0.4, cagr=0.35, sharpe=1.6, deflated_sharpe=0.99,
        sortino=2.0, calmar=2.0, max_drawdown=-0.12, max_drawdown_duration_bars=100,
        exposure=0.4, total_fees=Decimal(100), fee_drag=0.15, var_95=-0.02, cvar_95=-0.03,
        recovery_factor=3.0,
    )
    defaults.update(kw)
    return Metrics(**defaults)


def mc(**kw):
    defaults = dict(
        n_simulations=10_000, probability_of_profit=0.9, probability_of_ruin=0.005,
        median_return=0.3, p05_return=-0.05, p95_return=0.8, median_max_drawdown=-0.1,
        p95_max_drawdown=-0.2, worst_return=-0.3, best_return=1.2,
    )
    defaults.update(kw)
    return MonteCarloReport(**defaults)


@dataclass(frozen=True)
class FakeWalkForward:
    """Stub with just the surface the gates read.

    Not a subclass: WalkForwardReport is a frozen slotted dataclass, and the
    gates only ever ask it two questions.
    """

    consistency: float
    worst_window: float


def good_paper():
    return PaperRecord(
        days_running=70, closed_trades=240, realized_sharpe=1.4, max_drawdown=-0.11,
        slippage_ratio=1.1, unhandled_exceptions=0, reconciliation_mismatches=0,
    )


def test_all_gates_can_pass_together():
    report = evaluate_gates(
        walk_forward=FakeWalkForward(0.8, -0.10),
        out_of_sample=metrics(),
        monte_carlo=mc(),
        stressed_cost_return=0.12,
        benchmark_returns={"buy_and_hold": 0.2, "random_entry": -0.1},
        paper=good_paper(),
        human_approved=True,
    )
    assert report.passed, report.render()


def test_missing_paper_evidence_fails_rather_than_skips():
    report = evaluate_gates(
        walk_forward=FakeWalkForward(0.9, -0.05),
        out_of_sample=metrics(),
        monte_carlo=mc(),
        stressed_cost_return=0.1,
        benchmark_returns={"buy_and_hold": 0.1},
        paper=None,
        human_approved=True,
    )
    assert not report.passed
    assert any("paper" in g.name for g in report.failures)


def test_human_approval_is_required_even_when_every_number_is_good():
    report = evaluate_gates(
        walk_forward=FakeWalkForward(1.0, 0.0),
        out_of_sample=metrics(),
        monte_carlo=mc(),
        stressed_cost_return=0.2,
        benchmark_returns={"buy_and_hold": 0.1},
        paper=good_paper(),
        human_approved=False,
    )
    assert not report.passed
    assert report.failures[-1].number == 12


def test_losing_to_buy_and_hold_fails_the_benchmark_gate():
    report = evaluate_gates(
        walk_forward=FakeWalkForward(0.9, -0.05),
        out_of_sample=metrics(total_return=0.05),
        monte_carlo=mc(),
        stressed_cost_return=0.02,
        benchmark_returns={"buy_and_hold": 0.60},
        paper=good_paper(),
        human_approved=True,
    )
    assert not report.passed
    assert any(g.number == 6 for g in report.failures)


def test_strategy_that_dies_at_double_cost_is_rejected():
    report = evaluate_gates(
        walk_forward=FakeWalkForward(0.9, -0.05),
        out_of_sample=metrics(),
        monte_carlo=mc(),
        stressed_cost_return=-0.03,
        benchmark_returns={"buy_and_hold": 0.1},
        paper=good_paper(),
        human_approved=True,
    )
    assert any(g.number == 5 for g in report.failures)


def test_high_ruin_probability_fails():
    report = evaluate_gates(
        walk_forward=FakeWalkForward(0.9, -0.05),
        out_of_sample=metrics(),
        monte_carlo=mc(probability_of_ruin=0.08),
        stressed_cost_return=0.1,
        benchmark_returns={"buy_and_hold": 0.1},
        paper=good_paper(),
        human_approved=True,
    )
    assert any(g.number == 4 for g in report.failures)


def test_low_deflated_sharpe_fails_even_with_a_high_raw_sharpe():
    """A Sharpe of 3 found after 500 trials is not a Sharpe of 3."""
    report = evaluate_gates(
        walk_forward=FakeWalkForward(0.9, -0.05),
        out_of_sample=metrics(sharpe=3.0, deflated_sharpe=0.30),
        monte_carlo=mc(),
        stressed_cost_return=0.1,
        benchmark_returns={"buy_and_hold": 0.1},
        paper=good_paper(),
        human_approved=True,
    )
    assert any(g.number == 3 for g in report.failures)


def test_live_allocation_is_zero_until_every_gate_is_green():
    failing = GateReport(gates=[])
    assert initial_live_allocation(Decimal("100000000"), failing) == Decimal(0)


def test_live_allocation_starts_at_two_percent():
    report = evaluate_gates(
        walk_forward=FakeWalkForward(0.8, -0.10),
        out_of_sample=metrics(),
        monte_carlo=mc(),
        stressed_cost_return=0.12,
        benchmark_returns={"buy_and_hold": 0.2},
        paper=good_paper(),
        human_approved=True,
    )
    assert initial_live_allocation(Decimal("100000000"), report) == Decimal("2000000")


def test_report_renders_a_verdict():
    report = evaluate_gates(
        walk_forward=FakeWalkForward(0.1, -0.4),
        out_of_sample=metrics(total_return=-0.2),
        monte_carlo=mc(probability_of_ruin=0.5),
        stressed_cost_return=-0.5,
        benchmark_returns={"buy_and_hold": 0.3},
        paper=None,
        human_approved=False,
    )
    text = report.render()
    assert "NO-GO" in text
    assert "FAIL" in text


# ---------------------------------------------------------------- feasibility


def test_modest_targets_are_plausible_and_extreme_ones_are_not():
    modest = target_feasibility(0.02, 0.011, 20)
    extreme = target_feasibility(2.00, 0.011, 60)
    assert modest["verdict"] == "plausible"
    assert "ruin" in str(extreme["verdict"])
    assert extreme["annual_multiple"] > 500_000


def test_feasibility_exposes_how_much_of_gross_the_venue_takes():
    result = target_feasibility(0.02, 0.011, 60)
    assert result["cost_share_of_gross"] > 0.8      # frequency is a cost decision


def test_feasibility_rejects_nonsense_frequency():
    with pytest.raises(ValueError, match="trades_per_month"):
        target_feasibility(0.02, 0.01, 0)
