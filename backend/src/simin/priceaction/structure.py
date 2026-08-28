"""Price action: what the chart says without any indicator on it.

Three layers, each built on the one before:

1. **Swing points** — the pivot highs and lows that define the shape.
2. **Market structure** — higher-highs/higher-lows, and the two events that
   matter: BOS (break of structure, trend continues) and CHoCH (change of
   character, trend may be ending).
3. **Levels** — where price has repeatedly turned. These become stop and target
   locations, because a stop placed just past a level that has held four times
   is a very different bet from a stop placed at a round number.

Everything here is strictly causal: a swing at index `i` is only confirmed at
index `i + right`, and every function returns values indexed by the bar at which
the information was actually *available*, not the bar it describes. That
distinction is the difference between a backtest and a fantasy.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass

from simin.core.types import Candle


class SwingKind(enum.StrEnum):
    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class Swing:
    index: int
    price: float
    kind: SwingKind
    #: The bar at which this swing became knowable. Always index + right.
    confirmed_at: int


class Structure(enum.StrEnum):
    UPTREND = "uptrend"
    DOWNTREND = "downtrend"
    RANGE = "range"


class StructureEvent(enum.StrEnum):
    BOS_UP = "bos_up"
    BOS_DOWN = "bos_down"
    CHOCH_UP = "choch_up"
    CHOCH_DOWN = "choch_down"
    NONE = "none"


def find_swings(candles: Sequence[Candle], left: int = 2, right: int = 2) -> list[Swing]:
    """Fractal pivots: a high with `left` lower highs before and `right` after.

    `right` is the confirmation lag and is the reason this is honest. A pivot is
    not a pivot until the bars after it have printed.
    """
    if left < 1 or right < 1:
        raise ValueError("left and right must both be >= 1")
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    swings: list[Swing] = []
    for i in range(left, len(candles) - right):
        window_h = highs[i - left : i + right + 1]
        if highs[i] == max(window_h) and window_h.count(highs[i]) == 1:
            swings.append(Swing(i, highs[i], SwingKind.HIGH, i + right))
        window_l = lows[i - left : i + right + 1]
        if lows[i] == min(window_l) and window_l.count(lows[i]) == 1:
            swings.append(Swing(i, lows[i], SwingKind.LOW, i + right))
    swings.sort(key=lambda s: (s.confirmed_at, s.index))
    return swings


@dataclass(frozen=True, slots=True)
class StructureState:
    """The read of market structure as of one bar."""

    structure: Structure
    event: StructureEvent
    last_high: float | None
    last_low: float | None
    prev_high: float | None
    prev_low: float | None

    @property
    def is_trending(self) -> bool:
        return self.structure is not Structure.RANGE

    @property
    def bias(self) -> int:
        if self.structure is Structure.UPTREND:
            return 1
        if self.structure is Structure.DOWNTREND:
            return -1
        return 0


def read_structure(
    candles: Sequence[Candle], left: int = 2, right: int = 2
) -> list[StructureState]:
    """Market structure at every bar, using only information available then.

    A BOS is price closing beyond the last confirmed swing in the direction of
    the existing trend. A CHoCH is price closing beyond it *against* the trend —
    the first hint the trend is done. Requiring a *close* beyond, not a wick,
    removes most of the false signals from stop hunts.
    """
    swings = find_swings(candles, left, right)
    by_bar: dict[int, list[Swing]] = {}
    for s in swings:
        by_bar.setdefault(s.confirmed_at, []).append(s)

    states: list[StructureState] = []
    highs: list[float] = []
    lows: list[float] = []
    structure = Structure.RANGE

    for i, candle in enumerate(candles):
        for s in by_bar.get(i, ()):
            (highs if s.kind is SwingKind.HIGH else lows).append(s.price)

        last_h = highs[-1] if highs else None
        last_l = lows[-1] if lows else None
        prev_h = highs[-2] if len(highs) > 1 else None
        prev_l = lows[-2] if len(lows) > 1 else None
        close = float(candle.close)
        event = StructureEvent.NONE

        if last_h is not None and close > last_h:
            event = (
                StructureEvent.BOS_UP
                if structure is Structure.UPTREND
                else StructureEvent.CHOCH_UP
            )
            structure = Structure.UPTREND
        elif last_l is not None and close < last_l:
            event = (
                StructureEvent.BOS_DOWN
                if structure is Structure.DOWNTREND
                else StructureEvent.CHOCH_DOWN
            )
            structure = Structure.DOWNTREND

        states.append(StructureState(structure, event, last_h, last_l, prev_h, prev_l))
    return states


@dataclass(frozen=True, slots=True)
class Level:
    """A horizontal price level that has been respected more than once."""

    price: float
    touches: int
    kind: SwingKind
    last_touch_index: int
    #: 0..1. Combines touch count and recency — an old level that has not been
    #: tested in 500 bars is a historical curiosity, not a trading level.
    strength: float


def find_levels(
    candles: Sequence[Candle],
    swings: Sequence[Swing] | None = None,
    tolerance: float = 0.0025,
    min_touches: int = 2,
    max_levels: int = 8,
    as_of: int | None = None,
    lookback: int = 400,
) -> list[Level]:
    """Cluster swing points into support/resistance levels, as of one bar.

    `as_of` is not optional in spirit. Levels computed over an entire dataset
    and then handed to every bar are a lookahead bug wearing a very convincing
    disguise: at bar 500 the strategy sees a level that will not form until bar
    1800, places its stop just past it, and backtests beautifully. Passing
    `as_of` restricts the input to swings already confirmed at that bar, and
    measures recency from there rather than from the end of history.

    `tolerance` is fractional, not absolute: 0.25% of price. An absolute
    tolerance that works for BTC at 60,000 is nonsense for a token at 0.4.
    """
    if swings is None:
        swings = find_swings(candles)
    reference = len(candles) - 1 if as_of is None else as_of
    if as_of is not None:
        # Only swings the market had already printed AND confirmed by this bar.
        swings = [
            s
            for s in swings
            if s.confirmed_at <= as_of and s.index >= as_of - lookback
        ]
    if not swings:
        return []

    ordered = sorted(swings, key=lambda s: s.price)
    clusters: list[list[Swing]] = [[ordered[0]]]
    for s in ordered[1:]:
        anchor = clusters[-1][0].price
        if anchor > 0 and abs(s.price - anchor) / anchor <= tolerance:
            clusters[-1].append(s)
        else:
            clusters.append([s])

    levels: list[Level] = []
    for cluster in clusters:
        if len(cluster) < min_touches:
            continue
        price = sum(s.price for s in cluster) / len(cluster)
        last = max(s.index for s in cluster)
        recency = 1.0 - min((reference - last) / max(lookback, 1), 1.0)
        touch_score = min(len(cluster) / 5.0, 1.0)
        highs = sum(1 for s in cluster if s.kind is SwingKind.HIGH)
        levels.append(
            Level(
                price=price,
                touches=len(cluster),
                kind=SwingKind.HIGH if highs * 2 >= len(cluster) else SwingKind.LOW,
                last_touch_index=last,
                strength=round(0.6 * touch_score + 0.4 * recency, 4),
            )
        )
    levels.sort(key=lambda lv: lv.strength, reverse=True)
    return levels[:max_levels]


def nearest_level(levels: Sequence[Level], price: float, above: bool) -> Level | None:
    """The closest level above (resistance) or below (support) a price."""
    candidates = [lv for lv in levels if (lv.price > price if above else lv.price < price)]
    if not candidates:
        return None
    return min(candidates, key=lambda lv: abs(lv.price - price))
