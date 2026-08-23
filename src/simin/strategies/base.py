"""Strategy plugin interface.

A strategy answers one question: *given everything knowable at this bar close,
do I want exposure, and where does the idea stop being right?* It returns
intents, never sizes — sizing belongs to the risk engine, which alone knows the
account state.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from simin.features.engine import FeatureRow
from simin.features.regime import RegimeState
from simin.risk.engine import Intent, OpenPosition


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Everything a strategy may see. By construction, nothing from the future."""

    ts: datetime
    symbol: str
    row: FeatureRow
    regime: RegimeState
    position: OpenPosition | None
    bar_index: int

    def feature(self, name: str) -> float | None:
        return self.row.get(name)

    def require(self, *names: str) -> tuple[float, ...] | None:
        """Fetch features, or None if any is still warming up.

        Strategies must not fire on partial data: an indicator that is None
        during warm-up will happily be treated as 0.0 by careless code, which
        manufactures signals out of missing values.
        """
        values = [self.row.get(n) for n in names]
        if any(v is None for v in values):
            return None
        return tuple(float(v) for v in values if v is not None)


class Strategy(abc.ABC):
    name: str
    #: Minimum bars needed before this strategy may act.
    warmup: int = 200
    #: "risk"  -> size from the risk budget and the stop distance (every real strategy)
    #: "full"  -> deploy all available capital, ignoring the stop distance
    #:
    #: "full" exists for capital benchmarks such as buy-and-hold. A risk-sized
    #: buy-and-hold deploys a sliver of the account and returns roughly zero,
    #: which would make "beats buy and hold" a gate that passes by default —
    #: the comparison has to be against actually holding the asset.
    allocation: str = "risk"

    @abc.abstractmethod
    def generate(self, ctx: StrategyContext) -> Intent | None:
        """Return an entry intent, or None."""

    def exit_signal(self, ctx: StrategyContext) -> bool:
        """Return True to close an open position for a reason other than the stop.

        Default: never. Stops and trailing stops handle most exits; a strategy
        overrides this only when the *thesis* itself has expired.
        """
        return False

    def stop_for(self, ctx: StrategyContext, direction: int, entry: Decimal) -> Decimal | None:
        """ATR-based protective stop. Volatility sets the distance, not a round number."""
        atr = ctx.feature("atr14")
        if atr is None or atr <= 0:
            return None
        distance = Decimal(str(atr)) * Decimal(str(self.stop_atr_multiple))
        return entry - distance if direction > 0 else entry + distance

    stop_atr_multiple: float = 2.5
