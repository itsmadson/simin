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
        #: Simulated margin book: symbol -> (signed qty, average entry, margin
        #: posted). Without it the ledger cannot tell an opening order from a
        #: closing one, and treats a short's proceeds as spendable cash while
        #: never releasing the margin behind a closed position — so the balance
        #: drifts away from reality and starts refusing valid entries.
        self._book: dict[str, tuple[Decimal, Decimal, Decimal]] = {}

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

    async def order_book(self, symbol: str, limit: int = 50):
        """Depth comes from the real venue. A simulated account over real prices
        must measure real depth, or the universe scanner silently concludes that
        every market is untradeable — which is what happened the first time this
        ran behind a paper wrapper."""
        if self._source is None:
            return None
        return await self._source.order_book(symbol, limit)

    async def tickers(self) -> list[dict[str, object]]:
        """Bulk 24h stats, delegated. Market data is market data; only the
        *account* is simulated here."""
        fetch = getattr(self._source, "tickers", None)
        if fetch is None:
            return []
        return list(await fetch())

    async def fees(self, symbol: str) -> Fees:
        return Fees(self._costs.maker_fee, self._costs.taker_fee, self._costs.funding_rate)

    def set_mark(self, symbol: str, price: Decimal) -> None:
        """Used by the live runner to keep the simulated book in step."""
        self._marks[symbol] = price

    @property
    def source_name(self) -> str:
        """Where the prices come from.

        Reporting only "paper" hides the thing the user actually chose. A paper
        account over live CoinEx prices and a paper account over generated data
        are very different situations, and the status line has to distinguish
        them or "lab mode" means nothing in particular.
        """
        if self._source is None:
            return self.name
        return f"{self._source.name} (paper)"

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

    def _apply_to_book(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal, leverage: Decimal
    ) -> tuple[Decimal, bool]:
        """Update the simulated position and return `(cash delta, is closing)`.

        A margin account, not a spot one. Opening posts margin; closing releases
        the margin that was posted and realises the profit or loss against the
        average entry. Modelling it as spot — where selling simply credits the
        proceeds — makes a short look like income and leaves the margin behind a
        closed position locked forever.
        """
        signed = qty * side.sign
        held, avg, margin = self._book.get(symbol, (ZERO, ZERO, ZERO))
        lev = leverage if leverage > 0 else Decimal(1)

        # Same direction, or opening from flat: post margin.
        if held == 0 or (held > 0) == (signed > 0):
            new_qty = held + signed
            total = abs(held) * avg + abs(signed) * price
            new_avg = total / abs(new_qty) if new_qty else ZERO
            posted = abs(signed) * price / lev
            self._book[symbol] = (new_qty, new_avg, margin + posted)
            return -posted, False

        # Opposite direction: reduce, and possibly flip.
        closed = min(abs(signed), abs(held))
        direction = Decimal(1) if held > 0 else Decimal(-1)
        realised = (price - avg) * closed * direction
        released = margin * (closed / abs(held))
        remaining = held + signed
        cash = released + realised

        if remaining == 0:
            self._book.pop(symbol, None)
        elif (remaining > 0) == (held > 0):
            self._book[symbol] = (remaining, avg, margin - released)
        else:
            # Flipped through flat: the excess opens a new position the other way.
            opened = abs(remaining) * price / lev
            self._book[symbol] = (remaining, price, opened)
            cash -= opened
        return cash, True

    def position_book(self) -> dict[str, tuple[Decimal, Decimal, Decimal]]:
        """(signed qty, average entry, posted margin) per symbol. For tests."""
        return dict(self._book)

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
        cash_delta, closing = self._apply_to_book(symbol, side, qty, fill_price, leverage)
        self._credit(self.quote_asset, cash_delta - fee, closing=closing or reduce_only)

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
