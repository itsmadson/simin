"""Paper trading adapter: real market data, simulated money.

Fills are simulated against the *live* order book using the same cost model the
backtester uses, so paper results are directly comparable to backtest results.
That comparability is the point: divergence between the two says the simulator is
wrong, and learning it here is far cheaper than learning it with funded capital.

Balances live in memory and are mirrored to the database by the caller. This
adapter never contacts a venue for trading and holds no credentials.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from simin.backtest.costs import CostModel
from simin.exchanges.base import ExchangeAdapter, OrderRejected
from simin.types import (
    TF,
    Bar,
    Balance,
    FeeSchedule,
    Fill,
    Order,
    OrderBook,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    SymbolInfo,
    Ticker,
    VenueHealth,
)


@dataclass(slots=True)
class PaperAdapter(ExchangeAdapter):
    """Wraps a read-only data adapter and simulates the trading surface."""

    data: ExchangeAdapter
    cost: CostModel
    quote_asset: str = "IRT"
    starting_balance: Decimal = Decimal("100000000")
    venue: str = "paper"
    supports_trading: bool = True
    _balances: dict[str, Decimal] = field(default_factory=dict)
    _orders: dict[str, Order] = field(default_factory=dict)
    _client_index: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._balances.setdefault(self.quote_asset, self.starting_balance)

    # ---------------------------------------------------------------- data

    async def get_symbols(self) -> list[SymbolInfo]:
        return await self.data.get_symbols()

    async def get_ticker(self, symbol: str) -> Ticker:
        return await self.data.get_ticker(symbol)

    async def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBook:
        return await self.data.get_orderbook(symbol, depth)

    async def get_ohlcv(self, symbol: str, tf: TF, since: datetime, limit: int = 1000) -> list[Bar]:
        return await self.data.get_ohlcv(symbol, tf, since, limit)

    def fee_schedule(self, symbol: str | None = None) -> FeeSchedule:
        return self.cost.fees

    async def health(self) -> VenueHealth:
        upstream = await self.data.health()
        return VenueHealth(
            venue=self.venue,
            ok=upstream.ok,
            latency_p95_ms=upstream.latency_p95_ms,
            error_rate=upstream.error_rate,
            clock_skew_ms=upstream.clock_skew_ms,
            checked_at=datetime.now(UTC),
        )

    # ------------------------------------------------------------- trading

    async def get_balance(self) -> list[Balance]:
        return [
            Balance(asset=asset, free=amount, locked=Decimal(0))
            for asset, amount in sorted(self._balances.items())
        ]

    async def create_order(self, req: OrderRequest) -> Order:
        """Simulate a fill against the current book.

        Idempotent on ``client_order_id``: a retry after a timeout returns the
        original order instead of opening a second position. Every live adapter
        must behave this way, so the paper adapter models it too — otherwise the
        first real outage teaches the lesson.
        """
        existing = self._client_index.get(req.client_order_id)
        if existing is not None:
            return self._orders[existing]

        if req.type not in (OrderType.MARKET, OrderType.LIMIT):
            raise OrderRejected(f"paper adapter does not simulate {req.type} orders yet")

        book = await self.get_orderbook(req.symbol, depth=20)
        filled_qty, sweep_price = book.sweep(req.side, req.qty)
        if filled_qty <= 0:
            return self._record(req, OrderStatus.REJECTED, reason="no liquidity")

        if req.type is OrderType.LIMIT and req.price is not None:
            crosses = sweep_price <= req.price if req.side is Side.BUY else sweep_price >= req.price
            if not crosses:
                # A limit that does not cross rests. Assuming otherwise is the
                # single most flattering simplification a paper engine can make.
                return self._record(req, OrderStatus.NEW)

        depth = book.depth_notional(req.side, 5)
        price = self.cost.fill_price(sweep_price, req.side, filled_qty, depth)
        notional = price * filled_qty
        fee = self.cost.fee(notional, is_maker=False)
        base = self._base_asset(req.symbol)

        if req.side is Side.BUY:
            need = notional + fee
            if self._balances.get(self.quote_asset, Decimal(0)) < need:
                return self._record(req, OrderStatus.REJECTED, reason="insufficient balance")
            self._balances[self.quote_asset] -= need
            self._balances[base] = self._balances.get(base, Decimal(0)) + filled_qty
        else:
            if self._balances.get(base, Decimal(0)) < filled_qty:
                return self._record(req, OrderStatus.REJECTED, reason="insufficient position")
            self._balances[base] -= filled_qty
            self._balances[self.quote_asset] = (
                self._balances.get(self.quote_asset, Decimal(0)) + notional - fee
            )

        status = OrderStatus.FILLED if filled_qty == req.qty else OrderStatus.PARTIALLY_FILLED
        order = self._record(req, status, filled_qty=filled_qty, price=price, fee=fee)
        return order

    async def cancel_order(self, order_id: str, symbol: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise OrderRejected(f"unknown order {order_id}")
        if not order.is_open:
            raise OrderRejected(f"order {order_id} is {order.status}")
        order.status = OrderStatus.CANCELED
        return order

    async def get_order(self, order_id: str, symbol: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise OrderRejected(f"unknown order {order_id}")
        return order

    # ------------------------------------------------------------- helpers

    def equity(self, marks: dict[str, Decimal]) -> Decimal:
        """Quote-denominated equity given current marks for each base asset."""
        total = self._balances.get(self.quote_asset, Decimal(0))
        for asset, amount in self._balances.items():
            if asset == self.quote_asset or amount == 0:
                continue
            total += amount * marks.get(asset, Decimal(0))
        return total

    def position_qty(self, symbol: str) -> Decimal:
        return self._balances.get(self._base_asset(symbol), Decimal(0))

    def _base_asset(self, symbol: str) -> str:
        if symbol.endswith(self.quote_asset):
            return symbol[: -len(self.quote_asset)]
        return symbol

    def _record(
        self,
        req: OrderRequest,
        status: OrderStatus,
        *,
        filled_qty: Decimal = Decimal(0),
        price: Decimal | None = None,
        fee: Decimal = Decimal(0),
        reason: str | None = None,
    ) -> Order:
        order_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        order = Order(
            client_order_id=req.client_order_id,
            symbol=req.symbol,
            side=req.side,
            type=req.type,
            qty=req.qty,
            status=status,
            exchange_order_id=order_id,
            price=price if price is not None else req.price,
            filled_qty=filled_qty,
            avg_price=price,
            fills=(
                [Fill(ts=now, price=price, qty=filled_qty, fee=fee, fee_asset=self.quote_asset,
                      is_maker=False)]
                if price is not None and filled_qty > 0
                else []
            ),
            created_at=now,
            reject_reason=reason,
        )
        self._orders[order_id] = order
        self._client_index[req.client_order_id] = order_id
        return order
