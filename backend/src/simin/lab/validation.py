"""Walk-forward analysis and Monte Carlo — deciding whether a result is real.

A single backtest number is nearly worthless. Run enough configurations over one
history and something will look excellent by chance alone; that is not a
discovery, it is the multiple-comparisons problem with a candlestick chart.

Two defences:

**Walk-forward.** Split history into consecutive windows. Each window is scored
on data the configuration has never influenced. A strategy that works in-sample
and collapses out-of-sample was fitted to noise, and this is the test that says
so.

**Monte Carlo.** Resample the actual trade sequence thousands of times. The
realised equity curve is one draw from a distribution; reporting only that draw
hides how much of the outcome was ordering luck. What comes out is the range of
outcomes the same edge could plausibly have produced — including how often it
ends in ruin, which is the number the aggressive dial levels exist to expose.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from simin.core.types import TF, Symbol, Trade
from simin.exchanges.costs import CostModel
from simin.indicators.features import FeatureFrame
from simin.lab.backtest import Backtester, BacktestResult
from simin.lab.metrics import Metrics
from simin.risk.dial import RiskProfile
from simin.strategies.base import Strategy, build_many


@dataclass(frozen=True, slots=True)
class Window:
    index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int


def rolling_windows(
    total_bars: int, train_bars: int, test_bars: int, warmup: int
) -> list[Window]:
    """Consecutive train/test splits, walking forward with no overlap in test.

    Test windows never overlap, so out-of-sample bars are counted once and only
    once. Overlapping them inflates the apparent sample size, which makes a
    weak result look statistically solid.
    """
    if train_bars <= 0 or test_bars <= 0:
        raise ValueError("train and test window sizes must be positive")
    windows: list[Window] = []
    start = 0
    idx = 0
    while start + train_bars + test_bars <= total_bars:
        windows.append(
            Window(
                index=idx,
                train_start=start,
                train_end=start + train_bars,
                test_start=start + train_bars - warmup,
                test_end=start + train_bars + test_bars,
            )
        )
        start += test_bars
        idx += 1
    return windows


@dataclass(slots=True)
class WalkForwardResult:
    windows: list[Window]
    in_sample: list[Metrics]
    out_of_sample: list[Metrics]
    combined_oos: Metrics | None
    profile_level: int
    #: Every out-of-sample trade, pooled across windows. This is the largest
    #: genuinely unseen sample available, and it is what Monte Carlo resamples.
    oos_trades: list[Trade] = field(default_factory=list)

    @property
    def oos_consistency(self) -> float:
        """Fraction of out-of-sample windows that made money. Below ~0.5 the
        strategy is a coin flip regardless of the aggregate return."""
        if not self.out_of_sample:
            return 0.0
        return sum(1 for m in self.out_of_sample if m.total_return > 0) / len(self.out_of_sample)

    @property
    def degradation(self) -> float:
        """How much worse out-of-sample is than in-sample.

        Some degradation is normal and expected. Above ~0.6 the in-sample
        result was mostly curve fit.
        """
        ins = [m.total_return for m in self.in_sample]
        oos = [m.total_return for m in self.out_of_sample]
        if not ins or not oos:
            return 1.0
        mean_in = statistics.fmean(ins)
        mean_oos = statistics.fmean(oos)
        if mean_in <= 0:
            return 0.0 if mean_oos >= mean_in else 1.0
        return max(0.0, min((mean_in - mean_oos) / abs(mean_in), 1.0))

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_level": self.profile_level,
            "windows": len(self.windows),
            "oos_consistency": self.oos_consistency,
            "degradation": self.degradation,
            "in_sample": [m.to_dict() for m in self.in_sample],
            "out_of_sample": [m.to_dict() for m in self.out_of_sample],
            "combined_oos": self.combined_oos.to_dict() if self.combined_oos else None,
        }


def walk_forward(
    profile: RiskProfile,
    strategy_names: Sequence[str],
    frames: dict[str, FeatureFrame],
    symbols: dict[str, Symbol],
    tf: TF,
    costs: CostModel,
    starting_equity: Decimal = Decimal("10000"),
    train_bars: int = 2000,
    test_bars: int = 500,
) -> WalkForwardResult:
    """Run the configuration across rolling windows.

    Strategies are rebuilt for every window. Reusing an instance leaks state —
    `MacdMomentum` remembers the previous histogram, `RsiDivergence` keeps a
    price/RSI buffer — and that state carrying across a window boundary is a
    small but real information leak from one period into the next.
    """
    n = min(len(f) for f in frames.values())
    warmup = max(f.warmup_complete_at() for f in frames.values())
    windows = rolling_windows(n, train_bars, test_bars, warmup)
    if not windows:
        raise ValueError(
            f"{n} bars cannot fit even one {train_bars}+{test_bars} window; "
            "fetch more history or shrink the windows"
        )

    ins: list[Metrics] = []
    oos: list[Metrics] = []
    all_oos_trades: list[Trade] = []

    for w in windows:
        train = {k: FeatureFrame(k, tf, f.candles[w.train_start : w.train_end])
                 for k, f in frames.items()}
        test = {k: FeatureFrame(k, tf, f.candles[max(w.test_start, 0) : w.test_end])
                for k, f in frames.items()}
        try:
            r_in = Backtester(
                profile, build_many(strategy_names), costs, starting_equity
            ).run(train, symbols, tf)
            r_out = Backtester(
                profile, build_many(strategy_names), costs, starting_equity
            ).run(test, symbols, tf)
        except ValueError:
            continue
        ins.append(r_in.metrics)
        oos.append(r_out.metrics)
        all_oos_trades.extend(r_out.trades)

    combined = None
    if oos:
        combined = _combine(oos, all_oos_trades, starting_equity)

    return WalkForwardResult(windows, ins, oos, combined, profile.level, all_oos_trades)


def _combine(
    windows: Sequence[Metrics], trades: Sequence[Trade], start: Decimal
) -> Metrics:
    """Chain the out-of-sample windows into one continuous track record.

    Returns compound: three +10% windows are +33%, not +30%. Averaging them
    would be the arithmetic-mean error that flatters volatile systems.
    """
    from dataclasses import replace

    equity = float(start)
    for m in windows:
        equity *= 1.0 + m.total_return
    total_days = sum(m.days for m in windows)
    months = total_days / 30.44 if total_days else 0.0
    base = float(start)
    total_return = equity / base - 1.0 if base > 0 else 0.0
    monthly = (equity / base) ** (1.0 / months) - 1.0 if months > 0 and equity > 0 else -1.0
    years = total_days / 365.0

    n = len(trades)
    wins = [t for t in trades if t.net_pnl > 0]
    gross_win = sum(float(t.net_pnl) for t in wins)
    gross_loss = abs(sum(float(t.net_pnl) for t in trades if t.net_pnl <= 0))

    return replace(
        windows[0],
        start_equity=base,
        end_equity=equity,
        total_return=total_return,
        monthly_return=monthly,
        annualised_return=(equity / base) ** (1.0 / max(years, 1e-9)) - 1.0
        if equity > 0 and base > 0
        else -1.0,
        # The worst single-window drawdown is a floor on the true figure, not
        # the figure itself: a drawdown spanning a window boundary is deeper
        # than either window shows. Labelled as such wherever it is displayed.
        max_drawdown=max((m.max_drawdown for m in windows), default=0.0),
        sharpe=statistics.fmean([m.sharpe for m in windows]) if windows else 0.0,
        trades=n,
        win_rate=len(wins) / n if n else 0.0,
        profit_factor=gross_win / gross_loss if gross_loss > 0 else math.inf,
        expectancy_r=statistics.fmean([float(t.r_multiple) for t in trades]) if n else 0.0,
        trades_per_month=n / months if months > 0 else 0.0,
        days=total_days,
    )


# --- Monte Carlo ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MonteCarloResult:
    runs: int
    final_return_p05: float
    final_return_p25: float
    final_return_median: float
    final_return_p75: float
    final_return_p95: float
    max_drawdown_median: float
    max_drawdown_p95: float
    max_drawdown_worst: float
    #: Probability of ending below the starting balance.
    prob_loss: float
    #: Probability of losing half the account at any point.
    prob_half_loss: float
    #: Probability of the drawdown halt firing — the practical definition of
    #: ruin for this bot, since it stops trading there.
    prob_ruin: float
    monthly_return_median: float
    monthly_return_p05: float
    monthly_return_p95: float

    def to_dict(self) -> dict[str, object]:
        return {
            "runs": self.runs,
            "final_return_p05": self.final_return_p05,
            "final_return_p25": self.final_return_p25,
            "final_return_median": self.final_return_median,
            "final_return_p75": self.final_return_p75,
            "final_return_p95": self.final_return_p95,
            "max_drawdown_median": self.max_drawdown_median,
            "max_drawdown_p95": self.max_drawdown_p95,
            "max_drawdown_worst": self.max_drawdown_worst,
            "prob_loss": self.prob_loss,
            "prob_half_loss": self.prob_half_loss,
            "prob_ruin": self.prob_ruin,
            "monthly_return_median": self.monthly_return_median,
            "monthly_return_p05": self.monthly_return_p05,
            "monthly_return_p95": self.monthly_return_p95,
        }


#: Below this, a resampled distribution says more about the sample than the edge.
MIN_TRADES_FOR_MC = 20


def monte_carlo(
    trades: Sequence[Trade],
    starting_equity: Decimal,
    profile: RiskProfile,
    days_covered: float,
    runs: int = 2000,
    seed: int = 20260828,
) -> MonteCarloResult | None:
    """Resample the trade sequence to get a distribution instead of a point.

    Trades are resampled as *fractional* returns on the equity at the time, not
    as absolute currency amounts. Replaying absolute amounts assumes position
    size never adapts to the balance, which is false — the risk engine sizes
    from equity — and it systematically understates both the upside and the
    depth of drawdowns.

    Sampling is with replacement, which assumes trades are independent. They are
    not perfectly: losses cluster in bad regimes. So this understates tail risk
    somewhat, and the honest reading of `prob_ruin` is "at least this".
    """
    if len(trades) < MIN_TRADES_FOR_MC:
        # Resampling 8 trades produces a confident-looking distribution built
        # from almost nothing. Returning None makes the caller say "not enough
        # trades" instead of quoting a fabricated ruin probability.
        return None

    rng = random.Random(seed)
    start = float(starting_equity)
    # Each trade becomes the fraction of equity it returned. `r_multiple` is
    # already PnL measured in units of the risk taken, so multiplying by the
    # level's risk-per-trade converts it into an equity fraction that scales
    # correctly as the balance changes — which is what the risk engine actually
    # does, and what replaying raw currency amounts would get wrong.
    risk = float(profile.risk_per_trade)
    fractions = [
        float(t.r_multiple) * risk if t.r_multiple else float(t.net_pnl) / start
        for t in trades
    ]
    fractions = [f for f in fractions if math.isfinite(f)]
    if not fractions:
        return None

    n = len(fractions)
    months = max(days_covered / 30.44, 1e-9)
    halt_level = float(profile.max_drawdown_halt)

    finals: list[float] = []
    drawdowns: list[float] = []
    ruined = halved = lost = 0

    for _ in range(runs):
        equity = start
        peak = start
        worst = 0.0
        hit_halt = False
        hit_half = False
        for _ in range(n):
            equity *= 1.0 + rng.choice(fractions)
            if equity <= 0:
                equity = 0.0
                worst = 1.0
                hit_halt = hit_half = True
                break
            peak = max(peak, equity)
            dd = (peak - equity) / peak
            worst = max(worst, dd)
            if dd >= halt_level:
                hit_halt = True
            if equity <= start * 0.5:
                hit_half = True
        finals.append(equity / start - 1.0)
        drawdowns.append(worst)
        ruined += hit_halt
        halved += hit_half
        lost += equity < start

    finals.sort()
    drawdowns.sort()

    def pct(data: list[float], q: float) -> float:
        if not data:
            return 0.0
        i = min(int(q * (len(data) - 1)), len(data) - 1)
        return data[i]

    def monthly(total: float) -> float:
        return (1.0 + total) ** (1.0 / months) - 1.0 if total > -1.0 else -1.0

    return MonteCarloResult(
        runs=runs,
        final_return_p05=pct(finals, 0.05),
        final_return_p25=pct(finals, 0.25),
        final_return_median=pct(finals, 0.50),
        final_return_p75=pct(finals, 0.75),
        final_return_p95=pct(finals, 0.95),
        max_drawdown_median=pct(drawdowns, 0.50),
        max_drawdown_p95=pct(drawdowns, 0.95),
        max_drawdown_worst=drawdowns[-1],
        prob_loss=lost / runs,
        prob_half_loss=halved / runs,
        prob_ruin=ruined / runs,
        monthly_return_median=monthly(pct(finals, 0.50)),
        monthly_return_p05=monthly(pct(finals, 0.05)),
        monthly_return_p95=monthly(pct(finals, 0.95)),
    )


# --- Gates ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Gate:
    name: str
    passed: bool
    detail: str
    critical: bool = True


@dataclass(slots=True)
class GateReport:
    gates: list[Gate] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gates if g.critical)

    @property
    def failures(self) -> list[Gate]:
        return [g for g in self.gates if not g.passed]

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "gates": [
                {"name": g.name, "passed": g.passed, "detail": g.detail,
                 "critical": g.critical}
                for g in self.gates
            ],
        }


def evaluate_gates(
    oos: Metrics,
    stressed: Metrics | None,
    wf: WalkForwardResult | None,
    mc: MonteCarloResult | None,
    benchmark: Metrics | None,
    profile: RiskProfile,
) -> GateReport:
    """The checks a configuration must survive before REAL mode is offered.

    These are deliberately hard to pass. A configuration that fails them is not
    forbidden — the user can still run it — but the UI must say it failed and
    which gate, because "the bot is running a strategy that failed validation"
    is the single most important thing a trader can know about their bot.
    """
    r = GateReport()

    r.gates.append(Gate(
        "sample_size",
        oos.trades >= 30,
        f"{oos.trades} out-of-sample trades (need 30+ for any statistic to mean anything)",
    ))
    r.gates.append(Gate(
        "profitable_net_of_costs",
        oos.total_return > 0,
        f"out-of-sample return {oos.total_return:+.2%} after all fees, spread and slippage",
    ))
    r.gates.append(Gate(
        "profit_factor",
        oos.profit_factor > 1.15,
        f"profit factor {oos.profit_factor:.2f} (need > 1.15)",
    ))
    r.gates.append(Gate(
        "positive_expectancy",
        oos.expectancy_r > 0.03,
        f"expectancy {oos.expectancy_r:+.3f}R per trade (need > 0.03R)",
    ))
    r.gates.append(Gate(
        "drawdown_within_dial",
        oos.max_drawdown < float(profile.max_drawdown_halt),
        f"max drawdown {oos.max_drawdown:.1%} vs level {profile.level} halt at "
        f"{float(profile.max_drawdown_halt):.0%}",
    ))
    r.gates.append(Gate(
        "survives_double_costs",
        stressed is not None and stressed.total_return > 0,
        f"return at 2x costs: {stressed.total_return:+.2%}" if stressed
        else "not tested",
    ))
    r.gates.append(Gate(
        "cost_drag_tolerable",
        oos.cost_drag < 0.5,
        f"fees and funding consume {oos.cost_drag:.0%} of gross PnL (need < 50%)",
    ))
    r.gates.append(Gate(
        "beats_buy_and_hold",
        benchmark is None or oos.total_return > benchmark.total_return,
        f"{oos.total_return:+.2%} vs buy-and-hold {benchmark.total_return:+.2%}"
        if benchmark else "no benchmark run",
    ))
    r.gates.append(Gate(
        "walk_forward_consistency",
        wf is not None and wf.oos_consistency >= 0.5,
        f"{wf.oos_consistency:.0%} of walk-forward windows profitable" if wf
        else "walk-forward not run",
    ))
    r.gates.append(Gate(
        "not_curve_fitted",
        wf is not None and wf.degradation < 0.6,
        f"in-sample to out-of-sample degradation {wf.degradation:.0%} (need < 60%)"
        if wf else "walk-forward not run",
    ))
    r.gates.append(Gate(
        "ruin_risk_acceptable",
        mc is not None and mc.prob_ruin < 0.25,
        f"Monte Carlo: {mc.prob_ruin:.1%} chance of hitting the drawdown halt"
        if mc else "Monte Carlo not run",
    ))
    r.gates.append(Gate(
        "median_beats_zero",
        mc is not None and mc.final_return_median > 0,
        f"Monte Carlo median outcome {mc.final_return_median:+.1%}" if mc
        else "Monte Carlo not run",
    ))
    r.gates.append(Gate(
        "sharpe_positive",
        oos.sharpe > 0.3,
        f"Sharpe {oos.sharpe:.2f} (need > 0.3)",
        critical=False,
    ))
    r.gates.append(Gate(
        "target_plausible",
        mc is not None and mc.monthly_return_p05 > -0.5,
        f"5th-percentile monthly outcome {mc.monthly_return_p05:+.1%} vs level "
        f"{profile.level} target {profile.target_monthly_return:+.0%}"
        if mc else "not tested",
        critical=False,
    ))
    return r
