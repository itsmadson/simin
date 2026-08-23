"""Simin command line.

argparse rather than a CLI framework: one less dependency in the container, and
the operational commands here (backfill, quality check, gate report) must work
in a bare recovery environment.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from simin.config import get_settings
from simin.data.ingest import backfill
from simin.data.quality import check_bars
from simin.db.repo import Repo, make_engine
from simin.exchanges.public_global import PublicGlobalAdapter
from simin.exchanges.venues import PROFILES, profile
from simin.logging import configure_logging, get_logger
from simin.types import TF

log = get_logger(__name__)


def _parse_date(raw: str) -> datetime:
    return datetime.fromisoformat(raw).replace(tzinfo=UTC)


async def _cmd_backfill(args: argparse.Namespace) -> int:
    settings = get_settings()
    adapter = PublicGlobalAdapter(settings.public_data_base, timeout=settings.http_timeout)
    engine = make_engine(settings.pg_dsn)
    repo = Repo(engine)
    try:
        venue_id = await repo.upsert_venue(adapter.venue, "Global public data")
        symbols = {s.symbol: s for s in await adapter.get_symbols()}
        for symbol in args.symbols:
            info = symbols.get(symbol)
            if info is None:
                log.error("backfill.unknown_symbol", symbol=symbol)
                continue
            symbol_id = await repo.upsert_symbol(venue_id, info)
            for tf_raw in args.timeframes:
                result = await backfill(
                    adapter,
                    repo,
                    symbol_id=symbol_id,
                    symbol=symbol,
                    tf=TF.parse(tf_raw),
                    start=_parse_date(args.start),
                    end=_parse_date(args.end) if args.end else None,
                )
                print(
                    f"{symbol:12s} {tf_raw:4s} stored={result.stored:6d} "
                    f"gaps={result.gaps_found} repaired={result.gaps_repaired} "
                    f"errors={len(result.report.errors)}"
                )
    finally:
        await adapter.close()
        await engine.dispose()
    return 0


async def _cmd_check(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = make_engine(settings.pg_dsn)
    repo = Repo(engine)
    tf = TF.parse(args.timeframe)
    try:
        bars = await repo.get_bars(
            args.symbol_id,
            args.symbol,
            tf,
            _parse_date(args.start),
            _parse_date(args.end) if args.end else datetime.now(UTC),
        )
        report = check_bars(bars)
        print(f"{args.symbol} {tf.value}: {report.n_bars} bars, {len(report.issues)} issue(s)")
        for issue in report.issues[:50]:
            print(f"  [{issue.severity}] {issue.kind} @ {issue.ts}: {issue.detail}")
        return 0 if report.ok else 1
    finally:
        await engine.dispose()


def _cmd_costs(_args: argparse.Namespace) -> int:
    """Print the cost floor every strategy has to clear before it is worth anything."""
    print(f"{'venue':24s} {'maker':>8s} {'taker':>8s} {'spread':>8s} {'round trip':>11s}")
    for code in PROFILES:
        p = profile(code)
        rt = p.round_trip_cost()
        print(
            f"{p.display[:24]:24s} {float(p.fees.maker):8.4%} {float(p.fees.taker):8.4%} "
            f"{float(p.typical_spread_bps):7.0f}b {float(rt):11.4%}"
        )
    print(
        "\nA signal whose expected move is smaller than the round-trip cost has "
        "negative expectancy no matter how good the chart looks."
    )
    return 0


def _cmd_target(args: argparse.Namespace) -> int:
    """Show what a monthly return target actually demands. Reality check, not advice."""
    monthly = args.monthly_pct / 100.0
    annual = (1 + monthly) ** 12 - 1
    trades = args.trades_per_month
    cost = float(profile("local_irt_generic").round_trip_cost())
    needed_per_trade = (1 + monthly) ** (1 / trades) - 1
    gross_per_trade = needed_per_trade + cost
    print(f"target                 : {monthly:.1%} / month")
    print(f"compounded             : {annual:,.1%} / year  ({(1 + monthly) ** 12:,.1f}x)")
    print(f"trades assumed         : {trades} / month")
    print(f"net edge needed / trade: {needed_per_trade:.3%}")
    print(f"round-trip cost        : {cost:.3%}")
    print(f"GROSS edge needed      : {gross_per_trade:.3%} per trade")
    if gross_per_trade > 0.01:
        print(
            "\nVERDICT: a persistent gross edge above ~1% per trade at this frequency "
            "is not observed in liquid crypto markets. Reaching this target requires "
            "leverage at which probability of ruin approaches 1. Treat any backtest "
            "that shows it as a bug hunt, not a discovery."
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="simin", description="Simin trading platform CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    bf = sub.add_parser("backfill", help="download and store historical bars")
    bf.add_argument("--symbols", nargs="+", required=True)
    bf.add_argument("--timeframes", nargs="+", default=["1h", "4h", "1d"])
    default_start = (datetime.now(UTC) - timedelta(days=730)).date().isoformat()
    bf.add_argument("--start", default=default_start)
    bf.add_argument("--end", default=None)
    bf.set_defaults(func=lambda a: asyncio.run(_cmd_backfill(a)))

    ck = sub.add_parser("check", help="run data-quality checks on a stored series")
    ck.add_argument("--symbol", required=True)
    ck.add_argument("--symbol-id", type=int, required=True)
    ck.add_argument("--timeframe", default="1h")
    ck.add_argument("--start", required=True)
    ck.add_argument("--end", default=None)
    ck.set_defaults(func=lambda a: asyncio.run(_cmd_check(a)))

    co = sub.add_parser("costs", help="show per-venue transaction cost floor")
    co.set_defaults(func=_cmd_costs)

    tg = sub.add_parser("target", help="what a monthly return target actually requires")
    tg.add_argument("--monthly-pct", type=float, default=200.0)
    tg.add_argument("--trades-per-month", type=int, default=60)
    tg.set_defaults(func=_cmd_target)
    return parser


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, as_json=settings.log_json)
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
