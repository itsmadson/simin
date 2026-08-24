"""Portfolio backtester: many symbols, one account, one risk engine.

The single-symbol backtester answers "does this signal work?". It cannot answer
the question that actually decides whether a system is tradeable day to day:
*how often do entries arrive across a universe, and does the account survive
holding several of them at once?*

Breadth is also the only honest way to raise trade frequency. Trading one symbol
more often means shorter holds, and on real data the edge below ~12 hours is
indistinguishable from zero — so higher frequency there is a fee-payment
schedule, not a strategy. Trading twenty symbols on a 2-4 day signal produces
entries most days while every position still respects its holding ceiling.

Timeline handling is the delicate part: symbols are merged into one clock and a
bar is only visible once it has closed, so a fast-moving symbol can never leak
information into a slower one's decision.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal

from simin.backtest.costs import CostModel
from simin.backtest.metrics import Metrics, TradeStat, compute_metrics
from simin.features.engine import BARS_PER_YEAR, FeatureRow, build_features
from simin.features.regime import RegimeConfig, classify
from simin.risk.engine import (
    Intent,
    OpenPosition,
    RiskEngine,
    new_account,
)
from simin.strategies.base import Strategy, StrategyContext
from simin.types import TF, Bar, Side


@dataclass(frozen=True, slots=True)
class PortfolioConfig:
    starting_equity: Decimal = Decimal("100000000")
    cost: CostModel | None = None
    use_regime_filter: bool = True
    trail_atr_multiple: Decimal = Decimal(3)
    #: Hard holding ceiling in bars. With 1h bars, 96 = 4 days.
    max_hold_bars: int = 96
    max_participation: Decimal = Decimal("0.10")
    regime_config: RegimeConfig | None = None
    n_trials: int = 1


@dataclass(slots=True)
class PortfolioResult:
    strategies: list[str]
    symbols: list[str]
    tf: TF
    stamps: list[datetime]
    equity: list[float]
    trades: list[TradeStat]
    metrics: Metrics
    rejections: dict[str, int] = field(default_factory=dict)
    trades_per_day: float = 0.0
    active_days: int = 0
    total_days: int = 0
    avg_hold_bars: float = 0.0
    max_hold_bars_seen: int = 0
    daily_returns: dict[str, float] = field(default_factory=dict)

    @property
    def day_coverage(self) -> float:
        """Fraction of days with at least one entry or exit — 'is it doing anything?'"""
        return self.active_days / self.total_days if self.total_days else 0.0

    def summary(self) -> str:
        m = self.metrics
        return (
            f"ret={m.total_return:>8.2%} cagr={m.cagr:>7.2%} sharpe={m.sharpe:>5.2f} "
            f"dd={m.max_drawdown:>7.2%} trades={m.n_trades:>4} "
            f"{self.trades_per_day:.2f}/day active-days={self.day_coverage:.0%} "
            f"hold~{self.avg_hold_bars:.0f}h(max {self.max_hold_bars_seen}h) "
            f"fees={m.fee_drag:.0%}gross"
        )


class PortfolioBacktester:
    def __init__(self, risk: RiskEngine, config: PortfolioConfig | None = None) -> None:
        self.risk = risk
        self.config = config or PortfolioConfig()
        if self.config.cost is None:
            from simin.exchanges.venues import profile

            local = profile("local_irt_generic")
            self.cost = CostModel(fees=local.fees, spread_bps=local.typical_spread_bps)
        else:
            self.cost = self.config.cost

    def run(
        self, series: Mapping[str, Sequence[Bar]], strategies: Sequence[Strategy]
    ) -> PortfolioResult:
        if not series:
            raise ValueError("no symbols supplied")
        tf = next(iter(series.values()))[0].tf
        features: dict[str, list[FeatureRow]] = {
            sym: build_features(bars, tf) for sym, bars in series.items()
        }
        index_of: dict[str, dict[datetime, int]] = {
            sym: {row.ts: i for i, row in enumerate(rows)} for sym, rows in features.items()
        }
        bar_at: dict[str, dict[datetime, Bar]] = {
            sym: {b.ts: b for b in bars} for sym, bars in series.items()
        }

        timeline = sorted({b.ts for bars in series.values() for b in bars})
        state = new_account(self.config.starting_equity)
        cash = state.equity
        positions: dict[str, OpenPosition] = {}
        entry_fees: dict[str, Decimal] = {}
        opened_index: dict[str, int] = {}
        best_price: dict[str, Decimal] = {}
        pending: list[tuple[str, Intent, Decimal]] = []

        trades: list[TradeStat] = []
        rejections: dict[str, int] = defaultdict(int)
        equity_curve: list[float] = []
        stamps: list[datetime] = []
        holds: list[int] = []
        active_dates: set[date] = set()
        all_dates: set[date] = set()
        day = timeline[0].date()
        week = timeline[0].isocalendar().week

        for step, ts in enumerate(timeline):
            all_dates.add(ts.date())
            if ts.date() != day:
                day = ts.date()
                state.roll_day()
            if ts.isocalendar().week != week:
                week = ts.isocalendar().week
                state.roll_week()

            # 1. fill orders decided on the previous bar, at this bar's open
            for symbol, intent, qty in pending:
                bar = bar_at[symbol].get(ts)
                if bar is None or symbol in positions:
                    continue
                depth = bar.close * bar.volume * self.config.max_participation
                fill = self.cost.fill_price(bar.open, Side.BUY, qty, depth)
                notional = fill * qty
                fee = self.cost.fee(notional)
                if notional + fee > cash:
                    rejections["insufficient_cash"] += 1
                    continue
                cash -= notional + fee
                positions[symbol] = OpenPosition(
                    symbol=symbol, direction=intent.direction, qty=qty, entry=fill,
                    stop=intent.stop, strategy=intent.strategy, opened_at=ts,
                )
                state.positions[symbol] = positions[symbol]
                entry_fees[symbol] = fee
                opened_index[symbol] = step
                best_price[symbol] = fill
                active_dates.add(ts.date())
            pending = []

            # 2. manage open positions against this bar
            for symbol in list(positions):
                bar = bar_at[symbol].get(ts)
                if bar is None:
                    continue
                position = positions[symbol]
                held = step - opened_index[symbol]
                exit_price: Decimal | None = None
                reason = ""
                if bar.low <= position.stop:
                    exit_price, reason = min(position.stop, bar.open), "stop"
                elif held >= self.config.max_hold_bars:
                    exit_price, reason = bar.close, "time_stop"
                else:
                    row_index = index_of[symbol].get(ts)
                    if row_index is not None:
                        ctx = StrategyContext(
                            ts=ts, symbol=symbol, row=features[symbol][row_index],
                            regime=classify(features[symbol], row_index, self.config.regime_config),
                            position=position, bar_index=row_index,
                        )
                        owner = next(
                            (s for s in strategies if s.name == position.strategy), None
                        )
                        if owner is not None and owner.exit_signal(ctx):
                            exit_price, reason = bar.close, "signal"

                if exit_price is not None:
                    depth = bar.close * bar.volume * self.config.max_participation
                    fill = self.cost.fill_price(exit_price, Side.SELL, position.qty, depth)
                    proceeds = fill * position.qty
                    exit_fee = self.cost.fee(proceeds)
                    cash += proceeds - exit_fee
                    basis = position.entry * position.qty
                    pnl = proceeds - exit_fee - basis - entry_fees[symbol]
                    trades.append(
                        TradeStat(
                            opened_at=position.opened_at or ts, closed_at=ts, symbol=symbol,
                            strategy=position.strategy, pnl=pnl,
                            return_pct=float(pnl / basis) if basis else 0.0,
                            fees=entry_fees[symbol] + exit_fee, regime=reason,
                        )
                    )
                    holds.append(held)
                    state.consecutive_losses = state.consecutive_losses + 1 if pnl <= 0 else 0
                    active_dates.add(ts.date())
                    for store in (positions, entry_fees, opened_index, best_price):
                        store.pop(symbol, None)
                    state.positions.pop(symbol, None)
                else:
                    best_price[symbol] = max(best_price[symbol], bar.high)
                    # Trail by the ORIGINAL stop distance, and only once the
                    # trade is up by 1R. Trailing from the first bar with a
                    # one-bar ATR quietly converts a multi-day strategy into an
                    # intraday one, which is where the edge is zero.
                    risk_distance = position.entry - position.stop
                    if risk_distance > 0 and best_price[symbol] >= position.entry + risk_distance:
                        new_stop = best_price[symbol] - risk_distance
                        if new_stop > position.stop:
                            positions[symbol] = replace(position, stop=new_stop)
                            state.positions[symbol] = positions[symbol]

            # 3. mark to market
            marked = cash
            for symbol, position in positions.items():
                bar = bar_at[symbol].get(ts)
                if bar is not None:
                    marked += position.qty * bar.close
            state.mark(marked)
            equity_curve.append(float(marked))
            stamps.append(ts)

            if state.kill_switch:
                continue

            # 4. scan every symbol for new entries
            #
            # Queued-but-unfilled orders must count against the position limit.
            # Without this reservation, several symbols pass the check on the
            # same bar and all fill on the next one, so a 5-position limit
            # quietly becomes 7. Over-exposure by race condition is a live
            # trading failure, not a backtest artefact.
            for symbol, rows in features.items():
                if symbol in positions or any(p[0] == symbol for p in pending):
                    continue
                if len(positions) + len(pending) >= self.risk.limits.max_open_positions:
                    rejections["max_positions"] += 1
                    break
                row_index = index_of[symbol].get(ts)
                if row_index is None or row_index < max(s.warmup for s in strategies):
                    continue
                bar = bar_at[symbol][ts]
                regime = classify(rows, row_index, self.config.regime_config)
                for strategy in strategies:
                    if self.config.use_regime_filter and not regime.allows(strategy.name):
                        rejections["regime_forbids_strategy"] += 1
                        continue
                    ctx = StrategyContext(
                        ts=ts, symbol=symbol, row=rows[row_index], regime=regime,
                        position=None, bar_index=row_index,
                    )
                    candidate = strategy.generate(ctx)
                    if candidate is None:
                        continue
                    depth = bar.close * bar.volume * self.config.max_participation
                    decision = self.risk.evaluate(state, candidate, available_depth=depth, now=ts)
                    if decision.rejected:
                        rejections[str(decision.reason)] += 1
                        break
                    pending.append((symbol, candidate, decision.qty))
                    break

        days = max(1, (timeline[-1] - timeline[0]).days)
        metrics = compute_metrics(
            stamps, equity_curve, trades,
            periods_per_year=BARS_PER_YEAR[tf], n_trials=self.config.n_trials,
        )
        return PortfolioResult(
            strategies=[s.name for s in strategies],
            symbols=sorted(series),
            tf=tf,
            stamps=stamps,
            equity=equity_curve,
            trades=trades,
            metrics=metrics,
            rejections=dict(rejections),
            trades_per_day=len(trades) / days,
            active_days=len(active_dates),
            total_days=len(all_dates),
            avg_hold_bars=(sum(holds) / len(holds)) if holds else 0.0,
            max_hold_bars_seen=max(holds) if holds else 0,
            daily_returns=_daily_returns(stamps, equity_curve),
        )


def _daily_returns(stamps: Sequence[datetime], equity: Sequence[float]) -> dict[str, float]:
    by_day: dict[str, tuple[float, float]] = {}
    for ts, value in zip(stamps, equity, strict=False):
        key = ts.date().isoformat()
        first, _ = by_day.get(key, (value, value))
        by_day[key] = (first, value)
    return {k: (last / first - 1.0) if first else 0.0 for k, (first, last) in by_day.items()}
