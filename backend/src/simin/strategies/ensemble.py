"""Combining several strategies into one decision.

Running six strategies and acting on whichever fires first is not an ensemble —
it is six uncorrelated bots sharing an account, and the one with the loosest
filter dominates the trade log. This module makes them vote.

Two rules do the real work:

**Regime muting.** A strategy declares the regime it was built for. Running in
the wrong one, its vote is down-weighted rather than dropped, so it can still
veto but cannot initiate. A mean-reversion system screaming "buy" in a
freight-train downtrend should not be silenced entirely — its disagreement with
the trend strategies is information — but it must not be allowed to open the
trade.

**Disagreement is a reason not to trade.** If two strategies want long and one
wants short, the net confidence falls. The dial's `min_confluence` then decides
whether what remains clears the bar. That threshold is the entire link between
the risk dial and how often the bot trades.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from simin.core.types import Direction, Intent, Signal
from simin.indicators.features import FeatureRow
from simin.risk.dial import RiskProfile
from simin.strategies.base import Context, Strategy
from simin.strategies.library import ADX_CHOPPY, ADX_TRENDING

#: Weight multiplier for a strategy voting outside the regime it was built for.
OUT_OF_REGIME_WEIGHT = 0.35


@dataclass(frozen=True, slots=True)
class Vote:
    strategy: str
    signal: Signal
    confidence: float
    weight: float
    stop_price: Decimal | None
    reasons: tuple[str, ...]

    @property
    def direction(self) -> int:
        if self.signal is Signal.LONG:
            return 1
        if self.signal is Signal.SHORT:
            return -1
        return 0


@dataclass(frozen=True, slots=True)
class Decision:
    """The ensemble's verdict, with the full reasoning kept for the UI."""

    intent: Intent
    votes: tuple[Vote, ...]
    regime: str
    #: Score before the dial's threshold was applied.
    raw_score: float
    #: The threshold it had to clear.
    threshold: float
    accepted: bool
    rejected_because: str = ""

    @property
    def agreeing(self) -> tuple[str, ...]:
        d = self.intent.direction
        if d is None:
            return ()
        want = 1 if d is Direction.LONG else -1
        return tuple(v.strategy for v in self.votes if v.direction == want)

    @property
    def dissenting(self) -> tuple[str, ...]:
        d = self.intent.direction
        if d is None:
            return ()
        want = 1 if d is Direction.LONG else -1
        return tuple(v.strategy for v in self.votes if v.direction == -want)


def classify_regime(row: FeatureRow) -> str:
    """trend / range / unknown, from ADX and the structural read."""
    adx = row.get("adx")
    if adx is None:
        return "unknown"
    if adx >= ADX_TRENDING:
        return "trend"
    if adx <= ADX_CHOPPY:
        return "range"
    return "range" if not row.structure.is_trending else "trend"


