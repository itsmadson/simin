"""Calibration: replacing the dial's promises with measurements.

Every level of the risk dial ships with a `target_monthly_return`. That number
is a design intent — it says what the level was *built* to reach, and for the
top of the dial it is frankly a hypothesis under test. This module runs each
level through walk-forward and Monte Carlo on real history and writes back an
`EmpiricalProfile`: what the level actually returned, how deep it actually drew
down, and how often it actually ruined the account.

The UI then shows both, side by side, always. The gap between them is the most
useful information the whole system produces.

Results are cached to disk keyed by the data they were computed from, because
calibrating ten levels across several symbols is minutes of work and nobody
should be tempted to skip it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from simin.core.types import TF, Symbol
from simin.exchanges.costs import CostModel
from simin.indicators.features import FeatureFrame
from simin.lab.backtest import Backtester
from simin.lab.metrics import Metrics
from simin.lab.validation import (
    GateReport,
    MonteCarloResult,
    WalkForwardResult,
    evaluate_gates,
    monte_carlo,
    walk_forward,
)
from simin.risk.dial import EmpiricalProfile, RiskProfile, all_profiles, profile
from simin.strategies.base import build_many
from simin.strategies.library import strategies_for_level

CACHE_VERSION = 3


class CalibrationStore:
    """Empirical profiles on disk, keyed by a fingerprint of the input data."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        if data.get("version") != CACHE_VERSION:
            return {}
        return data.get("levels", {})

    def get(self, level: int, fingerprint: str) -> EmpiricalProfile | None:
        row = self._load().get(str(level))
        if row is None or row.get("fingerprint") != fingerprint:
            return None
        payload = {k: v for k, v in row.items() if k != "fingerprint"}
        try:
            return EmpiricalProfile(**payload)
        except TypeError:
            return None

    def put(self, level: int, fingerprint: str, e: EmpiricalProfile) -> None:
        levels = self._load()
        levels[str(level)] = {**asdict(e), "fingerprint": fingerprint}
        self.path.write_text(
            json.dumps(
                {"version": CACHE_VERSION, "updated_at": datetime.now(UTC).isoformat(),
                 "levels": levels},
                indent=2,
            )
        )

    def all_empirical(self, fingerprint: str | None = None) -> dict[int, EmpiricalProfile]:
        out: dict[int, EmpiricalProfile] = {}
        for key, row in self._load().items():
            if fingerprint and row.get("fingerprint") != fingerprint:
                continue
            payload = {k: v for k, v in row.items() if k != "fingerprint"}
            try:
                out[int(key)] = EmpiricalProfile(**payload)
            except (TypeError, ValueError):
                continue
        return out


def fingerprint(frames: dict[str, FeatureFrame], costs: CostModel) -> str:
    """Identify the exact data and cost assumptions a calibration was run on.

    Costs are part of the key because the same history at CoinEx futures fees
    and at Nobitex spot fees produces genuinely different answers, and serving
    one as if it were the other is exactly the kind of quiet lie this whole
    module exists to prevent.
    """
    h = hashlib.sha256()
    h.update(f"v{CACHE_VERSION}".encode())
    for name in sorted(frames):
        f = frames[name]
        h.update(name.encode())
        h.update(f.tf.value.encode())
        h.update(str(len(f)).encode())
        if f.candles:
            h.update(f.candles[0].ts.isoformat().encode())
            h.update(f.candles[-1].ts.isoformat().encode())
            h.update(str(f.candles[-1].close).encode())
    h.update(str(costs.round_trip).encode())
    return h.hexdigest()[:16]


class LevelReport:
    """Everything learned about one dial level."""

    __slots__ = ("level", "profile", "oos", "stressed", "walk", "mc", "gates", "benchmark")

    def __init__(
        self,
        level: int,
        prof: RiskProfile,
        oos: Metrics,
        stressed: Metrics | None,
        walk: WalkForwardResult | None,
        mc: MonteCarloResult | None,
        gates: GateReport,
        benchmark: Metrics | None,
    ) -> None:
        self.level = level
        self.profile = prof
        self.oos = oos
        self.stressed = stressed
        self.walk = walk
        self.mc = mc
        self.gates = gates
        self.benchmark = benchmark

    def empirical(self, months: float, scope: str) -> EmpiricalProfile:
        mc = self.mc
        return EmpiricalProfile(
            monthly_return_median=(
                mc.monthly_return_median if mc else self.oos.monthly_return
            ),
            monthly_return_p05=mc.monthly_return_p05 if mc else self.oos.monthly_return,
            monthly_return_p95=mc.monthly_return_p95 if mc else self.oos.monthly_return,
            max_drawdown_median=mc.max_drawdown_median if mc else self.oos.max_drawdown,
            max_drawdown_p95=mc.max_drawdown_p95 if mc else self.oos.max_drawdown,
            win_rate=self.oos.win_rate,
            profit_factor=(
                self.oos.profit_factor if self.oos.profit_factor != float("inf") else 99.0
            ),
            sharpe=self.oos.sharpe,
            trades_per_month=self.oos.trades_per_month,
            ruin_probability=mc.prob_ruin if mc else 0.0,
            sample_months=int(months),
            calibrated_at=datetime.now(UTC).isoformat(),
            symbol_scope=scope,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "level": self.level,
            "name_en": self.profile.name_en,
            "name_fa": self.profile.name_fa,
            "target_monthly_return": self.profile.target_monthly_return,
            "out_of_sample": self.oos.to_dict(),
            "stressed_2x_costs": self.stressed.to_dict() if self.stressed else None,
            "benchmark": self.benchmark.to_dict() if self.benchmark else None,
            "walk_forward": self.walk.to_dict() if self.walk else None,
            "monte_carlo": self.mc.to_dict() if self.mc else None,
            "gates": self.gates.to_dict(),
        }


