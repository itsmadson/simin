"""Which markets can this account actually trade?

The instinct to trade everything the venue lists is a good one and, on CoinEx
futures, wrong. The venue lists 203 USDT perpetuals. The median one turns over
about $107,000 in 24 hours. A $10,000 account at 2x is putting $2,000-5,000 to
work per position, and moving several thousand dollars through a book that
handles a hundred thousand a day means *you are the market*: your own order
walks the price away from you by more than the entire fee budget, on both sides.

So this module answers a narrower and much more useful question than "what is
listed": **at my account size, what can I get in and out of without the exit
costing more than the idea was worth?**

It answers it by walking the real order book rather than by trusting turnover.
Turnover is a headline number that one whale and a hundred bots can manufacture;
depth is what your order actually meets. The difference is not small — on the
data this was built against, KASUSDT showed healthy volume and cost 0.16% in
slippage on a *thousand-dollar* order, which is most of a round trip gone before
the trade has an opinion.

The output is a ranked, capacity-filtered list plus, for everything excluded,
the reason. "Excluded" is as much of an answer as "included".
"""

from __future__ import annotations

import asyncio
import enum
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from simin.core.types import TF, Symbol
from simin.exchanges.base import Exchange, ExchangeError
from simin.exchanges.costs import CostModel
from simin.logging import get_logger

log = get_logger(__name__)


class Verdict(enum.StrEnum):
    TRADEABLE = "tradeable"
    THIN = "thin"
    TOO_EXPENSIVE = "too_expensive"
    TOO_QUIET = "too_quiet"
    NO_HISTORY = "no_history"
    STABLE = "stable"
    NO_DATA = "no_data"

    @property
    def ok(self) -> bool:
        return self is Verdict.TRADEABLE


@dataclass(frozen=True, slots=True)
class MarketScore:
    """One market, measured."""

    symbol: str
    verdict: Verdict
    reason: str = ""

    #: 24h turnover in quote currency.
    turnover: float = 0.0
    #: 24h high-low range as a fraction of price. The raw material a strategy
    #: has to work with — a market that does not move cannot pay for its fees.
    daily_range: float = 0.0
    #: One-way slippage, from walking the real book at the intended size.
    slippage: float | None = None
    #: Round trip: two fees plus two slippages. Everything must clear this.
    round_trip: float = 0.0
    #: daily_range / round_trip. How many times over a typical day's move
    #: covers the cost of one trade. Below ~5 there is nothing to work with.
    edge_ratio: float = 0.0
    #: Bars of history available on the signal timeframe.
    bars: int = 0
    #: 0..1 composite, used only to order the survivors.
    score: float = 0.0
    max_leverage: int = 1

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "verdict": self.verdict.value,
            "reason": self.reason,
            "turnover": self.turnover,
            "daily_range": self.daily_range,
            "slippage": self.slippage,
            "round_trip": self.round_trip,
            "edge_ratio": self.edge_ratio,
            "bars": self.bars,
            "score": self.score,
            "max_leverage": self.max_leverage,
            "tradeable": self.verdict.ok,
        }


