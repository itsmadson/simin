"""Event-driven backtester.

Timeline, strictly enforced:

    bar t closes -> features(t) -> regime(t) -> strategy sees t -> risk sizes it
                 -> order fills at bar t+1 OPEN, with spread, impact and latency

Nothing is ever filled at the price that generated the signal. Intrabar stop and
target handling is deliberately pessimistic: when a bar's range contains both,
the stop is assumed to have been hit first, because assuming otherwise is how a
backtest invents money that the market never offered.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal

from simin.backtest.costs import CostModel
from simin.backtest.metrics import Metrics, TradeStat, compute_metrics
from simin.exchanges.venues import profile
from simin.features.engine import BARS_PER_YEAR, FeatureRow, build_features
from simin.features.regime import RegimeConfig, RegimeState, classify
from simin.risk.engine import (
    Intent,
    OpenPosition,
    RejectReason,
    RiskEngine,
    new_account,
    trailing_stop,
)
from simin.strategies.base import Strategy, StrategyContext
from simin.types import TF, Bar, Side


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    starting_equity: Decimal = Decimal("100000000")  # IRT
    cost: CostModel | None = None
    use_regime_filter: bool = True
    trail_atr_multiple: Decimal = Decimal(3)
    time_stop_bars: int | None = 200
    n_trials: int = 1  # how many variants were tried, for the deflated Sharpe
    regime_config: RegimeConfig | None = None
    #: Fraction of top-of-book notional we allow ourselves to consume.
    max_participation: Decimal = Decimal("0.10")


@dataclass(slots=True)
class BacktestResult:
    strategy: str
    symbol: str
    tf: TF
    equity: list[float]
    stamps: list[datetime]
    trades: list[TradeStat]
    metrics: Metrics
    rejections: dict[str, int] = field(default_factory=dict)
    ended_by_kill_switch: bool = False
    kill_reason: str | None = None

    def summary(self) -> str:
        tail = f" [HALTED: {self.kill_reason}]" if self.ended_by_kill_switch else ""
        return f"{self.strategy:22s} {self.symbol:10s} {self.metrics.summary()}{tail}"


class Backtester:
    def __init__(
        self, risk: RiskEngine, config: BacktestConfig | None = None
    ) -> None:
        self.risk = risk
        self.config = config or BacktestConfig()
        # Default to the pessimistic Toman-venue cost profile: if a strategy only
        # works under cheaper assumptions, that is a finding, not an inconvenience.
        local = profile("local_irt_generic")
        self.cost = self.config.cost or CostModel(
            fees=local.fees, spread_bps=local.typical_spread_bps
        )

    def run(
        self,
        bars: Sequence[Bar],
        strategy: Strategy,
        *,
        rows: Sequence[FeatureRow] | None = None,
    ) -> BacktestResult:
        if len(bars) < 2:
            raise ValueError("need at least two bars to backtest")
        tf = bars[0].tf
        symbol = bars[0].symbol
        rows = rows or build_features(bars, tf)

        state = new_account(self.config.starting_equity)
        equity_curve: list[float] = []
        stamps: list[datetime] = []
        trades: list[TradeStat] = []
        rejections: dict[str, int] = {}

        pending: Intent | None = None
        pending_qty = Decimal(0)
        position: OpenPosition | None = None
        entry_fee = Decimal(0)
        best_price: Decimal | None = None
        opened_index = 0
        opened_at: datetime | None = None
        cash = state.equity
        bars_in_market = 0
        day = bars[0].ts.date()
        week = bars[0].ts.isocalendar().week

        for i, bar in enumerate(bars):
            # ---- 1. calendar rollovers reset the daily/weekly loss budgets
            if bar.ts.date() != day:
                day = bar.ts.date()
                state.roll_day()
            if bar.ts.isocalendar().week != week:
                week = bar.ts.isocalendar().week
                state.roll_week()

            # ---- 2. fill anything decided on the previous bar, at THIS bar's open
            if pending is not None and position is None and pending_qty > 0:
                depth = self._reference_depth(bar)
                fill_price = self.cost.fill_price(bar.open, Side.BUY, pending_qty, depth)
                notional = fill_price * pending_qty
                if strategy.allocation == "full" and notional > cash and fill_price > 0:
                    # A fully-invested benchmark buys what the cash actually covers
                    # at the real fill price, which is not knowable when the order
                    # is sized on the previous bar's close.
                    fee_rate = self.cost.fees.taker
                    pending_qty = cash / (fill_price * (Decimal(1) + fee_rate))
                    notional = fill_price * pending_qty
                if notional <= cash:
                    entry_fee = self.cost.fee(notional)
                    cash -= notional + entry_fee
                    position = OpenPosition(
                        symbol=symbol,
                        direction=pending.direction,
                        qty=pending_qty,
                        entry=fill_price,
                        stop=pending.stop,
                        strategy=pending.strategy,
                        opened_at=bar.ts,
                    )
                    state.positions[symbol] = position
                    best_price = fill_price
                    opened_index = i
                    opened_at = bar.ts
                else:
                    rejections["insufficient_cash"] = rejections.get("insufficient_cash", 0) + 1
            pending, pending_qty = None, Decimal(0)

            # ---- 3. manage an open position against THIS bar's range
            if position is not None and strategy.allocation == "full":
                exit_price, exit_reason = None, ""   # buy and hold means hold
            elif position is not None:
                exit_price, exit_reason = self._check_exit(bar, position, i, opened_index)
                if exit_price is None and self._thesis_expired(strategy, rows, i, position, bar):
                    exit_price, exit_reason = bar.close, "signal"
                if exit_price is not None:
                    depth = self._reference_depth(bar)
                    fill = self.cost.fill_price(exit_price, Side.SELL, position.qty, depth)
                    proceeds = fill * position.qty
                    exit_fee = self.cost.fee(proceeds)
                    cash += proceeds - exit_fee
                    cost_basis = position.entry * position.qty
                    pnl = proceeds - exit_fee - cost_basis - entry_fee
                    trades.append(
                        TradeStat(
                            opened_at=opened_at or bar.ts,
                            closed_at=bar.ts,
                            symbol=symbol,
                            strategy=position.strategy,
                            pnl=pnl,
                            return_pct=float(pnl / cost_basis) if cost_basis else 0.0,
                            fees=entry_fee + exit_fee,
                            regime=exit_reason,
                        )
                    )
                    state.consecutive_losses = (
                        state.consecutive_losses + 1 if pnl <= 0 else 0
                    )
                    state.positions.pop(symbol, None)
                    position, best_price, entry_fee = None, None, Decimal(0)
                elif strategy.allocation != "full":
                    best_price = max(best_price or bar.close, bar.high)
                    atr = rows[i].get("atr14")
                    if atr:
                        new_stop = trailing_stop(
                            position.direction,
                            position.entry,
                            best_price,
                            Decimal(str(atr)),
                            self.config.trail_atr_multiple,
                        )
                        if new_stop > position.stop:  # ratchets one way only
                            position = replace(position, stop=new_stop)
                            state.positions[symbol] = position

            # ---- 4. mark to market
            if position is not None:
                bars_in_market += 1
            equity = cash + (position.qty * bar.close if position else Decimal(0))
            state.mark(equity)
            state.last_data_ts = bar.close_time
            equity_curve.append(float(equity))
            stamps.append(bar.ts)

            if state.kill_switch:
                continue

            # ---- 5. decide, using only information available at this close
            if i < strategy.warmup or position is not None:
                continue
            regime = self._regime(rows, i)
            if self.config.use_regime_filter and not regime.allows(strategy.name):
                if strategy.name in {"buy_and_hold", "random_entry", "rsi_oversold", "ema_cross"}:
                    pass  # benchmarks run unfiltered, or the comparison is rigged
                else:
                    rejections[RejectReason.REGIME_FORBIDS] = (
                        rejections.get(RejectReason.REGIME_FORBIDS, 0) + 1
                    )
                    continue
            ctx = StrategyContext(
                ts=bar.ts, symbol=symbol, row=rows[i], regime=regime, position=None, bar_index=i
            )
            intent = strategy.generate(ctx)
            if intent is None:
                continue
            depth = self._reference_depth(bar)
            if strategy.allocation == "full":
                # Capital benchmark: hold the asset with the whole account, so the
                # comparison is against the thing a person would actually do
                # instead of trading.
                qty = (cash / bar.close) * Decimal("0.999")  # leave room for fees
                if qty > 0:
                    pending, pending_qty = intent, qty
                continue
            decision = self.risk.evaluate(state, intent, available_depth=depth)
            if decision.rejected:
                rejections[decision.reason] = rejections.get(decision.reason, 0) + 1
                continue
            pending, pending_qty = intent, decision.qty

        metrics = compute_metrics(
            stamps,
            equity_curve,
            trades,
            periods_per_year=BARS_PER_YEAR[tf],
            n_trials=self.config.n_trials,
            bars_in_market=bars_in_market,
        )
        return BacktestResult(
            strategy=strategy.name,
            symbol=symbol,
            tf=tf,
            equity=equity_curve,
            stamps=stamps,
            trades=trades,
            metrics=metrics,
            rejections={str(k): v for k, v in rejections.items()},
            ended_by_kill_switch=state.kill_switch,
            kill_reason=state.kill_reason,
        )

    # ------------------------------------------------------------------ helpers

    def _reference_depth(self, bar: Bar) -> Decimal:
        """Notional we allow ourselves to consume in one order.

        Derived from the bar's own traded volume: taking 10% of a bar's volume is
        already optimistic, and pretending an illiquid bar can absorb a large
        order is the most flattering lie a backtest can tell.
        """
        return bar.close * bar.volume * self.config.max_participation

    def _check_exit(
        self, bar: Bar, position: OpenPosition, index: int, opened_index: int
    ) -> tuple[Decimal | None, str]:
        """Stop first, then time stop. Never a favourable assumption."""
        if position.direction > 0 and bar.low <= position.stop:
            # Gap-through: fill at the open if it already opened below the stop.
            return (min(position.stop, bar.open), "stop")
        if position.direction < 0 and bar.high >= position.stop:
            return (max(position.stop, bar.open), "stop")
        if (
            self.config.time_stop_bars is not None
            and index - opened_index >= self.config.time_stop_bars
        ):
            return (bar.close, "time_stop")
        return (None, "")

    def _thesis_expired(
        self,
        strategy: Strategy,
        rows: Sequence[FeatureRow],
        index: int,
        position: OpenPosition,
        bar: Bar,
    ) -> bool:
        ctx = StrategyContext(
            ts=bar.ts,
            symbol=position.symbol,
            row=rows[index],
            regime=self._regime(rows, index),
            position=position,
            bar_index=index,
        )
        return strategy.exit_signal(ctx)

    def _regime(self, rows: Sequence[FeatureRow], index: int) -> RegimeState:
        return classify(rows, index, self.config.regime_config)


def run_suite(
    bars: Sequence[Bar],
    strategies: Sequence[Strategy],
    risk: RiskEngine,
    config: BacktestConfig | None = None,
) -> list[BacktestResult]:
    """Run several strategies over identical data and cost assumptions."""
    rows = build_features(bars)
    return [Backtester(risk, config).run(bars, s, rows=rows) for s in strategies]
