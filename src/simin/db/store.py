"""Persistence for a live trading session.

Without this the trader is a black box: it decides, it acts, and nothing is left
behind to look at. Every signal, order, fill, position change, equity mark and
risk event is written here so the dashboard shows what actually happened rather
than a plausible reconstruction.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from simin.types import RunMode


@dataclass(frozen=True, slots=True)
class RunHandle:
    id: uuid.UUID
    mode: RunMode


class SessionStore:
    """Writes what a run does, and reads it back for the dashboard."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    # ------------------------------------------------------------------ write

    async def start_run(
        self,
        mode: RunMode,
        *,
        code_sha: str,
        config_hash: str,
        risk_profile: str,
        notes: str | None = None,
        seed: int | None = None,
    ) -> RunHandle:
        run_id = uuid.uuid4()
        sql = text(
            """
            INSERT INTO runs (id, mode, code_sha, config_hash, risk_profile, seed, notes)
            VALUES (:id, CAST(:mode AS run_mode), :sha, :cfg, :profile, :seed, :notes)
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                sql,
                {
                    "id": run_id, "mode": mode.value, "sha": code_sha, "cfg": config_hash,
                    "profile": risk_profile, "seed": seed, "notes": notes,
                },
            )
        return RunHandle(id=run_id, mode=mode)

    async def end_run(self, run: RunHandle) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text("UPDATE runs SET ended_at = now() WHERE id = :id"), {"id": run.id}
            )

    async def record_signal(
        self,
        run: RunHandle,
        *,
        symbol_id: int,
        ts: datetime,
        tf: str,
        direction: str,
        entry: Decimal,
        stop: Decimal,
        strategy: str,
        regime: str | None,
        confidence: float,
        expected_cost: Decimal,
        probability: float | None = None,
        risk_reward: float | None = None,
        expected_value: float | None = None,
    ) -> uuid.UUID:
        signal_id = uuid.uuid4()
        sql = text(
            """
            INSERT INTO signals (id, run_id, ts, symbol_id, tf, direction, entry, stop,
                                 probability, confidence, risk_reward, expected_value,
                                 expected_cost, strategy, regime)
            VALUES (:id, :run, :ts, :sym, :tf, :dir, :entry, :stop, :prob, :conf, :rr,
                    :ev, :cost, :strategy, :regime)
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(sql, {
                "id": signal_id, "run": run.id, "ts": ts, "sym": symbol_id, "tf": tf,
                "dir": direction, "entry": entry, "stop": stop, "prob": probability,
                "conf": confidence, "rr": risk_reward, "ev": expected_value,
                "cost": expected_cost, "strategy": strategy, "regime": regime,
            })
        return signal_id

    async def record_order(
        self,
        run: RunHandle,
        *,
        venue_id: int,
        symbol_id: int,
        side: str,
        order_type: str,
        qty: Decimal,
        status: str,
        client_order_id: str,
        price: Decimal | None = None,
        exchange_order_id: str | None = None,
        signal_id: uuid.UUID | None = None,
        reject_reason: str | None = None,
    ) -> uuid.UUID:
        order_id = uuid.uuid4()
        sql = text(
            """
            INSERT INTO orders (id, run_id, signal_id, venue_id, symbol_id, side, type, qty,
                                price, status, client_order_id, exchange_order_id, reject_reason)
            VALUES (:id, :run, :sig, :venue, :sym, :side, :type, :qty, :price, :status,
                    :cid, :xid, :reject)
            ON CONFLICT (client_order_id) DO UPDATE SET
                status = EXCLUDED.status, updated_at = now()
            RETURNING id
            """
        )
        async with self._engine.begin() as conn:
            row = (await conn.execute(sql, {
                "id": order_id, "run": run.id, "sig": signal_id, "venue": venue_id,
                "sym": symbol_id, "side": side, "type": order_type, "qty": qty,
                "price": price, "status": status, "cid": client_order_id,
                "xid": exchange_order_id, "reject": reject_reason,
            })).one()
        return uuid.UUID(str(row[0]))

    async def record_fill(
        self,
        order_id: uuid.UUID,
        *,
        ts: datetime,
        price: Decimal,
        qty: Decimal,
        fee: Decimal,
        fee_asset: str,
        is_maker: bool,
        slippage_bps: Decimal | None = None,
    ) -> None:
        sql = text(
            """
            INSERT INTO fills (id, order_id, ts, price, qty, fee, fee_asset, is_maker,
                               slippage_bps)
            VALUES (:id, :order, :ts, :price, :qty, :fee, :asset, :maker, :slip)
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(sql, {
                "id": uuid.uuid4(), "order": order_id, "ts": ts, "price": price, "qty": qty,
                "fee": fee, "asset": fee_asset, "maker": is_maker, "slip": slippage_bps,
            })

    async def open_position(
        self,
        run: RunHandle,
        *,
        symbol_id: int,
        side: str,
        qty: Decimal,
        avg_entry: Decimal,
        stop: Decimal | None,
        opened_at: datetime,
        strategy: str,
        regime: str | None,
        fees_paid: Decimal = Decimal(0),
    ) -> uuid.UUID:
        position_id = uuid.uuid4()
        sql = text(
            """
            INSERT INTO positions (id, run_id, symbol_id, side, qty, avg_entry, stop,
                                   opened_at, strategy, regime, fees_paid)
            VALUES (:id, :run, :sym, :side, :qty, :entry, :stop, :opened, :strategy,
                    :regime, :fees)
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(sql, {
                "id": position_id, "run": run.id, "sym": symbol_id, "side": side, "qty": qty,
                "entry": avg_entry, "stop": stop, "opened": opened_at, "strategy": strategy,
                "regime": regime, "fees": fees_paid,
            })
        return position_id

    async def update_stop(self, position_id: uuid.UUID, stop: Decimal) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text("UPDATE positions SET stop = :stop WHERE id = :id"),
                {"stop": stop, "id": position_id},
            )

    async def close_position(
        self,
        position_id: uuid.UUID,
        *,
        closed_at: datetime,
        realized_pnl_irt: Decimal,
        realized_pnl_usdt: Decimal | None = None,
        fees_paid: Decimal = Decimal(0),
    ) -> None:
        sql = text(
            """
            UPDATE positions
            SET closed_at = :closed, realized_pnl_irt = :pnl, realized_pnl_usdt = :pnl_usdt,
                fees_paid = fees_paid + :fees
            WHERE id = :id
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(sql, {
                "closed": closed_at, "pnl": realized_pnl_irt, "pnl_usdt": realized_pnl_usdt,
                "fees": fees_paid, "id": position_id,
            })

    async def mark_equity(
        self,
        run: RunHandle,
        *,
        ts: datetime,
        balance: Decimal,
        equity: Decimal,
        equity_usdt: Decimal,
        unrealized: Decimal = Decimal(0),
        drawdown: Decimal = Decimal(0),
        exposure: Decimal = Decimal(0),
    ) -> None:
        sql = text(
            """
            INSERT INTO equity_curve (run_id, ts, balance_irt, equity_irt, equity_usdt,
                                      unrealized, drawdown, exposure)
            VALUES (:run, :ts, :bal, :eq, :eq_usdt, :unreal, :dd, :exp)
            ON CONFLICT (run_id, ts) DO UPDATE SET
                balance_irt = EXCLUDED.balance_irt, equity_irt = EXCLUDED.equity_irt,
                equity_usdt = EXCLUDED.equity_usdt, unrealized = EXCLUDED.unrealized,
                drawdown = EXCLUDED.drawdown, exposure = EXCLUDED.exposure
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(sql, {
                "run": run.id, "ts": ts, "bal": balance, "eq": equity, "eq_usdt": equity_usdt,
                "unreal": unrealized, "dd": drawdown, "exp": exposure,
            })

    async def record_risk_event(
        self, run: RunHandle | None, kind: str, detail: dict[str, Any]
    ) -> None:
        sql = text(
            """
            INSERT INTO risk_events (run_id, kind, detail)
            VALUES (:run, :kind, CAST(:detail AS jsonb))
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                sql,
                {
                    "run": run.id if run else None,
                    "kind": kind,
                    "detail": json.dumps(detail, default=str),
                },
            )

    # ------------------------------------------------------------------- read

    async def _rows(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            result = await conn.execute(text(sql), params or {})
            return [dict(r._mapping) for r in result]

    async def active_run(self) -> dict[str, Any] | None:
        rows = await self._rows(
            """
            SELECT id, mode::text AS mode, started_at, ended_at, risk_profile, code_sha
            FROM runs ORDER BY started_at DESC LIMIT 1
            """
        )
        return rows[0] if rows else None

    async def latest_equity(self, run_id: uuid.UUID) -> dict[str, Any] | None:
        rows = await self._rows(
            """
            SELECT ts, balance_irt, equity_irt, equity_usdt, unrealized, drawdown, exposure
            FROM equity_curve WHERE run_id = :run ORDER BY ts DESC LIMIT 1
            """,
            {"run": run_id},
        )
        return rows[0] if rows else None

    async def equity_curve(self, run_id: uuid.UUID, limit: int = 2000) -> list[dict[str, Any]]:
        rows = await self._rows(
            """
            SELECT ts, equity_irt, equity_usdt, drawdown FROM equity_curve
            WHERE run_id = :run ORDER BY ts DESC LIMIT :lim
            """,
            {"run": run_id, "lim": limit},
        )
        return list(reversed(rows))

    async def open_positions(self, run_id: uuid.UUID) -> list[dict[str, Any]]:
        return await self._rows(
            """
            SELECT p.id, s.symbol, p.side, p.qty, p.avg_entry, p.stop, p.opened_at,
                   p.strategy, p.regime
            FROM positions p JOIN symbols s ON s.id = p.symbol_id
            WHERE p.run_id = :run AND p.closed_at IS NULL
            ORDER BY p.opened_at DESC
            """,
            {"run": run_id},
        )

    async def closed_positions(self, run_id: uuid.UUID, limit: int = 200) -> list[dict[str, Any]]:
        return await self._rows(
            """
            SELECT p.id, s.symbol, p.side, p.qty, p.avg_entry, p.opened_at, p.closed_at,
                   p.realized_pnl_irt, p.fees_paid, p.strategy, p.regime
            FROM positions p JOIN symbols s ON s.id = p.symbol_id
            WHERE p.run_id = :run AND p.closed_at IS NOT NULL
            ORDER BY p.closed_at DESC LIMIT :lim
            """,
            {"run": run_id, "lim": limit},
        )

    async def recent_signals(self, run_id: uuid.UUID, limit: int = 100) -> list[dict[str, Any]]:
        return await self._rows(
            """
            SELECT g.ts, s.symbol, g.tf, g.direction, g.entry, g.stop, g.confidence,
                   g.strategy, g.regime, g.expected_cost
            FROM signals g JOIN symbols s ON s.id = g.symbol_id
            WHERE g.run_id = :run ORDER BY g.ts DESC LIMIT :lim
            """,
            {"run": run_id, "lim": limit},
        )

    async def recent_orders(self, run_id: uuid.UUID, limit: int = 100) -> list[dict[str, Any]]:
        return await self._rows(
            """
            SELECT o.created_at, s.symbol, o.side, o.type, o.qty, o.price, o.status,
                   o.reject_reason
            FROM orders o JOIN symbols s ON s.id = o.symbol_id
            WHERE o.run_id = :run ORDER BY o.created_at DESC LIMIT :lim
            """,
            {"run": run_id, "lim": limit},
        )

    async def recent_risk_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._rows(
            "SELECT ts, kind, detail FROM risk_events ORDER BY ts DESC LIMIT :lim",
            {"lim": limit},
        )

    async def data_coverage(self) -> list[dict[str, Any]]:
        return await self._rows(
            """
            SELECT s.symbol, o.tf, count(*) AS bars, min(o.ts) AS first_bar, max(o.ts) AS last_bar
            FROM ohlcv o JOIN symbols s ON s.id = o.symbol_id
            GROUP BY s.symbol, o.tf ORDER BY s.symbol, o.tf
            """
        )

    async def symbols(self) -> list[dict[str, Any]]:
        return await self._rows(
            "SELECT id, symbol, base, quote FROM symbols ORDER BY symbol"
        )

    async def pnl_breakdown(self, run_id: uuid.UUID, column: str) -> list[dict[str, Any]]:
        """PnL grouped by strategy, regime or symbol. Column is validated, not interpolated."""
        allowed = {"strategy": "p.strategy", "regime": "p.regime", "symbol": "s.symbol"}
        if column not in allowed:
            raise ValueError(f"cannot group by {column!r}")
        return await self._rows(
            f"""
            SELECT {allowed[column]} AS bucket, count(*) AS trades,
                   coalesce(sum(p.realized_pnl_irt), 0) AS pnl,
                   coalesce(sum(p.fees_paid), 0) AS fees
            FROM positions p JOIN symbols s ON s.id = p.symbol_id
            WHERE p.run_id = :run AND p.closed_at IS NOT NULL
            GROUP BY 1 ORDER BY pnl DESC
            """,
            {"run": run_id},
        )


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_float(value: Any) -> float:
    return float(value) if value is not None else 0.0


def rows_to_jsonable(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Decimal/UUID/datetime to JSON-safe primitives for the API."""
    out: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, Decimal):
                item[key] = float(value)
            elif isinstance(value, datetime):
                item[key] = value.isoformat()
            elif isinstance(value, uuid.UUID):
                item[key] = str(value)
            else:
                item[key] = value
        out.append(item)
    return out
