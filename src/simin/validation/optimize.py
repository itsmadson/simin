"""Parameter search that cannot lie to you.

Two rules are enforced structurally rather than by discipline:

1. The search only ever sees the training window. The holdout is a separate
   argument, evaluated once, after the winner has been chosen.
2. Every configuration tried is counted, and that count feeds the deflated
   Sharpe ratio. Picking the best of forty configurations and reporting its raw
   Sharpe is how a noise-fitting exercise gets published as a strategy.

The winner is chosen on a robustness criterion, not on return. A configuration
that made the most money in-sample is usually the one that fitted the training
window best, which is precisely what should not be selected.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from simin.backtest.portfolio import PortfolioBacktester, PortfolioConfig, PortfolioResult
from simin.risk.engine import RiskEngine
from simin.strategies.base import Strategy
from simin.types import Bar

StrategyFactory = Callable[[Mapping[str, Any]], list[Strategy]]


@dataclass(frozen=True, slots=True)
class Trial:
    params: Mapping[str, Any]
    result: PortfolioResult

    @property
    def score(self) -> float:
        """Robustness score: risk-adjusted, penalised for inactivity and churn.

        Deliberately not total return. Return alone selects the configuration
        that best fitted the training window; Sharpe with a trade-count floor
        selects one that was right repeatedly.
        """
        m = self.result.metrics
        if m.n_trades < 40:
            return -99.0          # too few trades to distinguish skill from luck
        if m.max_drawdown < -0.5:
            return -99.0          # would not have been survivable
        return m.sharpe


@dataclass(slots=True)
class SweepReport:
    trials: list[Trial] = field(default_factory=list)
    holdout: PortfolioResult | None = None
    holdout_params: Mapping[str, Any] | None = None
    n_trials: int = 0

    @property
    def best(self) -> Trial | None:
        ranked = sorted(self.trials, key=lambda t: t.score, reverse=True)
        return ranked[0] if ranked else None

    def table(self, limit: int = 12) -> str:
        lines = [f"{'score':>7} {'return':>9} {'sharpe':>7} {'maxdd':>8} {'trades':>7} params"]
        for trial in sorted(self.trials, key=lambda t: t.score, reverse=True)[:limit]:
            m = trial.result.metrics
            params = " ".join(f"{k}={v}" for k, v in trial.params.items())
            lines.append(
                f"{trial.score:>7.2f} {m.total_return:>8.2%} {m.sharpe:>7.2f} "
                f"{m.max_drawdown:>7.1%} {m.n_trades:>7} {params}"
            )
        return "\n".join(lines)

    def verdict(self) -> str:
        if self.best is None or self.holdout is None:
            return "no verdict: sweep incomplete"
        train = self.best.result.metrics
        test = self.holdout.metrics
        held_up = test.total_return > 0 and test.sharpe > 0
        decay = (
            (test.sharpe / train.sharpe) if train.sharpe > 0 else 0.0
        )
        lines = [
            f"train  {train.summary()}",
            f"holdout {test.summary()}",
            f"trials tried: {self.n_trials}  |  deflated Sharpe (holdout): "
            f"{test.deflated_sharpe:.2f}  |  Sharpe retention: {decay:.0%}",
        ]
        if held_up and test.deflated_sharpe >= 0.95:
            lines.append("VERDICT: the edge survived out of sample.")
        elif held_up:
            lines.append(
                "VERDICT: positive out of sample, but the deflated Sharpe does not clear "
                "0.95 given the number of configurations tried. Suggestive, not proven."
            )
        else:
            lines.append(
                "VERDICT: the training result did not survive. The parameters fitted the "
                "training window. This configuration is INVALID."
            )
        return "\n".join(lines)


def grid(**axes: Sequence[Any]) -> list[dict[str, Any]]:
    """Cartesian product of parameter axes, as a list of kwargs dicts."""
    keys = list(axes)
    return [dict(zip(keys, values, strict=True)) for values in itertools.product(*axes.values())]


def sweep(
    train: Mapping[str, Sequence[Bar]],
    holdout: Mapping[str, Sequence[Bar]],
    combos: Sequence[Mapping[str, Any]],
    factory: StrategyFactory,
    *,
    risk: RiskEngine,
    config: PortfolioConfig,
    on_progress: Callable[[int, int, Trial], None] | None = None,
) -> SweepReport:
    """Search ``combos`` on ``train``, then evaluate the winner once on ``holdout``."""
    report = SweepReport(n_trials=len(combos))
    for i, params in enumerate(combos, start=1):
        result = PortfolioBacktester(risk, config).run(train, factory(params))
        trial = Trial(params=params, result=result)
        report.trials.append(trial)
        if on_progress:
            on_progress(i, len(combos), trial)

    best = report.best
    if best is None:
        return report

    # The holdout is opened exactly once, with the trial count carried in so the
    # deflated Sharpe accounts for the whole search.
    holdout_config = PortfolioConfig(
        starting_equity=config.starting_equity,
        cost=config.cost,
        use_regime_filter=config.use_regime_filter,
        trail_atr_multiple=config.trail_atr_multiple,
        max_hold_bars=config.max_hold_bars,
        max_participation=config.max_participation,
        regime_config=config.regime_config,
        n_trials=len(combos),
    )
    report.holdout = PortfolioBacktester(risk, holdout_config).run(
        holdout, factory(best.params)
    )
    report.holdout_params = best.params
    return report


def split_by_date(
    series: Mapping[str, Sequence[Bar]], cutoff: datetime
) -> tuple[dict[str, list[Bar]], dict[str, list[Bar]]]:
    """Chronological split. Nothing after ``cutoff`` may influence the search."""
    train: dict[str, list[Bar]] = {}
    test: dict[str, list[Bar]] = {}
    for symbol, bars in series.items():
        before = [b for b in bars if b.ts < cutoff]
        after = [b for b in bars if b.ts >= cutoff]
        if before:
            train[symbol] = before
        if after:
            test[symbol] = after
    return train, test


def starting_equity(value: str | Decimal) -> Decimal:
    return Decimal(str(value))
