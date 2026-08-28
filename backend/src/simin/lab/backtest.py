"""The event-driven backtester.

The whole design exists to make one class of bug impossible: acting on
information that did not exist yet.

The bar loop is therefore strictly ordered, and the order is the point:

    for bar i:
      1. Fill any entry decided at bar i-1, at bar i's OPEN.
      2. Mark open positions against bar i's high/low/close; exit if hit.
      3. Evaluate strategies against bar i's CLOSE.
      4. Queue any resulting entry for bar i+1.

Step 3 happens after step 2, and its result cannot be acted on until step 1 of
the next bar. That single-bar delay is what a real bot experiences — you cannot
see a candle close and simultaneously trade at that close — and removing it is
the most common way a backtest invents returns that never existed.

Within a bar, when both the stop and the target were touched, the stop is
assumed to have come first. We cannot know the intrabar path from OHLC, and
assuming the favourable one is how a losing system backtests profitably.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from simin.core.types import (
    TF,
    Candle,
    Direction,
    EquityPoint,
    ExitReason,
    Intent,
    Position,
    Side,
    Symbol,
    Trade,
    ZERO,
)
from simin.exchanges.costs import CostModel
from simin.indicators.features import FeatureFrame
from simin.lab.metrics import Metrics, compute
from simin.risk.dial import RiskProfile
from simin.risk.engine import AccountState, Halt, RiskEngine, Rejection
from simin.strategies.base import Context, Strategy
from simin.strategies.ensemble import Decision, Ensemble


@dataclass(slots=True)
class PendingEntry:
    """A decision made at bar i, to be filled at bar i+1's open."""

    symbol: str
    intent: Intent
    decided_at_bar: int
    decision: Decision


@dataclass(slots=True)
class BacktestResult:
    metrics: Metrics
    trades: list[Trade]
    curve: list[EquityPoint]
    profile_level: int
    symbols: tuple[str, ...]
    timeframe: str
    bars: int
    rejections: dict[str, int] = field(default_factory=dict)
    signals_generated: int = 0
    signals_taken: int = 0
    halted_at: str = ""
    halt_reason: str = ""
    #: Decisions kept for the UI's "why did it do that" view.
    decision_log: list[dict[str, object]] = field(default_factory=list)

    @property
    def signal_take_rate(self) -> float:
        return self.signals_taken / self.signals_generated if self.signals_generated else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "metrics": self.metrics.to_dict(),
            "profile_level": self.profile_level,
            "symbols": list(self.symbols),
            "timeframe": self.timeframe,
            "bars": self.bars,
            "rejections": self.rejections,
            "signals_generated": self.signals_generated,
            "signals_taken": self.signals_taken,
            "signal_take_rate": self.signal_take_rate,
            "halted_at": self.halted_at,
            "halt_reason": self.halt_reason,
            "trades": [
                {
                    "symbol": t.symbol,
                    "direction": t.direction.value,
                    "entry_price": float(t.entry_price),
                    "exit_price": float(t.exit_price),
                    "opened_at": t.opened_at.isoformat(),
                    "closed_at": t.closed_at.isoformat(),
                    "net_pnl": float(t.net_pnl),
                    "r_multiple": float(t.r_multiple),
                    "reason": t.reason.value,
                    "strategy": t.strategy,
                    "leverage": float(t.leverage),
                    "bars_held": t.bars_held,
                    "fees": float(t.fees),
                }
                for t in self.trades
            ],
            "curve": [
                {
                    "ts": p.ts.isoformat(),
                    "equity": float(p.equity),
                    "drawdown": float(p.drawdown),
                    "positions": p.open_positions,
                }
                for p in self.curve
            ],
        }


