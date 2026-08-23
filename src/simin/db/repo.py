"""Thin async data access layer over TimescaleDB.

Raw SQL on purpose: the hot paths are bulk upserts and range scans, where an ORM
buys nothing and hides the query plan.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from simin.types import TF, Bar, SymbolInfo


def make_engine(dsn: str, *, echo: bool = False) -> AsyncEngine:
    return create_async_engine(dsn, echo=echo, pool_pre_ping=True)


class Repo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def upsert_venue(self, code: str, name: str) -> int:
        sql = text(
            """
            INSERT INTO venues (code, name) VALUES (:code, :name)
            ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
            """
        )
        async with self._engine.begin() as conn:
            row = (await conn.execute(sql, {"code": code, "name": name})).one()
        return int(row[0])

    async def upsert_symbol(self, venue_id: int, info: SymbolInfo) -> int:
        sql = text(
            """
            INSERT INTO symbols (venue_id, symbol, base, quote, price_tick, qty_step,
                                 min_notional, listed_at, delisted_at)
            VALUES (:venue_id, :symbol, :base, :quote, :price_tick, :qty_step,
                    :min_notional, :listed_at, :delisted_at)
            ON CONFLICT (venue_id, symbol) DO UPDATE SET
                price_tick = EXCLUDED.price_tick,
                qty_step = EXCLUDED.qty_step,
                min_notional = EXCLUDED.min_notional,
                delisted_at = COALESCE(EXCLUDED.delisted_at, symbols.delisted_at)
            RETURNING id
            """
        )
        params = {
            "venue_id": venue_id,
            "symbol": info.symbol,
            "base": info.base,
            "quote": info.quote,
            "price_tick": info.price_tick,
            "qty_step": info.qty_step,
            "min_notional": info.min_notional,
            "listed_at": info.listed_at,
            "delisted_at": info.delisted_at,
        }
        async with self._engine.begin() as conn:
            row = (await conn.execute(sql, params)).one()
        return int(row[0])

    async def insert_bars(self, symbol_id: int, bars: Sequence[Bar]) -> int:
        """Idempotent bulk upsert. Re-running a backfill must never duplicate rows."""
        if not bars:
            return 0
        sql = text(
            """
            INSERT INTO ohlcv (symbol_id, tf, ts, open, high, low, close, volume,
                               quote_volume, trades)
            VALUES (:symbol_id, :tf, :ts, :open, :high, :low, :close, :volume,
                    :quote_volume, :trades)
            ON CONFLICT (symbol_id, tf, ts) DO UPDATE SET
                open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                close = EXCLUDED.close, volume = EXCLUDED.volume,
                quote_volume = EXCLUDED.quote_volume, trades = EXCLUDED.trades,
                ingest_time = now()
            """
        )
        rows = [
            {
                "symbol_id": symbol_id,
                "tf": b.tf.value,
                "ts": b.ts,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
                "quote_volume": b.quote_volume,
                "trades": b.trades,
            }
            for b in bars
        ]
        async with self._engine.begin() as conn:
            await conn.execute(sql, rows)
        return len(rows)

    async def get_bars(
        self, symbol_id: int, symbol: str, tf: TF, start: datetime, end: datetime
    ) -> list[Bar]:
        sql = text(
            """
            SELECT ts, open, high, low, close, volume, quote_volume, trades
            FROM ohlcv
            WHERE symbol_id = :symbol_id AND tf = :tf AND ts >= :start AND ts < :end
            ORDER BY ts
            """
        )
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    sql, {"symbol_id": symbol_id, "tf": tf.value, "start": start, "end": end}
                )
            ).all()
        return [
            Bar(
                symbol=symbol,
                tf=tf,
                ts=r[0],
                open=Decimal(str(r[1])),
                high=Decimal(str(r[2])),
                low=Decimal(str(r[3])),
                close=Decimal(str(r[4])),
                volume=Decimal(str(r[5])),
                quote_volume=Decimal(str(r[6])) if r[6] is not None else None,
                trades=r[7],
            )
            for r in rows
        ]

    async def last_bar_ts(self, symbol_id: int, tf: TF) -> datetime | None:
        sql = text("SELECT max(ts) FROM ohlcv WHERE symbol_id = :s AND tf = :tf")
        async with self._engine.connect() as conn:
            row = (await conn.execute(sql, {"s": symbol_id, "tf": tf.value})).one()
        value: datetime | None = row[0]
        return value

    async def stored_range(self, symbol_id: int, tf: TF) -> tuple[datetime | None, datetime | None]:
        """Oldest and newest stored bar. Both ends matter: resuming only from the
        newest would leave a request for *earlier* history silently unfulfilled."""
        sql = text("SELECT min(ts), max(ts) FROM ohlcv WHERE symbol_id = :s AND tf = :tf")
        async with self._engine.connect() as conn:
            row = (await conn.execute(sql, {"s": symbol_id, "tf": tf.value})).one()
        oldest: datetime | None = row[0]
        newest: datetime | None = row[1]
        return oldest, newest

    async def log_quality(
        self, symbol_id: int, tf: TF, n_bars: int, issues: list[dict[str, Any]]
    ) -> None:
        sql = text(
            """
            INSERT INTO data_quality_log (symbol_id, tf, n_bars, issues)
            VALUES (:s, :tf, :n, CAST(:issues AS jsonb))
            """
        )
        import json

        async with self._engine.begin() as conn:
            await conn.execute(
                sql,
                {"s": symbol_id, "tf": tf.value, "n": n_bars, "issues": json.dumps(issues)},
            )
