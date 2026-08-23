"""Per-venue cost and capability configuration.

Fees, spreads and limits are *data*, never constants in strategy code, and they
are pessimistic by default. Values here are documented starting points from the
research in docs/04-exchanges-iran.md; the live system overrides them with fees
actually observed on fills, which is the only number that can't lie to you.

This module contains no credentials and no endpoints for designated venues —
trading adapters are operator-supplied plugins. See docs/04.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from simin.types import FeeSchedule


@dataclass(frozen=True, slots=True)
class VenueProfile:
    code: str
    display: str
    fees: FeeSchedule
    typical_spread_bps: Decimal
    supports_short: bool
    quote_assets: tuple[str, ...]
    notes: str

    def round_trip_cost(self, *, taker_both_sides: bool = True) -> Decimal:
        """Total modelled cost of a round trip as a fraction of notional.

        fees (both sides) + full spread. This is the number a signal's expected
        edge must exceed before it is worth anything at all.
        """
        fee = self.fees.taker if taker_both_sides else self.fees.maker
        return fee * Decimal(2) + self.typical_spread_bps / Decimal(10_000)


PROFILES: dict[str, VenueProfile] = {
    "public_global": VenueProfile(
        code="public_global",
        display="Global public data (read-only)",
        fees=FeeSchedule(maker=Decimal("0.0010"), taker=Decimal("0.0010")),
        typical_spread_bps=Decimal("2"),
        supports_short=True,
        quote_assets=("USDT",),
        notes="Data source only. Never used for order placement.",
    ),
    "local_irt_generic": VenueProfile(
        code="local_irt_generic",
        display="Generic Iranian IRT venue (operator plugin)",
        fees=FeeSchedule(maker=Decimal("0.0020"), taker=Decimal("0.0025")),
        typical_spread_bps=Decimal("60"),
        supports_short=False,
        quote_assets=("IRT", "USDT"),
        notes=(
            "Pessimistic defaults for a Toman venue: long/flat only, wide spreads on "
            "alt pairs, no sandbox. Round-trip cost ~1.1%, which excludes every "
            "intraday strategy below the 1h timeframe."
        ),
    ),
}


def profile(code: str) -> VenueProfile:
    try:
        return PROFILES[code]
    except KeyError as exc:
        raise KeyError(f"unknown venue profile {code!r}") from exc
