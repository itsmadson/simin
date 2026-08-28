"""What a trade actually costs.

Most retail bots do not lose to bad indicators. They lose to fees, spread and
slippage, three small numbers applied several hundred times. A strategy showing
+0.4% per trade gross is a losing strategy on a venue charging 0.2% round trip
once slippage is counted, and no amount of parameter tuning fixes that.

So costs are modelled explicitly, pessimistically, and are applied identically
in the backtester and in paper mode. If the two disagree, the backtest is
fiction — and the same `CostModel` object is used by both precisely so they
cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from simin.core.types import Direction, OrderType, Side


@dataclass(frozen=True, slots=True)
class CostModel:
    """Fee, spread and slippage assumptions for one venue."""

    maker_fee: Decimal = Decimal("0.0002")
    taker_fee: Decimal = Decimal("0.0005")
    #: Half-spread paid on a market order, as a fraction of price.
    half_spread: Decimal = Decimal("0.0002")
    #: Extra adverse fill beyond the spread, as a fraction of price. Market
    #: orders walk the book; this is the part that walking costs.
    slippage: Decimal = Decimal("0.0003")
    #: Multiplier applied to slippage when a stop triggers. Stops fill during
    #: exactly the fast move that makes the book thin, and modelling a stop as
    #: filling at its trigger price is the most flattering lie a backtester can
    #: tell.
    stop_slippage_mult: Decimal = Decimal("3")
    #: 8-hourly perpetual funding.
    funding_rate: Decimal = Decimal("0.0001")

    @property
    def round_trip(self) -> Decimal:
        """Total fractional cost of entering and exiting with market orders."""
        return 2 * (self.taker_fee + self.half_spread + self.slippage)

    def fill_price(
        self,
        reference: Decimal,
        side: Side,
        order_type: OrderType = OrderType.MARKET,
        is_stop: bool = False,
    ) -> Decimal:
        """The price actually filled at, always worse than the reference.

        Limit orders are assumed to fill at their price with no slippage, which
        is optimistic in the other direction — but a limit that fills is a limit
        that got its price, and the pessimism belongs in whether it fills at
        all, which the caller decides.
        """
        if order_type is OrderType.LIMIT:
            return reference
        adverse = self.half_spread + self.slippage
        if is_stop:
            adverse = self.half_spread + self.slippage * self.stop_slippage_mult
        drift = reference * adverse
        return reference + drift if side is Side.BUY else reference - drift

    def fee(self, notional: Decimal, order_type: OrderType = OrderType.MARKET) -> Decimal:
        rate = self.maker_fee if order_type is OrderType.LIMIT else self.taker_fee
        return abs(notional) * rate

    def funding_cost(
        self, notional: Decimal, direction: Direction, hours: float, rate: Decimal | None = None
    ) -> Decimal:
        """Perpetual funding over a holding period. Positive = we paid.

        Longs pay shorts when the rate is positive, which it usually is in a
        bull market — a detail that quietly eats several percent a month from a
        leveraged long that is held rather than traded.
        """
        r = self.funding_rate if rate is None else rate
        periods = Decimal(str(hours / 8.0))
        cost = abs(notional) * r * periods
        return cost if direction is Direction.LONG else -cost

    def breakeven_move(self) -> Decimal:
        """How far price must move just to cover a round trip. The number every
        strategy's average win has to clear before anything is real."""
        return self.round_trip

    def stressed(self, factor: Decimal = Decimal("2")) -> CostModel:
        """A copy with costs multiplied. Every strategy is validated at 2x
        costs; one that only works at the quoted schedule is one bad week of
        liquidity away from not working."""
        return CostModel(
            maker_fee=self.maker_fee * factor,
            taker_fee=self.taker_fee * factor,
            half_spread=self.half_spread * factor,
            slippage=self.slippage * factor,
            stop_slippage_mult=self.stop_slippage_mult,
            funding_rate=self.funding_rate * factor,
        )


#: Published schedules, used as defaults. Real values are refreshed from the
#: venue at runtime where the API exposes them.
VENUE_COSTS: dict[str, CostModel] = {
    # CoinEx spot 0.2%/0.2%, futures 0.03%/0.05%. Futures assumed, since that
    # is what the leveraged dial levels use.
    "coinex": CostModel(
        maker_fee=Decimal("0.0003"),
        taker_fee=Decimal("0.0005"),
        half_spread=Decimal("0.0002"),
        slippage=Decimal("0.0003"),
    ),
    "coinex_spot": CostModel(
        maker_fee=Decimal("0.002"),
        taker_fee=Decimal("0.002"),
        half_spread=Decimal("0.0004"),
        slippage=Decimal("0.0005"),
    ),
    # Iranian venues: wider spreads, thinner books, higher fees. Toman pairs on
    # a quiet afternoon can cost far more than this to get out of.
    "nobitex": CostModel(
        maker_fee=Decimal("0.001"),
        taker_fee=Decimal("0.0013"),
        half_spread=Decimal("0.0015"),
        slippage=Decimal("0.0020"),
        stop_slippage_mult=Decimal("4"),
    ),
    "wallex": CostModel(
        maker_fee=Decimal("0.001"),
        taker_fee=Decimal("0.0015"),
        half_spread=Decimal("0.0018"),
        slippage=Decimal("0.0022"),
        stop_slippage_mult=Decimal("4"),
    ),
    "paper": CostModel(),
}


def cost_model(venue: str, kind: str = "futures") -> CostModel:
    key = f"{venue}_spot" if kind == "spot" and f"{venue}_spot" in VENUE_COSTS else venue
    return VENUE_COSTS.get(key, VENUE_COSTS["paper"])
