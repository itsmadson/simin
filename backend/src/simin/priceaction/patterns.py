"""Candlestick patterns, scored rather than boolean.

Most pattern libraries return True/False, which throws away the thing that
actually matters: a pin bar with a 4:1 wick-to-body ratio at a tested support
level is not the same trade as a pin bar with a 2:1 ratio in the middle of
nowhere, and collapsing both to `True` is how a strategy ends up taking the
second one.

So every detector here returns a strength in 0..1, and the confluence scorer
weights by that strength. Context (where the pattern happened) is applied by
the caller, which knows about levels; this module only judges the candle shape.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass

from simin.core.types import Candle


class PatternKind(enum.StrEnum):
    PIN_BAR_BULL = "pin_bar_bull"
    PIN_BAR_BEAR = "pin_bar_bear"
    ENGULFING_BULL = "engulfing_bull"
    ENGULFING_BEAR = "engulfing_bear"
    INSIDE_BAR = "inside_bar"
    OUTSIDE_BAR = "outside_bar"
    MARUBOZU_BULL = "marubozu_bull"
    MARUBOZU_BEAR = "marubozu_bear"
    DOJI = "doji"
    MORNING_STAR = "morning_star"
    EVENING_STAR = "evening_star"
    THREE_PUSH_UP = "three_push_up"
    THREE_PUSH_DOWN = "three_push_down"


@dataclass(frozen=True, slots=True)
class Pattern:
    kind: PatternKind
    #: 0..1 — how textbook this instance is.
    strength: float
    #: +1 bullish, -1 bearish, 0 neutral/undecided.
    bias: int
    index: int

    @property
    def is_bullish(self) -> bool:
        return self.bias > 0

    @property
    def is_bearish(self) -> bool:
        return self.bias < 0


def _f(x: object) -> float:
    return float(x)  # type: ignore[arg-type]


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def detect(candles: Sequence[Candle], index: int, atr_value: float | None = None) -> list[Pattern]:
    """Every pattern completing at `index`. Reads backwards only — never forwards.

    `atr_value` scales the significance checks: a 0.3% body is huge for a
    stablecoin pair and invisible for a meme coin, and only ATR knows which
    situation we are in. Without it the detectors fall back to relative-to-range
    tests, which work but are noisier.
    """
    if index < 0 or index >= len(candles):
        raise IndexError(f"index {index} out of range for {len(candles)} candles")
    out: list[Pattern] = []
    c = candles[index]
    rng = _f(c.range)
    if rng <= 0:
        return out

    body = _f(c.body)
    upper = _f(c.upper_wick)
    lower = _f(c.lower_wick)
    body_ratio = body / rng

    # --- Single-candle shapes ---------------------------------------------
    if body_ratio < 0.12:
        out.append(Pattern(PatternKind.DOJI, _clamp(1.0 - body_ratio / 0.12), 0, index))

    # Pin bar: one long wick, small body pushed to the far end. The wick is the
    # rejection; the body position confirms who won the bar.
    if body > 0 and lower >= 2.0 * body and lower / rng >= 0.55 and upper / rng <= 0.25:
        strength = _clamp((lower / body - 2.0) / 3.0 * 0.5 + lower / rng * 0.5)
        out.append(Pattern(PatternKind.PIN_BAR_BULL, strength, 1, index))
    if body > 0 and upper >= 2.0 * body and upper / rng >= 0.55 and lower / rng <= 0.25:
        strength = _clamp((upper / body - 2.0) / 3.0 * 0.5 + upper / rng * 0.5)
        out.append(Pattern(PatternKind.PIN_BAR_BEAR, strength, -1, index))

    # Marubozu: almost pure body. Continuation, not reversal.
    if body_ratio >= 0.85:
        kind = PatternKind.MARUBOZU_BULL if c.is_bull else PatternKind.MARUBOZU_BEAR
        out.append(
            Pattern(kind, _clamp((body_ratio - 0.85) / 0.15), 1 if c.is_bull else -1, index)
        )

    if index < 1:
        return out

    # --- Two-candle patterns ----------------------------------------------
    p = candles[index - 1]
    p_body = _f(p.body)

    if _f(c.high) <= _f(p.high) and _f(c.low) >= _f(p.low):
        # Inside bar = compression. Direction unknown; the break decides.
        compression = 1.0 - rng / max(_f(p.range), 1e-12)
        out.append(Pattern(PatternKind.INSIDE_BAR, _clamp(compression), 0, index))
    elif _f(c.high) > _f(p.high) and _f(c.low) < _f(p.low):
        expansion = rng / max(_f(p.range), 1e-12) - 1.0
        out.append(
            Pattern(
                PatternKind.OUTSIDE_BAR,
                _clamp(expansion),
                1 if c.is_bull else -1,
                index,
            )
        )

    # Engulfing: this bar's body swallows the previous body and closes past it.
    # Requiring the previous bar to have a real body rules out "engulfing a
    # doji", which is a meaningless pattern that fires constantly in chop.
    if p_body > 0.15 * _f(p.range):
        if (
            c.is_bull
            and not p.is_bull
            and _f(c.close) > _f(p.open)
            and _f(c.open) <= _f(p.close)
        ):
            out.append(
                Pattern(
                    PatternKind.ENGULFING_BULL,
                    _clamp(body / max(p_body, 1e-12) / 3.0),
                    1,
                    index,
                )
            )
        if (
            not c.is_bull
            and p.is_bull
            and _f(c.close) < _f(p.open)
            and _f(c.open) >= _f(p.close)
        ):
            out.append(
                Pattern(
                    PatternKind.ENGULFING_BEAR,
                    _clamp(body / max(p_body, 1e-12) / 3.0),
                    -1,
                    index,
                )
            )

    if index < 2:
        return out

    # --- Three-candle patterns --------------------------------------------
    a, b = candles[index - 2], candles[index - 1]
    a_mid = (_f(a.open) + _f(a.close)) / 2
    b_body_ratio = _f(b.body) / max(_f(b.range), 1e-12)

    # Star: strong bar, small indecisive bar, strong bar back the other way,
    # closing past the midpoint of the first.
    if not a.is_bull and b_body_ratio < 0.4 and c.is_bull and _f(c.close) > a_mid:
        depth = (_f(c.close) - a_mid) / max(_f(a.body), 1e-12)
        out.append(Pattern(PatternKind.MORNING_STAR, _clamp(depth), 1, index))
    if a.is_bull and b_body_ratio < 0.4 and not c.is_bull and _f(c.close) < a_mid:
        depth = (a_mid - _f(c.close)) / max(_f(a.body), 1e-12)
        out.append(Pattern(PatternKind.EVENING_STAR, _clamp(depth), -1, index))

    # Three pushes: exhaustion. Each leg smaller than the last means the move is
    # running out of participants — a reversal setup, so the bias is inverted.
    legs = [_f(x.close) - _f(x.open) for x in (a, b, c)]
    if all(v > 0 for v in legs) and legs[0] > legs[1] > legs[2]:
        fade = 1.0 - legs[2] / max(legs[0], 1e-12)
        out.append(Pattern(PatternKind.THREE_PUSH_UP, _clamp(fade), -1, index))
    if all(v < 0 for v in legs) and legs[0] < legs[1] < legs[2]:
        fade = 1.0 - abs(legs[2]) / max(abs(legs[0]), 1e-12)
        out.append(Pattern(PatternKind.THREE_PUSH_DOWN, _clamp(fade), 1, index))

    if atr_value and atr_value > 0:
        # Discard anything whose candle is small relative to normal volatility.
        # A textbook engulfing inside a dead 3am range is noise wearing a costume.
        significance = _clamp(rng / (2.0 * atr_value))
        out = [p_ for p_ in out if p_.strength * significance > 0.05]
        out = [Pattern(p_.kind, _clamp(p_.strength * (0.5 + 0.5 * significance)), p_.bias, p_.index) for p_ in out]

    return out


def net_bias(patterns: Sequence[Pattern]) -> float:
    """Signed −1..+1 summary of a bar's patterns, weighted by strength."""
    if not patterns:
        return 0.0
    total = sum(p.strength for p in patterns)
    if total <= 0:
        return 0.0
    signed = sum(p.strength * p.bias for p in patterns)
    return max(-1.0, min(1.0, signed / total))
