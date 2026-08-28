"""The live bot: one loop, driven by candle closes.

The design mirrors the backtester deliberately. Same risk engine, same ensemble,
same cost model, same order of operations. When live results diverge from the
backtest, the cause should be the market, not two different implementations of
the same idea drifting apart.

The loop:

    every poll interval:
      1. Fetch closed candles. If no NEW bar closed, do nothing at all.
      2. Reconcile: what does the venue think we hold? Trust it over ourselves.
      3. Manage open positions — stops, trails, targets.
      4. Evaluate the ensemble on the newly closed bar.
      5. Size and place, if everything agrees.

Step 1 is the one people get wrong. Polling every 15 seconds and re-evaluating
every time means acting on a bar that is still forming, which is the live
equivalent of lookahead: the decision is made from a candle that has not
happened yet. `_last_bar` gates the entire decision path on a genuinely new
close.

Step 2 exists because the venue is the source of truth. A stop can fill while
the process is restarting; a position can be liquidated; a manual trade can
happen in the app. Local state that disagrees with the venue is wrong by
definition.
"""

from __future__ import annotations

import asyncio
import enum
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from simin.config import Settings
from simin.core.types import (
    TF,
    Candle,
    Direction,
    EquityPoint,
    ExitReason,
    Mode,
    OrderType,
    Position,
    Side,
    Symbol,
    Trade,
    ZERO,
)
from simin.exchanges.base import Exchange, ExchangeError, InsufficientFunds
from simin.exchanges.costs import CostModel, cost_model
from simin.indicators.features import FeatureFrame
from simin.logging import get_logger
from simin.risk.dial import RiskProfile
from simin.risk.engine import AccountState, Halt, RiskEngine
from simin.strategies.base import Context, build_many
from simin.strategies.ensemble import Decision, Ensemble
from simin.strategies.library import strategies_for_level

log = get_logger(__name__)


class BotState(enum.StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    HALTED = "halted"
    ERROR = "error"

    @property
    def is_active(self) -> bool:
        return self in (BotState.STARTING, BotState.RUNNING, BotState.PAUSED)


@dataclass(slots=True)
class Event:
    """Something worth telling the user about. Streamed to the UI."""

    ts: datetime
    kind: str
    symbol: str
    message: str
    message_fa: str = ""
    data: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "ts": self.ts.isoformat(),
            "kind": self.kind,
            "symbol": self.symbol,
            "message": self.message,
            "message_fa": self.message_fa,
            "data": self.data,
        }


@dataclass(slots=True)
class RunnerStatus:
    state: BotState
    mode: Mode
    #: Human-readable, e.g. "coinex (paper)". For display only.
    venue: str
    #: The configured venue key, e.g. "coinex". Stable, and what the UI matches
    #: its selector against — a label that changes with the adapter cannot also
    #: serve as an identifier.
    venue_key: str
    risk_level: int
    profile_name: str
    started_at: datetime | None
    last_bar: datetime | None
    equity: Decimal
    starting_equity: Decimal
    cash: Decimal
    open_positions: int
    drawdown: Decimal
    day_realised: Decimal
    trades_today: int
    total_trades: int
    halt: Halt
    halt_note: str
    error: str = ""
    #: True when the venue could not apply the dial's leverage.
    profile_clamped: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "mode": self.mode.value,
            "venue": self.venue,
            "venue_key": self.venue_key,
            "risk_level": self.risk_level,
            "profile_name": self.profile_name,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_bar": self.last_bar.isoformat() if self.last_bar else None,
            "equity": float(self.equity),
            "starting_equity": float(self.starting_equity),
            "cash": float(self.cash),
            "return_pct": float(self.equity / self.starting_equity - 1)
            if self.starting_equity > 0 else 0.0,
            "open_positions": self.open_positions,
            "drawdown": float(self.drawdown),
            "day_realised": float(self.day_realised),
            "trades_today": self.trades_today,
            "total_trades": self.total_trades,
            "halt": self.halt.value,
            "halt_note": self.halt_note,
            "error": self.error,
            "profile_clamped": self.profile_clamped,
        }


