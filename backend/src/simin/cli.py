"""Command line: `simin <command>`.

Enough to run the whole system without the UI — serve the API, run a backtest,
calibrate the dial, and print what a risk level actually means.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from decimal import Decimal
from pathlib import Path

from simin.config import reset_settings_cache, settings
from simin.core.types import TF, MarketKind, Symbol
from simin.exchanges.costs import cost_model
from simin.exchanges.registry import build_exchange, venue_info
from simin.indicators.features import FeatureFrame
from simin.lab.backtest import Backtester
from simin.lab.calibrate import CalibrationStore, calibrate_level, fingerprint
from simin.lab.portfolio import analyse as analyse_portfolio
from simin.lab.portfolio import format_report as format_portfolio
from simin.lab.screen import format_report as format_screen
from simin.lab.screen import screen
from simin.lab.universe import format_report as format_universe
from simin.lab.universe import scan
from simin.logging import configure
from simin.risk.dial import all_profiles, profile
from simin.strategies.base import build_many
from simin.strategies.library import strategies_for_level


def _pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def cmd_dial(args: argparse.Namespace) -> int:
    """Print the risk dial, target beside measurement."""
    store = CalibrationStore(settings().data_dir / "calibration.json")
    measured = store.all_empirical()
    print(f"\n{'Lv':<3}{'Name':<17}{'Risk/trade':>11}{'Lev':>6}{'TF':>6}"
          f"{'Target/mo':>11}{'MEASURED/mo':>13}{'Max DD':>9}{'Ruin':>7}")
    print("-" * 83)
    for p in all_profiles():
        e = measured.get(p.level)
        meas = _pct(e.monthly_return_median) if e else "not measured"
        dd = f"{e.max_drawdown_median * 100:.1f}%" if e else "-"
        ruin = f"{e.ruin_probability * 100:.0f}%" if e else "-"
        print(f"{p.level:<3}{p.name_en:<17}{float(p.risk_per_trade) * 100:>10.2f}%"
              f"{float(p.max_leverage):>5.0f}x{p.signal_tf.value:>6}"
              f"{p.target_monthly_return * 100:>10.0f}%{meas:>13}{dd:>9}{ruin:>7}")
    if not measured:
        print("\nNothing has been measured yet. Run:  simin calibrate --level N")
    print("\nTarget columns say what a level was designed to attempt. They are not")
    print("forecasts. Only MEASURED reflects what happened in walk-forward testing.\n")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    """Everything one risk level does."""
    p = profile(args.level)
    print(f"\n  Level {p.level}: {p.name_en} / {p.name_fa}")
    print(f"  {p.description_en}")
    print(f"  {p.description_fa}\n")
    print(f"  Risk per trade        {float(p.risk_per_trade) * 100:.2f}% of equity")
    print(f"  Max leverage          {float(p.max_leverage):.0f}x  ({p.kind})")
    print(f"  Concurrent positions  {p.max_concurrent_positions}")
    print(f"  Trades per day        up to {p.max_trades_per_day}")
    print(f"  Timeframe             {p.signal_tf.value} signals, {p.context_tf.value} context")
    print(f"  Entry threshold       {p.min_confluence:.2f} confluence")
    print(f"  Shorts                {'yes' if p.allow_shorts else 'no'}")
    print(f"  Stop                  {float(p.atr_stop_mult):.1f} x ATR")
    print(f"  First target          {float(p.take_profit_r):.1f}R")
    print(f"  Breakeven at          {float(p.breakeven_at_r):.2f}R, then trail "
          f"{float(p.trail_atr_mult):.1f} x ATR")
    print(f"  Time stop             {p.time_stop_bars} bars")
    print("\n  Circuit breakers")
    print(f"    Daily loss halt     {float(p.daily_loss_halt) * 100:.1f}%")
    print(f"    Drawdown halt       {float(p.max_drawdown_halt) * 100:.0f}% "
          "(needs a human to clear)")
    print(f"    Loss streak halt    {p.loss_streak_halt} in a row")
    print(f"    Worst plausible day {float(p.worst_case_day) * 100:.1f}%")
    print(f"\n  Strategies            {', '.join(strategies_for_level(p.level))}")
    print(f"\n  Design target         {p.target_monthly_return * 100:.0f}% / month")
    if p.warnings_en:
        print("\n  Warnings")
        for w in p.warnings_en:
            print(f"    - {w}")
    print()
    return 0


async def _load_frames(
    venue: str, symbols: list[str], tf: TF, bars: int
) -> tuple[dict[str, FeatureFrame], dict[str, Symbol]]:
    import os

    os.environ["SIMIN_MODE"] = "lab"
    os.environ["SIMIN_VENUE"] = venue
    os.environ["SIMIN_SYMBOLS"] = " ".join(symbols)
    reset_settings_cache()
    exchange = build_exchange(settings())
    info = venue_info(venue)
    kind = MarketKind.FUTURES if info.supports_futures else MarketKind.SPOT
    frames: dict[str, FeatureFrame] = {}
    syms: dict[str, Symbol] = {}
    try:
        listed = {s.name: s for s in await exchange.symbols()}
        for name in symbols:
            rows = await exchange.candles(name, tf, limit=bars)
            if len(rows) < 400:
                raise SystemExit(
                    f"{name}: only {len(rows)} {tf.value} candles available, need 400+"
                )
            frames[name] = FeatureFrame(name, tf, rows)
            syms[name] = listed.get(name) or Symbol(
                name[:-4], name[-4:], venue, name, kind, 2, 6,
                min_qty=Decimal("0.0001"), min_notional=Decimal("5"),
                max_leverage=info.max_leverage,
            )
    finally:
        await exchange.close()
    return frames, syms


def cmd_backtest(args: argparse.Namespace) -> int:
    p = profile(args.level)
    tf = TF.parse(args.timeframe) if args.timeframe else p.signal_tf
    frames, syms = asyncio.run(_load_frames(args.venue, args.symbols, tf, args.bars))
    costs = cost_model(args.venue)

    result = Backtester(
        p, build_many(strategies_for_level(args.level)), costs,
        Decimal(str(args.equity)),
    ).run(frames, syms, tf)
    stressed = Backtester(
        p, build_many(strategies_for_level(args.level)), costs.stressed(),
        Decimal(str(args.equity)),
    ).run(frames, syms, tf)
    bench = Backtester(
        profile(4), build_many(["buy_and_hold"]), costs, Decimal(str(args.equity)),
    ).run(frames, syms, tf)

    m = result.metrics
    pf = "inf" if math.isinf(m.profit_factor) else f"{m.profit_factor:.2f}"
    print(f"\n  Level {p.level} ({p.name_en}) on {', '.join(args.symbols)} "
          f"{tf.value}, {result.bars} bars over {m.days:.0f} days")
    print(f"  Venue {args.venue}, round-trip cost {float(costs.round_trip) * 100:.2f}%\n")
    print(f"    Return              {_pct(m.total_return)}   ({_pct(m.monthly_return)}/month)")
    print(f"    Buy and hold        {_pct(bench.metrics.total_return)}")
    print(f"    At 2x costs         {_pct(stressed.metrics.total_return)}")
    print(f"    Max drawdown        {m.max_drawdown * 100:.1f}%  "
          f"(longest {m.max_drawdown_duration_days:.0f} days)")
    print(f"    Trades              {m.trades}  ({m.trades_per_month:.1f}/month)")
    print(f"    Win rate            {m.win_rate * 100:.0f}%")
    print(f"    Profit factor       {pf}")
    print(f"    Expectancy          {m.expectancy_r:+.3f}R per trade")
    print(f"    Sharpe              {m.sharpe:.2f}")
    print(f"    Fees paid           {m.total_fees:.2f}  ({m.cost_drag * 100:.0f}% of gross PnL)")
    print(f"    Signals             {result.signals_generated} generated, "
          f"{result.signals_taken} taken")
    if result.halt_reason:
        print(f"    HALTED              {result.halt_reason} at {result.halted_at}")
    if result.rejections:
        joined = ", ".join(f"{k}={v}" for k, v in list(result.rejections.items())[:5])
        print(f"\n    Rejections: {joined}")
    print(f"    Exits: {m.exit_breakdown}")

    if args.json:
        Path(args.json).write_text(json.dumps(result.to_dict(), indent=2))
        print(f"\n  Written to {args.json}")
    print()
    return 0 if m.total_return > 0 else 1


def cmd_calibrate(args: argparse.Namespace) -> int:
    tf = TF.parse(args.timeframe) if args.timeframe else profile(args.level or 4).signal_tf
    frames, syms = asyncio.run(_load_frames(args.venue, args.symbols, tf, args.bars))
    costs = cost_model(args.venue)
    store = CalibrationStore(settings().data_dir / "calibration.json")
    fp = fingerprint(frames, costs)

    levels = [args.level] if args.level else list(range(1, 11))
    for level in levels:
        print(f"  calibrating level {level}...", flush=True)
        report = calibrate_level(
            level, frames, syms, tf, costs, Decimal(str(args.equity)),
        )
        n = min(len(f) for f in frames.values())
        months = n * tf.seconds / 86400 / 30.44
        e = report.empirical(months, ",".join(sorted(frames)))
        store.put(level, fp, e)
        verdict = "PASSED" if report.gates.passed else "FAILED"
        print(f"    target {report.profile.target_monthly_return * 100:.0f}%/mo  "
              f"measured {_pct(e.monthly_return_median)}/mo  "
              f"maxDD {e.max_drawdown_median * 100:.1f}%  "
              f"ruin {e.ruin_probability * 100:.0f}%  gates {verdict}")
        if not report.gates.passed:
            for g in report.gates.failures:
                print(f"      failed: {g.name} — {g.detail}")
    print()
    return 0


async def _scan(venue: str, equity: float, position: float, tf: TF, max_markets: int):
    import os

    os.environ["SIMIN_MODE"] = "lab"
    os.environ["SIMIN_VENUE"] = venue
    reset_settings_cache()
    exchange = build_exchange(settings())
    try:
        return await scan(
            exchange, cost_model(venue), Decimal(str(equity)), Decimal(str(position)),
            tf=tf, max_markets=max_markets, check_history=False,
        )
    finally:
        await exchange.close()


def cmd_universe(args: argparse.Namespace) -> int:
    """Which markets can this account actually trade?"""
    tf = TF.parse(args.timeframe) if args.timeframe else TF.H2
    report = asyncio.run(
        _scan(args.venue, args.equity, args.position, tf, args.max_markets)
    )
    print(format_universe(report, limit=args.limit))
    return 0


async def _load_universe(
    venue: str, symbols: list[str], tf: TF, bars: int
) -> tuple[dict[str, FeatureFrame], dict[str, Symbol]]:
    import os

    os.environ["SIMIN_MODE"] = "lab"
    os.environ["SIMIN_VENUE"] = venue
    reset_settings_cache()
    exchange = build_exchange(settings())
    frames: dict[str, FeatureFrame] = {}
    syms: dict[str, Symbol] = {}
    try:
        listed = {s.name: s for s in await exchange.symbols()}
        for name in symbols:
            sym = listed.get(name)
            if sym is None:
                print(f"    {name}: not listed on {venue}")
                continue
            rows = await exchange.candles(sym.venue_symbol, tf, limit=bars)
            if len(rows) < 1200:
                print(f"    {name}: only {len(rows)} bars — skipped")
                continue
            frames[name] = FeatureFrame(name, tf, rows)
            syms[name] = sym
            print(f"    {name}: {len(rows)} bars")
    finally:
        await exchange.close()
    return frames, syms


def cmd_screen(args: argparse.Namespace) -> int:
    """Walk-forward every candidate market, then correct for having tested them all."""
    prof = profile(args.level)
    tf = TF.parse(args.timeframe) if args.timeframe else prof.signal_tf

    names = list(args.symbols)
    if not names:
        print(f"  scanning {args.venue} for tradeable markets...")
        report = asyncio.run(
            _scan(args.venue, args.equity, args.position, tf, args.max_markets)
        )
        names = report.top(args.top)
        print(f"  {len(names)} tradeable at ${args.position:,.0f} per position\n")

    print("  loading history...")
    frames, syms = asyncio.run(_load_universe(args.venue, names, tf, args.bars))
    if len(frames) < 2:
        raise SystemExit("need at least 2 markets with enough history to screen")

    result = screen(
        prof, frames, syms, tf, cost_model(args.venue), Decimal(str(args.equity)),
        train_bars=args.train, test_bars=args.test, null_runs=args.null_runs,
        extra_trials=args.extra_trials,
    )
    print(format_screen(result, limit=args.limit))

    if args.json:
        Path(args.json).write_text(json.dumps(result.to_dict(), indent=2))
        print(f"  written to {args.json}\n")
    return 0 if result.survivors else 1


def cmd_portfolio(args: argparse.Namespace) -> int:
    """How many independent bets is this basket really making?"""
    tf = TF.parse(args.timeframe) if args.timeframe else TF.H2
    names = list(args.symbols)
    if not names:
        print(f"  scanning {args.venue}...")
        report = asyncio.run(
            _scan(args.venue, args.equity, args.position, tf, args.max_markets)
        )
        names = report.top(args.top)
    print("  loading history...")
    frames, _ = asyncio.run(_load_universe(args.venue, names, tf, args.bars))
    if len(frames) < 2:
        raise SystemExit("need at least 2 markets to measure correlation")
    result = analyse_portfolio(
        frames, ranked=names, threshold=args.threshold,
        max_positions=args.max_positions or None,
    )
    print(format_portfolio(result))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    s = settings()
    configure(s.log_level, s.log_json)
    uvicorn.run(
        "simin.api.app:app",
        host=args.host or s.api_host,
        port=args.port or s.api_port,
        reload=args.reload,
        log_level=s.log_level.lower(),
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="simin",
        description="سیمین — adaptive crypto trading with an honest risk dial",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("dial", help="show the risk dial, target beside measurement").set_defaults(
        func=cmd_dial
    )

    ex = sub.add_parser("explain", help="everything one risk level does")
    ex.add_argument("level", type=int, choices=range(1, 11))
    ex.set_defaults(func=cmd_explain)

    def add_data_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--venue", default="offline",
                        help="coinex, nobitex, wallex, or offline (synthetic)")
        sp.add_argument("--symbols", nargs="+", default=["BTCUSDT"])
        sp.add_argument("--timeframe", default="", help="defaults to the level's own")
        sp.add_argument("--bars", type=int, default=4000)
        sp.add_argument("--equity", type=float, default=10000.0)

    bt = sub.add_parser("backtest", help="run one configuration over history")
    bt.add_argument("--level", type=int, required=True, choices=range(1, 11))
    add_data_args(bt)
    bt.add_argument("--json", default="", help="write the full result here")
    bt.set_defaults(func=cmd_backtest)

    cal = sub.add_parser("calibrate", help="measure what a level actually does")
    cal.add_argument("--level", type=int, default=0, choices=range(0, 11),
                     help="0 calibrates every level")
    add_data_args(cal)
    cal.set_defaults(func=cmd_calibrate)

    def add_scan_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--venue", default="coinex")
        sp.add_argument("--equity", type=float, default=10000.0)
        sp.add_argument("--position", type=float, default=3000.0,
                        help="notional of ONE position — this is what the book must absorb")
        sp.add_argument("--max-markets", type=int, default=45,
                        help="how many of the most liquid markets to measure depth on")
        sp.add_argument("--timeframe", default="")

    uni = sub.add_parser("universe", help="which markets this account can actually trade")
    add_scan_args(uni)
    uni.add_argument("--limit", type=int, default=30)
    uni.set_defaults(func=cmd_universe)

    sc = sub.add_parser("screen",
                        help="walk-forward many markets, corrected for multiple testing")
    sc.add_argument("--level", type=int, default=4, choices=range(1, 11))
    sc.add_argument("--symbols", nargs="*", default=[],
                    help="omit to auto-select from the universe scan")
    add_scan_args(sc)
    sc.add_argument("--top", type=int, default=20)
    sc.add_argument("--bars", type=int, default=5000)
    sc.add_argument("--train", type=int, default=1500)
    sc.add_argument("--test", type=int, default=450)
    sc.add_argument("--null-runs", type=int, default=15,
                    help="bootstrap runs on structureless data")
    sc.add_argument("--extra-trials", type=int, default=1,
                    help="multiplier if you are also searching over risk levels")
    sc.add_argument("--limit", type=int, default=30)
    sc.add_argument("--json", default="")
    sc.set_defaults(func=cmd_screen)

    pf = sub.add_parser("portfolio", help="correlation structure and effective breadth")
    pf.add_argument("--symbols", nargs="*", default=[])
    add_scan_args(pf)
    pf.add_argument("--top", type=int, default=20)
    pf.add_argument("--bars", type=int, default=2000)
    pf.add_argument("--threshold", type=float, default=0.75)
    pf.add_argument("--max-positions", type=int, default=0)
    pf.set_defaults(func=cmd_portfolio)

    sv = sub.add_parser("serve", help="run the API")
    sv.add_argument("--host", default="")
    sv.add_argument("--port", type=int, default=0)
    sv.add_argument("--reload", action="store_true")
    sv.set_defaults(func=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except SystemExit as exc:
        print(f"\n  {exc}\n", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
