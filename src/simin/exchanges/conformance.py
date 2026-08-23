"""Conformance checks every adapter must pass before it is trusted with money.

Run against a plugin adapter in read-only mode, and against a throwaway account
with minimum-size orders before enabling it for real. The checks encode the parts
of the adapter contract that are easy to implement subtly wrong and impossible to
debug later: timezone handling, bar closure, ordering, and idempotency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from simin.data.quality import check_bars
from simin.exchanges.base import ExchangeAdapter
from simin.types import TF


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    venue: str
    checks: list[Check]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def render(self) -> str:
        lines = [f"ADAPTER CONFORMANCE: {self.venue}"]
        for check in self.checks:
            lines.append(f"[{'PASS' if check.passed else 'FAIL'}] {check.name} {check.detail}")
        lines.append("PASS" if self.passed else "FAIL — do not trade through this adapter")
        return "\n".join(lines)


async def check_market_data(
    adapter: ExchangeAdapter, symbol: str, tf: TF = TF.H1, *, now: datetime | None = None
) -> ConformanceReport:
    """Read-only checks. Safe to run against any venue, no credentials needed.

    ``now`` defaults to the wall clock, which is what a live adapter should be
    judged against; replay-backed adapters pass their simulated clock instead so
    the same checks apply to both.
    """
    checks: list[Check] = []
    now = now or datetime.now(UTC)
    since = now - tf.delta * 200

    bars = await adapter.get_ohlcv(symbol, tf, since, limit=200)
    checks.append(Check("returns bars", bool(bars), f"{len(bars)} bars"))
    if bars:
        checks.append(
            Check(
                "timestamps are UTC-aware",
                all(b.ts.tzinfo is not None and b.ts.utcoffset() == timedelta(0) for b in bars),
            )
        )
        checks.append(
            Check("bars are ascending", all(a.ts < b.ts for a, b in zip(bars, bars[1:], strict=False)))
        )
        checks.append(
            Check(
                "no unclosed bar is returned",
                all(b.close_time <= now for b in bars),
                "an in-progress bar is look-ahead bias at the source",
            )
        )
        report = check_bars(bars)
        checks.append(Check("series has no gaps or duplicates", report.ok,
                            f"{len(report.errors)} error(s)"))

    book = await adapter.get_orderbook(symbol, depth=10)
    checks.append(Check("order book is uncrossed",
                        not (book.bids and book.asks) or book.bids[0].price < book.asks[0].price))

    ticker = await adapter.get_ticker(symbol)
    checks.append(Check("ticker spread is positive", ticker.ask > ticker.bid))

    health = await adapter.health()
    checks.append(Check("health check responds", health.venue == adapter.venue))

    fees = adapter.fee_schedule(symbol)
    checks.append(Check("fee schedule is available", fees.taker >= fees.maker,
                        "taker should not be cheaper than maker"))
    return ConformanceReport(venue=adapter.venue, checks=checks)