class Runner:
    """One bot. One mode, one venue, one risk level, N symbols."""

    #: Failed close attempts before a stuck exit is reported as an alarm rather
    #: than as a routine retry.
    STUCK_EXIT_ALARM = 5

    def __init__(
        self,
        settings: Settings,
        exchange: Exchange,
        profile: RiskProfile,
        symbols: dict[str, Symbol],
        costs: CostModel | None = None,
        max_events: int = 500,
    ) -> None:
        self.settings = settings
        self.exchange = exchange
        self.profile = profile
        self.symbols = symbols
        self.costs = costs or cost_model(exchange.name)
        self.risk = RiskEngine(profile, settings.max_capital)
        self.ensemble = Ensemble(
            build_many(strategies_for_level(profile.level)), profile
        )

        self.state = BotState.STOPPED
        self.started_at: datetime | None = None
        self.error = ""
        self.profile_clamped = False

        self.account = AccountState(
            cash=settings.starting_equity,
            equity=settings.starting_equity,
            peak_equity=settings.starting_equity,
            day_start_equity=settings.starting_equity,
        )
        self.trades: list[Trade] = []
        self.curve: list[EquityPoint] = []
        self.events: list[Event] = []
        self.last_decisions: dict[str, Decision] = {}

        self._max_events = max_events
        #: Consecutive failed close attempts per symbol. See `_close`.
        self._exit_failures: dict[str, int] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        #: Last bar timestamp acted on, per symbol. The new-bar gate.
        self._last_bar: dict[str, datetime] = {}
        self._frames: dict[str, FeatureFrame] = {}
        self._bar_index = 0
        self._lock = asyncio.Lock()

    # --- Lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if self.state.is_active:
            raise RuntimeError(f"bot is already {self.state.value}")

        problems = self.settings.validate_for_start()
        if problems:
            self.state = BotState.ERROR
            self.error = "; ".join(problems)
            raise RuntimeError(self.error)

        # The last gate before a live bot exists. `assert_can_trade` raises if
        # the adapter cannot place real orders and the mode says it must.
        self.exchange.assert_can_trade(self.settings.mode)

        self.state = BotState.STARTING
        self.started_at = datetime.now(UTC)
        self._stop.clear()
        self.error = ""
        self._emit(
            "start", "",
            f"Bot starting: {self.settings.mode.value} mode on "
            f"{getattr(self.exchange, 'source_name', self.exchange.name)}, "
            f"risk level {self.profile.level} ({self.profile.name_en})",
            f"ربات در حال شروع: حالت {self.settings.mode.value} روی "
            f"{getattr(self.exchange, 'source_name', self.exchange.name)}، "
            f"سطح ریسک {self.profile.level}",
        )
        self._task = asyncio.create_task(self._loop(), name="simin-runner")

    async def stop(self, flatten: bool = False) -> None:
        """Stop the loop. `flatten` closes every open position first.

        Stopping without flattening leaves positions live on the venue with
        their stops in place — correct for a restart, wrong for "I want out".
        The caller decides, and the UI asks.
        """
        self._stop.set()
        if flatten:
            await self.flatten_all(ExitReason.MANUAL)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.state = BotState.STOPPED
        self._emit("stop", "", "Bot stopped", "ربات متوقف شد")

    async def pause(self) -> None:
        """Stop opening new positions; keep managing the open ones.

        This is the correct response to "I'm not sure about this" — abandoning
        open positions with live stops is strictly worse than continuing to
        manage them.
        """
        if self.state is BotState.RUNNING:
            self.state = BotState.PAUSED
            self._emit("pause", "", "Paused: managing open positions, taking no new ones",
                       "مکث: مدیریت موقعیت‌های باز، بدون ورود جدید")

    async def resume(self) -> None:
        if self.state is BotState.PAUSED:
            self.state = BotState.RUNNING
            self._emit("resume", "", "Resumed", "ادامه یافت")

    async def kill(self) -> None:
        """Emergency: flatten everything and refuse to trade until restarted."""
        self.account.halt = Halt.KILL_SWITCH
        self.account.halt_note = "manual kill switch"
        await self.flatten_all(ExitReason.KILL_SWITCH)
        self.state = BotState.HALTED
        self._emit("kill", "", "KILL SWITCH: all positions closed, trading disabled",
                   "کلید اضطراری: تمام موقعیت‌ها بسته شد، معامله غیرفعال است")

    async def set_risk_level(self, level: int) -> None:
        """Change the dial while running.

        Open positions keep the profile they were opened under — their stop and
        size were derived from it, and retro-fitting a new risk level onto an
        existing position would either widen a stop that is already placed or
        claim a risk figure that was never true.
        """
        from simin.risk.dial import profile as get_profile
        from simin.exchanges.registry import adapt_profile

        new = adapt_profile(get_profile(level), self.exchange)
        async with self._lock:
            old = self.profile.level
            self.profile = new
            self.profile_clamped = new.max_leverage < get_profile(level).max_leverage
            self.risk = RiskEngine(new, self.settings.max_capital)
            self.ensemble = Ensemble(build_many(strategies_for_level(level)), new)
        self._emit(
            "risk_change", "",
            f"Risk level {old} -> {level} ({new.name_en}). Open positions keep their "
            "original stops and sizing.",
            f"سطح ریسک از {old} به {level} ({new.name_fa}) تغییر کرد. موقعیت‌های باز "
            "حد ضرر و حجم اولیه خود را نگه می‌دارند.",
        )

    # --- The loop ---------------------------------------------------------

    async def _loop(self) -> None:
        try:
            await self._warmup()
            self.state = BotState.RUNNING
            while not self._stop.is_set():
                try:
                    async with self._lock:
                        await self._tick()
                except (ExchangeError, InsufficientFunds) as exc:
                    # Venue trouble is expected and transient. Log, keep going;
                    # stopping the bot on a 500 would leave positions unmanaged.
                    log.warning("venue error during tick", error=str(exc))
                    self._emit("venue_error", "", f"Venue error: {exc}", "خطای صرافی")
                except Exception as exc:  # noqa: BLE001
                    log.exception("unexpected error in runner tick")
                    self.error = str(exc)
                    self._emit("error", "", f"Unexpected error: {exc}", "خطای غیرمنتظره")
                await self._sleep(self.settings.poll_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("runner died")
            self.state = BotState.ERROR
            self.error = str(exc)
            self._emit("fatal", "", f"Bot stopped with an error: {exc}", "ربات با خطا متوقف شد")

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _warmup(self) -> None:
        """Load enough history for every indicator before deciding anything."""
        tf = self.profile.signal_tf
        for name in self.symbols:
            candles = await self.exchange.candles(
                self.symbols[name].venue_symbol, tf, limit=self.settings.warmup_bars
            )
            if len(candles) < 210:
                raise RuntimeError(
                    f"{name}: only {len(candles)} candles available on {tf.value}; "
                    "indicators need at least 210. Try a longer timeframe."
                )
            self._frames[name] = FeatureFrame(name, tf, candles)
            self._last_bar[name] = candles[-1].ts
            self._emit(
                "warmup", name,
                f"{name}: loaded {len(candles)} {tf.value} candles",
                f"{name}: {len(candles)} کندل {tf.value} بارگذاری شد",
            )

    async def _tick(self) -> None:
        tf = self.profile.signal_tf
        now = datetime.now(UTC)
        self.account.roll_day(now)

        for name, symbol in self.symbols.items():
            candles = await self.exchange.candles(symbol.venue_symbol, tf, limit=400)
            if not candles:
                continue
            latest = candles[-1]

            # --- The new-bar gate ------------------------------------------
            previous = self._last_bar.get(name)
            is_new_bar = previous is None or latest.ts > previous
            self._frames[name] = FeatureFrame(name, tf, candles)
            frame = self._frames[name]
            i = len(frame) - 1
            row = frame.row(i)

            # A position's age is counted in closed bars, not in polls. The
            # time stop is defined in bars, so incrementing per poll would fire
            # it within minutes at a 15-second interval, and never incrementing
            # it — which is what happens if this lives in the backtester only —
            # means the time stop never fires live at all.
            if is_new_bar:
                held = self.account.positions.get(name)
                if held is not None:
                    held.bars_held += 1

            # Position management runs on every poll, not just on a new bar.
            # A stop needs checking now, not in two hours.
            await self._manage(name, symbol, latest, row.get("atr"))

            if not is_new_bar:
                continue
            self._last_bar[name] = latest.ts
            self._bar_index += 1

            if self.state is not BotState.RUNNING:
                continue
            halt = self.risk.check_halts(self.account, self.settings.trading_frozen)
            if halt.is_halted:
                if self.account.halt is not halt:
                    self.account.halt = halt
                    self.account.halt_note = f"triggered at {now.isoformat()}"
                    self._emit(
                        "halt", name,
                        f"Trading halted: {halt.value}. "
                        f"{'Requires manual restart.' if halt.is_permanent else 'Resumes tomorrow.'}",
                        f"معامله متوقف شد: {halt.value}",
                        {"halt": halt.value, "permanent": halt.is_permanent},
                    )
                if halt.is_permanent:
                    self.state = BotState.HALTED
                continue

            if name in self.account.positions:
                continue

            ctx = Context(
                row=row,
                context_row=None,
                symbol=name,
                position=None,
                bar_index=self._bar_index,
                allow_shorts=self.profile.allow_shorts,
                allow_counter_trend=self.profile.allow_counter_trend,
            )
            decision = self.ensemble.decide(ctx)
            self.last_decisions[name] = decision
            if decision.accepted:
                await self._open(name, symbol, decision, row.get("atr"))

        await self._mark(now)

    # --- Position handling ------------------------------------------------

    async def _manage(
        self, name: str, symbol: Symbol, candle: Candle, atr: float | None
    ) -> None:
        pos = self.account.positions.get(name)
        if pos is None:
            return
        ticker = await self.exchange.ticker(symbol.venue_symbol)
        price = ticker.last
        atr_dec = Decimal(str(atr)) if atr else None

        new_stop, reason = self.risk.manage(
            pos, max(candle.high, price), min(candle.low, price), price, atr_dec
        )
        if new_stop != pos.stop_price:
            pos.stop_price = new_stop
            self._emit(
                "stop_moved", name,
                f"{name}: stop moved to {new_stop}",
                f"{name}: حد ضرر به {new_stop} منتقل شد",
                {"stop": float(new_stop)},
            )
        if reason is not None:
            await self._close(name, symbol, reason, price)

    async def _open(
        self, name: str, symbol: Symbol, decision: Decision, atr: float | None
    ) -> None:
        ticker = await self.exchange.ticker(symbol.venue_symbol)
        direction = decision.intent.direction
        if direction is None:
            return
        price = ticker.ask if direction is Direction.LONG else ticker.bid

        sizing = self.risk.size(
            decision.intent, symbol, price, self.account, self._bar_index,
            frozen=self.settings.trading_frozen,
        )
        if not sizing.approved:
            self._emit(
                "signal_rejected", name,
                f"{name}: {direction.value} signal at {decision.intent.confidence:.0%} "
                f"confluence rejected by risk engine ({sizing.rejection.value})",
                f"{name}: سیگنال رد شد ({sizing.rejection.value})",
                {"rejection": sizing.rejection.value, "note": sizing.note},
            )
            return

        if sizing.leverage > 1:
            await self.exchange.set_leverage(symbol.venue_symbol, sizing.leverage)

        try:
            order = await self.exchange.place_order(
                symbol=symbol.venue_symbol,
                side=direction.entry_side,
                type=OrderType.MARKET,
                qty=sizing.qty,
                client_id=f"simin-{name}-{self._bar_index}",
            )
        except InsufficientFunds as exc:
            self._emit("order_failed", name, f"{name}: insufficient funds — {exc}",
                       f"{name}: موجودی کافی نیست")
            return
        except ExchangeError as exc:
            self._emit("order_failed", name, f"{name}: order rejected — {exc}",
                       f"{name}: سفارش رد شد")
            return

        fill = order.avg_price if order.avg_price > 0 else price
        fee = order.fee if order.fee > 0 else self.costs.fee(sizing.qty * fill)
        self.account.cash -= fee

        pos = Position(
            symbol=name,
            direction=direction,
            qty=order.filled_qty if order.filled_qty > 0 else sizing.qty,
            entry_price=fill,
            stop_price=sizing.stop_price,
            take_profit=sizing.take_profit,
            leverage=sizing.leverage,
            opened_at=datetime.now(UTC),
            strategy=decision.intent.strategy,
            risk_level=self.profile.level,
            risk_amount=sizing.risk_amount,
            initial_stop=sizing.stop_price,
            fees_paid=fee,
        )
        self.account.positions[name] = pos
        self.account.trades_today += 1

        # Place a protective stop at the venue. If this process dies, the stop
        # is still there. A bot whose only stop lives in its own memory is one
        # crash away from an unbounded loss.
        try:
            await self.exchange.place_order(
                symbol=symbol.venue_symbol,
                side=direction.exit_side,
                type=OrderType.STOP_MARKET,
                qty=pos.qty,
                stop_price=sizing.stop_price,
                reduce_only=True,
                client_id=f"simin-stop-{name}-{self._bar_index}",
            )
        except ExchangeError as exc:
            self._emit(
                "stop_order_failed", name,
                f"{name}: WARNING — venue rejected the protective stop ({exc}). "
                "The position is managed by this process only; if it dies the "
                "position is unprotected.",
                f"{name}: هشدار — صرافی حد ضرر محافظ را رد کرد.",
            )

        self._emit(
            "entry", name,
            f"{name} {direction.value.upper()} {pos.qty} @ {fill} "
            f"({sizing.leverage}x, stop {sizing.stop_price}, risking "
            f"{sizing.risk_amount:.2f})",
            f"{name} {direction.value} به مقدار {pos.qty} در قیمت {fill}",
            {
                "direction": direction.value,
                "qty": float(pos.qty),
                "price": float(fill),
                "stop": float(sizing.stop_price),
                "take_profit": float(sizing.take_profit) if sizing.take_profit else None,
                "leverage": float(sizing.leverage),
                "liquidation": float(sizing.liquidation_price),
                "confidence": decision.intent.confidence,
                "reasons": list(decision.intent.reasons),
                "strategy": decision.intent.strategy,
            },
        )

    async def _close(
        self, name: str, symbol: Symbol, reason: ExitReason, price: Decimal
    ) -> None:
        pos = self.account.positions.get(name)
        if pos is None:
            return
        try:
            order = await self.exchange.place_order(
                symbol=symbol.venue_symbol,
                side=pos.direction.exit_side,
                type=OrderType.MARKET,
                qty=pos.qty,
                reduce_only=True,
                client_id=f"simin-exit-{name}-{self._bar_index}",
            )
            exit_price = order.avg_price if order.avg_price > 0 else price
            exit_fee = order.fee if order.fee > 0 else self.costs.fee(pos.qty * exit_price)
        except ExchangeError as exc:
            # A close that keeps failing is the most dangerous state the bot can
            # be in: the stop has fired, the position is still on, and the loss
            # is still growing. Retrying quietly on every poll turns that into a
            # wall of identical log lines nobody reads. Count the failures and
            # escalate, so a stuck exit is impossible to mistake for noise.
            self._exit_failures[name] = self._exit_failures.get(name, 0) + 1
            attempts = self._exit_failures[name]
            if attempts <= 2 or attempts % 10 == 0:
                severe = attempts >= self.STUCK_EXIT_ALARM
                self._emit(
                    "exit_stuck" if severe else "exit_failed",
                    name,
                    (
                        f"{name}: CANNOT CLOSE after {attempts} attempts — {exc}. "
                        "The position is still open and its loss is still growing. "
                        "Close it manually on the exchange."
                        if severe
                        else f"{name}: could not close — {exc}. Will retry."
                    ),
                    (
                        f"{name}: پس از {attempts} تلاش بسته نشد. موقعیت باز است و "
                        "زیان آن در حال افزایش. آن را دستی در صرافی ببندید."
                        if severe
                        else f"{name}: بستن موقعیت ناموفق بود، دوباره تلاش می‌شود."
                    ),
                    {"attempts": attempts, "error": str(exc)},
                )
            return
        else:
            self._exit_failures.pop(name, None)

        gross = (exit_price - pos.entry_price) * pos.qty * pos.direction.sign
        net = gross - exit_fee - pos.funding_paid
        self.account.cash += gross - exit_fee

        trade = Trade(
            symbol=name, direction=pos.direction, qty=pos.qty,
            entry_price=pos.entry_price, exit_price=exit_price,
            opened_at=pos.opened_at, closed_at=datetime.now(UTC),
            gross_pnl=gross, fees=pos.fees_paid + exit_fee, funding=pos.funding_paid,
            net_pnl=net,
            r_multiple=net / pos.risk_amount if pos.risk_amount > 0 else ZERO,
            reason=reason, strategy=pos.strategy, risk_level=pos.risk_level,
            leverage=pos.leverage, bars_held=pos.bars_held,
            max_favorable=pos.max_favorable, max_adverse=pos.max_adverse,
        )
        self.trades.append(trade)
        del self.account.positions[name]
        self.risk.record_close(self.account, trade, self._bar_index)

        self._emit(
            "exit", name,
            f"{name} closed at {exit_price} ({reason.value}): "
            f"{net:+.2f} = {trade.r_multiple:+.2f}R",
            f"{name} بسته شد در {exit_price}: {net:+.2f}",
            {
                "reason": reason.value, "pnl": float(net),
                "r": float(trade.r_multiple), "exit_price": float(exit_price),
            },
        )

    async def flatten_all(self, reason: ExitReason = ExitReason.MANUAL) -> int:
        closed = 0
        for name in list(self.account.positions):
            symbol = self.symbols[name]
            try:
                ticker = await self.exchange.ticker(symbol.venue_symbol)
                await self._close(name, symbol, reason, ticker.last)
                closed += 1
            except ExchangeError as exc:
                self._emit("flatten_failed", name, f"{name}: {exc}", "")
        return closed

    async def _mark(self, now: datetime) -> None:
        unrealised = ZERO
        for name, pos in self.account.positions.items():
            try:
                ticker = await self.exchange.ticker(self.symbols[name].venue_symbol)
            except ExchangeError:
                continue
            unrealised += pos.unrealized(ticker.last)
        self.account.equity = self.account.cash + unrealised
        self.account.peak_equity = max(self.account.peak_equity, self.account.equity)
        self.curve.append(
            EquityPoint(
                ts=now, equity=self.account.equity, cash=self.account.cash,
                exposure=self.account.gross_exposure,
                open_positions=len(self.account.positions),
                drawdown=self.account.drawdown,
            )
        )
        if len(self.curve) > 5000:
            del self.curve[: len(self.curve) - 5000]

    # --- Reporting --------------------------------------------------------

    def _emit(
        self, kind: str, symbol: str, message: str, message_fa: str = "",
        data: dict[str, object] | None = None,
    ) -> None:
        event = Event(datetime.now(UTC), kind, symbol, message, message_fa, data or {})
        self.events.append(event)
        if len(self.events) > self._max_events:
            del self.events[: len(self.events) - self._max_events]
        log.info(kind, symbol=symbol, message=message)

    def status(self) -> RunnerStatus:
        return RunnerStatus(
            state=self.state,
            mode=self.settings.mode,
            # A paper adapter reports where its prices actually come from, so
            # "CoinEx (paper)" and "offline (paper)" never look the same.
            venue=getattr(self.exchange, "source_name", self.exchange.name),
            venue_key=self.settings.venue,
            risk_level=self.profile.level,
            profile_name=self.profile.name_en,
            started_at=self.started_at,
            last_bar=max(self._last_bar.values()) if self._last_bar else None,
            equity=self.account.equity,
            starting_equity=self.settings.starting_equity,
            cash=self.account.cash,
            open_positions=len(self.account.positions),
            drawdown=self.account.drawdown,
            day_realised=self.account.day_realised,
            trades_today=self.account.trades_today,
            total_trades=len(self.trades),
            halt=self.account.halt,
            halt_note=self.account.halt_note,
            error=self.error,
            profile_clamped=self.profile_clamped,
        )

    def recent_events(self, limit: int = 50) -> list[dict[str, object]]:
        return [e.to_dict() for e in self.events[-limit:]][::-1]

    def positions_view(self) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for name, pos in self.account.positions.items():
            frame = self._frames.get(name)
            price = frame.candles[-1].close if frame and frame.candles else pos.entry_price
            # An unleveraged position has no liquidation price — the model
            # returns infinity for a short, and JSON has no infinity, so it
            # would silently become null. Sending null deliberately is the same
            # value with an explicit meaning the UI can rely on.
            liq = pos.liquidation_price()
            out.append({
                "symbol": name,
                "direction": pos.direction.value,
                "qty": float(pos.qty),
                "entry_price": float(pos.entry_price),
                "current_price": float(price),
                "stop_price": float(pos.stop_price),
                "take_profit": float(pos.take_profit) if pos.take_profit else None,
                "liquidation_price": float(liq) if liq.is_finite() and liq > 0 else None,
                "leverage": float(pos.leverage),
                "unrealized": float(pos.unrealized(price)),
                "r_multiple": float(pos.r_multiple(price)),
                "opened_at": pos.opened_at.isoformat(),
                "bars_held": pos.bars_held,
                "strategy": pos.strategy,
                "risk_amount": float(pos.risk_amount),
                "breakeven_armed": pos.breakeven_armed,
            })
        return out
