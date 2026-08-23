"""Go/No-Go gates for enabling live trading.

Twelve checks, evaluated mechanically. LIVE mode stays disabled until every one
passes and a human signs off. The gates exist because the moment a backtest
looks good is exactly the moment judgement is least reliable — so the decision is
delegated to a checklist written before the result was known.

Thresholds come from docs/03-risk-and-validation.md §5.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from simin.backtest.metrics import Metrics
from simin.validation.montecarlo import MonteCarloReport
from simin.validation.walkforward import WalkForwardReport


@dataclass(frozen=True, slots=True)
class Gate:
    number: int
    name: str
    passed: bool
    observed: str
    required: str

    def line(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        return (
            f"[{mark}] {self.number:>2}. {self.name:<34} "
            f"{self.observed:>18}  (need {self.required})"
        )


@dataclass(frozen=True, slots=True)
class GateReport:
    gates: list[Gate] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.gates) and all(g.passed for g in self.gates)

    @property
    def failures(self) -> list[Gate]:
        return [g for g in self.gates if not g.passed]

    def render(self) -> str:
        header = "GO / NO-GO REPORT"
        body = "\n".join(g.line() for g in self.gates)
        verdict = (
            "VERDICT: GO — all gates green. Live trading may be enabled by a human, "
            "starting at <=2% of intended capital."
            if self.passed
            else f"VERDICT: NO-GO — {len(self.failures)} gate(s) failed. "
            "Live trading stays disabled."
        )
        return f"{header}\n{'=' * len(header)}\n{body}\n\n{verdict}"


@dataclass(frozen=True, slots=True)
class PaperRecord:
    """What paper trading must demonstrate before real money is risked."""

    days_running: int
    closed_trades: int
    realized_sharpe: float
    max_drawdown: float
    slippage_ratio: float  # realized / modelled
    unhandled_exceptions: int
    reconciliation_mismatches: int


def evaluate_gates(
    *,
    walk_forward: WalkForwardReport,
    out_of_sample: Metrics,
    monte_carlo: MonteCarloReport,
    stressed_cost_return: float,
    benchmark_returns: dict[str, float],
    paper: PaperRecord | None = None,
    human_approved: bool = False,
) -> GateReport:
    """Evaluate all twelve gates. Missing paper evidence fails, never skips."""
    gates: list[Gate] = []

    gates.append(
        Gate(1, "walk-forward consistency", walk_forward.consistency >= 0.70,
             f"{walk_forward.consistency:.0%}", ">=70%")
    )
    gates.append(
        Gate(2, "worst walk-forward window", walk_forward.worst_window >= -0.15,
             f"{walk_forward.worst_window:.1%}", ">=-15%")
    )
    gates.append(
        Gate(3, "deflated Sharpe (OOS)", out_of_sample.deflated_sharpe >= 0.95,
             f"{out_of_sample.deflated_sharpe:.2f}", ">=0.95")
    )
    gates.append(
        Gate(4, "Monte Carlo P(ruin)", monte_carlo.probability_of_ruin <= 0.01,
             f"{monte_carlo.probability_of_ruin:.2%}", "<=1%")
    )
    gates.append(
        Gate(5, "survives 2x modelled cost", stressed_cost_return > 0,
             f"{stressed_cost_return:.1%}", ">0%")
    )
    beaten = all(out_of_sample.total_return > v for v in benchmark_returns.values())
    worst_benchmark = (
        max(benchmark_returns, key=lambda k: benchmark_returns[k]) if benchmark_returns else "n/a"
    )
    gates.append(
        Gate(6, "beats every benchmark", beaten,
             f"{out_of_sample.total_return:.1%} vs {worst_benchmark}", "strictly better")
    )
    gates.append(
        Gate(7, "max drawdown within MC p95",
             out_of_sample.max_drawdown >= monte_carlo.p95_max_drawdown,
             f"{out_of_sample.max_drawdown:.1%}", f">={monte_carlo.p95_max_drawdown:.1%}")
    )
    gates.append(
        Gate(8, "trade count is meaningful", out_of_sample.n_trades >= 100,
             str(out_of_sample.n_trades), ">=100")
    )

    if paper is None:
        for number, name in (
            (9, "paper trading duration"),
            (10, "paper closed trades"),
            (11, "operational stability"),
        ):
            gates.append(Gate(number, name, False, "no paper record", "see docs/03 §5"))
    else:
        gates.append(
            Gate(9, "paper trading duration", paper.days_running >= 60,
                 f"{paper.days_running}d", ">=60d")
        )
        gates.append(
            Gate(10, "paper closed trades", paper.closed_trades >= 200,
                 str(paper.closed_trades), ">=200")
        )
        stable = (
            paper.unhandled_exceptions == 0
            and paper.reconciliation_mismatches == 0
            and paper.slippage_ratio <= 1.5
        )
        gates.append(
            Gate(11, "operational stability", stable,
                 f"exc={paper.unhandled_exceptions} slip={paper.slippage_ratio:.2f}x",
                 "0 exceptions, slippage <=1.5x")
        )

    gates.append(Gate(12, "human approval recorded", human_approved,
                      "yes" if human_approved else "no", "explicit sign-off"))
    return GateReport(gates=gates)


def initial_live_allocation(intended_capital: Decimal, gates: GateReport) -> Decimal:
    """Live trading starts at 2% of intended capital — or zero if any gate is red.

    Scaling up is a separate decision made after 30 further days of matched
    performance, not something the code does on its own.
    """
    if not gates.passed:
        return Decimal(0)
    return (intended_capital * Decimal("0.02")).quantize(Decimal("1"))


def target_feasibility(
    monthly_target: float, round_trip_cost: float, trades_per_month: int
) -> dict[str, float | str]:
    """What a monthly return target demands, stated plainly.

    Kept as code rather than prose so the number is recomputed from the current
    cost model every time it is shown, instead of being an opinion in a README.
    """
    if trades_per_month <= 0:
        raise ValueError("trades_per_month must be positive")
    annual = (1 + monthly_target) ** 12 - 1
    net_per_trade = (1 + monthly_target) ** (1 / trades_per_month) - 1
    gross_per_trade = net_per_trade + round_trip_cost
    cost_share = round_trip_cost / gross_per_trade if gross_per_trade > 0 else 1.0

    # Judged on the NET edge the strategy must produce above costs. Documented
    # systematic strategies land around 0.1-0.3% net per trade; 0.5% is already
    # exceptional and 1%+ is not observed persistently in liquid crypto.
    verdict = (
        "plausible" if net_per_trade <= 0.002
        else "hard but documented" if net_per_trade <= 0.005
        else "exceptional, treat any backtest showing it as a bug hunt"
        if net_per_trade <= 0.010
        else "not achievable without leverage at which ruin is near-certain"
    )
    # Trade frequency is a cost decision before it is a strategy decision: when
    # fees eat more than half of gross, the venue is the counterparty that wins.
    max_sane_trades = (
        int((0.693 / round_trip_cost) * monthly_target) if round_trip_cost > 0 else trades_per_month
    )
    return {
        "monthly_target": monthly_target,
        "annual_multiple": (1 + monthly_target) ** 12,
        "annual_return": annual,
        "net_edge_per_trade": net_per_trade,
        "gross_edge_per_trade": gross_per_trade,
        "cost_share_of_gross": cost_share,
        "max_trades_before_costs_dominate": max_sane_trades,
        "verdict": verdict,
    }
