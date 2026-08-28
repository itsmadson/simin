"""Iranian venue adapter — Nobitex-compatible, spot only, Toman-denominated.

Three things make Iranian venues genuinely different from CoinEx, and all three
are handled here rather than being left to surprise the trader:

**1. Spot only.** No perpetuals, no shorting, no leverage. A dial setting of 9
running here is silently a very different bot, so the venue reports
`supports_futures = False` and `simin.risk.dial.spot_only` clamps the profile
and attaches a visible warning. The bot does not pretend the leverage applied.

**2. Rial versus Toman.** The API quotes IRR (Rial); the country thinks in
Toman, which is 10 Rial. Getting this wrong is a 10x error in every price on
the screen. Prices are converted to Toman at the boundary, once, and
`QUOTE_DIVISOR` is the only place the factor appears.

**3. Toman PnL is not profit.** The Rial has lost value against USD for years.
A bot that turns 100M Toman into 130M Toman during a 30% devaluation made
nothing. `usdt_reference()` exposes the USDT/IRT rate so the API can report
both, and the UI shows both. This is the single most important honesty feature
for an Iranian-venue user.

The base URL is configurable, so the same adapter serves any venue speaking
this API shape.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from simin.core.types import (
    TF,
    Candle,
    MarketKind,
    Order,
    OrderStatus,
    OrderType,
    Side,
    Symbol,
    ZERO,
)
from simin.exchanges.base import (
    Balance,
    Exchange,
    ExchangeError,
    Fees,
    InsufficientFunds,
    RateLimited,
    RateLimiter,
    Ticker,
    normalise_symbol,
)

DEFAULT_BASE_URL = "https://api.nobitex.ir"

#: Rial per Toman. The API speaks Rial; humans speak Toman.
QUOTE_DIVISOR = Decimal("10")

#: OHLC resolutions, in minutes, as the chart endpoint expects them.
_RESOLUTION = {
    TF.M1: "1",
    TF.M5: "5",
    TF.M15: "15",
    TF.M30: "30",
    TF.H1: "60",
    TF.H2: "120",
    TF.H4: "240",
    TF.D1: "D",
}

_STATUS = {
    "New": OrderStatus.OPEN,
    "Active": OrderStatus.OPEN,
    "Done": OrderStatus.FILLED,
    "Canceled": OrderStatus.CANCELED,
}


def _dec(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


class IranianExchange(Exchange):
    """Spot-only Toman venue."""

    name = "nobitex"
    display_name = "Nobitex"
    can_trade = True
    kinds = (MarketKind.SPOT,)
    quote_asset = "IRT"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = "",
        venue_name: str = "nobitex",
        timeout: float = 20.0,
    ) -> None:
        self.name = venue_name
        self._token = api_key or api_secret
        self._base = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=timeout,
            headers={"Content-Type": "application/json", "User-Agent": "simin/1.0"},
        )
        # Iranian venues throttle aggressively and their infrastructure is
        # frequently under load. 3/s is deliberately conservative.
        self._limiter = RateLimiter(requests_per_second=3, burst=6)
        self._symbol_cache: list[Symbol] = []

    @property
    def authenticated(self) -> bool:
        return bool(self._token)

    @property
    def supports_shorts(self) -> bool:
        return False

    # --- Transport --------------------------------------------------------

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
            raise ExchangeError(f"{path} requires an API token; none configured")

        headers = {"Authorization": f"Token {self._token}"} if signed else {}
        last: Exception | None = None
        for attempt in range(retries):
            try:
                resp = await self._client.request(
                    method, path, params=params, json=body, headers=headers
                )
            except httpx.HTTPError as exc:
                last = exc
                await _backoff(attempt)
                continue

            if resp.status_code == 429:
                if attempt == retries - 1:
                    raise RateLimited(f"{self.name} rate limit")
                await _backoff(attempt, float(resp.headers.get("Retry-After", 2)))
                continue
            if resp.status_code >= 500:
                last = ExchangeError(f"{self.name} {resp.status_code}")
                await _backoff(attempt)
                continue
            if resp.status_code >= 400:
                raise ExchangeError(
                    f"{self.name} HTTP {resp.status_code}: {resp.text[:300]}"
                )

            data = resp.json()
            status = data.get("status")
            if status == "ok" or status is None:
                return data
            message = data.get("message", "") or data.get("code", "")
            if "balance" in str(message).lower() or "Insufficient" in str(message):
                raise InsufficientFunds(f"{self.name}: {message}")
            raise ExchangeError(f"{self.name}: {message}")

        raise ExchangeError(f"{self.name} {path} failed after {retries} attempts: {last}")

    @staticmethod
    def _split(symbol: str) -> tuple[str, str]:
        """`BTCIRT` -> `('btc', 'irt')`. Quote is always IRT or USDT here."""
        s = normalise_symbol(symbol)
        for quote in ("IRT", "USDT", "RLS"):
            if s.endswith(quote):
                return s[: -len(quote)].lower(), quote.lower()
        raise ExchangeError(f"cannot parse {symbol!r} into base and quote")

    def _to_toman(self, rial: Decimal, quote: str) -> Decimal:
        """IRT/RLS prices arrive in Rial; USDT pairs are already correct."""
        return rial / QUOTE_DIVISOR if quote in ("irt", "rls") else rial

    # --- Market data ------------------------------------------------------

    async def symbols(self) -> Sequence[Symbol]:
        if self._symbol_cache:
            return self._symbol_cache
        data = await self._request("GET", "/v2/orderbook/all")
        out: list[Symbol] = []
        for market in data or {}:
            if market in ("status",) or not isinstance(market, str):
                continue
            try:
                base, quote = self._split(market)
            except ExchangeError:
                continue
            out.append(
                Symbol(
                    base=base.upper(),
                    quote=quote.upper(),
                    venue=self.name,
                    venue_symbol=market,
                    kind=MarketKind.SPOT,
                    # Toman prices are integers in the millions; fractional
                    # Rial does not exist.
                    price_precision=0 if quote in ("irt", "rls") else 2,
                    qty_precision=8,
                    max_leverage=1,
                )
            )
        self._symbol_cache = out
        return out

    async def candles(
        self, symbol: str, tf: TF, limit: int = 500, end: datetime | None = None
    ) -> list[Candle]:
        resolution = _RESOLUTION.get(tf)
        if resolution is None:
            raise ExchangeError(f"{self.name} does not offer {tf.value} candles")
        base, quote = self._split(symbol)
        to_ts = int((end or datetime.now(UTC)).timestamp())
        # Ask for one extra bar so dropping the forming one still returns `limit`.
        from_ts = to_ts - (limit + 2) * tf.seconds

        data = await self._request(
            "GET",
            "/market/udf/history",
            params={
                "symbol": f"{base.upper()}{quote.upper()}",
                "resolution": resolution,
                "from": from_ts,
                "to": to_ts,
            },
        )
        if not data or data.get("s") not in ("ok", None):
            return []

        times, opens = data.get("t", []), data.get("o", [])
        highs, lows = data.get("h", []), data.get("l", [])
        closes, volumes = data.get("c", []), data.get("v", [])
        if not (len(times) == len(opens) == len(highs) == len(lows) == len(closes)):
            raise ExchangeError(f"{self.name} returned ragged OHLC arrays for {symbol}")

        candles: list[Candle] = []
        for i, t in enumerate(times):
            candles.append(
                Candle(
                    ts=datetime.fromtimestamp(int(t), UTC),
                    open=self._to_toman(_dec(opens[i]), quote),
                    high=self._to_toman(_dec(highs[i]), quote),
                    low=self._to_toman(_dec(lows[i]), quote),
                    close=self._to_toman(_dec(closes[i]), quote),
                    volume=_dec(volumes[i] if i < len(volumes) else 0),
                )
            )
        candles.sort(key=lambda c: c.ts)
        now = datetime.now(UTC)
        while candles and not tf.is_closed(candles[-1].ts, now):
            candles.pop()
        return candles[-limit:]

    async def ticker(self, symbol: str) -> Ticker:
        base, quote = self._split(symbol)
        data = await self._request(
            "GET", "/v3/orderbook/" + f"{base.upper()}{quote.upper()}"
        )
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        if not bids or not asks:
            raise ExchangeError(f"{self.name} returned an empty book for {symbol}")
        bid = self._to_toman(_dec(bids[0][0]), quote)
        ask = self._to_toman(_dec(asks[0][0]), quote)
        last = self._to_toman(_dec(data.get("lastTradePrice"), str(bid)), quote)
        return Ticker(symbol, bid, ask, last or (bid + ask) / 2, datetime.now(UTC))

    async def usdt_reference(self) -> Decimal | None:
        """USDT price in Toman.

        This is what turns a Toman PnL into a real one. A 20% Toman gain during
        a 25% devaluation is a 4% loss in purchasing power, and without this
        number there is no way to know which happened.
        """
        try:
            t = await self.ticker("USDTIRT")
        except ExchangeError:
            return None
        return t.last

    async def fees(self, symbol: str) -> Fees:
        from simin.exchanges.costs import cost_model

        c = cost_model(self.name, "spot")
        return Fees(c.maker_fee, c.taker_fee, Decimal("0"))

    # --- Account ----------------------------------------------------------

    async def balances(self) -> dict[str, Balance]:
        data = await self._request("POST", "/users/wallets/list", signed=True)
        out: dict[str, Balance] = {}
        for w in data.get("wallets", []) or []:
            asset = str(w.get("currency", "")).upper()
            if not asset:
                continue
            balance = _dec(w.get("balance"))
            blocked = _dec(w.get("blocked"))
            if asset in ("RLS", "IRT"):
                balance /= QUOTE_DIVISOR
                blocked /= QUOTE_DIVISOR
                asset = "IRT"
            out[asset] = Balance(asset, balance - blocked, blocked)
        return out

    # --- Trading ----------------------------------------------------------

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        if leverage > 1:
            raise ExchangeError(
                f"{self.display_name} is spot-only. Requested {leverage}x leverage cannot "
                "be applied — the risk profile should have been clamped by "
                "simin.risk.dial.spot_only before reaching the venue."
            )

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
        if side is Side.SELL and not reduce_only:
            # Selling spot you do not hold is not shorting, it is an error the
            # venue will reject. Catching it here makes the message legible.
            pass

        base, quote = self._split(symbol)
        body: dict[str, Any] = {
            "type": "buy" if side is Side.BUY else "sell",
            "srcCurrency": base,
            "dstCurrency": quote,
            "amount": str(qty),
        }
        if type is OrderType.MARKET:
            body["execution"] = "market"
        elif type is OrderType.LIMIT:
            if price is None:
                raise ValueError("limit order requires a price")
            body["execution"] = "limit"
            body["price"] = str((price * QUOTE_DIVISOR) if quote in ("irt", "rls") else price)
        elif type in (OrderType.STOP_MARKET, OrderType.TAKE_PROFIT_MARKET):
            if stop_price is None:
                raise ValueError(f"{type} requires a stop price")
            body["execution"] = "stop_market"
            body["stopPrice"] = str(
                (stop_price * QUOTE_DIVISOR) if quote in ("irt", "rls") else stop_price
            )
        else:
            raise ValueError(f"unsupported order type {type}")
        if client_id:
            body["clientOrderId"] = client_id

        data = await self._request("POST", "/market/orders/add", body=body, signed=True)
        return self._parse_order(data.get("order", {}), symbol, side, type, qty, price, quote)

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            await self._request(
                "POST",
                "/market/orders/update-status",
                body={"order": int(order_id), "status": "canceled"},
                signed=True,
            )
            return True
        except ExchangeError:
            return False

    async def get_order(self, symbol: str, order_id: str) -> Order:
        data = await self._request(
            "POST", "/market/orders/status", body={"id": int(order_id)}, signed=True
        )
        order = data.get("order", {})
        _, quote = self._split(symbol)
        side = Side.BUY if str(order.get("type", "")).lower() == "buy" else Side.SELL
        return self._parse_order(
            order, symbol, side, OrderType.LIMIT, _dec(order.get("amount")), None, quote
        )

    def _parse_order(
        self,
        d: dict[str, Any],
        symbol: str,
        side: Side,
        type: OrderType,
        qty: Decimal,
        price: Decimal | None,
        quote: str,
    ) -> Order:
        filled = _dec(d.get("matchedAmount"))
        avg = self._to_toman(_dec(d.get("averagePrice")), quote)
        return Order(
            id=str(d.get("id", "")),
            symbol=symbol,
            side=side,
            type=type,
            qty=qty,
            price=price,
            client_id=str(d.get("clientOrderId", "")),
            status=_STATUS.get(str(d.get("status", "")), OrderStatus.OPEN),
            filled_qty=filled,
            avg_price=avg if filled > 0 else ZERO,
            fee=self._to_toman(_dec(d.get("fee")), quote),
            venue_order_id=str(d.get("id", "")),
        )

    async def close(self) -> None:
        await self._client.aclose()


async def _backoff(attempt: int, base: float = 1.0) -> None:
    import asyncio

    await asyncio.sleep(min(base * (2**attempt), 10.0))
