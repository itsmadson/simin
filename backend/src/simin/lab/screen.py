"""Testing many markets without fooling yourself.

This is the module that decides whether "we found a great symbol" means
anything, and the honest answer is usually no.

The problem, stated plainly. Run one strategy over 23 symbols and the best one
will look good. Run it over 23 symbols at 10 risk levels and the best of 230
will look excellent. Neither result is evidence, because **the maximum of many
random draws is large by construction**. With 230 independent coin-flip
strategies whose true edge is exactly zero, the best will show a Sharpe near 2.8
purely by chance. Reporting that number as a discovery is not optimism, it is a
category error — and it is the single most common way backtesting destroys
money.

The fix is not to test fewer things. It is to *state how many things you tested*
and raise the bar accordingly.

Two corrections are applied:

**Deflated Sharpe Ratio** (Bailey & López de Prado, 2014). Given N trials, it
computes the Sharpe you would expect the luckiest trial to reach under a null of
no skill, then asks how confident we can be that the observed Sharpe beats *that*
threshold rather than beating zero. It also corrects for the non-normality of
trading returns — skew and fat tails inflate a naive Sharpe, and crypto has both
in quantity.

**An empirical null.** The analytic correction assumes independent trials, and
symbols that all follow BTC are not independent. So the same strategies are also
run over bootstrapped returns — real return distributions with the ordering
destroyed, which preserves the fat tails while removing any predictable
structure. Whatever the strategy scores there is what it scores on nothing. A
result that does not clearly exceed its own null is noise, however good the
equity curve looks.

A screener that returns "nothing survived" is working correctly. That will be
the usual outcome, and it is worth far more than a ranked list of lucky coins.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from simin.core.types import TF, Symbol, Trade
from simin.exchanges.costs import CostModel
from simin.indicators.features import FeatureFrame
from simin.lab.backtest import Backtester
from simin.lab.metrics import Metrics
from simin.lab.validation import WalkForwardResult, walk_forward
from simin.logging import get_logger
from simin.risk.dial import RiskProfile
from simin.strategies.base import build_many
from simin.strategies.library import strategies_for_level

log = get_logger(__name__)

#: Euler–Mascheroni constant, used in the expected-maximum-Sharpe term.
EULER = 0.5772156649015329

_NORM = statistics.NormalDist()


def expected_max_sharpe(trials: int, sharpe_variance: float) -> float:
    """The Sharpe the luckiest of `trials` strategies reaches with zero skill.

    This is the bar a result has to clear. With enough trials it rises well above
    anything most real strategies produce, which is the point: it makes the cost
    of searching visible instead of free.
    """
    if trials <= 1 or sharpe_variance <= 0:
        return 0.0
    sd = math.sqrt(sharpe_variance)
    a = _NORM.inv_cdf(1 - 1.0 / trials)
    b = _NORM.inv_cdf(1 - 1.0 / (trials * math.e))
    return sd * ((1 - EULER) * a + EULER * b)


def per_trade_sharpe(r_values: Sequence[float]) -> float:
    """Sharpe in units of one trade: mean R over standard deviation of R.

    **Not annualised.** Everything in this module works in per-observation
    units, because that is what the deflated-Sharpe derivation assumes and
    mixing the two is a silent, catastrophic error: feed an annualised 1.8 where
    a per-trade 0.13 belongs and the correction returns 1.000 for literally any
    input, quietly certifying noise as skill. The annualised figure is kept for
    display only, and never enters a test.
    """
    if len(r_values) < 3:
        return 0.0
    sd = statistics.pstdev(r_values)
    if sd <= 0:
        return 0.0
    return statistics.fmean(r_values) / sd


def annualise(per_obs_sharpe: float, observations_per_year: float) -> float:
    """Per-observation Sharpe -> annualised. For display only."""
    return per_obs_sharpe * math.sqrt(max(observations_per_year, 0.0))


def deflated_sharpe(
    sharpe: float,
    n_returns: int,
    trials: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
    sharpe_variance: float | None = None,
) -> float:
    """Probability the observed Sharpe reflects skill rather than search luck.

    `sharpe` must be **per-observation** — see `per_trade_sharpe`. Passing an
    annualised value makes this function return 1.0 for everything.

    Returns a probability in 0..1. Above ~0.95 is the conventional bar; below
    0.5 the result is more likely selection noise than edge.

    The denominator is where the fat tails are handled. A naive Sharpe assumes
    normal returns; a strategy that wins small constantly and loses enormously
    occasionally — which describes most short-volatility behaviour, and a lot of
    accidental short-volatility behaviour — posts a flattering Sharpe right up
    until it does not. Negative skew and high kurtosis both widen the error bar,
    as they should.
    """
    if n_returns < 3 or trials < 1:
        return 0.0
    var = sharpe_variance if sharpe_variance is not None else (1.0 / max(n_returns - 1, 1))
    threshold = expected_max_sharpe(trials, var)

    denom = 1.0 - skew * sharpe + ((kurtosis - 1.0) / 4.0) * sharpe**2
    if denom <= 0:
        return 0.0
    z = (sharpe - threshold) * math.sqrt(max(n_returns - 1, 1)) / math.sqrt(denom)
    return float(_NORM.cdf(z))


def _moments(values: Sequence[float]) -> tuple[float, float]:
    """(skew, kurtosis) of a return series. Kurtosis is non-excess: 3 is normal."""
    n = len(values)
    if n < 4:
        return 0.0, 3.0
    mean = statistics.fmean(values)
    sd = statistics.pstdev(values)
    if sd <= 0:
        return 0.0, 3.0
    m3 = sum((v - mean) ** 3 for v in values) / n
    m4 = sum((v - mean) ** 4 for v in values) / n
    return m3 / sd**3, m4 / sd**4


@dataclass(frozen=True, slots=True)
class SymbolResult:
    """One market's out-of-sample result, and how much to believe it."""

    symbol: str
    trades: int
    #: Annualised, for display only.
    sharpe: float
    #: Per-trade. This is what every test above uses.
    trade_sharpe: float
    total_return: float
    monthly_return: float
    max_drawdown: float
    profit_factor: float
    expectancy_r: float
    win_rate: float
    oos_consistency: float
    degradation: float
    skew: float
    kurtosis: float

    #: Probability this survives the search, given how many things were tried.
    dsr: float = 0.0
    #: Fraction of bootstrapped null runs this beat. Above 0.95 is meaningful.
    null_percentile: float = 0.0
    #: The Sharpe the luckiest null trial reached.
    null_sharpe_p95: float = 0.0
    survived: bool = False
    failed_because: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "trades": self.trades,
            "sharpe": self.sharpe,
            "trade_sharpe": self.trade_sharpe,
            "total_return": self.total_return,
            "monthly_return": self.monthly_return,
            "max_drawdown": self.max_drawdown,
            "profit_factor": self.profit_factor,
            "expectancy_r": self.expectancy_r,
            "win_rate": self.win_rate,
            "oos_consistency": self.oos_consistency,
            "degradation": self.degradation,
            "skew": self.skew,
            "kurtosis": self.kurtosis,
            "dsr": self.dsr,
            "null_percentile": self.null_percentile,
            "null_sharpe_p95": self.null_sharpe_p95,
            "survived": self.survived,
            "failed_because": self.failed_because,
        }


