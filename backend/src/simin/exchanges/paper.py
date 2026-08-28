"""Paper trading: real market data, simulated money.

This is what LAB mode runs against live prices. It wraps a real read-only data
source so the prices and spreads are genuine, and simulates only the account.

`can_trade` is False and stays False. That is the property that makes it
impossible for a misconfiguration to turn a paper session into a live one: the
trader refuses REAL mode against any adapter that reports False, and this class
has no code path that sets it True.

Fills use the same `CostModel` the backtester uses. If paper and backtest
disagree on what a trade cost, one of them is lying, and the only way to be
sure they agree is to share the object.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from simin.core.types import (
    TF,
    Candle,
    Fill,
    MarketKind,
    Order,
    OrderStatus,
    OrderType,
    Side,
    Symbol,
    ZERO,
)
from simin.exchanges.base import Balance, Exchange, Fees, InsufficientFunds, Ticker
from simin.exchanges.costs import CostModel, cost_model


class PaperExchange(Exchange):
    """Simulated account over a real (or replayed) data source."""

    name = "paper"
    display_name = "Paper (simulated)"
    can_trade = False
    kinds = (MarketKind.SPOT, MarketKind.FUTURES)

    def __init__(
        self,
        data_source: Exchange | None = None,
        starting_balance: Decimal = Decimal("10000"),
        quote: str = "USDT",
        costs: CostModel | None = None,
        symbols: Sequence[Symbol] | None = None,
    ) -> None:
        self._source = data_source
        self.quote_asset = quote
        self._costs = costs or cost_model("paper")
        self._balances: dict[str, Balance] = {
            quote: Balance(quote, starting_balance, ZERO)
        }
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._ids = itertools.count(1)
        self._symbols: list[Symbol] = list(symbols or ())
        self._leverage: dict[str, Decimal] = {}
        #: Last seen price per symbol, used when no data source is attached.
        self._marks: dict[str, Decimal] = {}

    # --- Market data: delegated to the real source ------------------------

    async def symbols(self) -> Sequence[Symbol]:
        if self._source is not None:
            return await self._source.symbols()
        return self._symbols

    async def candles(
        self, symbol: str, tf: TF, limit: int = 500, end: datetime | None = None
    ) -> list[Candle]:
        if self._source is None:
            raise RuntimeError(
                "PaperExchange has no data source; attach one or use the backtester"
            )
        candles = await self._source.candles(symbol, tf, limit, end)
        if candles:
            self._marks[symbol] = candles[-1].close
        return candles

    async def ticker(self, symbol: str) -> Ticker:
        if self._source is not None:
            t = await self._source.ticker(symbol)
            self._marks[symbol] = t.last
            return t
        mark = self._marks.get(symbol)
        if mark is None:
            raise RuntimeError(f"no mark price known for {symbol}")
        spread = mark * self._costs.half_spread
        return Ticker(symbol, mark - spread, mark + spread, mark, datetime.now(UTC))

    async def fees(self, symbol: str) -> Fees:
        return Fees(self._costs.maker_fee, self._costs.taker_fee, self._costs.funding_rate)

    def set_mark(self, symbol: str, price: Decimal) -> None:
        """Used by the live runner to keep the simulated book in step."""
        self._marks[symbol] = price

    # --- Account ----------------------------------------------------------

    async def balances(self) -> dict[str, Balance]:
        return dict(self._balances)

    def _credit(self, asset: str, amount: Decimal, closing: bool = False) -> None:
        """Move simulated cash.

        `closing` is the important flag. An order that *reduces* exposure must
        never be refused for lack of funds: refusing it leaves the position open,
        so the loss it was going to realise keeps growing instead — the exact
        opposite of what a funds check is for. A losing short is the clearest
        case, since buying it back costs more than the sale brought in, and a
        naive balance check would trap the position forever while the runner
        retried the close on every poll.

        Opening orders are still checked, because there a refusal is correct.
        """
        current = self._balances.get(asset, Balance(asset, ZERO, ZERO))
        new_free = current.free + amount
        if new_free < 0 and not closing:
            raise InsufficientFunds(
                f"paper account cannot open this position: {current.free} in {asset} "
                f"is not enough for {-amount}"
            )
        self._balances[asset] = Balance(asset, new_free, current.locked)

    # --- Trading ----------------------------------------------------------

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        self._leverage[symbol] = leverage

    async def place_order(
        self,
        symbol: str,
        side: Side,
        type: OrderType,
        qty: Decimal,
        price: Decimal | None = None,
        stop_price: Decimal | None = None,
        reduce_only: bool = False,
        client_id: str = "",
    ) -> Order:
        if qty <= 0:
            raise ValueError(f"order quantity must be positive, got {qty}")

        oid = f"paper-{next(self._ids)}"
        now = datetime.now(UTC)

        if type is OrderType.LIMIT:
            if price is None:
                raise ValueError("limit order requires a price")
            # Resting orders are not filled here; the runner marks them when
            # price trades through. Pretending a limit fills immediately at its
            # price is the same optimism that makes bad backtests.
            order = Order(
                id=oid, symbol=symbol, side=side, type=type, qty=qty, price=price,
                stop_price=stop_price, reduce_only=reduce_only, client_id=client_id,
                status=OrderStatus.OPEN, created_at=now,
            )
            self._orders[oid] = order
            return order

        ticker = await self.ticker(symbol)
        reference = ticker.ask if side is Side.BUY else ticker.bid
        fill_price = self._costs.fill_price(
            reference, side, type, is_stop=type is OrderType.STOP_MARKET
        )
        notional = qty * fill_price
        fee = self._costs.fee(notional, type)

        leverage = self._leverage.get(symbol, Decimal(1))
        margin = notional / leverage
        cash_delta = -(margin + fee) if side is Side.BUY else (margin - fee)
        # Spot semantics for a simple simulated account: buying consumes quote,
        # selling returns it. Futures margin is handled by the position manager
        # above this layer, which is why leverage only scales the cash held.
        #
        # This ledger is for display; the runner's AccountState is the authority
        # on equity and PnL. Keeping them separate is why a reduce-only order
        # must not be blocked by this balance — the two views disagreeing is
        # expected, and this one is not the one that decides anything.
        self._credit(self.quote_asset, cash_delta, closing=reduce_only)

        order = Order(
            id=oid, symbol=symbol, side=side, type=type, qty=qty, price=price,
            stop_price=stop_price, reduce_only=reduce_only, client_id=client_id,
            status=OrderStatus.FILLED, filled_qty=qty, avg_price=fill_price, fee=fee,
            created_at=now, venue_order_id=oid,
        )
        self._orders[oid] = order
        self._fills.append(Fill(oid, symbol, side, qty, fill_price, fee, now))
        return order

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        order = self._orders.get(order_id)
        if order is None or order.status.is_terminal:
            return False
        self._orders[order_id] = replace(order, status=OrderStatus.CANCELED)
        return True

    async def get_order(self, symbol: str, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise KeyError(f"unknown paper order {order_id}")
        return order

    @property
    def fills(self) -> list[Fill]:
        return list(self._fills)

    async def close(self) -> None:
        if self._source is not None:
            await self._source.close()