class Backtester:
    """Runs one configuration over history."""

    def __init__(
        self,
        profile: RiskProfile,
        strategies: Sequence[Strategy],
        costs: CostModel,
        starting_equity: Decimal = Decimal("10000"),
        max_capital: Decimal = ZERO,
        keep_decision_log: bool = False,
    ) -> None:
        self.profile = profile
        self.costs = costs
        self.starting_equity = starting_equity
        self.risk = RiskEngine(profile, max_capital)
        self._strategies = list(strategies)
        self._keep_log = keep_decision_log

    def run(
        self,
        frames: dict[str, FeatureFrame],
        symbols: dict[str, Symbol],
        tf: TF,
        context_frames: dict[str, FeatureFrame] | None = None,
    ) -> BacktestResult:
        if not frames:
            raise ValueError("backtest needs at least one symbol")

        # All symbols must share a bar grid, otherwise "bar i" means different
        # times for different symbols and the portfolio equity is nonsense.
        lengths = {name: len(f) for name, f in frames.items()}
        n = min(lengths.values())
        if n < 50:
            raise ValueError(f"not enough bars to backtest: {lengths}")

        ensemble = Ensemble(self._strategies, self.profile)
        warmup = max(
            max(f.warmup_complete_at() for f in frames.values()),
            ensemble.warmup,
        )
        if warmup >= n - 10:
            raise ValueError(
                f"warm-up needs {warmup} bars but only {n} are available; "
                "fetch more history"
            )

        acct = AccountState(
            cash=self.starting_equity,
            equity=self.starting_equity,
            peak_equity=self.starting_equity,
            day_start_equity=self.starting_equity,
        )
        acct.day = frames[next(iter(frames))].candles[warmup].ts.date()

        trades: list[Trade] = []
        curve: list[EquityPoint] = []
        rejections: dict[str, int] = {}
        decision_log: list[dict[str, object]] = []
        pending: dict[str, PendingEntry] = {}
        signals = taken = 0
        halted_at = halt_reason = ""

        names = sorted(frames)

        for i in range(warmup, n):
            ts = frames[names[0]].candles[i].ts
            acct.roll_day(ts)

            # --- 1. Fill entries decided on the previous bar ----------------
            for name, entry in list(pending.items()):
                if entry.decided_at_bar != i - 1:
                    del pending[name]
                    continue
                del pending[name]
                candle = frames[name].candles[i]
                # Fill at this bar's OPEN — the first price actually reachable
                # after the decision.
                took = self._try_enter(
                    entry.intent, symbols[name], candle.open, acct, i, ts,
                    frames[name].row(i).get("atr"), rejections,
                )
                if took is not None:
                    acct.positions[name] = took
                    acct.trades_today += 1
                    taken += 1
                    if self._keep_log:
                        decision_log.append({
                            "ts": ts.isoformat(), "symbol": name, "action": "entry",
                            "direction": took.direction.value,
                            "price": float(took.entry_price),
                            "confidence": entry.intent.confidence,
                            "reasons": list(entry.intent.reasons),
                            "leverage": float(took.leverage),
                        })

            # --- 2. Mark open positions and exit ---------------------------
            for name in list(acct.positions):
                pos = acct.positions[name]
                candle = frames[name].candles[i]
                pos.bars_held += 1
                atr_now = frames[name].row(i).get("atr")
                atr_dec = Decimal(str(atr_now)) if atr_now else None

                new_stop, reason = self.risk.manage(
                    pos, candle.high, candle.low, candle.close, atr_dec
                )
                pos.stop_price = new_stop

                if reason is not None:
                    trade = self._close(pos, candle, reason, ts, acct, i)
                    trades.append(trade)
                    self.risk.record_close(acct, trade, i)
                    del acct.positions[name]
                    if self._keep_log:
                        decision_log.append({
                            "ts": ts.isoformat(), "symbol": name, "action": "exit",
                            "reason": reason.value, "pnl": float(trade.net_pnl),
                            "r": float(trade.r_multiple),
                        })

            # --- 3. Mark to market -----------------------------------------
            unrealised = ZERO
            for name, pos in acct.positions.items():
                close = frames[name].candles[i].close
                unrealised += pos.unrealized(close)
                # Funding accrues on leveraged positions, every bar.
                if pos.leverage > 1:
                    hours = tf.seconds / 3600.0
                    f = self.costs.funding_cost(pos.notional, pos.direction, hours)
                    pos.funding_paid += f
                    acct.cash -= f
            acct.equity = acct.cash + unrealised
            acct.peak_equity = max(acct.peak_equity, acct.equity)

            curve.append(
                EquityPoint(
                    ts=ts,
                    equity=acct.equity,
                    cash=acct.cash,
                    exposure=acct.gross_exposure,
                    open_positions=len(acct.positions),
                    drawdown=acct.drawdown,
                )
            )

            # --- 4. Halt check ---------------------------------------------
            halt = self.risk.check_halts(acct)
            if halt.is_halted:
                if halt.is_permanent:
                    acct.halt = halt
                    halted_at = ts.isoformat()
                    halt_reason = halt.value
                    # Flatten everything: a halted bot holding leveraged
                    # positions is not halted, it is unsupervised.
                    for name in list(acct.positions):
                        pos = acct.positions[name]
                        trade = self._close(
                            pos, frames[name].candles[i], ExitReason.KILL_SWITCH, ts, acct, i
                        )
                        trades.append(trade)
                        del acct.positions[name]
                    break
                continue  # daily / streak halts: skip entries, keep marking

            # --- 5. Decide (on this bar's close), queue for the next bar ----
            if i >= n - 1:
                continue
            for name in names:
                if name in acct.positions or name in pending:
                    continue
                row = frames[name].row(i)
                ctx_row = None
                if context_frames and name in context_frames:
                    cf = context_frames[name]
                    j = _align(cf, row.ts)
                    if j is not None:
                        ctx_row = cf.row(j)
                ctx = Context(
                    row=row,
                    context_row=ctx_row,
                    symbol=name,
                    position=None,
                    bar_index=i,
                    allow_shorts=self.profile.allow_shorts,
                    allow_counter_trend=self.profile.allow_counter_trend,
                )
                decision = ensemble.decide(ctx)
                if not decision.accepted:
                    continue
                signals += 1
                pending[name] = PendingEntry(name, decision.intent, i, decision)

        # Close anything still open at the end, at the last close.
        final_i = min(len(curve) + warmup - 1, n - 1)
        for name in list(acct.positions):
            pos = acct.positions[name]
            candle = frames[name].candles[final_i]
            trades.append(
                self._close(pos, candle, ExitReason.SESSION_END, candle.ts, acct, final_i)
            )
            del acct.positions[name]
        if trades and curve:
            acct.equity = acct.cash
            curve[-1] = EquityPoint(
                ts=curve[-1].ts, equity=acct.equity, cash=acct.cash,
                exposure=ZERO, open_positions=0, drawdown=acct.drawdown,
            )

        periods_per_year = 365.0 * tf.per_day
        return BacktestResult(
            metrics=compute(trades, curve, self.starting_equity, periods_per_year),
            trades=trades,
            curve=curve,
            profile_level=self.profile.level,
            symbols=tuple(names),
            timeframe=tf.value,
            bars=len(curve),
            rejections=dict(sorted(rejections.items(), key=lambda kv: -kv[1])),
            signals_generated=signals,
            signals_taken=taken,
            halted_at=halted_at,
            halt_reason=halt_reason,
            decision_log=decision_log,
        )

    # --- Internals --------------------------------------------------------

    def _try_enter(
        self,
        intent: Intent,
        symbol: Symbol,
        open_price: Decimal,
        acct: AccountState,
        bar: int,
        ts: datetime,
        atr: float | None,
        rejections: dict[str, int],
    ) -> Position | None:
        direction = intent.direction
        if direction is None:
            return None
        side = direction.entry_side
        fill = self.costs.fill_price(open_price, side)

        # The stop was computed from the previous close. Re-validate it against
        # the actual fill: a gap through the stop overnight means the trade is
        # already wrong before it starts, and taking it is a guaranteed loss.
        stop = intent.stop_price
        if stop is None:
            return None
        if (direction is Direction.LONG and fill <= stop) or (
            direction is Direction.SHORT and fill >= stop
        ):
            rejections["gapped_through_stop"] = rejections.get("gapped_through_stop", 0) + 1
            return None

        sizing = self.risk.size(intent, symbol, fill, acct, bar, atr=None)
        if not sizing.approved:
            key = sizing.rejection.value
            rejections[key] = rejections.get(key, 0) + 1
            return None

        fee = self.costs.fee(sizing.notional)
        acct.cash -= fee
        return Position(
            symbol=symbol.name,
            direction=direction,
            qty=sizing.qty,
            entry_price=fill,
            stop_price=sizing.stop_price,
            take_profit=sizing.take_profit,
            leverage=sizing.leverage,
            opened_at=ts,
            strategy=intent.strategy,
            risk_level=self.profile.level,
            risk_amount=sizing.risk_amount,
            initial_stop=sizing.stop_price,
            fees_paid=fee,
        )

    def _close(
        self,
        pos: Position,
        candle: Candle,
        reason: ExitReason,
        ts: datetime,
        acct: AccountState,
        bar: int,
    ) -> Trade:
        """Realise a position. The exit price depends on *why* it closed."""
        is_stop = reason in (
            ExitReason.STOP_LOSS,
            ExitReason.TRAILING_STOP,
            ExitReason.LIQUIDATION,
        )
        if reason in (ExitReason.STOP_LOSS, ExitReason.TRAILING_STOP):
            reference = pos.stop_price
        elif reason is ExitReason.TAKE_PROFIT and pos.take_profit is not None:
            reference = pos.take_profit
        elif reason is ExitReason.LIQUIDATION:
            reference = pos.liquidation_price()
        else:
            reference = candle.close

        # A stop cannot fill better than the bar allowed. If price gapped
        # straight past it, the fill is the open, not the stop price — this is
        # the difference between a modelled −1R and a real −4R.
        if is_stop:
            if pos.direction is Direction.LONG:
                reference = min(reference, candle.open) if candle.open < reference else reference
            else:
                reference = max(reference, candle.open) if candle.open > reference else reference

        exit_price = self.costs.fill_price(
            reference, pos.direction.exit_side, is_stop=is_stop
        )
        exit_price = max(exit_price, Decimal("0.00000001"))

        gross = (exit_price - pos.entry_price) * pos.qty * pos.direction.sign
        exit_fee = self.costs.fee(pos.qty * exit_price)
        fees = pos.fees_paid + exit_fee
        net = gross - exit_fee - pos.funding_paid

        acct.cash += gross - exit_fee
        r = net / pos.risk_amount if pos.risk_amount > 0 else ZERO

        return Trade(
            symbol=pos.symbol,
            direction=pos.direction,
            qty=pos.qty,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            opened_at=pos.opened_at,
            closed_at=ts,
            gross_pnl=gross,
            fees=fees,
            funding=pos.funding_paid,
            net_pnl=net,
            r_multiple=r,
            reason=reason,
            strategy=pos.strategy,
            risk_level=pos.risk_level,
            leverage=pos.leverage,
            bars_held=pos.bars_held,
            max_favorable=pos.max_favorable,
            max_adverse=pos.max_adverse,
        )


def _align(frame: FeatureFrame, ts: datetime) -> int | None:
    """Latest context bar that had already CLOSED at time `ts`.

    Binary search rather than a scan, and strictly `<` rather than `<=`: the
    4h bar containing the current 15m bar has not closed yet, and using it is
    reading the future one timeframe up — the subtlest lookahead bug there is,
    because it produces a backtest that looks merely very good rather than
    impossible.
    """
    candles = frame.candles
    lo, hi = 0, len(candles) - 1
    best: int | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if candles[mid].ts + frame.tf.delta <= ts:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best
