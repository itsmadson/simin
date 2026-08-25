"""The live/paper trading loop.

One process, one loop, one leader. The same strategy code that ran in the
backtester runs here; only the clock and the adapter differ. Everything that can
halt trading — kill switch, circuit breakers, stale data, mode gating — is
checked before any order is created, and LIVE mode additionally refuses to start
without an approval token issued by the Go/No-Go process.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from simin.backtest.costs import CostModel
from simin.config import Settings, get_settings
from simin.data.ingest import stale_by
from simin.data.quality import check_bars, closed_only
from simin.db.repo import Repo, make_engine
from simin.db.store import RunHandle, SessionStore
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
from simin.types import TF, Order, OrderRequest, OrderType, RunMode, Side

log = get_logger(__name__)


@dataclass(slots=True)
class TraderConfig:
    """Live trading configuration.

    The universe is wide and the strategies are the swing pair, because that is
    what the horizon research concluded: entries arrive daily from breadth,
    while each position still resolves inside the 4-day ceiling. A narrow
    universe traded frequently is the configuration the data rules out.
    """

    symbols: tuple[str, ...] = (
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
        "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "ATOMUSDT",
    )
    tf: TF = TF.H1
    strategies: tuple[str, ...] = ("swing_momentum", "swing_pullback")
    poll_seconds: float = 60.0
    history_bars: int = 500
    max_staleness: timedelta = timedelta(hours=2)
    quote_asset: str = "USDT"
    #: Hard holding ceiling, in bars of ``tf``. 96 hourly bars = 4 days.
    max_hold_bars: int = 96


@dataclass(slots=True)
class TraderState:
    account: AccountState
    last_bar_ts: dict[str, datetime] = field(default_factory=dict)
    orders_sent: int = 0
    errors: int = 0
    #: db position id per symbol, so a close can update the row it opened
    position_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    ticks: int = 0
    paused: bool = False


class Trader:
    """Poll closed bars, decide, and route approved orders through the adapter."""

    def __init__(
        self,
        adapter: ExchangeAdapter,
        risk: RiskEngine,
        strategies: list[Strategy],
        config: TraderConfig,
        settings: Settings,
        store: SessionStore | None = None,
        symbol_ids: dict[str, int] | None = None,
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
        # Persistence is optional so the trader stays unit-testable, but in every
        # real deployment it is present: a session that leaves no record behind
        # cannot be reviewed, and an unreviewable trading system is a rumour.
        self.store = store
        self.symbol_ids = symbol_ids or {}
        self.run: RunHandle | None = None
        self._venue_id = 1
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def start_session(self, venue_id: int = 1) -> None:
        """Open a run row so everything that follows is attributable to it."""
        self._venue_id = venue_id
        if self.store is None:
            return
        self.run = await self.store.start_run(
            self.settings.mode,
            code_sha=os.environ.get("SIMIN_CODE_SHA", "dev"),
            config_hash=str(hash((self.settings.risk_profile, self.config.strategies))),
            risk_profile=str(self.settings.risk_profile),
            notes=f"symbols={','.join(self.config.symbols)} tf={self.config.tf.value}",
        )
        await self.store.mark_equity(
            self.run,
            ts=datetime.now(UTC),
            balance=self.state.account.equity,
            equity=self.state.account.equity,
            equity_usdt=self.state.account.equity,
        )

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
        self.state.ticks += 1

        # The operator's pause lives in the database because the dashboard runs
        # in a different process. Checked first, and checked every cycle, so
        # pressing Pause takes effect within one poll interval.
        if self.store is not None:
            control = await self.store.get_control()
            paused = bool(control.get("paused"))
            if paused != self.state.paused:
                self.state.paused = paused
                log.info("trader.control", paused=paused, reason=control.get("reason"))
            if paused:
                # Open positions are still managed: stops and the holding
                # ceiling keep working. Pause means "stop opening", not
                # "abandon what is open".
                await self._manage_all_positions()
                return

        if self.state.account.kill_switch:
            log.error("trader.halted", reason=self.state.account.kill_reason)
            await self._record_risk("halted", {"reason": self.state.account.kill_reason})
            return

        health = await self.adapter.health()
        breaker = self.risk.check_circuit_breakers(
            self.state.account,
            venue_error_rate=health.error_rate,
            clock_skew_ms=health.clock_skew_ms,
            now=datetime.now(UTC),
        )
        if breaker:
            # A transient condition pauses entries but still manages positions;
            # a latched one stops everything until a human intervenes.
            latched = bool(self.state.account.kill_switch)
            log.warning("trader.circuit_breaker", reason=breaker, latched=latched)
            await self._record_risk("circuit_breaker", {"reason": breaker, "latched": latched})
            if not latched:
                await self._manage_all_positions()
            return

        now = datetime.now(UTC)
        marks: dict[str, Decimal] = {}
        for symbol in self.config.symbols:
            price = await self._process_symbol(symbol, now)
            if price is not None:
                marks[symbol] = price
        await self._mark_equity(now, marks)

    async def _process_symbol(self, symbol: str, now: datetime) -> Decimal | None:
        since = now - self.config.tf.delta * self.config.history_bars
        bars = closed_only(await self.adapter.get_ohlcv(symbol, self.config.tf, since), now)
        if len(bars) < 250:
            log.info("trader.insufficient_history", symbol=symbol, bars=len(bars))
            return None

        report = check_bars(bars)
        if not report.ok:
            # Bad data is worse than no data: it produces confident wrong signals.
            log.warning("trader.data_quality", symbol=symbol, errors=len(report.errors))
            await self._record_risk(
                "data_quality", {"symbol": symbol, "errors": len(report.errors)}
            )
            return None

        staleness = stale_by(bars, now, self.config.tf)
        if staleness > self.config.max_staleness:
            log.warning(
                "trader.stale_feed", symbol=symbol, stale_seconds=staleness.total_seconds()
            )
            await self._record_risk("stale_feed", {"symbol": symbol})
            return None

        last_ts = bars[-1].ts
        price = bars[-1].close
        if self.state.last_bar_ts.get(symbol) == last_ts:
            return price  # already acted on this bar; never trade the same bar twice
        self.state.last_bar_ts[symbol] = last_ts
        self.state.account.last_data_ts = bars[-1].close_time

        rows = build_features(bars, self.config.tf)
        index = len(rows) - 1
        regime = classify(rows, index)
        position = self.state.account.positions.get(symbol)

        await self._manage_open_position(symbol, position, rows, index, regime, price)

        if symbol in self.state.account.positions:
            return price

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
                await self._record_risk(
                    "order_rejected",
                    {"symbol": symbol, "strategy": strategy.name, "reason": str(decision.reason)},
                )
                continue
            await self._record_signal(symbol, intent, strategy.name, last_ts)
            await self._submit(symbol, intent.direction, decision.qty, intent, strategy.name)
            return price
        return price

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
                stored_id = self.state.position_ids.get(symbol)
                if self.store is not None and stored_id is not None:
                    await self.store.update_stop(stored_id, new_stop)
                self.state.account.positions[symbol] = OpenPosition(
                    symbol=position.symbol, direction=position.direction, qty=position.qty,
                    entry=position.entry, stop=new_stop, strategy=position.strategy,
                    beta=position.beta, venue=position.venue, opened_at=position.opened_at,
                )
        stop_hit = price <= position.stop if position.direction > 0 else price >= position.stop
        if stop_hit:
            await self._close(symbol, position, reason="stop")
            return
        # The ceiling is enforced here as well as in the backtester: a rule that
        # only exists in simulation is not a rule.
        if position.opened_at is not None:
            held = datetime.now(UTC) - position.opened_at
            if held >= self.config.tf.delta * self.config.max_hold_bars:
                await self._close(symbol, position, reason="time_stop")

    async def _manage_all_positions(self) -> None:
        """Run stop and ceiling management without considering new entries."""
        now = datetime.now(UTC)
        for symbol in list(self.state.account.positions):
            position = self.state.account.positions.get(symbol)
            if position is None:
                continue
            try:
                ticker = await self.adapter.get_ticker(symbol)
            except VenueError:
                continue
            stop_hit = (
                ticker.last <= position.stop
                if position.direction > 0
                else ticker.last >= position.stop
            )
            if stop_hit:
                await self._close(symbol, position, reason="stop")
            elif position.opened_at is not None:
                held = now - position.opened_at
                if held >= self.config.tf.delta * self.config.max_hold_bars:
                    await self._close(symbol, position, reason="time_stop")

    async def _record_risk(self, kind: str, detail: dict[str, object]) -> None:
        if self.store is not None:
            await self.store.record_risk_event(self.run, kind, detail)

    async def _record_signal(
        self, symbol: str, intent: Intent, strategy_name: str, ts: datetime
    ) -> uuid.UUID | None:
        if self.store is None or self.run is None:
            return None
        symbol_id = self.symbol_ids.get(symbol)
        if symbol_id is None:
            return None
        cost = self.adapter.fee_schedule(symbol).taker * Decimal(2)
        return await self.store.record_signal(
            self.run,
            symbol_id=symbol_id,
            ts=ts,
            tf=self.config.tf.value,
            direction="long" if intent.direction > 0 else "short",
            entry=intent.entry,
            stop=intent.stop,
            strategy=strategy_name,
            regime=intent.regime,
            confidence=intent.confidence,
            expected_cost=cost,
        )

    async def _mark_equity(self, ts: datetime, marks: dict[str, Decimal]) -> None:
        """Value the account and persist the point. This is the equity curve the
        dashboard draws, so it must be written every cycle, not only on trades."""
        positions = self.state.account.positions
        unrealized = sum(
            (
                (marks.get(sym, pos.entry) - pos.entry) * pos.qty * Decimal(pos.direction)
                for sym, pos in positions.items()
            ),
            start=Decimal(0),
        )
        equity = self.state.account.equity + unrealized
        self.state.account.mark(equity)
        exposure = (
            sum((pos.notional(marks.get(sym)) for sym, pos in positions.items()), Decimal(0))
            / equity
            if equity > 0
            else Decimal(0)
        )
        if self.store is None or self.run is None:
            return
        await self.store.mark_equity(
            self.run,
            ts=ts,
            balance=self.state.account.equity,
            equity=equity,
            equity_usdt=equity,
            unrealized=unrealized,
            drawdown=self.state.account.drawdown,
            exposure=exposure,
        )

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
        opened_at = datetime.now(UTC)
        order_id = await self._persist_order(symbol, order, side)
        if order.filled_qty > 0 and order.avg_price is not None:
            self.state.account.positions[symbol] = OpenPosition(
                symbol=symbol, direction=direction, qty=order.filled_qty,
                entry=order.avg_price, stop=intent.stop, strategy=strategy_name,
                opened_at=opened_at,
            )
            await self._persist_fills(order_id, order)
            if self.store is not None and self.run is not None:
                symbol_id = self.symbol_ids.get(symbol)
                if symbol_id is not None:
                    self.state.position_ids[symbol] = await self.store.open_position(
                        self.run,
                        symbol_id=symbol_id,
                        side="long" if direction > 0 else "short",
                        qty=order.filled_qty,
                        avg_entry=order.avg_price,
                        stop=intent.stop,
                        opened_at=opened_at,
                        strategy=strategy_name,
                        regime=intent.regime,
                        fees_paid=sum((f.fee for f in order.fills), Decimal(0)),
                    )
        log.info(
            "trader.order", symbol=symbol, side=str(side), qty=str(qty),
            status=str(order.status), strategy=strategy_name,
            filled=str(order.filled_qty), price=str(order.avg_price),
        )

    async def _close(self, symbol: str, position: OpenPosition, *, reason: str) -> None:
        side = Side.SELL if position.direction > 0 else Side.BUY
        req = OrderRequest(
            symbol=symbol,
            side=side,
            type=OrderType.MARKET,
            qty=position.qty,
            client_order_id=f"simin-exit-{uuid.uuid4()}",
        )
        order = await self.adapter.create_order(req)
        self.state.account.positions.pop(symbol, None)
        order_id = await self._persist_order(symbol, order, side)
        await self._persist_fills(order_id, order)

        fees = sum((f.fee for f in order.fills), Decimal(0))
        if order.avg_price is not None:
            gross = (order.avg_price - position.entry) * position.qty * Decimal(position.direction)
            realized = gross - fees
            self.state.account.equity += realized
            self.state.account.consecutive_losses = (
                self.state.account.consecutive_losses + 1 if realized <= 0 else 0
            )
        else:
            realized = Decimal(0)

        position_id = self.state.position_ids.pop(symbol, None)
        if self.store is not None and position_id is not None:
            await self.store.close_position(
                position_id,
                closed_at=datetime.now(UTC),
                realized_pnl_irt=realized,
                fees_paid=fees,
            )
        log.info(
            "trader.close", symbol=symbol, reason=reason, status=str(order.status),
            realized=str(realized),
        )

    async def _persist_order(self, symbol: str, order: Order, side: Side) -> uuid.UUID | None:
        if self.store is None or self.run is None:
            return None
        symbol_id = self.symbol_ids.get(symbol)
        if symbol_id is None:
            return None
        return await self.store.record_order(
            self.run,
            venue_id=self._venue_id,
            symbol_id=symbol_id,
            side=str(side),
            order_type=str(order.type),
            qty=order.qty,
            status=str(order.status),
            client_order_id=order.client_order_id,
            price=order.avg_price,
            exchange_order_id=order.exchange_order_id,
            reject_reason=order.reject_reason,
        )

    async def _persist_fills(self, order_id: uuid.UUID | None, order: Order) -> None:
        if self.store is None or order_id is None:
            return
        for fill in order.fills:
            await self.store.record_fill(
                order_id,
                ts=fill.ts,
                price=fill.price,
                qty=fill.qty,
                fee=fill.fee,
                fee_asset=fill.fee_asset,
                is_maker=fill.is_maker,
            )


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
    engine = make_engine(settings.pg_dsn)
    repo = Repo(engine)
    store = SessionStore(engine)

    venue_id = await repo.upsert_venue(adapter.venue, "Paper (simulated fills)")
    symbol_ids: dict[str, int] = {}
    catalogue = {s.symbol: s for s in await data.get_symbols()}
    for symbol in config.symbols:
        info = catalogue.get(symbol)
        if info is not None:
            symbol_ids[symbol] = await repo.upsert_symbol(venue_id, info)

    trader = Trader(
        adapter=adapter,
        risk=RiskEngine(settings.limits),
        strategies=[build_strategy(name) for name in config.strategies],
        config=config,
        settings=settings,
        store=store,
        symbol_ids=symbol_ids,
    )
    await store.ensure_control()
    await trader.start_session(venue_id=venue_id)
    try:
        await trader.run_forever()
    finally:
        if trader.run is not None:
            await store.end_run(trader.run)
        await data.close()
        await engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
