"""Read-only public market data from a global spot venue.

No API key, no order endpoints, no credentials — this adapter exists so the
research pipeline has a deep, clean history to work on. The venue you *trade* on
is configured separately; global data is used for regimes, derivatives context
and the USDT reference leg of every Toman pair (docs/01-research.md §0.1).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from simin.exchanges.base import (
    ExchangeAdapter,
    RateLimited,
    VenueError,
    VenueUnavailable,
)
from simin.exchanges.ratelimit import CircuitBreaker, TokenBucket, with_retry
from simin.types import (
    TF,
    Bar,
    FeeSchedule,
    Level,
    OrderBook,
    SymbolInfo,
    Ticker,
    VenueHealth,
)

_INTERVAL = {
    TF.M1: "1m",
    TF.M3: "3m",
    TF.M5: "5m",
    TF.M15: "15m",
    TF.M30: "30m",
    TF.H1: "1h",
    TF.H4: "4h",
    TF.D1: "1d",
}


def _ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


class PublicGlobalAdapter(ExchangeAdapter):
    venue = "public_global"
    supports_trading = False

    def __init__(
        self,
        base_url: str = "https://api.binance.com",
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
        requests_per_minute: float = 1000.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(base_url=self._base, timeout=timeout)
        self._bucket = TokenBucket(capacity=requests_per_minute, per_seconds=60.0)
        self._breaker = CircuitBreaker()
        self._latencies: list[float] = []
        self._errors = 0
        self._calls = 0

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self._breaker.is_open:
            raise VenueUnavailable(f"{self.venue}: circuit breaker open")
        await self._bucket.acquire()

        async def call() -> Any:
            started = datetime.now(UTC)
            try:
                resp = await self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                self._errors += 1
                self._breaker.record_failure()
                raise VenueUnavailable(f"{self.venue}: {exc!s}") from exc
            finally:
                self._calls += 1
                self._latencies.append((datetime.now(UTC) - started).total_seconds() * 1000)
                del self._latencies[:-500]

            if resp.status_code in (418, 429):
                self._errors += 1
                retry_after = resp.headers.get("retry-after")
                raise RateLimited(
                    f"{self.venue}: rate limited",
                    retry_after=float(retry_after) if retry_after else None,
                )
            if resp.status_code >= 500:
                self._errors += 1
                self._breaker.record_failure()
                raise VenueUnavailable(f"{self.venue}: HTTP {resp.status_code}")
            if resp.status_code >= 400:
                raise VenueError(f"{self.venue}: HTTP {resp.status_code} {resp.text[:200]}")
            self._breaker.record_success()
            return resp.json()

        return await with_retry(call)

    async def get_symbols(self) -> list[SymbolInfo]:
        data = await self._get("/api/v3/exchangeInfo")
        out: list[SymbolInfo] = []
        for s in data.get("symbols", []):
            if s.get("status") != "TRADING":
                continue
            filters = {f["filterType"]: f for f in s.get("filters", [])}
            out.append(
                SymbolInfo(
                    venue=self.venue,
                    symbol=s["symbol"],
                    base=s["baseAsset"],
                    quote=s["quoteAsset"],
                    price_tick=Decimal(filters.get("PRICE_FILTER", {}).get("tickSize", "0")),
                    qty_step=Decimal(filters.get("LOT_SIZE", {}).get("stepSize", "0")),
                    min_notional=Decimal(
                        filters.get("NOTIONAL", {}).get("minNotional", "0")
                    ),
                )
            )
        return out

    async def get_ticker(self, symbol: str) -> Ticker:
        data = await self._get("/api/v3/ticker/bookTicker", {"symbol": symbol})
        return Ticker(
            symbol=symbol,
            ts=datetime.now(UTC),
            bid=Decimal(data["bidPrice"]),
            ask=Decimal(data["askPrice"]),
            last=(Decimal(data["bidPrice"]) + Decimal(data["askPrice"])) / Decimal(2),
        )

    async def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBook:
        data = await self._get("/api/v3/depth", {"symbol": symbol, "limit": depth})
        return OrderBook(
            symbol=symbol,
            ts=datetime.now(UTC),
            bids=tuple(Level(Decimal(p), Decimal(q)) for p, q in data["bids"]),
            asks=tuple(Level(Decimal(p), Decimal(q)) for p, q in data["asks"]),
        )

    async def get_ohlcv(self, symbol: str, tf: TF, since: datetime, limit: int = 1000) -> list[Bar]:
        """Fetch closed bars from ``since`` (inclusive).

        The final element returned by the venue is the *in-progress* bar; it is
        dropped here rather than downstream, because a partially-formed bar that
        reaches the feature engine is look-ahead bias with extra steps.
        """
        raw = await self._get(
            "/api/v3/klines",
            {
                "symbol": symbol,
                "interval": _INTERVAL[tf],
                "startTime": _ms(since),
                "limit": min(limit, 1000),
            },
        )
        now = datetime.now(UTC)
        bars: list[Bar] = []
        for k in raw:
            ts = _dt(int(k[0]))
            if ts + tf.delta > now:
                continue
            bars.append(
                Bar(
                    symbol=symbol,
                    tf=tf,
                    ts=ts,
                    open=Decimal(k[1]),
                    high=Decimal(k[2]),
                    low=Decimal(k[3]),
                    close=Decimal(k[4]),
                    volume=Decimal(k[5]),
                    quote_volume=Decimal(k[7]),
                    trades=int(k[8]),
                )
            )
        return bars

    def fee_schedule(self, symbol: str | None = None) -> FeeSchedule:
        # Read-only adapter: fees are irrelevant for data, and are never guessed
        # for a venue you actually trade on — those come from venue config.
        return FeeSchedule(maker=Decimal("0"), taker=Decimal("0"))

    async def health(self) -> VenueHealth:
        started = datetime.now(UTC)
        skew_ms = 0.0
        ok = True
        try:
            data = await self._get("/api/v3/time")
            server = _dt(int(data["serverTime"]))
            skew_ms = (server - datetime.now(UTC)).total_seconds() * 1000
        except VenueError:
            ok = False
        latencies = sorted(self._latencies)
        p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0.0
        return VenueHealth(
            venue=self.venue,
            ok=ok and not self._breaker.is_open,
            latency_p95_ms=p95,
            error_rate=(self._errors / self._calls) if self._calls else 0.0,
            clock_skew_ms=skew_ms,
            checked_at=started,
        )

    async def close(self) -> None:
        await self._client.aclose()