@dataclass(slots=True)
class ScreenReport:
    risk_level: int
    timeframe: str
    trials: int
    results: list[SymbolResult] = field(default_factory=list)
    #: Sharpe values produced by the strategies on structureless data.
    null_sharpes: list[float] = field(default_factory=list)
    screened_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def survivors(self) -> list[SymbolResult]:
        return [r for r in self.results if r.survived]

    @property
    def null_p95(self) -> float:
        if not self.null_sharpes:
            return 0.0
        ordered = sorted(self.null_sharpes)
        return ordered[min(int(0.95 * (len(ordered) - 1)), len(ordered) - 1)]

    @property
    def verdict(self) -> str:
        n = len(self.survivors)
        if n == 0:
            return (
                f"Nothing survived. {self.trials} configurations were tested; the best "
                "results are consistent with luck, and none of them should be traded."
            )
        return (
            f"{n} of {self.trials} configurations survived correction for having "
            f"tested {self.trials} of them. Treat this as a shortlist to examine, "
            "not a portfolio to deploy."
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "risk_level": self.risk_level,
            "timeframe": self.timeframe,
            "trials": self.trials,
            "screened_at": self.screened_at.isoformat(),
            "survivors": len(self.survivors),
            "null_sharpe_p95": self.null_p95,
            "verdict": self.verdict,
            "results": [r.to_dict() for r in self.results],
        }


