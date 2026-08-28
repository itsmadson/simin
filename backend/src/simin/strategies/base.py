"""Strategy interface and the confluence machinery underneath it.

A strategy answers one question: *given everything knowable at this bar close,
do I want exposure, in which direction, and where does the idea stop being
right?* It returns an `Intent` — never a size. Sizing belongs to the risk
engine, which alone knows the account balance and the dial setting.

## Confluence

Every strategy builds its confidence the same way: by collecting `Evidence`.
Each piece of evidence has a direction (+1/−1), a weight (how much this kind of
evidence is worth), and a strength in 0..1 (how emphatic this instance is).

The score is the *net agreement*: total signed evidence over total possible
evidence. So five weak confirmations do not equal one strong one, and evidence
pointing the other way actively subtracts rather than being quietly ignored.
That last property is the important one — a scorer that only counts supporting
evidence will always find a reason to trade.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from simin.core.types import Direction, Intent, Position, Signal
from simin.indicators.features import FeatureRow


@dataclass(frozen=True, slots=True)
class Evidence:
    """One reason to be long or short."""

    name: str
    #: +1 bullish, −1 bearish.
    direction: int
    #: How much this class of evidence counts. Set by the strategy.
    weight: float
    #: 0..1 — how emphatic this particular instance is.
    strength: float
    detail: str = ""

    def __post_init__(self) -> None:
        if self.direction not in (-1, 0, 1):
            raise ValueError(f"evidence direction must be -1, 0 or 1, got {self.direction}")
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError(f"evidence strength must be in 0..1, got {self.strength}")
        if self.weight < 0:
            raise ValueError("evidence weight must be non-negative")

    @property
    def signed(self) -> float:
        return self.direction * self.weight * self.strength


@dataclass(slots=True)
class Confluence:
    """A running tally of evidence for one bar."""

    items: list[Evidence] = field(default_factory=list)

    def add(
        self, name: str, direction: int, weight: float, strength: float = 1.0, detail: str = ""
    ) -> None:
        if strength <= 0 or direction == 0:
            return
        self.items.append(
            Evidence(name, direction, weight, min(1.0, max(0.0, strength)), detail)
        )

    def vote(self, name: str, condition: bool, direction: int, weight: float, strength: float = 1.0, detail: str = "") -> None:
        """Add evidence only if the condition holds. Keeps strategies readable."""
        if condition:
            self.add(name, direction, weight, strength, detail)

    @property
    def total_weight(self) -> float:
        return sum(e.weight for e in self.items)

    @property
    def net(self) -> float:
        return sum(e.signed for e in self.items)

    def score(self) -> tuple[int, float]:
        """(direction, confidence 0..1).

        Confidence is |net| / total_weight, so it measures *agreement*, not
        volume. Ten pieces of evidence split six-to-four score lower than three
        that all point the same way, which is the correct ranking: a split
        chart is a chart to stay out of.
        """
        total = self.total_weight
        if total <= 0:
            return 0, 0.0
        net = self.net
        if net == 0:
            return 0, 0.0
        return (1 if net > 0 else -1), min(abs(net) / total, 1.0)

    def reasons(self, direction: int, limit: int = 6) -> tuple[str, ...]:
        """The strongest supporting evidence, for the UI and the trade log."""
        supporting = [e for e in self.items if e.direction == direction]
        supporting.sort(key=lambda e: e.weight * e.strength, reverse=True)
        return tuple(f"{e.name}: {e.detail}" if e.detail else e.name for e in supporting[:limit])

    def against(self, direction: int) -> tuple[str, ...]:
        opposing = [e for e in self.items if e.direction == -direction]
        opposing.sort(key=lambda e: e.weight * e.strength, reverse=True)
        return tuple(e.name for e in opposing[:4])


@dataclass(frozen=True, slots=True)
class Context:
    """Everything a strategy may see. By construction, nothing from the future."""

    row: FeatureRow
    #: The same bar's row on the higher timeframe, for trend context. None
    #: while the higher timeframe is still warming up.
    context_row: FeatureRow | None
    symbol: str
    position: Position | None
    bar_index: int
    #: True when the dial permits shorting. Strategies must respect it rather
    #: than emitting shorts the risk layer silently drops — a dropped signal
    #: still consumes the daily trade budget.
    allow_shorts: bool = True
    allow_counter_trend: bool = True

    @property
    def price(self) -> float:
        return self.row.price

    @property
    def in_position(self) -> bool:
        return self.position is not None

    @property
    def higher_bias(self) -> int:
        """Trend direction on the context timeframe: +1, −1, or 0 if unknown."""
        if self.context_row is None:
            return 0
        vals = self.context_row.require("ema_fast", "ema_slow")
        if vals is None:
            return self.context_row.structure.bias
        fast, slow = vals
        if fast > slow:
            return 1
        if fast < slow:
            return -1
        return 0


class Strategy(abc.ABC):
    """Base class. Subclasses implement `evaluate` and declare a warm-up."""

    #: Stable identifier used in configs, trade records and the UI.
    name: str = "unnamed"
    name_fa: str = ""
    #: Bars of history required before this strategy may act.
    warmup: int = 210
    #: Which regime this strategy is designed for. The ensemble uses this to
    #: mute strategies that are running in conditions they were not built for.
    regime: str = "any"  # "trend" | "range" | "any"
    #: Human-readable one-liner for the UI.
    description: str = ""
    description_fa: str = ""

    @abc.abstractmethod
    def evaluate(self, ctx: Context) -> Intent:
        """The strategy's opinion at this bar."""

    # --- Shared helpers ---------------------------------------------------

    def _stop_from_atr(self, ctx: Context, direction: Direction, mult: float) -> Decimal | None:
        """A stop `mult` ATRs away, snapped past the nearest structural level.

        Snapping matters: a stop that sits just *inside* a level that has held
        four times will be taken out by exactly the move the level is there to
        cause. Pushing it just past turns a guaranteed stop-out into a real one.
        """
        atr = ctx.row.get("atr")
        if atr is None or atr <= 0:
            return None
        price = ctx.price
        raw = price - mult * atr if direction is Direction.LONG else price + mult * atr

        from simin.priceaction.structure import nearest_level

        level = nearest_level(ctx.row.levels, price, above=direction is Direction.SHORT)
        if level is not None:
            buffer = 0.15 * atr
            if direction is Direction.LONG and level.price - buffer < price:
                raw = min(raw, level.price - buffer)
            elif direction is Direction.SHORT and level.price + buffer > price:
                raw = max(raw, level.price + buffer)

        if direction is Direction.LONG and raw >= price:
            return None
        if direction is Direction.SHORT and raw <= price:
            return None
        return Decimal(str(round(max(raw, 1e-12), 10)))

    def _intent(
        self,
        ctx: Context,
        direction: int,
        confidence: float,
        conf: Confluence,
        stop_mult: float,
    ) -> Intent:
        if direction == 0 or confidence <= 0:
            return Intent(Signal.FLAT, 0.0, strategy=self.name)
        d = Direction.LONG if direction > 0 else Direction.SHORT
        if d is Direction.SHORT and not ctx.allow_shorts:
            return Intent(Signal.FLAT, 0.0, strategy=self.name)
        stop = self._stop_from_atr(ctx, d, stop_mult)
        if stop is None:
            return Intent(Signal.FLAT, 0.0, strategy=self.name)
        return Intent(
            signal=Signal.LONG if d is Direction.LONG else Signal.SHORT,
            confidence=round(confidence, 4),
            stop_price=stop,
            strategy=self.name,
            reasons=conf.reasons(direction),
        )

    def flat(self) -> Intent:
        return Intent(Signal.FLAT, 0.0, strategy=self.name)


def registry() -> dict[str, type[Strategy]]:
    """All built-in strategies, by name. Imported lazily to avoid a cycle."""
    from simin.strategies import library

    return library.STRATEGIES


def build(name: str, **kwargs: object) -> Strategy:
    reg = registry()
    if name not in reg:
        raise ValueError(f"unknown strategy {name!r}; available: {sorted(reg)}")
    return reg[name](**kwargs)  # type: ignore[arg-type]


def build_many(names: Sequence[str]) -> list[Strategy]:
    return [build(n) for n in names]
