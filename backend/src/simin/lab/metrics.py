"""Performance measurement.

Two principles shape everything here:

**Report the distribution, not the point.** A single "return" number hides
whether it came from one lucky trade or two hundred consistent ones. Every
summary carries the drawdown, the trade count, and the dispersion alongside the
return, because the risk dial's whole promise is a trade-off and you cannot see
a trade-off from one number.

**Every metric is net.** Fees, spread, slippage and funding are already
subtracted before anything reaches this module. There is no "gross" figure
exposed anywhere, because gross performance is the number that convinces people
to trade systems that lose money.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from simin.core.types import EquityPoint, ExitReason, Trade


@dataclass(frozen=True, slots=True)
class Metrics:
    """The full picture for one run."""

    start_equity: float
    end_equity: float
    total_return: float
    monthly_return: float
    annualised_return: float
    max_drawdown: float
    max_drawdown_duration_days: float
    sharpe: float
    sortino: float
    calmar: float
    trades: int
    win_rate: float
    profit_factor: float
    expectancy_r: float
    avg_win_r: float
    avg_loss_r: float
    largest_win: float
    largest_loss: float
    max_consecutive_losses: int
    avg_bars_held: float
    trades_per_month: float
    total_fees: float
    total_funding: float
    cost_drag: float
    exposure: float
    exit_breakdown: dict[str, int] = field(default_factory=dict)
    days: float = 0.0

    @property
    def is_viable(self) -> bool:
        """A rough first filter. Not a substitute for the validation gates."""
        return (
            self.trades >= 30
            and self.profit_factor > 1.1
            and self.total_return > 0
            and self.max_drawdown < 0.5
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "start_equity": self.start_equity,
            "end_equity": self.end_equity,
            "total_return": self.total_return,
            "monthly_return": self.monthly_return,
            "annualised_return": self.annualised_return,
            "max_drawdown": self.max_drawdown,
            "max_drawdown_duration_days": self.max_drawdown_duration_days,
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "calmar": self.calmar,
            "trades": self.trades,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "expectancy_r": self.expectancy_r,
            "avg_win_r": self.avg_win_r,
            "avg_loss_r": self.avg_loss_r,
            "largest_win": self.largest_win,
            "largest_loss": self.largest_loss,
            "max_consecutive_losses": self.max_consecutive_losses,
            "avg_bars_held": self.avg_bars_held,
            "trades_per_month": self.trades_per_month,
            "total_fees": self.total_fees,
            "total_funding": self.total_funding,
            "cost_drag": self.cost_drag,
            "exposure": self.exposure,
            "exit_breakdown": self.exit_breakdown,
            "days": self.days,
            "is_viable": self.is_viable,
        }


def _f(x: Decimal | float) -> float:
    return float(x)


def max_drawdown(equity: Sequence[float]) -> tuple[float, int, int]:
    """Deepest peak-to-trough fall, and the indices where it began and ended."""
    if not equity:
        return 0.0, 0, 0
    peak = equity[0]
    peak_i = 0
    worst = 0.0
    start = end = 0
    for i, v in enumerate(equity):
        if v > peak:
            peak, peak_i = v, i
        elif peak > 0:
            dd = (peak - v) / peak
            if dd > worst:
                worst, start, end = dd, peak_i, i
    return worst, start, end


def _drawdown_duration_days(curve: Sequence[EquityPoint]) -> float:
    """Longest time spent below a previous high. Often more painful than depth:
    a 15% drawdown recovered in a week is nothing; the same 15% lasting eight
    months is what makes people turn the bot off at the bottom."""
    if len(curve) < 2:
        return 0.0
    peak = _f(curve[0].equity)
    peak_ts = curve[0].ts
    longest = timedelta(0)
    for point in curve:
        eq = _f(point.equity)
        if eq >= peak:
            longest = max(longest, point.ts - peak_ts)
            peak, peak_ts = eq, point.ts
    longest = max(longest, curve[-1].ts - peak_ts)
    return longest.total_seconds() / 86400


def _period_returns(curve: Sequence[EquityPoint]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(curve)):
        prev = _f(curve[i - 1].equity)
        if prev > 0:
            out.append(_f(curve[i].equity) / prev - 1.0)
    return out


def compute(
    trades: Sequence[Trade],
    curve: Sequence[EquityPoint],
    start_equity: Decimal,
    periods_per_year: float = 365.0,
) -> Metrics:
    """Everything, from a trade list and an equity curve.

    `periods_per_year` is how many equity samples make a year — 365 for daily
    marks, 4380 for 2h bars. Getting it wrong scales Sharpe by a constant, which
    is how implausible Sharpe ratios get published.
    """
    start = _f(start_equity)
    if not curve:
        return _empty(start)

    end = _f(curve[-1].equity)
    equity = [_f(p.equity) for p in curve]
    days = max((curve[-1].ts - curve[0].ts).total_seconds() / 86400, 1e-9)
    years = days / 365.0
    months = days / 30.44

    total_return = end / start - 1.0 if start > 0 else 0.0
    # Geometric, not arithmetic. Averaging monthly percentages is the trick that
    # turns +50%/−50% into "0% average" when it is really −25%.
    if start > 0 and end > 0 and months > 0:
        monthly = (end / start) ** (1.0 / months) - 1.0
        annualised = (end / start) ** (1.0 / max(years, 1e-9)) - 1.0
    else:
        monthly = annualised = -1.0

    dd, _, _ = max_drawdown(equity)
    dd_days = _drawdown_duration_days(curve)

    rets = _period_returns(curve)
    sharpe = sortino = 0.0
    if len(rets) > 2:
        mean = statistics.fmean(rets)
        sd = statistics.pstdev(rets)
        if sd > 0:
            sharpe = mean / sd * math.sqrt(periods_per_year)
        downside = [r for r in rets if r < 0]
        if len(downside) > 1:
            dsd = statistics.pstdev(downside)
            if dsd > 0:
                sortino = mean / dsd * math.sqrt(periods_per_year)

    calmar = annualised / dd if dd > 0 else 0.0

    n = len(trades)
    if n == 0:
        return Metrics(
            start_equity=start, end_equity=end, total_return=total_return,
            monthly_return=monthly, annualised_return=annualised, max_drawdown=dd,
            max_drawdown_duration_days=dd_days, sharpe=sharpe, sortino=sortino,
            calmar=calmar, trades=0, win_rate=0.0, profit_factor=0.0,
            expectancy_r=0.0, avg_win_r=0.0, avg_loss_r=0.0, largest_win=0.0,
            largest_loss=0.0, max_consecutive_losses=0, avg_bars_held=0.0,
            trades_per_month=0.0, total_fees=0.0, total_funding=0.0, cost_drag=0.0,
            exposure=0.0, days=days,
        )

    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    gross_win = sum(_f(t.net_pnl) for t in wins)
    gross_loss = abs(sum(_f(t.net_pnl) for t in losses))
    # A run with no losers has an undefined profit factor. Reporting `inf` is
    # honest; reporting a large finite number implies a precision that is not
    # there, and 30 trades without a loss means the sample is too small anyway.
    profit_factor = gross_win / gross_loss if gross_loss > 0 else math.inf

    r_values = [_f(t.r_multiple) for t in trades]
    win_r = [r for r in r_values if r > 0]
    loss_r = [r for r in r_values if r <= 0]

    streak = worst_streak = 0
    for t in trades:
        if t.net_pnl <= 0:
            streak += 1
            worst_streak = max(worst_streak, streak)
        else:
            streak = 0

    fees = sum(_f(t.fees) for t in trades)
    funding = sum(_f(t.funding) for t in trades)
    gross_pnl = sum(_f(t.gross_pnl) for t in trades)
    cost_drag = (fees + abs(funding)) / abs(gross_pnl) if gross_pnl else 0.0

    exits: dict[str, int] = {}
    for t in trades:
        exits[t.reason.value] = exits.get(t.reason.value, 0) + 1

    bars_in_market = sum(t.bars_held for t in trades)
    exposure = bars_in_market / len(curve) if curve else 0.0

    return Metrics(
        start_equity=start,
        end_equity=end,
        total_return=total_return,
        monthly_return=monthly,
        annualised_return=annualised,
        max_drawdown=dd,
        max_drawdown_duration_days=dd_days,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        trades=n,
        win_rate=len(wins) / n,
        profit_factor=profit_factor,
        expectancy_r=statistics.fmean(r_values),
        avg_win_r=statistics.fmean(win_r) if win_r else 0.0,
        avg_loss_r=statistics.fmean(loss_r) if loss_r else 0.0,
        largest_win=max((_f(t.net_pnl) for t in trades), default=0.0),
        largest_loss=min((_f(t.net_pnl) for t in trades), default=0.0),
        max_consecutive_losses=worst_streak,
        avg_bars_held=statistics.fmean([t.bars_held for t in trades]),
        trades_per_month=n / months if months > 0 else 0.0,
        total_fees=fees,
        total_funding=funding,
        cost_drag=cost_drag,
        exposure=min(exposure, 1.0),
        exit_breakdown=dict(sorted(exits.items(), key=lambda kv: -kv[1])),
        days=days,
    )


def _empty(start: float) -> Metrics:
    return Metrics(
        start_equity=start, end_equity=start, total_return=0.0, monthly_return=0.0,
        annualised_return=0.0, max_drawdown=0.0, max_drawdown_duration_days=0.0,
        sharpe=0.0, sortino=0.0, calmar=0.0, trades=0, win_rate=0.0,
        profit_factor=0.0, expectancy_r=0.0, avg_win_r=0.0, avg_loss_r=0.0,
        largest_win=0.0, largest_loss=0.0, max_consecutive_losses=0,
        avg_bars_held=0.0, trades_per_month=0.0, total_fees=0.0, total_funding=0.0,
        cost_drag=0.0, exposure=0.0,
    )
