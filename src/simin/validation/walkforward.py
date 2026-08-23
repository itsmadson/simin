"""Walk-forward analysis.

A single backtest over one period answers the wrong question. The right question
is whether the process — choose parameters on what you knew, then trade what came
next — produced money repeatedly. Every window is reported, never just the mean:
a strategy that works in one window out of six is a curve fit with good manners.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from simin.backtest.engine import BacktestConfig, Backtester, BacktestResult
from simin.risk.engine import RiskEngine
from simin.strategies.base import Strategy
from simin.types import Bar

StrategyFactory = Callable[[], Strategy]


@dataclass(frozen=True, slots=True)
class Window:
    index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    result: BacktestResult

    @property
    def total_return(self) -> float:
        return self.result.metrics.total_return

    @property
    def sharpe(self) -> float:
        return self.result.metrics.sharpe


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    strategy: str
    windows: list[Window]

    @property
    def n_windows(self) -> int:
        return len(self.windows)

    @property
    def profitable_windows(self) -> int:
        return sum(1 for w in self.windows if w.total_return > 0)

    @property
    def consistency(self) -> float:
        """Fraction of windows in profit. The number gates read."""
        return self.profitable_windows / self.n_windows if self.windows else 0.0

    @property
    def worst_window(self) -> float:
        return min((w.total_return for w in self.windows), default=0.0)

    @property
    def mean_sharpe(self) -> float:
        if not self.windows:
            return 0.0
        return sum(w.sharpe for w in self.windows) / len(self.windows)

    @property
    def compounded_return(self) -> float:
        """Return of actually trading every window in sequence."""
        total = 1.0
        for w in self.windows:
            total *= 1 + w.total_return
        return total - 1

    def table(self) -> str:
        header = (
            f"{'window':>6} {'test start':<12} {'return':>9} "
            f"{'sharpe':>8} {'maxdd':>8} {'trades':>7}"
        )
        lines = [header]
        for w in self.windows:
            m = w.result.metrics
            lines.append(
                f"{w.index:>6} {w.test_start.date().isoformat():<12} "
                f"{m.total_return:>8.2%} {m.sharpe:>8.2f} {m.max_drawdown:>7.1%} {m.n_trades:>7}"
            )
        lines.append(
            f"consistency {self.consistency:.0%} across {self.n_windows} windows, "
            f"worst {self.worst_window:.1%}, compounded {self.compounded_return:.1%}"
        )
        return "\n".join(lines)


def walk_forward(
    bars: Sequence[Bar],
    factory: StrategyFactory,
    risk: RiskEngine,
    *,
    train_bars: int,
    test_bars: int,
    step_bars: int | None = None,
    config: BacktestConfig | None = None,
    optimizer: Callable[[Sequence[Bar]], Strategy] | None = None,
) -> WalkForwardReport:
    """Roll a train/test window across the series.

    ``optimizer`` may fit parameters on the training slice; it is handed *only*
    that slice. The test slice is never passed to it, which is the entire point:
    the moment the optimiser sees the test data, the test stops being a test.
    """
    step = step_bars or test_bars
    windows: list[Window] = []
    start = 0
    index = 0
    while start + train_bars + test_bars <= len(bars):
        train = bars[start : start + train_bars]
        test = bars[start + train_bars : start + train_bars + test_bars]
        strategy = optimizer(train) if optimizer else factory()
        # The test run is warmed up on training bars so indicators are mature,
        # while trades can only occur inside the test window.
        warmup_tail = train[-min(len(train), 300) :]
        combined = [*warmup_tail, *test]
        result = Backtester(risk, config or BacktestConfig()).run(combined, strategy)
        cut = len(warmup_tail)
        trimmed = BacktestResult(
            strategy=result.strategy,
            symbol=result.symbol,
            tf=result.tf,
            equity=result.equity[cut:],
            stamps=result.stamps[cut:],
            trades=[t for t in result.trades if t.opened_at >= test[0].ts],
            metrics=result.metrics,
            rejections=result.rejections,
            ended_by_kill_switch=result.ended_by_kill_switch,
            kill_reason=result.kill_reason,
        )
        windows.append(
            Window(
                index=index,
                train_start=train[0].ts,
                train_end=train[-1].ts,
                test_start=test[0].ts,
                test_end=test[-1].ts,
                result=trimmed,
            )
        )
        start += step
        index += 1
    return WalkForwardReport(strategy=factory().name, windows=windows)


def split_holdout(
    bars: Sequence[Bar], holdout_fraction: float = 0.2
) -> tuple[list[Bar], list[Bar]]:
    """Chronological research/holdout split.

    The holdout is opened once, at the end. Looking at it, adjusting, and looking
    again converts it into another training set — expensively, because you will
    still believe its number.
    """
    if not 0 < holdout_fraction < 1:
        raise ValueError("holdout_fraction must be in (0, 1)")
    cut = int(len(bars) * (1 - holdout_fraction))
    return list(bars[:cut]), list(bars[cut:])


def equity_to_returns(equity: Sequence[float]) -> list[float]:
    return [
        (equity[i] / equity[i - 1] - 1.0) if equity[i - 1] else 0.0 for i in range(1, len(equity))
    ]


def starting_equity_of(config: BacktestConfig | None) -> Decimal:
    return (config or BacktestConfig()).starting_equity
