"""End-to-end research run: benchmark suite, walk-forward, Monte Carlo, gates.

This is the module that answers "does this work?" honestly. It runs the strategy
and every benchmark over identical data and identical costs, repeats the exercise
at double cost, walks it forward, stresses it, and then renders a Go/No-Go
verdict. It is designed so that a bad strategy produces a clear NO-GO rather than
an ambiguous pile of numbers.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from simin.backtest.costs import CostModel
from simin.backtest.engine import BacktestConfig, Backtester, BacktestResult
from simin.exchanges.venues import profile
from simin.risk.engine import RiskEngine
from simin.strategies import BENCHMARKS, build
from simin.types import Bar
from simin.validation.gates import GateReport, PaperRecord, evaluate_gates
from simin.validation.montecarlo import MonteCarloReport, simulate
from simin.validation.walkforward import WalkForwardReport, split_holdout, walk_forward


@dataclass(frozen=True, slots=True)
class ResearchReport:
    strategy: str
    in_sample: BacktestResult
    out_of_sample: BacktestResult
    stressed: BacktestResult
    benchmarks: dict[str, BacktestResult]
    walk_forward: WalkForwardReport
    monte_carlo: MonteCarloReport
    gates: GateReport

    def render(self) -> str:
        lines = [
            f"STRATEGY: {self.strategy}",
            "=" * 78,
            "",
            "IN SAMPLE      " + self.in_sample.metrics.summary(),
            "OUT OF SAMPLE  " + self.out_of_sample.metrics.summary(),
            "AT 2x COST     " + self.stressed.metrics.summary(),
            "",
            "BENCHMARKS (out of sample)",
        ]
        for name, result in sorted(
            self.benchmarks.items(), key=lambda kv: kv[1].metrics.total_return, reverse=True
        ):
            marker = (
                "beaten"
                if self.out_of_sample.metrics.total_return > result.metrics.total_return
                else "LOSES TO THIS"
            )
            lines.append(f"  {name:<20} {result.metrics.total_return:>8.2%}  {marker}")
        lines += [
            "",
            "WALK FORWARD",
            self.walk_forward.table(),
            "",
            "MONTE CARLO",
            "  " + self.monte_carlo.summary(),
            "",
            self.gates.render(),
        ]
        return "\n".join(lines)


def run_research(
    bars: Sequence[Bar],
    strategy_name: str,
    *,
    risk: RiskEngine,
    starting_equity: Decimal = Decimal("100000000"),
    venue_code: str = "local_irt_generic",
    n_trials: int = 1,
    train_bars: int = 2000,
    test_bars: int = 500,
    paper: PaperRecord | None = None,
    human_approved: bool = False,
    monte_carlo_runs: int = 5000,
) -> ResearchReport:
    venue = profile(venue_code)
    cost = CostModel(fees=venue.fees, spread_bps=venue.typical_spread_bps)
    config = BacktestConfig(starting_equity=starting_equity, cost=cost, n_trials=n_trials)
    stressed_config = BacktestConfig(
        starting_equity=starting_equity, cost=cost.scaled(Decimal(2)), n_trials=n_trials
    )

    research_bars, holdout_bars = split_holdout(bars, 0.25)

    in_sample = Backtester(risk, config).run(research_bars, build(strategy_name))
    out_of_sample = Backtester(risk, config).run(holdout_bars, build(strategy_name))
    stressed = Backtester(risk, stressed_config).run(holdout_bars, build(strategy_name))

    benchmarks = {
        name: Backtester(risk, config).run(holdout_bars, build(name)) for name in BENCHMARKS
    }

    wf = walk_forward(
        bars, lambda: build(strategy_name), risk,
        train_bars=train_bars, test_bars=test_bars, config=config,
    )
    mc = simulate(out_of_sample.trades, n_simulations=monte_carlo_runs)

    gates = evaluate_gates(
        walk_forward=wf,
        out_of_sample=out_of_sample.metrics,
        monte_carlo=mc,
        stressed_cost_return=stressed.metrics.total_return,
        benchmark_returns={k: v.metrics.total_return for k, v in benchmarks.items()},
        paper=paper,
        human_approved=human_approved,
    )
    return ResearchReport(
        strategy=strategy_name,
        in_sample=in_sample,
        out_of_sample=out_of_sample,
        stressed=stressed,
        benchmarks=benchmarks,
        walk_forward=wf,
        monte_carlo=mc,
        gates=gates,
    )
