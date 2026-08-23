"""Market regime classification.

A deterministic, auditable state machine — chosen over an HMM for v1 because it
is explainable on a dashboard, cannot silently drift, and can be argued with. The
statistical classifier (Phase 7) has to beat *this* on downstream PnL before it
replaces it; beating it on classification accuracy would prove nothing, since
nobody trades a label.

Regimes are computed from closed bars only, and every threshold is a percentile
of the asset's own recent history rather than a hardcoded constant — a "high
volatility" number for BTC is a quiet day for a small-cap.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass

from simin.features.engine import FeatureRow


class Regime(enum.StrEnum):
    STRONG_BULL = "strong_bull"
    WEAK_BULL = "weak_bull"
    SIDEWAYS_LOW_VOL = "sideways_low_vol"
    SIDEWAYS_HIGH_VOL = "sideways_high_vol"
    WEAK_BEAR = "weak_bear"
    STRONG_BEAR = "strong_bear"
    BREAKOUT = "breakout"
    PANIC = "panic"
    UNKNOWN = "unknown"


#: Which strategy families are permitted to act in each regime. The mapping is a
#: *permission*, not a prediction: sideways+high-vol allows nothing because it is
#: the regime where both trend and mean-reversion bleed (docs/01 §3).
REGIME_PLAYBOOK: dict[Regime, tuple[str, ...]] = {
    Regime.STRONG_BULL: ("trend_follow", "donchian_breakout", "vol_breakout"),
    Regime.WEAK_BULL: ("trend_follow", "range_mean_reversion"),
    Regime.SIDEWAYS_LOW_VOL: ("range_mean_reversion",),
    Regime.SIDEWAYS_HIGH_VOL: (),
    Regime.WEAK_BEAR: ("range_mean_reversion",),
    Regime.STRONG_BEAR: ("trend_follow",),
    Regime.BREAKOUT: ("donchian_breakout", "vol_breakout"),
    Regime.PANIC: (),
    Regime.UNKNOWN: (),
}


@dataclass(frozen=True, slots=True)
class RegimeState:
    regime: Regime
    trend_strength: float | None
    vol_percentile: float | None
    reason: str

    @property
    def allows_new_risk(self) -> bool:
        return bool(REGIME_PLAYBOOK[self.regime])

    def allows(self, strategy: str) -> bool:
        return strategy in REGIME_PLAYBOOK[self.regime]


@dataclass(frozen=True, slots=True)
class RegimeConfig:
    adx_trending: float = 25.0
    adx_strong: float = 40.0
    vol_high_pct: float = 0.80
    vol_low_pct: float = 0.35
    panic_return: float = -0.08
    panic_vol_pct: float = 0.95
    breakout_vol_ratio: float = 1.6
    lookback: int = 250


def percentile_rank(history: Sequence[float], value: float) -> float:
    """Fraction of history at or below ``value``. Only past values are passed in."""
    if not history:
        return 0.5
    below = sum(1 for h in history if h <= value)
    return below / len(history)


def classify(
    rows: Sequence[FeatureRow], index: int, config: RegimeConfig | None = None
) -> RegimeState:
    """Classify the regime as of ``rows[index]``, using only rows up to it."""
    cfg = config or RegimeConfig()
    row = rows[index]
    adx = row.get("adx14")
    vol = row.get("vol24")
    ema_spread = row.get("ema_spread")
    mom6 = row.get("mom6")
    vol_ratio = row.get("vol_ratio")
    close = row.get("close")
    donch_up = row.get("donchian_up20")

    if adx is None or vol is None or ema_spread is None:
        return RegimeState(Regime.UNKNOWN, adx, None, "warming up")

    start = max(0, index - cfg.lookback)
    history = [r.get("vol24") for r in rows[start:index]]
    vol_hist = [v for v in history if v is not None]
    vol_pct = percentile_rank(vol_hist, vol)

    # Panic first: it overrides everything, because the cost of trading through a
    # crash dwarfs the cost of missing the bounce.
    if mom6 is not None and mom6 <= cfg.panic_return and vol_pct >= cfg.panic_vol_pct:
        return RegimeState(Regime.PANIC, adx, vol_pct, f"{mom6:.1%} over 6 bars at vol p{vol_pct:.0%}")

    trending = adx >= cfg.adx_trending
    strong = adx >= cfg.adx_strong
    bullish = ema_spread > 0

    if (
        vol_ratio is not None
        and vol_ratio >= cfg.breakout_vol_ratio
        and close is not None
        and donch_up is not None
        and close > donch_up
    ):
        return RegimeState(Regime.BREAKOUT, adx, vol_pct, "range break on expanding volatility")

    if trending:
        if bullish:
            regime = Regime.STRONG_BULL if strong else Regime.WEAK_BULL
        else:
            regime = Regime.STRONG_BEAR if strong else Regime.WEAK_BEAR
        return RegimeState(regime, adx, vol_pct, f"ADX {adx:.0f}, ema spread {ema_spread:+.2%}")

    if vol_pct >= cfg.vol_high_pct:
        return RegimeState(
            Regime.SIDEWAYS_HIGH_VOL, adx, vol_pct, f"no trend, vol p{vol_pct:.0%} — stand down"
        )
    return RegimeState(Regime.SIDEWAYS_LOW_VOL, adx, vol_pct, f"no trend, vol p{vol_pct:.0%}")


def classify_series(
    rows: Sequence[FeatureRow], config: RegimeConfig | None = None
) -> list[RegimeState]:
    return [classify(rows, i, config) for i in range(len(rows))]
