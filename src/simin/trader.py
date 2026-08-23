"""The live/paper trading loop.

One process, one loop, one leader. The same strategy code that ran in the
backtester runs here; only the clock and the adapter differ. Everything that can
halt trading — kill switch, circuit breakers, stale data, mode gating — is
checked before any order is created, and LIVE mode additionally refuses to start
without an approval token issued by the Go/No-Go process.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from simin.backtest.costs import CostModel
from simin.config import Settings, get_settings
from simin.data.ingest import stale_by
from simin.data.quality import check_bars, closed_only
from simin.exchanges.base import ExchangeAdapter, VenueError
from simin.exchanges.paper import PaperAdapter
from simin.exchanges.public_global import PublicGlobalAdapter
from simin.exchanges.venues import profile
from simin.features.engine import FeatureRow, build_features
from simin.features.regime import RegimeState, classify
from simin.logging import configure_logging, get_logger
from simin.risk.engine import (
    AccountState,
    Intent,
    OpenPosition,
    RiskEngine,
    new_account,
    trailing_stop,
)
from simin.strategies import build as build_strategy
from simin.strategies.base import Strategy, StrategyContext
from simin.types import TF, OrderRequest, OrderType, RunMode, Side

log = get_logger(__name__)


@dataclass(slots=True)
class TraderConfig:
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    tf: TF = TF.H1
    strategies: tuple[str, ...] = ("trend_follow", "donchian_breakout")
    poll_seconds: float = 30.0
    history_bars: int = 500
    max_staleness: timedelta = timedelta(hours=2)
    quote_asset: str = "USDT"


@dataclass(slots=True)
class TraderState:
    account: AccountState
    last_bar_ts: dict[str, datetime] = field(default_factory=dict)
    orders_sent: int = 0
    errors: int = 0


class Trader:
    """Poll closed bars, decide, and route approved orders through the adapter."""

    def __init__(
        self,
        adapter: ExchangeAdapter,
        risk: RiskEngine,
        strategies: list[Strategy],
        config: TraderConfig,
        settings: Settings,
    ) -> None:
        settings.assert_live_allowed()
        if settings.mode is RunMode.LIVE and not adapter.supports_trading:
            raise RuntimeError("LIVE mode requires a trading-capable adapter")
        self.adapter = adapter
        self.risk = risk
        self.strategies = strategies
        self.config = config
        self.settings = settings
        self.state = TraderState(account=new_account(settings.paper_start_balance_irt))
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        log.info(
            "trader.start",
            mode=self.settings.mode,
            profile=self.settings.risk_profile,
            symbols=list(self.config.symbols),
            strategies=[s.name for s in self.strategies],
        )
        while not self._stop.is_set():
            try:
                await self.tick()
            except VenueError as exc:
                self.state.errors += 1
                log.warning("trader.venue_error", error=str(exc))
            except Exception as exc:
                self.state.errors += 1
                self.state.account.trip(f"unhandled exception: {exc!r}")
                log.exception("trader.unhandled", error=str(exc))
            await asyncio.sleep(self.config.poll_seconds)

    async def tick(self) -> None:
        """One decision cycle across all symbols."""
        if self.state.account.kill_switch:
            log.error("trader.halted", reason=self.state.account.kill_reason)
            return

        health = await self.adapter.health()
        breaker = self.risk.check_circuit_breakers(
            self.state.account,
            venue_error_rate=health.error_rate,
            clock_skew_ms=health.clock_skew_ms,
        )
        if breaker:
            log.error("trader.circuit_breaker", reason=breaker)
            return

        now = datetime.now(UTC)
        for symbol in self.config.symbols:
            await self._process_symbol(symbol, now)

    async def _process_symbol(self, symbol: str, now: datetime) -> None:
        since = now - self.config.tf.delta * self.config.history_bars
        bars = closed_only(await self.adapter.get_ohlcv(symbol, self.config.tf, since), now)
        if len(bars) < 250:
            log.info("trader.insufficient_history", symbol=symbol, bars=len(bars))
            return

        report = check_bars(bars)
        if not report.ok:
            # Bad data is worse than no data: it produces confident wrong signals.
            log.warning("trader.data_quality", symbol=symbol, errors=len(report.errors))
            return

        staleness = stale_by(bars, now, self.config.tf)
        if staleness > self.config.max_staleness:
            log.warning("trader.stale_feed", symbol=symbol, stale_seconds=staleness.total_seconds())
            return

        last_ts = bars[-1].ts
        if self.state.last_bar_ts.get(symbol) == last_ts:
            return  # already acted on this bar; never trade the same bar twice
        self.state.last_bar_ts[symbol] = last_ts
        self.state.account.last_data_ts = bars[-1].close_time

        rows = build_features(bars, self.config.tf)
        index = len(rows) - 1
        regime = classify(rows, index)
        position = self.state.account.positions.get(symbol)

        await self._manage_open_position(symbol, position, rows, index, regime, bars[-1].close)

        if symbol in self.state.account.positions:
            return

        for strategy in self.strategies:
            if not regime.allows(strategy.name):
                continue
            ctx = StrategyContext(
                ts=last_ts, symbol=symbol, row=rows[index], regime=regime,
                position=None, bar_index=index,
            )
            intent = strategy.generate(ctx)
            if intent is None:
                continue
            book = await self.adapter.get_orderbook(symbol, depth=10)
            depth = book.depth_notional(Side.BUY, 5)
            decision = self.risk.evaluate(
                self.state.account, intent, available_depth=depth, now=now
            )
            if decision.rejected:
                log.info(
                    "trader.rejected", symbol=symbol, strategy=strategy.name,
                    reason=str(decision.reason),
                )
                continue
            await self._submit(symbol, intent.direction, decision.qty, intent, strategy.name)
            return

    async def _manage_open_position(
        self,
        symbol: str,
        position: OpenPosition | None,
        rows: Sequence[FeatureRow],
        index: int,
        regime: RegimeState,
        price: Decimal,
    ) -> None:
        if position is None:
            return
        atr = rows[index].get("atr14")
        if atr:
            new_stop = trailing_stop(
                position.direction, position.entry, price, Decimal(str(atr))
            )
            if new_stop > position.stop:
                self.state.account.positions[symbol] = OpenPosition(
                    symbol=position.symbol, direction=position.direction, qty=position.qty,
                    entry=position.entry, stop=new_stop, strategy=position.strategy,
                    beta=position.beta, venue=position.venue, opened_at=position.opened_at,
                )
        stop_hit = price <= position.stop if position.direction > 0 else price >= position.stop
        if stop_hit:
            await self._close(symbol, position, reason="stop")

    async def _submit(
        self, symbol: str, direction: int, qty: Decimal, intent: Intent, strategy_name: str
    ) -> None:
        side = Side.BUY if direction > 0 else Side.SELL
        req = OrderRequest(
            symbol=symbol, side=side, type=OrderType.MARKET, qty=qty,
            client_order_id=f"simin-{uuid.uuid4()}",
        )
        order = await self.adapter.create_order(req)
        self.state.orders_sent += 1
        if order.filled_qty > 0 and order.avg_price is not None:
            self.state.account.positions[symbol] = OpenPosition(
                symbol=symbol, direction=direction, qty=order.filled_qty,
                entry=order.avg_price, stop=intent.stop, strategy=strategy_name,
                opened_at=datetime.now(UTC),
            )
        log.info(
            "trader.order", symbol=symbol, side=str(side), qty=str(qty),
            status=str(order.status), strategy=strategy_name,
            filled=str(order.filled_qty), price=str(order.avg_price),
        )

    async def _close(self, symbol: str, position: OpenPosition, *, reason: str) -> None:
        req = OrderRequest(
            symbol=symbol,
            side=Side.SELL if position.direction > 0 else Side.BUY,
            type=OrderType.MARKET,
            qty=position.qty,
            client_order_id=f"simin-exit-{uuid.uuid4()}",
        )
        order = await self.adapter.create_order(req)
        self.state.account.positions.pop(symbol, None)
        log.info("trader.close", symbol=symbol, reason=reason, status=str(order.status))


async def main() -> None:  # pragma: no cover - process entry point
    settings = get_settings()
    configure_logging(settings.log_level, as_json=settings.log_json)
    if settings.mode is RunMode.BACKTEST:
        raise SystemExit("trader does not run in BACKTEST mode; use the research CLI")

    data = PublicGlobalAdapter(settings.public_data_base, timeout=settings.http_timeout)
    venue = profile("local_irt_generic")
    adapter = PaperAdapter(
        data=data,
        cost=CostModel(fees=venue.fees, spread_bps=venue.typical_spread_bps),
        quote_asset="USDT",
        starting_balance=settings.paper_start_balance_irt,
    )
    config = TraderConfig()
    trader = Trader(
        adapter=adapter,
        risk=RiskEngine(settings.limits),
        strategies=[build_strategy(name) for name in config.strategies],
        config=config,
        settings=settings,
    )
    try:
        await trader.run_forever()
    finally:
        await data.close()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
