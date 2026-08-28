"""How many independent bets are you actually making?

Twenty symbols is not twenty positions. In crypto it is closer to one position
with twenty sets of fees attached, because almost everything moves with BTC. A
portfolio of BTC, ETH, SOL, AVAX, NEAR and SUI held long is a leveraged bet on
one thing, sized as though it were six — which is how an account that looks
conservatively diversified takes a 6x hit on a bad Tuesday.

So this module measures the diversification rather than assuming it:

**Correlation clustering.** Group markets whose returns move together. One
position per cluster is the default, because the second one adds cost and
almost no independent information.

**Effective breadth.** `N_eff = N² / ΣΣρ` — the number of *independent* bets a
correlated basket is really making. Twenty symbols at an average pairwise
correlation of 0.8 gives roughly 1.5 independent bets. That number, not the
symbol count, is what should drive position sizing, and seeing it stated is
usually a surprise.

**Correlation-aware exposure.** The risk dial caps concurrent positions, but
that cap assumes the positions are distinct. When they are not, the effective
risk per "trade" is far higher than the dial promises, and this reports by how
much.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field

from simin.indicators.features import FeatureFrame
from simin.logging import get_logger

log = get_logger(__name__)


def returns_of(frame: FeatureFrame, lookback: int = 1500) -> list[float]:
    """Bar-to-bar fractional returns, most recent `lookback` bars."""
    candles = frame.candles[-(lookback + 1) :]
    out: list[float] = []
    for prev, cur in zip(candles, candles[1:], strict=False):
        p = float(prev.close)
        if p > 0:
            out.append(float(cur.close) / p - 1.0)
    return out


def correlation(a: Sequence[float], b: Sequence[float]) -> float:
    """Pearson correlation over the overlapping tail of two return series."""
    n = min(len(a), len(b))
    if n < 30:
        return 0.0
    x, y = list(a[-n:]), list(b[-n:])
    mx, my = statistics.fmean(x), statistics.fmean(y)
    sx = statistics.pstdev(x)
    sy = statistics.pstdev(y)
    if sx <= 0 or sy <= 0:
        return 0.0
    cov = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y, strict=True)) / n
    return max(-1.0, min(cov / (sx * sy), 1.0))


@dataclass(frozen=True, slots=True)
class Cluster:
    """A group of markets that move together closely enough to be one bet."""

    members: tuple[str, ...]
    #: Average pairwise correlation inside the cluster.
    cohesion: float
    #: The member kept when only one position per cluster is allowed. Chosen by
    #: the caller's ranking, not by correlation.
    representative: str

    def to_dict(self) -> dict[str, object]:
        return {
            "members": list(self.members),
            "cohesion": self.cohesion,
            "representative": self.representative,
        }


@dataclass(slots=True)
class PortfolioReport:
    symbols: list[str]
    matrix: dict[str, dict[str, float]] = field(default_factory=dict)
    clusters: list[Cluster] = field(default_factory=list)
    average_correlation: float = 0.0
    effective_breadth: float = 0.0
    #: Symbols to actually trade: one per cluster, in the caller's ranked order.
    selected: list[str] = field(default_factory=list)

    @property
    def concentration_multiplier(self) -> float:
        """How much a simultaneous adverse move hurts, versus the assumption
        that positions are independent.

        Sizing N positions as independent when they are really N_eff independent
        means a correlated drawdown is worse than modelled by this factor.
        """
        if self.effective_breadth <= 0:
            return 1.0
        return math.sqrt(len(self.symbols) / self.effective_breadth)

    def summary(self) -> str:
        n = len(self.symbols)
        return (
            f"{n} markets, average pairwise correlation {self.average_correlation:.2f}, "
            f"which is {self.effective_breadth:.1f} genuinely independent bets. "
            f"Holding all {n} at once hurts "
            f"{self.concentration_multiplier:.1f}x more in a correlated move than "
            f"position count suggests."
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "symbols": self.symbols,
            "average_correlation": self.average_correlation,
            "effective_breadth": self.effective_breadth,
            "concentration_multiplier": self.concentration_multiplier,
            "clusters": [c.to_dict() for c in self.clusters],
            "selected": self.selected,
            "summary": self.summary(),
            "matrix": self.matrix,
        }


def effective_breadth(matrix: dict[str, dict[str, float]], symbols: Sequence[str]) -> float:
    """`N² / ΣΣρ` — independent bets in a correlated basket.

    Equals N when everything is uncorrelated and collapses toward 1 as
    correlations approach 1. Negative correlations are clamped at zero for this
    purpose: a genuinely hedged pair does reduce risk, but treating it as
    *increased* breadth would let a badly-behaved matrix produce a breadth above
    the number of assets, which is nonsense.
    """
    n = len(symbols)
    if n == 0:
        return 0.0
    if n == 1:
        return 1.0
    total = 0.0
    for a in symbols:
        for b in symbols:
            total += max(matrix.get(a, {}).get(b, 0.0), 0.0)
    if total <= 0:
        return float(n)
    return min((n * n) / total, float(n))


def cluster_symbols(
    matrix: dict[str, dict[str, float]],
    ranked: Sequence[str],
    threshold: float = 0.75,
) -> list[Cluster]:
    """Greedy correlation clustering, seeded in the caller's ranked order.

    Greedy rather than hierarchical on purpose: the ranking already encodes
    which market we would rather trade (liquidity, then screen result), so
    seeding clusters in that order means the representative is the one we
    actually want. A dendrogram would pick a mathematically tidier partition
    and then need this same decision made anyway.
    """
    unassigned = list(ranked)
    clusters: list[Cluster] = []
    while unassigned:
        seed = unassigned.pop(0)
        members = [seed]
        remaining: list[str] = []
        for other in unassigned:
            if matrix.get(seed, {}).get(other, 0.0) >= threshold:
                members.append(other)
            else:
                remaining.append(other)
        unassigned = remaining

        pairs = [
            matrix.get(a, {}).get(b, 0.0)
            for i, a in enumerate(members)
            for b in members[i + 1 :]
        ]
        clusters.append(
            Cluster(
                members=tuple(members),
                cohesion=round(statistics.fmean(pairs), 4) if pairs else 1.0,
                representative=seed,
            )
        )
    return clusters


def analyse(
    frames: dict[str, FeatureFrame],
    ranked: Sequence[str] | None = None,
    threshold: float = 0.75,
    lookback: int = 1500,
    max_positions: int | None = None,
) -> PortfolioReport:
    """Correlation structure of a candidate universe, and what to trade from it.

    `ranked` is the preference order — usually liquidity, or the screener's
    output. `max_positions` truncates the final selection to what the risk dial
    permits open at once.
    """
    symbols = list(frames)
    order = list(ranked) if ranked else symbols
    order = [s for s in order if s in frames] + [s for s in symbols if s not in (ranked or [])]

    rets = {s: returns_of(frames[s], lookback) for s in order}
    matrix: dict[str, dict[str, float]] = {}
    for a in order:
        matrix[a] = {}
        for b in order:
            matrix[a][b] = 1.0 if a == b else round(correlation(rets[a], rets[b]), 4)

    pairs = [
        matrix[a][b] for i, a in enumerate(order) for b in order[i + 1 :]
    ]
    avg = round(statistics.fmean(pairs), 4) if pairs else 0.0
    breadth = round(effective_breadth(matrix, order), 2)
    clusters = cluster_symbols(matrix, order, threshold)

    selected = [c.representative for c in clusters]
    if max_positions is not None:
        selected = selected[:max_positions]

    return PortfolioReport(
        symbols=order,
        matrix=matrix,
        clusters=clusters,
        average_correlation=avg,
        effective_breadth=breadth,
        selected=selected,
    )


def format_report(report: PortfolioReport, show_matrix: int = 10) -> str:
    lines = ["", f"  {report.summary()}", ""]
    lines.append(f"  {len(report.clusters)} clusters at the correlation threshold:")
    for c in report.clusters:
        if len(c.members) == 1:
            lines.append(f"    {c.representative:<12} (alone)")
        else:
            others = ", ".join(m for m in c.members if m != c.representative)
            lines.append(
                f"    {c.representative:<12} carries {len(c.members) - 1} more "
                f"at rho {c.cohesion:.2f}: {others}"
            )
    lines += ["", f"  Trade these: {', '.join(report.selected)}", ""]

    head = report.symbols[:show_matrix]
    if len(head) > 1:
        lines.append("  Correlation matrix (top markets):")
        lines.append("    " + " " * 10 + "".join(f"{s[:6]:>8}" for s in head))
        for a in head:
            row = "".join(f"{report.matrix[a][b]:>8.2f}" for b in head)
            lines.append(f"    {a[:10]:<10}{row}")
        lines.append("")
    return "\n".join(lines)
