"""Performance statistics.

Includes the two numbers that decide whether a backtest means anything: the
**deflated Sharpe ratio** (Bailey & Lopez de Prado 2014), which discounts the
Sharpe by how many strategy variants were tried before this one, and the
**probability of ruin**. A raw Sharpe picked as the best of 500 Optuna trials is
not evidence; it is the maximum of 500 draws from noise.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

SQRT_2PI = math.sqrt(2 * math.pi)


@dataclass(frozen=True, slots=True)
class TradeStat:
    opened_at: datetime
    closed_at: datetime
    symbol: str
    strategy: str
    pnl: Decimal
    return_pct: float
    fees: Decimal
    regime: str | None = None


@dataclass(frozen=True, slots=True)
class Metrics:
    n_trades: int
    win_rate: float
    profit_factor: float
    expectancy: float
    avg_win: float
    avg_loss: float
    total_return: float
    cagr: float
    sharpe: float
    deflated_sharpe: float
    sortino: float
    calmar: float
    max_drawdown: float
    max_drawdown_duration_bars: int
    exposure: float
    total_fees: Decimal
    fee_drag: float
    var_95: float
    cvar_95: float
    recovery_factor: float
    monthly_returns: dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"trades={self.n_trades} win={self.win_rate:.1%} pf={self.profit_factor:.2f} "
            f"ret={self.total_return:.1%} cagr={self.cagr:.1%} sharpe={self.sharpe:.2f} "
            f"dsr={self.deflated_sharpe:.2f} maxdd={self.max_drawdown:.1%} "
            f"fees={self.fee_drag:.1%} of gross"
        )


def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def norm_ppf(p: float) -> float:
    """Inverse normal CDF (Acklam's rational approximation, ~1e-9 accurate)."""
    if not 0 < p < 1:
        raise ValueError("p must be in (0, 1)")
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1
    )


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def skewness(xs: Sequence[float]) -> float:
    s = stdev(xs)
    if s == 0 or len(xs) < 3:
        return 0.0
    m = mean(xs)
    return sum((x - m) ** 3 for x in xs) / len(xs) / s**3


def kurtosis(xs: Sequence[float]) -> float:
    """Non-excess kurtosis (normal == 3). Crypto returns run far above it."""
    s = stdev(xs)
    if s == 0 or len(xs) < 4:
        return 3.0
    m = mean(xs)
    return sum((x - m) ** 4 for x in xs) / len(xs) / s**4


def sharpe_ratio(returns: Sequence[float], periods_per_year: int) -> float:
    s = stdev(returns)
    if s == 0:
        return 0.0
    return mean(returns) / s * math.sqrt(periods_per_year)


def sortino_ratio(returns: Sequence[float], periods_per_year: int) -> float:
    downside = [r for r in returns if r < 0]
    if not downside:
        return float("inf") if mean(returns) > 0 else 0.0
    dd = math.sqrt(sum(r**2 for r in downside) / len(returns))
    if dd == 0:
        return 0.0
    return mean(returns) / dd * math.sqrt(periods_per_year)


def deflated_sharpe_ratio(
    returns: Sequence[float],
    observed_sharpe: float,
    n_trials: int,
    periods_per_year: int,
) -> float:
    """Probability that the true Sharpe exceeds zero, given how many were tried.

    Bailey & Lopez de Prado (2014). The expected maximum Sharpe from ``n_trials``
    of pure noise is subtracted before testing significance, and the test accounts
    for skew and fat tails. Returns a probability in [0, 1]: below ~0.95 means the
    result is indistinguishable from having searched hard enough.
    """
    n = len(returns)
    if n < 10 or n_trials < 1:
        return 0.0
    sr = observed_sharpe / math.sqrt(periods_per_year)  # de-annualize
    variance = stdev([r for r in returns]) ** 2
    if variance == 0:
        return 0.0
    gamma = 0.5772156649015329  # Euler-Mascheroni
    if n_trials > 1:
        expected_max = (1 - gamma) * norm_ppf(1 - 1 / n_trials) + gamma * norm_ppf(
            1 - 1 / (n_trials * math.e)
        )
    else:
        expected_max = 0.0
    sr_star = expected_max / math.sqrt(n)  # noise-only benchmark at this sample size
    skew = skewness(returns)
    kurt = kurtosis(returns)
    denom = math.sqrt(max(1e-12, 1 - skew * sr + (kurt - 1) / 4 * sr**2))
    z = (sr - sr_star) * math.sqrt(n - 1) / denom
    return norm_cdf(z)


