"""The feature frame: every number a strategy is allowed to see, per bar.

Computed once per symbol per timeframe, then indexed by bar. Strategies read
from a `FeatureRow` and cannot reach the raw candle list, which is a deliberate
constraint: a strategy that cannot index into the future cannot accidentally
read from it.

Warm-up is represented as `None`, never 0.0. `FeatureRow.require()` returns
`None` for the whole request if any single feature is missing, so a strategy
either has everything it needs or does nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from simin.core.types import Candle, TF
from simin.indicators import core as ind
from simin.priceaction.patterns import Pattern, detect, net_bias
from simin.priceaction.structure import (
    Level,
    StructureState,
    find_levels,
    find_swings,
    read_structure,
)


@dataclass(frozen=True, slots=True)
class FeatureRow:
    """One bar's worth of everything."""

    index: int
    ts: datetime
    candle: Candle
    values: dict[str, float | None]
    structure: StructureState
    patterns: tuple[Pattern, ...]
    levels: tuple[Level, ...]

    def get(self, name: str) -> float | None:
        if name not in self.values:
            raise KeyError(f"unknown feature {name!r}; did you forget to add it to FeatureFrame?")
        return self.values[name]

    def require(self, *names: str) -> tuple[float, ...] | None:
        """All requested features, or None if any is still warming up.

        Strategies must not fire on partial data. An indicator that is None
        during warm-up gets treated as 0.0 by careless arithmetic, which
        manufactures signals out of missing values — RSI 0 reads as maximally
        oversold, and the bot buys the warm-up window every single time.
        """
        out: list[float] = []
        for n in names:
            v = self.get(n)
            if v is None:
                return None
            out.append(v)
        return tuple(out)

    @property
    def price(self) -> float:
        return float(self.candle.close)

    @property
    def pattern_bias(self) -> float:
        return net_bias(self.patterns)


