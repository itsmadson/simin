"""CoinEx v2 adapter — spot and perpetual futures.

CoinEx is the leveraged venue for this bot: it lists perpetuals up to 100x
(the dial caps at 10x), has a documented v2 REST API, and quotes in USDT so
performance is measured in a currency that is not itself moving.

Two venue details worth knowing, both handled here:

* **Everything is a string.** Prices, quantities and balances all come back as
  decimal strings. They are parsed straight into `Decimal` — routing them
  through `float` first, which is what most wrappers do, reintroduces exactly
  the rounding error the string encoding exists to prevent.

* **Klines include the forming candle.** The last element of a kline response is
  the current, still-changing bar. It is dropped unconditionally in `candles()`.
  Acting on it means deciding from a bar that has not happened yet.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from simin.core.types import (
    TF,
    Candle,
    Direction,
    MarketKind,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Symbol,
    ZERO,
)
from simin.exchanges.base import (
    Balance,
    DepthLevel,
    Exchange,
    ExchangeError,
    Fees,
    InsufficientFunds,
    OrderBook,
    RateLimited,
    RateLimiter,
    Ticker,
    normalise_symbol,
)

BASE_URL = "https://api.coinex.com"

#: CoinEx period strings, keyed by our timeframe.
_PERIOD = {
    TF.M1: "1min",
    TF.M3: "3min",
    TF.M5: "5min",
    TF.M15: "15min",
    TF.M30: "30min",
    TF.H1: "1hour",
    TF.H2: "2hour",
    TF.H4: "4hour",
    TF.H6: "6hour",
    TF.H12: "12hour",
    TF.D1: "1day",
}

_STATUS = {
    "open": OrderStatus.OPEN,
    "part_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "done": OrderStatus.FILLED,
    "part_canceled": OrderStatus.CANCELED,
    "canceled": OrderStatus.CANCELED,
}


def _dec(value: Any, default: str = "0") -> Decimal:
    """Parse a venue string into Decimal without passing through float."""
    if value is None or value == "":
        return Decimal(default)
    try:
        return Decimal(str(value))
    except InvalidOperation:
        # The venue sent something that is not a number at all. Returning the
        # default beats crashing a whole market listing over one odd field.
        return Decimal(default)


def _max_leverage(row: dict[str, Any]) -> int:
    """Highest leverage CoinEx allows on this market.

    `leverage` is a *list* of the permitted tiers — ["1","2","3","5",…,"100"] —
    not a single number. Reading it as a scalar throws `InvalidOperation` and
    takes down the entire symbol listing, which is how this was found: the first
    call against the live venue, not against any fixture.
    """
    raw = row.get("leverage") or row.get("max_leverage")
    if isinstance(raw, (list, tuple)):
        tiers = [int(_dec(x, "1")) for x in raw if str(x).strip()]
        return max(tiers) if tiers else 1
    return max(int(_dec(raw, "1")), 1)


class CoinExExchange(Exchange):
    name = "coinex"
    display_name = "CoinEx"
    can_trade = True
    kinds = (MarketKind.SPOT, MarketKind.FUTURES)
    quote_asset = "USDT"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = "",
        kind: MarketKind = MarketKind.FUTURES,
        timeout: float = 15.0,
    ) -> None:
        self._key = api_key
        self._secret = api_secret
        self._base = (base_url or BASE_URL).rstrip("/")
        self._kind = kind
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=timeout,
            headers={"Content-Type": "application/json", "User-Agent": "simin/1.0"},
        )
        # CoinEx allows far more, but a bot that hammers the endpoint gets
        # throttled at exactly the wrong moment. 8/s with a burst of 20 is
        # plenty for candle-by-candle trading.
        self._limiter = RateLimiter(requests_per_second=8, burst=20)
        self._symbol_cache: list[Symbol] = []

    @property
    def _market_path(self) -> str:
        return "futures" if self._kind is MarketKind.FUTURES else "spot"

    @property
    def authenticated(self) -> bool:
        return bool(self._key and self._secret)

    # --- Transport --------------------------------------------------------

    def _sign(self, method: str, path: str, body: str, timestamp: str) -> str:
        """CoinEx v2: HMAC-SHA256 over METHOD + path + body + timestamp."""
        payload = f"{method}{path}{body}{timestamp}"
        return hmac.new(
            self._secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest().lower()

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        signed: bool = False,
        retries: int = 3,
    ) -> Any:
        await self._limiter.acquire()
        if signed and not self.authenticated:
            raise ExchangeError(f"{path} requires API credentials; none configured")

        query = ""
        if params:
            query = "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v is not None)
        full_path = path + query
        payload = json.dumps(body, separators=(",", ":")) if body else ""
        headers: dict[str, str] = {}
        if signed:
            ts = str(int(time.time() * 1000))
            headers = {
                "X-COINEX-KEY": self._key,
                "X-COINEX-SIGN": self._sign(method.upper(), full_path, payload, ts),
                "X-COINEX-TIMESTAMP": ts,
            }

        last: Exception | None = None
        for attempt in range(retries):
            try:
                resp = await self._client.request(
                    method, full_path, content=payload or None, headers=headers
                )
            except httpx.HTTPError as exc:
                last = exc
                # Network faults are retried; a rejected order is not, and the
                # difference is handled below by not retrying ExchangeError.
                await _backoff(attempt)
                continue

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 1 + attempt))
                if attempt == retries - 1:
                    raise RateLimited("CoinEx rate limit", retry_after)
                await _backoff(attempt, retry_after)
                continue
            if resp.status_code >= 500:
                last = ExchangeError(f"CoinEx {resp.status_code}: {resp.text[:200]}")
                await _backoff(attempt)
                continue
            if resp.status_code >= 400:
                raise ExchangeError(f"CoinEx HTTP {resp.status_code}: {resp.text[:300]}")

            data = resp.json()
            code = data.get("code", 0)
            if code == 0:
                return data.get("data")
            message = data.get("message", "")
            if code in (3109, 3127) or "balance" in message.lower():
                raise InsufficientFunds(f"CoinEx {code}: {message}")
            if code in (4001, 4213):
                if attempt == retries - 1:
                    raise RateLimited(f"CoinEx {code}: {message}")
                await _backoff(attempt)
                continue
            raise ExchangeError(f"CoinEx error {code}: {message}")

        raise ExchangeError(f"CoinEx {path} failed after {retries} attempts: {last}")

    # --- Market data ------------------------------------------------------

    async def symbols(self) -> Sequence[Symbol]:
        if self._symbol_cache:
            return self._symbol_cache
        data = await self._request("GET", f"/v2/{self._market_path}/market")
        out: list[Symbol] = []
        for m in data or []:
            name = normalise_symbol(m.get("market", ""))
            base = m.get("base_ccy") or m.get("base_currency") or ""
            quote = m.get("quote_ccy") or m.get("quote_currency") or "USDT"
            if not base:
                continue
            # Skip anything not actually tradeable. A delisted market still
            # appears in the listing and will accept a symbol lookup, then
            # reject every order against it.
            if m.get("status") not in (None, "online"):
                continue
            if m.get("is_market_available") is False:
                continue
            out.append(
                Symbol(
                    base=base.upper(),
                    quote=quote.upper(),
                    venue=self.name,
                    venue_symbol=m.get("market", name),
                    kind=self._kind,
                    price_precision=int(m.get("quote_ccy_precision", 2) or 2),
                    qty_precision=int(m.get("base_ccy_precision", 6) or 6),
                    min_qty=_dec(m.get("min_amount"), "0"),
                    min_notional=_dec(m.get("min_notional"), "0"),
                    max_leverage=_max_leverage(m),
                )
            )
        self._symbol_cache = out
        return out

    #: Hard cap the venue enforces on a single kline request.
    PAGE = 1000

    async def candles(
        self, symbol: str, tf: TF, limit: int = 500, end: datetime | None = None
    ) -> list[Candle]:
        """Closed candles, oldest first, paginating when more than a page is asked for.

        The venue returns at most 1000 bars per call — 83 days on a 2h chart,
        which is not enough to walk-forward anything. Asking for more pages
        backwards through `end_time` until the request is satisfied or the
        listing runs out. Without this, every symbol silently caps at 999 bars
        and the screener quietly has nothing to test.
        """
        period = _PERIOD.get(tf)
        if period is None:
            raise ExchangeError(f"CoinEx does not offer {tf.value} candles")

        collected: dict[datetime, Candle] = {}
        cursor_ms = int(end.timestamp() * 1000) if end else None
        # Bound the loop independently of the data: a venue that keeps returning
        # the same page must not spin forever.
        for _ in range(max(1, -(-limit // self.PAGE)) + 2):
            params: dict[str, Any] = {
                "market": symbol,
                "period": period,
                "limit": self.PAGE,
            }
            if cursor_ms is not None:
                params["end_time"] = cursor_ms
                params["start_time"] = cursor_ms - self.PAGE * tf.seconds * 1000
            page = await self._parse_klines(
                await self._request("GET", f"/v2/{self._market_path}/kline", params=params),
                symbol,
            )
            if not page:
                break
            fresh = [c for c in page if c.ts not in collected]
            for c in page:
                collected[c.ts] = c
            if len(collected) >= limit + 1 or not fresh:
                break
            cursor_ms = int(min(collected).timestamp() * 1000) - 1

        candles = sorted(collected.values(), key=lambda c: c.ts)
        # Drop the still-forming bar. Never negotiable.
        now = datetime.now(UTC)
        while candles and not tf.is_closed(candles[-1].ts, now):
            candles.pop()
        return candles[-limit:]

    @staticmethod
    async def _parse_klines(data: Any, symbol: str) -> list[Candle]:
        candles: list[Candle] = []
        for k in data or []:
            try:
                candles.append(
                    Candle(
                        ts=datetime.fromtimestamp(int(k["created_at"]) / 1000, UTC),
                        open=_dec(k["open"]),
                        high=_dec(k["high"]),
                        low=_dec(k["low"]),
                        close=_dec(k["close"]),
                        volume=_dec(k.get("volume", "0")),
                    )
                )
            except (KeyError, ValueError) as exc:
                raise ExchangeError(f"malformed CoinEx kline for {symbol}: {exc}") from exc

        candles.sort(key=lambda c: c.ts)
        return candles

    async def ticker(self, symbol: str) -> Ticker:
        data = await self._request(
            "GET", f"/v2/{self._market_path}/ticker", params={"market": symbol}
        )
        rows = data or []
        if not rows:
            raise ExchangeError(f"CoinEx returned no ticker for {symbol}")
        t = rows[0]
        last = _dec(t.get("last"))
        bid = _dec(t.get("best_bid") or t.get("bid"), str(last))
        ask = _dec(t.get("best_ask") or t.get("ask"), str(last))
        if bid <= 0 or ask <= 0:
            bid = ask = last
        return Ticker(symbol, bid, ask, last, datetime.now(UTC))

    async def order_book(self, symbol: str, limit: int = 50) -> OrderBook | None:
        data = await self._request(
            "GET",
            f"/v2/{self._market_path}/depth",
            # interval=0 asks for un-aggregated levels. Aggregated depth hides
            # the gaps that make a book expensive to cross.
            params={"market": symbol, "limit": min(limit, 50), "interval": "0"},
        )
        book = (data or {}).get("depth") or data or {}
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return None
        return OrderBook(
            symbol=symbol,
            bids=tuple(DepthLevel(_dec(p), _dec(q)) for p, q in bids),
            asks=tuple(DepthLevel(_dec(p), _dec(q)) for p, q in asks),
            ts=datetime.now(UTC),
        )

    async def tickers(self) -> list[dict[str, Any]]:
        """Every market's 24h stats in one call.

        Scanning 200 markets one ticker at a time is 200 round trips and a rate
        limit; the venue offers them in a single response, so use it.
        """
        return list(await self._request("GET", f"/v2/{self._market_path}/ticker") or [])

    async def fees(self, symbol: str) -> Fees:
        from simin.exchanges.costs import cost_model

        c = cost_model(
            self.name, "futures" if self._kind is MarketKind.FUTURES else "spot"
        )
        return Fees(c.maker_fee, c.taker_fee, c.funding_rate)

    # --- Account ----------------------------------------------------------

    async def balances(self) -> dict[str, Balance]:
        path = (
            "/v2/assets/futures/balance"
            if self._kind is MarketKind.FUTURES
            else "/v2/assets/spot/balance"
        )
        data = await self._request("GET", path, signed=True)
        out: dict[str, Balance] = {}
        for row in data or []:
            asset = (row.get("ccy") or row.get("currency") or "").upper()
            if not asset:
                continue
            out[asset] = Balance(
                asset,
                _dec(row.get("available")),
                _dec(row.get("frozen") or row.get("margin")),
            )
        return out

    async def positions(self) -> Sequence[Position]:
        if self._kind is not MarketKind.FUTURES:
            return ()
        data = await self._request("GET", "/v2/futures/pending-position", signed=True)
        out: list[Position] = []
        for row in data or []:
            qty = _dec(row.get("close_avbl") or row.get("amount"))
            if qty <= 0:
                continue
            side = (row.get("side") or "").lower()
            out.append(
                Position(
                    symbol=normalise_symbol(row.get("market", "")),
                    direction=Direction.LONG if side == "long" else Direction.SHORT,
                    qty=qty,
                    entry_price=_dec(row.get("avg_entry_price")),
                    stop_price=_dec(row.get("stop_loss_price")),
                    take_profit=None,
                    leverage=_dec(row.get("leverage"), "1"),
                    opened_at=datetime.fromtimestamp(
                        int(row.get("created_at", 0)) / 1000, UTC
                    ),
                    strategy="venue",
                    risk_level=0,
                )
            )
        return out

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        if self._kind is not MarketKind.FUTURES:
            await super().set_leverage(symbol, leverage)
            return
        await self._request(
            "POST",
            "/v2/futures/adjust-position-leverage",
            body={
                "market": symbol,
                "market_type": "FUTURES",
                # Isolated margin: a loss on one position cannot consume the
                # margin backing another. Cross margin lets one bad trade take
                # the whole account, which no risk dial setting should permit.
                "margin_mode": "isolated",
                "leverage": int(leverage),
            },
            signed=True,
        )

    # --- Trading ----------------------------------------------------------

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

        body: dict[str, Any] = {
            "market": symbol,
            "market_type": "FUTURES" if self._kind is MarketKind.FUTURES else "SPOT",
            "side": "buy" if side is Side.BUY else "sell",
            "amount": str(qty),
        }
        if client_id:
            body["client_id"] = client_id
        if reduce_only:
            body["is_reduce_only"] = True

        if type is OrderType.MARKET:
            body["type"] = "market"
            path = f"/v2/{self._market_path}/order"
        elif type is OrderType.LIMIT:
            if price is None:
                raise ValueError("limit order requires a price")
            body["type"] = "limit"
            body["price"] = str(price)
            path = f"/v2/{self._market_path}/order"
        elif type in (OrderType.STOP_MARKET, OrderType.TAKE_PROFIT_MARKET):
            if stop_price is None:
                raise ValueError(f"{type} requires a stop price")
            body["type"] = "market"
            body["trigger_price"] = str(stop_price)
            path = f"/v2/{self._market_path}/stop-order"
        else:
            raise ValueError(f"unsupported order type {type}")

        data = await self._request("POST", path, body=body, signed=True)
        return self._parse_order(data, symbol, side, type, qty, price, stop_price, client_id)

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            await self._request(
                "POST",
                f"/v2/{self._market_path}/cancel-order",
                body={
                    "market": symbol,
                    "market_type": "FUTURES"
                    if self._kind is MarketKind.FUTURES
                    else "SPOT",
                    "order_id": int(order_id),
                },
                signed=True,
            )
            return True
        except ExchangeError:
            # An order that filled between the decision and the cancel is
            # already gone; that is not a failure worth propagating.
            return False

    async def get_order(self, symbol: str, order_id: str) -> Order:
        data = await self._request(
            "GET",
            f"/v2/{self._market_path}/order-status",
            params={"market": symbol, "order_id": order_id},
            signed=True,
        )
        side = Side.BUY if (data or {}).get("side") == "buy" else Side.SELL
        return self._parse_order(
            data, symbol, side, OrderType.LIMIT, _dec((data or {}).get("amount")), None, None, ""
        )

    @staticmethod
    def _parse_order(
        data: dict[str, Any] | None,
        symbol: str,
        side: Side,
        type: OrderType,
        qty: Decimal,
        price: Decimal | None,
        stop_price: Decimal | None,
        client_id: str,
    ) -> Order:
        d = data or {}
        venue_id = str(d.get("order_id") or d.get("stop_id") or "")
        filled = _dec(d.get("filled_amount"))
        status = _STATUS.get(str(d.get("status", "")).lower())
        if status is None:
            status = OrderStatus.FILLED if filled >= qty > 0 else OrderStatus.OPEN
        return Order(
            id=venue_id or client_id or "unknown",
            symbol=symbol,
            side=side,
            type=type,
            qty=qty,
            price=price,
            stop_price=stop_price,
            client_id=client_id,
            status=status,
            filled_qty=filled,
            avg_price=_dec(d.get("filled_value")) / filled if filled > 0 else ZERO,
            fee=_dec(d.get("quote_fee")) + _dec(d.get("base_fee")),
            venue_order_id=venue_id,
        )

    async def close(self) -> None:
        await self._client.aclose()


async def _backoff(attempt: int, base: float = 0.5) -> None:
    import asyncio

    await asyncio.sleep(min(base * (2**attempt), 8.0))