def drawdown_series(equity: Sequence[float]) -> list[float]:
    peak = equity[0] if equity else 0.0
    out: list[float] = []
    for value in equity:
        peak = max(peak, value)
        out.append(0.0 if peak == 0 else value / peak - 1.0)
    return out


def max_drawdown(equity: Sequence[float]) -> tuple[float, int]:
    """Worst peak-to-trough decline and its longest underwater stretch in bars."""
    if not equity:
        return 0.0, 0
    dd = drawdown_series(equity)
    worst = min(dd)
    longest = current = 0
    for value in dd:
        current = current + 1 if value < 0 else 0
        longest = max(longest, current)
    return worst, longest


def value_at_risk(returns: Sequence[float], confidence: float = 0.95) -> tuple[float, float]:
    """Historical VaR and CVaR (expected shortfall) at ``confidence``."""
    if not returns:
        return 0.0, 0.0
    ordered = sorted(returns)
    index = max(0, min(len(ordered) - 1, int((1 - confidence) * len(ordered))))
    var = ordered[index]
    tail = ordered[: index + 1]
    return var, (sum(tail) / len(tail) if tail else var)


def monthly_returns(stamps: Sequence[datetime], equity: Sequence[float]) -> dict[str, float]:
    if not stamps:
        return {}
    buckets: dict[str, tuple[float, float]] = {}
    for ts, value in zip(stamps, equity, strict=False):
        key = f"{ts.year}-{ts.month:02d}"
        first, _ = buckets.get(key, (value, value))
        buckets[key] = (first, value)
    return {k: (last / first - 1.0) if first else 0.0 for k, (first, last) in buckets.items()}


def compute_metrics(
    stamps: Sequence[datetime],
    equity: Sequence[float],
    trades: Sequence[TradeStat],
    *,
    periods_per_year: int,
    n_trials: int = 1,
    bars_in_market: int = 0,
) -> Metrics:
    if len(equity) < 2:
        return Metrics(
            0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0,
            0.0, Decimal(0), 0.0, 0.0, 0.0, 0.0, {},
        )
    rets = [
        (equity[i] / equity[i - 1] - 1.0) if equity[i - 1] else 0.0 for i in range(1, len(equity))
    ]
    total_return = equity[-1] / equity[0] - 1.0 if equity[0] else 0.0
    years = max(1e-9, (stamps[-1] - stamps[0]).total_seconds() / (365.25 * 86400))
    cagr = (equity[-1] / equity[0]) ** (1 / years) - 1 if equity[0] > 0 and equity[-1] > 0 else -1.0

    sharpe = sharpe_ratio(rets, periods_per_year)
    dsr = deflated_sharpe_ratio(rets, sharpe, n_trials, periods_per_year)
    mdd, mdd_len = max_drawdown(equity)

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = float(sum(t.pnl for t in wins))
    gross_loss = abs(float(sum(t.pnl for t in losses)))
    fees = sum((t.fees for t in trades), start=Decimal(0))
    var, cvar = value_at_risk(rets)

    return Metrics(
        n_trades=len(trades),
        win_rate=len(wins) / len(trades) if trades else 0.0,
        profit_factor=(gross_win / gross_loss) if gross_loss else (math.inf if gross_win else 0.0),
        expectancy=mean([t.return_pct for t in trades]) if trades else 0.0,
        avg_win=mean([t.return_pct for t in wins]) if wins else 0.0,
        avg_loss=mean([t.return_pct for t in losses]) if losses else 0.0,
        total_return=total_return,
        cagr=cagr,
        sharpe=sharpe,
        deflated_sharpe=dsr,
        sortino=sortino_ratio(rets, periods_per_year),
        calmar=(cagr / abs(mdd)) if mdd else 0.0,
        max_drawdown=mdd,
        max_drawdown_duration_bars=mdd_len,
        exposure=bars_in_market / len(equity) if equity else 0.0,
        total_fees=fees,
        fee_drag=(float(fees) / gross_win) if gross_win else 0.0,
        var_95=var,
        cvar_95=cvar,
        recovery_factor=(total_return / abs(mdd)) if mdd else 0.0,
        monthly_returns=monthly_returns(stamps, equity),
    )
