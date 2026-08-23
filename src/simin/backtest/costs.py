"""Execution cost model.

Costs are modelled pessimistically and applied to *every* fill. This module is
the reason most published retail backtests are wrong: they assume a fill at the
signal price, with no spread, no impact and no delay. Simin assumes the worst
plausible fill and then reports every result again at double the cost.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from simin.types import FeeSchedule, OrderBook, Side


@dataclass(frozen=True, slots=True)
class CostModel:
    fees: FeeSchedule
    spread_bps: Decimal = Decimal("20")
    #: Square-root market impact coefficient, in bps per sqrt(participation).
    #: Large orders move the price against you; linear models understate this.
    impact_coefficient: Decimal = Decimal("50")
    #: Extra adverse move assumed between decision and fill.
    latency_bps: Decimal = Decimal("2")
    stress_multiplier: Decimal = Decimal("1")

    def scaled(self, multiplier: Decimal) -> CostModel:
        """A copy with every cost component multiplied — used for the 2x cost report."""
        return CostModel(
            fees=FeeSchedule(
                maker=self.fees.maker * multiplier, taker=self.fees.taker * multiplier
            ),
            spread_bps=self.spread_bps * multiplier,
            impact_coefficient=self.impact_coefficient * multiplier,
            latency_bps=self.latency_bps * multiplier,
            stress_multiplier=multiplier,
        )

    def slippage_bps(self, qty: Decimal, reference_depth: Decimal) -> Decimal:
        """Half-spread + latency + sqrt impact on the participation rate."""
        base = self.spread_bps / Decimal(2) + self.latency_bps
        if reference_depth <= 0:
            return base + self.impact_coefficient
        participation = min(Decimal(1), qty / reference_depth)
        impact = self.impact_coefficient * Decimal(str(math.sqrt(float(participation))))
        return base + impact

    def fill_price(
        self, reference_price: Decimal, side: Side, qty: Decimal, reference_depth: Decimal
    ) -> Decimal:
        """Reference price adjusted against the trader. Always against, never for."""
        bps = self.slippage_bps(qty, reference_depth)
        adjust = reference_price * bps / Decimal(10_000)
        return reference_price + adjust if side is Side.BUY else reference_price - adjust

    def fee(self, notional: Decimal, *, is_maker: bool = False) -> Decimal:
        return self.fees.cost(abs(notional), is_maker=is_maker)

    def round_trip_bps(self) -> Decimal:
        """Total cost of entering and exiting, in bps. The edge floor."""
        return (self.fees.taker * Decimal(2)) * Decimal(10_000) + self.spread_bps + (
            self.latency_bps * Decimal(2)
        )

    @staticmethod
    def depth_from_book(book: OrderBook, side: Side, levels: int = 5) -> Decimal:
        return book.depth_notional(side, levels)