def calibrate_level(
    level: int,
    frames: dict[str, FeatureFrame],
    symbols: dict[str, Symbol],
    tf: TF,
    costs: CostModel,
    starting_equity: Decimal = Decimal("10000"),
    train_bars: int = 1500,
    test_bars: int = 500,
    mc_runs: int = 2000,
) -> LevelReport:
    """Everything for one level: walk-forward, stress, Monte Carlo, gates."""
    prof = profile(level)
    names = strategies_for_level(level)

    wf: WalkForwardResult | None = None
    try:
        wf = walk_forward(
            prof, names, frames, symbols, tf, costs, starting_equity, train_bars, test_bars
        )
    except ValueError:
        # Not enough history for windows. Fall back to a single split so the
        # level still reports something, clearly flagged by wf being None.
        wf = None

    oos = wf.combined_oos if wf is not None else None

    # A single holdout run supplies the trade list Monte Carlo needs; the
    # walk-forward supplies the consistency evidence. Both matter.
    n = min(len(f) for f in frames.values())
    split = int(n * 0.7)
    holdout = {k: FeatureFrame(k, tf, f.candles[split:]) for k, f in frames.items()}
    try:
        holdout_run = Backtester(
            prof, build_many(names), costs, starting_equity
        ).run(holdout, symbols, tf)
    except ValueError:
        holdout_run = Backtester(
            prof, build_many(names), costs, starting_equity
        ).run(frames, symbols, tf)

    if oos is None:
        oos = holdout_run.metrics

    stressed_metrics: Metrics | None = None
    try:
        stressed_metrics = Backtester(
            prof, build_many(names), costs.stressed(), starting_equity
        ).run(holdout, symbols, tf).metrics
    except ValueError:
        pass

    benchmark: Metrics | None = None
    try:
        benchmark = Backtester(
            profile(4), build_many(["buy_and_hold"]), costs, starting_equity
        ).run(holdout, symbols, tf).metrics
    except ValueError:
        pass

    # Prefer the pooled walk-forward trades: it is the biggest sample that the
    # configuration never saw. Fall back to the single holdout only when
    # walk-forward could not run at all.
    mc_trades = wf.oos_trades if (wf and len(wf.oos_trades) >= 20) else holdout_run.trades
    mc_days = (
        sum(m.days for m in wf.out_of_sample)
        if (wf and len(wf.oos_trades) >= 20)
        else holdout_run.metrics.days
    )
    mc = monte_carlo(
        mc_trades, starting_equity, prof, days_covered=mc_days, runs=mc_runs
    )
    gates = evaluate_gates(oos, stressed_metrics, wf, mc, benchmark, prof)
    return LevelReport(level, prof, oos, stressed_metrics, wf, mc, gates, benchmark)


def calibrate_all(
    frames: dict[str, FeatureFrame],
    symbols: dict[str, Symbol],
    tf: TF,
    costs: CostModel,
    store: CalibrationStore | None = None,
    starting_equity: Decimal = Decimal("10000"),
    levels: Sequence[int] | None = None,
    mc_runs: int = 2000,
    force: bool = False,
) -> dict[int, LevelReport]:
    """Calibrate the whole dial and persist the result."""
    fp = fingerprint(frames, costs)
    out: dict[int, LevelReport] = {}
    n = min(len(f) for f in frames.values())
    months = n * tf.seconds / 86400 / 30.44
    scope = ",".join(sorted(frames))

    for level in levels or range(1, 11):
        if store is not None and not force and store.get(level, fp) is not None:
            continue
        report = calibrate_level(
            level, frames, symbols, tf, costs, starting_equity, mc_runs=mc_runs
        )
        out[level] = report
        if store is not None:
            store.put(level, fp, report.empirical(months, scope))
    return out


def dial_with_empirical(store: CalibrationStore, fp: str | None = None) -> list[RiskProfile]:
    """The dial, with measured numbers attached wherever calibration exists."""
    measured = store.all_empirical(fp)
    return [
        p.with_empirical(measured[p.level]) if p.level in measured else p
        for p in all_profiles()
    ]