class Ensemble:
    """Holds the strategy instances and produces one decision per bar."""

    __slots__ = ("strategies", "profile")

    def __init__(self, strategies: Sequence[Strategy], profile: RiskProfile) -> None:
        if not strategies:
            raise ValueError("an ensemble needs at least one strategy")
        self.strategies = list(strategies)
        self.profile = profile

    @property
    def warmup(self) -> int:
        return max(s.warmup for s in self.strategies)

    def decide(self, ctx: Context) -> Decision:
        regime = classify_regime(ctx.row)
        votes: list[Vote] = []

        for strat in self.strategies:
            intent = strat.evaluate(ctx)
            weight = 1.0
            if strat.regime != "any" and regime != "unknown" and strat.regime != regime:
                weight = OUT_OF_REGIME_WEIGHT
            votes.append(
                Vote(
                    strategy=strat.name,
                    signal=intent.signal,
                    confidence=intent.confidence,
                    weight=weight,
                    stop_price=intent.stop_price,
                    reasons=intent.reasons,
                )
            )

        active = [v for v in votes if v.direction != 0 and v.confidence > 0]
        if not active:
            return Decision(
                intent=Intent(Signal.FLAT, 0.0),
                votes=tuple(votes),
                regime=regime,
                raw_score=0.0,
                threshold=self.profile.min_confluence,
                accepted=False,
                rejected_because="no strategy produced a signal",
            )

        net = sum(v.direction * v.confidence * v.weight for v in active)
        total = sum(v.confidence * v.weight for v in active)
        if total <= 0 or net == 0:
            return Decision(
                intent=Intent(Signal.FLAT, 0.0),
                votes=tuple(votes),
                regime=regime,
                raw_score=0.0,
                threshold=self.profile.min_confluence,
                accepted=False,
                rejected_because="strategies exactly cancelled out",
            )

        direction = 1 if net > 0 else -1
        # Agreement ratio, scaled by the best in-regime confidence available.
        # Scaling matters: three strategies agreeing weakly is not the same as
        # three agreeing strongly, and the agreement ratio alone cannot tell
        # them apart.
        agreement = abs(net) / total
        best = max(
            (v.confidence for v in active if v.direction == direction and v.weight == 1.0),
            default=max(v.confidence for v in active if v.direction == direction),
        )
        score = agreement * best

        supporters = [v for v in active if v.direction == direction]
        in_regime = [v for v in supporters if v.weight == 1.0]
        if not in_regime:
            return Decision(
                intent=Intent(Signal.FLAT, 0.0),
                votes=tuple(votes),
                regime=regime,
                raw_score=score,
                threshold=self.profile.min_confluence,
                accepted=False,
                rejected_because=f"only out-of-regime strategies want this ({regime} market)",
            )

        if direction < 0 and not self.profile.allow_shorts:
            return Decision(
                intent=Intent(Signal.FLAT, 0.0),
                votes=tuple(votes),
                regime=regime,
                raw_score=score,
                threshold=self.profile.min_confluence,
                accepted=False,
                rejected_because=f"risk level {self.profile.level} does not short",
            )

        threshold = self.profile.min_confluence
        if score < threshold:
            return Decision(
                intent=Intent(Signal.FLAT, 0.0),
                votes=tuple(votes),
                regime=regime,
                raw_score=round(score, 4),
                threshold=threshold,
                accepted=False,
                rejected_because=(
                    f"confluence {score:.2f} below level {self.profile.level} "
                    f"threshold {threshold:.2f}"
                ),
            )

        stop = self._consensus_stop(supporters, direction)
        if stop is None:
            return Decision(
                intent=Intent(Signal.FLAT, 0.0),
                votes=tuple(votes),
                regime=regime,
                raw_score=round(score, 4),
                threshold=threshold,
                accepted=False,
                rejected_because="no strategy supplied a valid stop",
            )

        reasons: list[str] = []
        for v in sorted(in_regime, key=lambda x: x.confidence, reverse=True):
            reasons.extend(v.reasons[:3])
        dissent = [v.strategy for v in active if v.direction == -direction]
        if dissent:
            reasons.append("against: " + ", ".join(dissent))

        return Decision(
            intent=Intent(
                signal=Signal.LONG if direction > 0 else Signal.SHORT,
                confidence=round(score, 4),
                stop_price=stop,
                strategy="+".join(v.strategy for v in in_regime),
                reasons=tuple(reasons[:8]),
            ),
            votes=tuple(votes),
            regime=regime,
            raw_score=round(score, 4),
            threshold=threshold,
            accepted=True,
        )

    @staticmethod
    def _consensus_stop(supporters: Sequence[Vote], direction: int) -> Decimal | None:
        """The most conservative stop among the agreeing strategies.

        Widest wins. If one strategy thinks the idea is dead 1 ATR away and
        another thinks 2.5, entering with the 1-ATR stop means being wrong about
        the trade *and* about how much room it needed.
        """
        stops = [v.stop_price for v in supporters if v.stop_price is not None]
        if not stops:
            return None
        return min(stops) if direction > 0 else max(stops)