@dataclass(slots=True)
class UniverseReport:
    scanned: int
    equity: Decimal
    position_notional: Decimal
    markets: list[MarketScore] = field(default_factory=list)
    scanned_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def tradeable(self) -> list[MarketScore]:
        return [m for m in self.markets if m.verdict.ok]

    @property
    def rejected(self) -> list[MarketScore]:
        return [m for m in self.markets if not m.verdict.ok]

    def top(self, n: int) -> list[str]:
        return [m.symbol for m in self.tradeable[:n]]

    def rejection_summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for m in self.rejected:
            counts[m.verdict.value] = counts.get(m.verdict.value, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def to_dict(self) -> dict[str, object]:
        return {
            "scanned": self.scanned,
            "equity": float(self.equity),
            "position_notional": float(self.position_notional),
            "scanned_at": self.scanned_at.isoformat(),
            "tradeable_count": len(self.tradeable),
            "rejection_summary": self.rejection_summary(),
            "markets": [m.to_dict() for m in self.markets],
        }


@dataclass(frozen=True, slots=True)
class ScanLimits:
    """Where the lines are drawn, and why.

    These are judgement calls, not laws, so they are parameters rather than
    constants buried in the logic — but the defaults are argued for.
    """

    #: A day's typical move must cover a round trip at least this many times.
    #: At 5x, a strategy capturing a fifth of the daily range breaks even; below
    #: that it is paying to guess.
    min_edge_ratio: float = 5.0
    #: One-way slippage above this makes the round trip cost more than most
    #: intraday edges are worth.
    max_slippage: float = 0.0025
    #: Turnover floor. Independent of depth because a book can look deep for one
    #: snapshot and evaporate; sustained volume is evidence it will still be
    #: there when you need to exit.
    min_turnover: float = 750_000.0
    #: Your position as a fraction of the market's daily turnover. Above this
    #: you are a participant rather than an observer, and the backtest's cost
    #: model — which assumes you do not move the price — stops being true.
    max_turnover_share: float = 0.01
    #: Indicators need warm-up; below this the first signal is noise.
    min_bars: int = 400
    #: Below this daily range there is nothing to trade, at any cost level.
    min_daily_range: float = 0.015
    #: Markets whose whole point is not moving.
    stable_assets: frozenset[str] = frozenset(
        {"USDC", "USDT", "DAI", "TUSD", "FDUSD", "USDE", "XAUT", "PAXG"}
    )


async def scan(
    exchange: Exchange,
    costs: CostModel,
    equity: Decimal,
    position_notional: Decimal,
    tf: TF = TF.H2,
    limits: ScanLimits | None = None,
    max_markets: int = 60,
    check_history: bool = True,
    concurrency: int = 6,
) -> UniverseReport:
    """Rank every market the venue lists by what this account can actually trade.

    `position_notional` is the size a single position would be — not the account
    size. That is the number the book has to absorb, and it is what makes this
    account-specific: a market that is untradeable for $50,000 may be perfectly
    fine for $2,000, and saying "this coin is illiquid" without naming a size is
    saying nothing.
    """
    lim = limits or ScanLimits()
    markets = list(await exchange.symbols())
    if not markets:
        raise ExchangeError(f"{exchange.name} returned no markets to scan")

    stats = await _turnover_by_symbol(exchange)
    scores: list[MarketScore] = []

    # First pass is free: reject on turnover and volatility before spending a
    # request on depth. Fetching 200 order books to then discard 180 of them on
    # data already in hand is a good way to get rate limited.
    candidates: list[Symbol] = []
    for sym in markets:
        row = stats.get(sym.name)
        if row is None:
            scores.append(MarketScore(sym.name, Verdict.NO_DATA, "no ticker data"))
            continue
        turnover, daily_range = row

        if sym.base.upper() in lim.stable_assets or sym.quote.upper() not in ("USDT", "USD", "USDC"):
            scores.append(
                MarketScore(sym.name, Verdict.STABLE, f"{sym.base} is not a directional market",
                            turnover=turnover, daily_range=daily_range)
            )
            continue
        if turnover < lim.min_turnover:
            scores.append(
                MarketScore(sym.name, Verdict.THIN,
                            f"24h turnover ${turnover:,.0f} below ${lim.min_turnover:,.0f}",
                            turnover=turnover, daily_range=daily_range)
            )
            continue
        share = float(position_notional) / turnover if turnover > 0 else 1.0
        if share > lim.max_turnover_share:
            scores.append(
                MarketScore(sym.name, Verdict.THIN,
                            f"one position is {share:.1%} of daily turnover — you would be "
                            f"moving this market, not trading it",
                            turnover=turnover, daily_range=daily_range)
            )
            continue
        if daily_range < lim.min_daily_range:
            scores.append(
                MarketScore(sym.name, Verdict.TOO_QUIET,
                            f"24h range {daily_range:.2%} — too still to pay for a round trip",
                            turnover=turnover, daily_range=daily_range)
            )
            continue
        candidates.append(sym)

    # Rank survivors by turnover and only pay for depth on the best ones.
    candidates.sort(key=lambda s: -stats[s.name][0])
    candidates = candidates[:max_markets]

    gate = asyncio.Semaphore(concurrency)

    async def measure(sym: Symbol) -> MarketScore:
        async with gate:
            turnover, daily_range = stats[sym.name]
            slippage: float | None = None
            try:
                book = await exchange.order_book(sym.venue_symbol)
                if book is not None:
                    swept = book.sweep(position_notional, buy=True)
                    slippage = float(swept) if swept is not None else None
                    if swept is None:
                        return MarketScore(
                            sym.name, Verdict.THIN,
                            f"visible book cannot absorb ${float(position_notional):,.0f}",
                            turnover=turnover, daily_range=daily_range,
                            max_leverage=sym.max_leverage,
                        )
            except ExchangeError as exc:
                log.warning("depth fetch failed", symbol=sym.name, error=str(exc))

            # Fall back to the venue's modelled slippage when depth is
            # unavailable — flagged, never treated as free.
            effective = slippage if slippage is not None else float(costs.slippage)
            round_trip = 2 * (float(costs.taker_fee) + effective)
            edge = daily_range / round_trip if round_trip > 0 else 0.0

            bars = 0
            if check_history:
                try:
                    candles = await exchange.candles(sym.venue_symbol, tf, limit=lim.min_bars + 50)
                    bars = len(candles)
                except ExchangeError:
                    bars = 0

            if slippage is not None and slippage > lim.max_slippage:
                return MarketScore(
                    sym.name, Verdict.TOO_EXPENSIVE,
                    f"slippage {slippage:.2%} one way at ${float(position_notional):,.0f}",
                    turnover=turnover, daily_range=daily_range, slippage=slippage,
                    round_trip=round_trip, edge_ratio=edge, bars=bars,
                    max_leverage=sym.max_leverage,
                )
            if edge < lim.min_edge_ratio:
                return MarketScore(
                    sym.name, Verdict.TOO_EXPENSIVE,
                    f"a typical day moves {daily_range:.2%} and a round trip costs "
                    f"{round_trip:.2%} — only {edge:.1f}x cover",
                    turnover=turnover, daily_range=daily_range, slippage=slippage,
                    round_trip=round_trip, edge_ratio=edge, bars=bars,
                    max_leverage=sym.max_leverage,
                )
            if check_history and bars < lim.min_bars:
                return MarketScore(
                    sym.name, Verdict.NO_HISTORY,
                    f"only {bars} {tf.value} candles — not enough to warm up, "
                    "let alone validate",
                    turnover=turnover, daily_range=daily_range, slippage=slippage,
                    round_trip=round_trip, edge_ratio=edge, bars=bars,
                    max_leverage=sym.max_leverage,
                )

            return MarketScore(
                symbol=sym.name,
                verdict=Verdict.TRADEABLE,
                turnover=turnover,
                daily_range=daily_range,
                slippage=slippage,
                round_trip=round_trip,
                edge_ratio=edge,
                bars=bars,
                score=_composite(turnover, edge, slippage, lim),
                max_leverage=sym.max_leverage,
            )

    measured = await asyncio.gather(*(measure(s) for s in candidates))
    scores.extend(measured)
    scores.sort(key=lambda m: (not m.verdict.ok, -m.score, -m.turnover))

    return UniverseReport(
        scanned=len(markets),
        equity=equity,
        position_notional=position_notional,
        markets=scores,
    )


def _composite(
    turnover: float, edge: float, slippage: float | None, lim: ScanLimits
) -> float:
    """0..1 ordering score for markets that already passed every gate.

    Deliberately crude. This decides display order among things already judged
    tradeable; it is not a prediction of returns, and dressing it up as one
    would invite exactly the "the scanner says BUY THIS" reading the whole
    module exists to avoid.
    """
    import math

    # Log, because turnover spans four orders of magnitude and a linear term
    # would make BTC the only market that ever scores.
    liquidity = min(math.log10(max(turnover, 1)) / 8.0, 1.0)
    headroom = min(edge / (lim.min_edge_ratio * 4), 1.0)
    cheapness = 1.0 - min((slippage or lim.max_slippage) / lim.max_slippage, 1.0)
    return round(0.45 * liquidity + 0.35 * headroom + 0.20 * cheapness, 4)


async def _turnover_by_symbol(exchange: Exchange) -> dict[str, tuple[float, float]]:
    """`symbol -> (24h turnover, 24h range as a fraction of price)`."""
    fetch = getattr(exchange, "tickers", None)
    if fetch is None:
        return {}
    try:
        rows = await fetch()
    except ExchangeError as exc:
        log.warning("bulk ticker fetch failed", error=str(exc))
        return {}

    from simin.exchanges.base import normalise_symbol

    out: dict[str, tuple[float, float]] = {}
    for row in rows:
        name = normalise_symbol(str(row.get("market", "")))
        if not name:
            continue
        try:
            last = float(row.get("last") or row.get("close") or 0)
            high = float(row.get("high") or 0)
            low = float(row.get("low") or 0)
            turnover = float(row.get("value") or 0)
        except (TypeError, ValueError):
            continue
        rng = (high - low) / last if last > 0 else 0.0
        out[name] = (turnover, rng)
    return out


def format_report(report: UniverseReport, limit: int = 25) -> str:
    """Human-readable scan, for the CLI."""
    lines = [
        "",
        f"  Scanned {report.scanned} markets at ${float(report.position_notional):,.0f} "
        f"per position (equity ${float(report.equity):,.0f})",
        f"  {len(report.tradeable)} tradeable, {len(report.rejected)} excluded",
        "",
        f"  {'market':<13}{'turnover/24h':>16}{'range':>9}{'slip':>9}"
        f"{'round trip':>12}{'cover':>8}{'bars':>7}",
        "  " + "-" * 74,
    ]
    for m in report.tradeable[:limit]:
        slip = f"{m.slippage:.3%}" if m.slippage is not None else "    n/a"
        lines.append(
            f"  {m.symbol:<13}{m.turnover:>16,.0f}{m.daily_range:>8.2%}{slip:>9}"
            f"{m.round_trip:>11.3%}{m.edge_ratio:>8.1f}{m.bars:>7}"
        )
    if not report.tradeable:
        lines.append("  (nothing passed — the position size is too large for this venue)")

    lines += ["", "  Excluded:"]
    for reason, count in report.rejection_summary().items():
        lines.append(f"    {reason:<16} {count}")

    worst = [m for m in report.rejected if m.verdict is Verdict.TOO_EXPENSIVE][:4]
    if worst:
        lines += ["", "  Examples of markets that look liquid but are not:"]
        for m in worst:
            lines.append(f"    {m.symbol:<13} {m.reason}")
    lines.append("")
    return "\n".join(lines)