def _null_sharpes(
    profile: RiskProfile,
    frames: dict[str, FeatureFrame],
    symbols: dict[str, Symbol],
    tf: TF,
    costs: CostModel,
    equity: Decimal,
    runs: int,
    seed: int,
) -> list[float]:
    """Sharpe ratios the same strategies achieve on data with no structure.

    Built by block-bootstrapping real bars: sampling contiguous chunks and
    reassembling them in a random order. That keeps each bar's realistic shape
    and the fat-tailed return distribution while destroying any longer-range
    predictability — so anything the strategies find is, by construction, not
    there.

    Blocks rather than individual bars, because shuffling bar by bar produces a
    series so jagged that no trend strategy can trade it at all, which would
    understate the null and flatter every real result.
    """
    from simin.core.types import Candle

    rng = random.Random(seed)
    names = sorted(frames)
    out: list[float] = []
    block = 24

    for run in range(runs):
        shuffled: dict[str, FeatureFrame] = {}
        for name in names:
            candles = frames[name].candles
            n = len(candles)
            if n < block * 4:
                continue
            # Reassemble from returns so prices stay positive and continuous.
            rets: list[float] = []
            for i in range(1, n):
                prev = float(candles[i - 1].close)
                if prev > 0:
                    rets.append(float(candles[i].close) / prev - 1.0)
            blocks = [rets[i : i + block] for i in range(0, len(rets) - block, block)]
            if len(blocks) < 4:
                continue
            rng.shuffle(blocks)
            flat = [r for b in blocks for r in b]

            price = float(candles[0].close)
            rebuilt: list[Candle] = []
            for i, r in enumerate(flat):
                o = price
                price = max(price * (1 + r), 1e-9)
                hi = max(o, price) * (1 + abs(rng.gauss(0, 0.002)))
                lo = min(o, price) * (1 - abs(rng.gauss(0, 0.002)))
                rebuilt.append(
                    Candle(
                        ts=candles[i].ts,
                        open=Decimal(str(round(o, 8))),
                        high=Decimal(str(round(hi, 8))),
                        low=Decimal(str(round(max(lo, 1e-9), 8))),
                        close=Decimal(str(round(price, 8))),
                        volume=candles[i].volume,
                    )
                )
            shuffled[name] = FeatureFrame(name, tf, rebuilt)

        if not shuffled:
            continue
        try:
            result = Backtester(
                profile, build_many(strategies_for_level(profile.level)), costs, equity
            ).run(shuffled, {k: symbols[k] for k in shuffled}, tf)
        except ValueError:
            continue
        # Per-trade units, to match what the real results are measured in.
        # Comparing an annualised null against a per-trade observation would
        # make every strategy look superhuman.
        r_values = [float(t.r_multiple) for t in result.trades]
        if len(r_values) >= 3:
            out.append(per_trade_sharpe(r_values))
        if run and run % 5 == 0:
            log.debug("null run", run=run, trades=len(r_values))
    return out


