"""Feature assembly, including the multi-timeframe join.

The join is the dangerous part. Attaching a 1D feature to a 4h bar is trivially
easy to get wrong, and getting it wrong means the 4h bar at 04:00 knows how the
day it sits inside will close. ``asof_join`` below refuses to do that: a
higher-timeframe row is only visible once its bar has *closed*.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from simin.features import indicators as ind
from simin.types import TF, Bar


@dataclass(frozen=True, slots=True)
class FeatureRow:
    ts: datetime
    symbol: str
    tf: TF
    values: dict[str, float | None]

    def get(self, name: str) -> float | None:
        return self.values.get(name)

    @property
    def is_complete(self) -> bool:
        """True when no feature is still warming up. Strategies must skip incomplete rows."""
        return all(v is not None for v in self.values.values())


BARS_PER_YEAR = {
    TF.M1: 525_600,
    TF.M3: 175_200,
    TF.M5: 105_120,
    TF.M15: 35_040,
    TF.M30: 17_520,
    TF.H1: 8_760,
    TF.H4: 2_190,
    TF.D1: 365,
}


def build_features(bars: Sequence[Bar], tf: TF | None = None) -> list[FeatureRow]:
    """Compute the v1 feature set for one symbol/timeframe.

    ~20 features spanning the six factor families identified in the research:
    trend, volatility, participation, mean-reversion, structure and quality.
    """
    if not bars:
        return []
    tf = tf or bars[0].tf
    symbol = bars[0].symbol
    c = ind.closes(bars)
    per_year = BARS_PER_YEAR[tf]

    computed: dict[str, ind.Series] = {
        "atr14": ind.atr(bars, 14),
        "rsi14": ind.rsi(c, 14),
        "adx14": ind.adx(bars, 14),
        "ema20": ind.ema(c, 20),
        "ema50": ind.ema(c, 50),
        "ema200": ind.ema(c, 200),
        "z20": ind.zscore(c, 20),
        "vol24": ind.realized_vol(c, 24, per_year),
        "vol72": ind.realized_vol(c, 72, per_year),
        "mom6": ind.momentum(c, 6),
        "mom24": ind.momentum(c, 24),
        "mom72": ind.momentum(c, 72),
        "trend_q20": ind.trend_quality(c, 20),
        "vol_z20": ind.volume_zscore(bars, 20),
        "vwap24": ind.vwap_session(bars, 24),
    }
    upper, lower = ind.donchian(bars, 20)
    computed["donchian_up20"] = upper
    computed["donchian_dn20"] = lower

    rows: list[FeatureRow] = []
    for i, bar in enumerate(bars):
        values: dict[str, float | None] = {k: v[i] for k, v in computed.items()}
        values["close"] = c[i]
        # derived, unit-free versions: a raw ATR is not comparable across assets
        # or across time, and a model trained on raw levels learns the price, not
        # the behaviour.
        atr_v = values["atr14"]
        values["atr_pct"] = (atr_v / c[i]) if atr_v and c[i] else None
        ema50, ema200 = values["ema50"], values["ema200"]
        values["ema_spread"] = (
            (ema50 - ema200) / ema200 if ema50 is not None and ema200 else None
        )
        values["dist_vwap"] = (
            (c[i] - values["vwap24"]) / values["vwap24"] if values["vwap24"] else None
        )
        vol24, vol72 = values["vol24"], values["vol72"]
        values["vol_ratio"] = (vol24 / vol72) if vol24 is not None and vol72 else None
        rows.append(FeatureRow(ts=bar.ts, symbol=symbol, tf=tf, values=values))
    return rows


def asof_join(
    base: Sequence[FeatureRow],
    higher: Sequence[FeatureRow],
    higher_tf: TF,
    *,
    prefix: str | None = None,
) -> list[FeatureRow]:
    """Attach higher-timeframe context to lower-timeframe rows, causally.

    A higher-TF row is eligible only when ``higher.ts + higher_tf.delta <= base.ts``
    — i.e. its bar has fully closed *before* the base bar opens. Using ``<=`` on
    the open timestamps instead (the intuitive version) leaks the higher bar's
    own future into every row inside it.
    """
    if not higher:
        return list(base)
    prefix = prefix or f"{higher_tf.value}_"
    close_times = [r.ts + higher_tf.delta for r in higher]
    out: list[FeatureRow] = []
    for row in base:
        idx = bisect_right(close_times, row.ts) - 1
        merged = dict(row.values)
        if idx >= 0:
            for key, value in higher[idx].values.items():
                merged[f"{prefix}{key}"] = value
        else:
            for key in higher[0].values:
                merged[f"{prefix}{key}"] = None
        out.append(FeatureRow(ts=row.ts, symbol=row.symbol, tf=row.tf, values=merged))
    return out


def rows_by_ts(rows: Sequence[FeatureRow]) -> dict[datetime, FeatureRow]:
    return {r.ts: r for r in rows}


def feature_matrix(
    rows: Sequence[FeatureRow], names: Sequence[str]
) -> tuple[list[datetime], list[list[float]]]:
    """Dense matrix of complete rows only, for model training.

    Incomplete rows are dropped rather than imputed: imputing a warm-up value
    invents data, and inventing data at the start of a series is how a model
    learns a regime that never existed.
    """
    stamps: list[datetime] = []
    matrix: list[list[float]] = []
    for row in rows:
        vals = [row.values.get(n) for n in names]
        if any(v is None for v in vals):
            continue
        stamps.append(row.ts)
        matrix.append([float(v) for v in vals if v is not None])
    return stamps, matrix
