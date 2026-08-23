"""Monte Carlo stress testing.

A backtest is one path. The strategy will not get that path again. These
simulations ask what the *distribution* of outcomes looks like when the same
edge meets a different ordering of luck, slightly worse fills, and the loss of
its best trades.

The output that matters is not the average — it is the 5th percentile and the
probability of ruin.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass

from simin.backtest.metrics import TradeStat, max_drawdown


@dataclass(frozen=True, slots=True)
class MonteCarloReport:
    n_simulations: int
    probability_of_profit: float
    probability_of_ruin: float
    median_return: float
    p05_return: float
    p95_return: float
    median_max_drawdown: float
    p95_max_drawdown: float
    worst_return: float
    best_return: float

    def summary(self) -> str:
        return (
            f"P(profit)={self.probability_of_profit:.0%} P(ruin)={self.probability_of_ruin:.2%} "
            f"return p5/p50/p95 = {self.p05_return:.1%}/{self.median_return:.1%}/"
            f"{self.p95_return:.1%} maxDD p50/p95 = {self.median_max_drawdown:.1%}/"
            f"{self.p95_max_drawdown:.1%}"
        )


def simulate(
    trades: Sequence[TradeStat],
    *,
    n_simulations: int = 10_000,
    ruin_threshold: float = 0.5,
    cost_perturbation: float = 0.5,
    drop_best_fraction: float = 0.05,
    seed: int = 0,
) -> MonteCarloReport:
    """Resample trade sequences and re-run the equity path.

    Three perturbations, each targeting a different way a backtest lies:

    * **Reordering** — the real sequence was one draw; clustering of losses is
      what actually causes ruin, and reordering exposes it.
    * **Cost perturbation** — fills will be worse than modelled sometimes.
    * **Dropping the best trades** — answers "was this just one lucky trade?",
      which for a fat-tailed strategy is a live possibility.
    """
    if not trades:
        return MonteCarloReport(0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    rng = random.Random(seed)
    returns = [t.return_pct for t in trades]
    n_drop = int(len(returns) * drop_best_fraction)

    finals: list[float] = []
    drawdowns: list[float] = []
    ruined = 0

    for _ in range(n_simulations):
        sample = [rng.choice(returns) for _ in returns]  # bootstrap with replacement
        if n_drop:
            best = sorted(range(len(sample)), key=lambda i: sample[i], reverse=True)[:n_drop]
            for i in best:
                sample[i] = 0.0
        equity = 1.0
        curve = [equity]
        for r in sample:
            noise = 1.0 + rng.uniform(-cost_perturbation, cost_perturbation)
            adjusted = r - abs(r) * 0.0 - (0.001 * noise if r != 0 else 0.0)
            equity *= 1 + adjusted
            if equity <= 0:
                equity = 0.0
                curve.append(equity)
                break
            curve.append(equity)
        finals.append(curve[-1] - 1.0)
        dd, _ = max_drawdown(curve)
        drawdowns.append(dd)
        if abs(dd) >= ruin_threshold:
            ruined += 1

    finals.sort()
    drawdowns.sort()

    def pct(values: list[float], p: float) -> float:
        if not values:
            return 0.0
        idx = min(len(values) - 1, max(0, int(p * len(values))))
        return values[idx]

    return MonteCarloReport(
        n_simulations=n_simulations,
        probability_of_profit=sum(1 for f in finals if f > 0) / len(finals),
        probability_of_ruin=ruined / n_simulations,
        median_return=pct(finals, 0.5),
        p05_return=pct(finals, 0.05),
        p95_return=pct(finals, 0.95),
        median_max_drawdown=pct(drawdowns, 0.5),
        p95_max_drawdown=pct(drawdowns, 0.05),  # drawdowns are negative: 5th pct is the worst tail
        worst_return=finals[0],
        best_return=finals[-1],
    )


def stress_scenarios() -> dict[str, dict[str, float]]:
    """Named shocks the system must survive without unbounded loss.

    Survival here means bounded, recoverable loss and correct state afterwards —
    not profit. A system that makes money in every scenario has been fitted to
    the scenarios.
    """
    return {
        "btc_crash_10": {"shock": -0.10, "vol_multiplier": 2.0},
        "btc_crash_20": {"shock": -0.20, "vol_multiplier": 3.0},
        "btc_crash_40": {"shock": -0.40, "vol_multiplier": 4.0},
        "flash_crash_recover": {"shock": -0.25, "recovery": 0.9, "vol_multiplier": 5.0},
        "sudden_pump": {"shock": 0.30, "vol_multiplier": 3.0},
        "spread_3x": {"spread_multiplier": 3.0},
        "fees_2x": {"fee_multiplier": 2.0},
        "slippage_2x": {"slippage_multiplier": 2.0},
        "venue_outage": {"missing_bars": 24.0},
        "extreme_volatility": {"vol_multiplier": 6.0},
    }