class FeatureFrame:
    """All features for one symbol on one timeframe."""

    __slots__ = (
        "symbol", "tf", "candles", "_series", "_structure", "_patterns", "_levels_at",
    )

    #: Bars between unconditional level-cache refreshes.
    LEVEL_REFRESH = 25

    def __init__(self, symbol: str, tf: TF, candles: Sequence[Candle]) -> None:
        self.symbol = symbol
        self.tf = tf
        self.candles = list(candles)
        self._series: dict[str, ind.Series] = {}
        self._structure: list[StructureState] = []
        self._patterns: list[tuple[Pattern, ...]] = []
        self._levels_at: list[tuple[Level, ...]] = []
        if self.candles:
            self._compute()

    def __len__(self) -> int:
        return len(self.candles)

    def _compute(self) -> None:
        cs = self.candles
        o = [float(c.open) for c in cs]
        h = [float(c.high) for c in cs]
        low = [float(c.low) for c in cs]
        c_ = [float(c.close) for c in cs]
        v = [float(c.volume) for c in cs]
        n = len(cs)
        S = self._series

        # Trend
        S["ema_fast"] = ind.ema(c_, 20)
        S["ema_slow"] = ind.ema(c_, 50)
        S["ema_trend"] = ind.ema(c_, 200)
        S["sma_20"] = ind.sma(c_, 20)

        # Momentum
        S["rsi"] = ind.rsi(c_, 14)
        S["rsi_fast"] = ind.rsi(c_, 7)
        macd_line, macd_sig, macd_hist = ind.macd(c_, 12, 26, 9)
        S["macd"] = macd_line
        S["macd_signal"] = macd_sig
        S["macd_hist"] = macd_hist
        S["roc"] = ind.roc(c_, 10)
        stoch_k, stoch_d = ind.stochastic(h, low, c_, 14, 3, 3)
        S["stoch_k"] = stoch_k
        S["stoch_d"] = stoch_d

        # Volatility
        S["atr"] = ind.atr(h, low, c_, 14)
        S["atr_pct"] = [
            (a / p) if (a is not None and p) else None for a, p in zip(S["atr"], c_, strict=True)
        ]
        bb_mid, bb_up, bb_dn, bb_pct = ind.bollinger(c_, 20, 2.0)
        S["bb_mid"] = bb_mid
        S["bb_upper"] = bb_up
        S["bb_lower"] = bb_dn
        S["bb_pct"] = bb_pct
        S["bb_width"] = ind.bandwidth(bb_up, bb_dn, bb_mid)
        kc_mid, kc_up, kc_dn = ind.keltner(h, low, c_, 20, 1.5)
        S["kc_upper"] = kc_up
        S["kc_lower"] = kc_dn

        # Regime
        adx_v, pdi, mdi = ind.adx(h, low, c_, 14)
        S["adx"] = adx_v
        S["di_plus"] = pdi
        S["di_minus"] = mdi
        st_line, st_dir = ind.supertrend(h, low, c_, 10, 3.0)
        S["supertrend"] = st_line
        S["supertrend_dir"] = [float(d) if d is not None else None for d in st_dir]

        # Volume / flow
        obv_v = ind.obv(c_, v)
        S["obv"] = list(obv_v)
        S["obv_slope"] = ind.roc(obv_v, 10) if any(obv_v) else [None] * n
        S["vol_sma"] = ind.sma(v, 20)
        S["vol_ratio"] = [
            (vv / m) if (m is not None and m > 0) else None
            for vv, m in zip(v, S["vol_sma"], strict=True)
        ]
        session = [i == 0 or cs[i].ts.date() != cs[i - 1].ts.date() for i in range(n)]
        S["vwap"] = ind.vwap_session(h, low, c_, v, session)
        S["vwap_dist"] = [
            ((p - w) / w) if (w is not None and w) else None
            for p, w in zip(c_, S["vwap"], strict=True)
        ]

        # Stretch — the oscillation strategies' core measurement
        S["zscore"] = ind.zscore(c_, 50)

        # Derived: distance of close from the trend EMA in ATR units. Scale-free
        # and directly comparable across BTC at 60k and a token at 0.4.
        S["trend_dist_atr"] = [
            ((c_[i] - S["ema_trend"][i]) / S["atr"][i])
            if (S["ema_trend"][i] is not None and S["atr"][i] and S["atr"][i] > 0)
            else None
            for i in range(n)
        ]
        S["candle_body_pct"] = [
            (float(cs[i].body) / float(cs[i].range)) if float(cs[i].range) > 0 else None
            for i in range(n)
        ]

        for name, series in S.items():
            if len(series) != n:
                raise AssertionError(f"feature {name!r} has length {len(series)}, expected {n}")

        # Price action
        self._structure = read_structure(cs, left=2, right=2)
        atr_s = S["atr"]
        self._patterns = [tuple(detect(cs, i, atr_s[i])) for i in range(n)]

        # Levels are recomputed as the market prints them, never once over the
        # whole dataset. Recomputing on every single bar is wasted work — the
        # set only changes when a new swing confirms — so the cache is refreshed
        # on swing confirmations and every LEVEL_REFRESH bars, and each refresh
        # sees strictly past data.
        swings = find_swings(cs, 2, 2)
        confirmations = {s.confirmed_at for s in swings}
        self._levels_at = []
        cached: tuple[Level, ...] = ()
        for i in range(n):
            if i in confirmations or i % self.LEVEL_REFRESH == 0:
                cached = tuple(find_levels(cs, swings, as_of=i))
            self._levels_at.append(cached)

    def row(self, index: int) -> FeatureRow:
        if not 0 <= index < len(self.candles):
            raise IndexError(f"bar {index} out of range (0..{len(self.candles) - 1})")
        return FeatureRow(
            index=index,
            ts=self.candles[index].ts,
            candle=self.candles[index],
            values={k: s[index] for k, s in self._series.items()},
            structure=self._structure[index],
            patterns=self._patterns[index],
            levels=self._levels_at[index],
        )

    def rows(self, start: int = 0) -> list[FeatureRow]:
        return [self.row(i) for i in range(start, len(self.candles))]

    def series(self, name: str) -> ind.Series:
        return self._series[name]

    @property
    def feature_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._series))

    def warmup_complete_at(self) -> int:
        """First bar where every feature has a value. Nothing may trade before it."""
        n = len(self.candles)
        for i in range(n):
            if all(s[i] is not None for s in self._series.values()):
                return i
        return n