def screen(
    profile: RiskProfile,
    frames: dict[str, FeatureFrame],
    symbols: dict[str, Symbol],
    tf: TF,
    costs: CostModel,
    equity: Decimal = Decimal("10000"),
    train_bars: int = 1500,
    test_bars: int = 500,
    null_runs: int = 20,
    extra_trials: int = 1,
    min_trades: int = 25,
    seed: int = 20260828,
) -> ScreenReport:
    """Walk-forward every symbol, then correct for having tested them all.

    `extra_trials` accounts for searching beyond this call. If you screened ten
    risk levels and are reporting the best, the trial count is ten times the
    symbol count, and the bar rises accordingly. Leaving it at 1 while quietly
    running the screen ten times is how the correction gets defeated.
    """
    names = sorted(frames)
    trials = max(len(names) * max(extra_trials, 1), 1)

    log.info("screening", symbols=len(names), trials=trials, level=profile.level)

    nulls = _null_sharpes(
        profile, frames, symbols, tf, costs, equity, runs=null_runs, seed=seed
    )
    null_sorted = sorted(nulls)
    null_p95 = (
        null_sorted[min(int(0.95 * (len(null_sorted) - 1)), len(null_sorted) - 1)]
        if null_sorted
        else 0.0
    )

    results: list[SymbolResult] = []
    for name in names:
        single = {name: frames[name]}
        try:
            wf: WalkForwardResult = walk_forward(
                profile, strategies_for_level(profile.level), single,
                {name: symbols[name]}, tf, costs, equity, train_bars, test_bars,
            )
        except ValueError as exc:
            results.append(
                SymbolResult(
                    symbol=name, trades=0, sharpe=0.0, trade_sharpe=0.0, total_return=0.0,
                    monthly_return=0.0, max_drawdown=0.0, profit_factor=0.0,
                    expectancy_r=0.0, win_rate=0.0, oos_consistency=0.0,
                    degradation=1.0, skew=0.0, kurtosis=3.0,
                    failed_because=f"could not walk forward: {exc}",
                )
            )
            continue

        m: Metrics | None = wf.combined_oos
        if m is None or m.trades < min_trades:
            results.append(
                SymbolResult(
                    symbol=name, trades=m.trades if m else 0,
                    sharpe=m.sharpe if m else 0.0, trade_sharpe=0.0,
                    total_return=m.total_return if m else 0.0,
                    monthly_return=m.monthly_return if m else 0.0,
                    max_drawdown=m.max_drawdown if m else 0.0,
                    profit_factor=m.profit_factor if m else 0.0,
                    expectancy_r=m.expectancy_r if m else 0.0,
                    win_rate=m.win_rate if m else 0.0,
                    oos_consistency=wf.oos_consistency, degradation=wf.degradation,
                    skew=0.0, kurtosis=3.0, null_sharpe_p95=null_p95,
                    failed_because=(
                        f"{m.trades if m else 0} out-of-sample trades, need {min_trades} "
                        "before any statistic means anything"
                    ),
                )
            )
            continue

        r_values = [float(t.r_multiple) for t in wf.oos_trades] or [0.0]
        skew, kurt = _moments(r_values)
        # Everything below is in per-trade units. `m.sharpe` is annualised and
        # is carried only for display.
        trade_sr = per_trade_sharpe(r_values)
        dsr = deflated_sharpe(
            sharpe=trade_sr, n_returns=max(len(r_values), 3), trials=trials,
            skew=skew, kurtosis=kurt,
        )
        beat_null = (
            sum(1 for s in nulls if trade_sr > s) / len(nulls) if nulls else 0.0
        )

        reasons: list[str] = []
        if dsr < 0.95:
            reasons.append(
                f"deflated Sharpe {dsr:.2f} — against {trials} trials this is "
                "indistinguishable from the luckiest coin"
            )
        if beat_null < 0.95:
            reasons.append(
                f"beat only {beat_null:.0%} of runs on structureless data "
                f"(null Sharpe p95 = {null_p95:.2f})"
            )
        if m.total_return <= 0:
            reasons.append("lost money out of sample")
        if wf.oos_consistency < 0.5:
            reasons.append(f"only {wf.oos_consistency:.0%} of windows profitable")
        if wf.degradation > 0.6:
            reasons.append(f"{wf.degradation:.0%} degradation from in-sample")

        results.append(
            SymbolResult(
                symbol=name, trades=m.trades, sharpe=m.sharpe, trade_sharpe=trade_sr,
                total_return=m.total_return, monthly_return=m.monthly_return,
                max_drawdown=m.max_drawdown, profit_factor=m.profit_factor,
                expectancy_r=m.expectancy_r, win_rate=m.win_rate,
                oos_consistency=wf.oos_consistency, degradation=wf.degradation,
                skew=skew, kurtosis=kurt, dsr=dsr, null_percentile=beat_null,
                null_sharpe_p95=null_p95,
                survived=not reasons,
                failed_because="; ".join(reasons),
            )
        )

    results.sort(key=lambda r: (not r.survived, -r.dsr, -r.trade_sharpe))
    return ScreenReport(
        risk_level=profile.level, timeframe=tf.value, trials=trials,
        results=results, null_sharpes=nulls,
    )


def format_report(report: ScreenReport, limit: int = 30) -> str:
    lines = [
        "",
        f"  Screened {len(report.results)} markets on {report.timeframe}, "
        f"risk level {report.risk_level}",
        f"  Correcting for {report.trials} trials. Null Sharpe p95 = "
        f"{report.null_p95:+.2f} (what the strategies score on structureless data)",
        "",
        f"  {'market':<12}{'trades':>8}{'SR/trade':>10}{'ann.SR':>9}{'return':>9}"
        f"{'maxDD':>8}{'DSR':>7}{'>null':>8}  verdict",
        "  " + "-" * 90,
    ]
    for r in report.results[:limit]:
        mark = "PASS" if r.survived else "----"
        lines.append(
            f"  {r.symbol:<12}{r.trades:>8}{r.trade_sharpe:>10.3f}{r.sharpe:>9.2f}"
            f"{r.total_return:>8.1%}{r.max_drawdown:>8.1%}{r.dsr:>7.2f}"
            f"{r.null_percentile:>7.0%}  {mark}"
        )
    lines += ["", f"  {report.verdict}", ""]

    losers = [r for r in report.results if not r.survived and r.failed_because][:3]
    if losers:
        lines.append("  Why the best-looking ones did not survive:")
        for r in losers:
            lines.append(f"    {r.symbol:<12} {r.failed_because[:96]}")
        lines.append("")
    return "\n".join(lines)
