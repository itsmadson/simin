"""HTTP API behind the dashboard.

Everything here reads what actually happened from the database. There is no
demo data and no placeholder: if a panel is empty it is because nothing has
happened yet, which is information rather than a bug.

Write endpoints are deliberately few. The kill switch stops trading; settings
change the paper account and the active profile. Enabling REAL mode is not an
API call — it requires an approval token issued out of band once the Go/No-Go
checklist passes, because a button that arms live trading will eventually be
pressed by accident.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from simin import __version__
from simin.backtest.costs import CostModel
from simin.backtest.engine import BacktestConfig, Backtester
from simin.config import RiskProfile, get_settings, limits_for
from simin.db.repo import Repo, make_engine
from simin.db.store import SessionStore, rows_to_jsonable
from simin.exchanges.plugins import available_plugins
from simin.exchanges.venues import PROFILES, profile
from simin.features.engine import build_features
from simin.features.regime import classify
from simin.risk.engine import RiskEngine
from simin.strategies import ALL_STRATEGIES, BENCHMARKS, build
from simin.trader import TraderConfig
from simin.types import TF, RunMode
from simin.validation.gates import target_feasibility

_WEB = Path(__file__).parent / "web"

#: Runtime toggles the operator can change from the UI. Persisted only for the
#: process lifetime: anything that must survive a restart belongs in the
#: environment, where it can be reviewed.
_STATE: dict[str, Any] = {
    "kill_switch": False,
    "kill_reason": None,
    "paper_balance": None,
    "risk_profile": None,
}

_ENGINE: AsyncEngine | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global _ENGINE
    _ENGINE = make_engine(get_settings().pg_dsn)
    try:
        yield
    finally:
        await _ENGINE.dispose()
        _ENGINE = None


app = FastAPI(title="Simin", version=__version__, lifespan=lifespan)


def _store() -> SessionStore:
    if _ENGINE is None:
        raise HTTPException(status_code=503, detail="database not ready")
    return SessionStore(_ENGINE)


class DatabaseUnavailable(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=503, detail="database unavailable")


async def _ensure_control(store: SessionStore) -> None:
    """Create the control row if an older database predates it."""
    try:
        await store.ensure_control()
    except (SQLAlchemyError, OSError, ConnectionError):
        return


async def _read[T](coro: Awaitable[T], default: T) -> T:
    """Run a database read, returning ``default`` if the database is not there.

    A dashboard panel that says "nothing yet" while the database starts is
    honest; a 500 that blanks the whole page because one panel could not connect
    is not. Write paths deliberately do not use this — a silent write failure
    would be far worse than a visible one.
    """
    try:
        return await coro
    except (SQLAlchemyError, OSError, ConnectionError):
        return default


def _repo() -> Repo:
    if _ENGINE is None:
        raise HTTPException(status_code=503, detail="database not ready")
    return Repo(_ENGINE)


def _profile() -> RiskProfile:
    override = _STATE.get("risk_profile")
    return RiskProfile(override) if override else get_settings().risk_profile


def _paper_balance() -> Decimal:
    override = _STATE.get("paper_balance")
    return Decimal(str(override)) if override else get_settings().paper_start_balance_irt


# --------------------------------------------------------------------- status


@app.get("/api/status")
async def status() -> dict[str, Any]:
    settings = get_settings()
    run = None
    try:
        run = await _store().active_run()
    except Exception:
        run = None
    return {
        "version": __version__,
        "mode": settings.mode,
        "mode_label": "LAB" if settings.mode is not RunMode.LIVE else "REAL",
        "risk_profile": _profile(),
        "trading_enabled": not _STATE["kill_switch"],
        "kill_reason": _STATE["kill_reason"],
        "real_mode_unlocked": settings.live_approval_token is not None,
        "server_time": datetime.now(UTC).isoformat(),
        "session": rows_to_jsonable([run])[0] if run else None,
    }


@app.get("/api/portfolio")
async def portfolio() -> dict[str, Any]:
    """Account state as recorded by the running session."""
    store = _store()
    run = await _read(store.active_run(), None)
    if run is None:
        return {
            "has_session": False,
            "balance": float(_paper_balance()),
            "equity": float(_paper_balance()),
            "message": "No trading session yet. The trader writes here once it starts.",
        }
    run_id = uuid.UUID(str(run["id"]))
    latest = await _read(store.latest_equity(run_id), None)
    open_positions = await _read(store.open_positions(run_id), [])
    closed = await _read(store.closed_positions(run_id, limit=1000), [])
    realized = sum(float(p["realized_pnl_irt"] or 0) for p in closed)
    fees = sum(float(p["fees_paid"] or 0) for p in closed)
    wins = [p for p in closed if float(p["realized_pnl_irt"] or 0) > 0]
    start_balance = float(_paper_balance())
    equity = float(latest["equity_irt"]) if latest else start_balance
    return {
        "has_session": True,
        "mode": run["mode"],
        "started_at": run["started_at"].isoformat(),
        "balance": float(latest["balance_irt"]) if latest else start_balance,
        "equity": equity,
        "start_balance": start_balance,
        "total_return": (equity / start_balance - 1) if start_balance else 0.0,
        "unrealized": float(latest["unrealized"]) if latest else 0.0,
        "realized": realized,
        "fees_paid": fees,
        "drawdown": float(latest["drawdown"]) if latest else 0.0,
        "exposure": float(latest["exposure"]) if latest else 0.0,
        "open_positions": len(open_positions),
        "closed_trades": len(closed),
        "win_rate": (len(wins) / len(closed)) if closed else 0.0,
    }


@app.get("/api/equity")
async def equity(limit: int = 1000) -> dict[str, Any]:
    store = _store()
    run = await _read(store.active_run(), None)
    if run is None:
        return {"points": []}
    points = await _read(store.equity_curve(uuid.UUID(str(run["id"])), limit=limit), [])
    return {"points": rows_to_jsonable(points)}


@app.get("/api/positions")
async def positions() -> dict[str, Any]:
    store = _store()
    run = await _read(store.active_run(), None)
    if run is None:
        return {"open": [], "closed": []}
    run_id = uuid.UUID(str(run["id"]))
    return {
        "open": rows_to_jsonable(await _read(store.open_positions(run_id), [])),
        "closed": rows_to_jsonable(await _read(store.closed_positions(run_id, limit=200), [])),
    }


@app.get("/api/signals")
async def signals(limit: int = 100) -> dict[str, Any]:
    store = _store()
    run = await _read(store.active_run(), None)
    if run is None:
        return {"signals": [], "orders": []}
    run_id = uuid.UUID(str(run["id"]))
    return {
        "signals": rows_to_jsonable(await _read(store.recent_signals(run_id, limit), [])),
        "orders": rows_to_jsonable(await _read(store.recent_orders(run_id, limit), [])),
    }


@app.get("/api/activity")
async def activity(limit: int = 60) -> dict[str, Any]:
    """Risk events: rejections, breakers, halts. The 'why nothing happened' feed."""
    return {"events": rows_to_jsonable(await _read(_store().recent_risk_events(limit), []))}


@app.get("/api/performance")
async def performance() -> dict[str, Any]:
    store = _store()
    run = await _read(store.active_run(), None)
    if run is None:
        return {"by_strategy": [], "by_symbol": [], "by_regime": []}
    run_id = uuid.UUID(str(run["id"]))
    return {
        "by_strategy": rows_to_jsonable(await _read(store.pnl_breakdown(run_id, "strategy"), [])),
        "by_symbol": rows_to_jsonable(await _read(store.pnl_breakdown(run_id, "symbol"), [])),
        "by_regime": rows_to_jsonable(await _read(store.pnl_breakdown(run_id, "regime"), [])),
    }


# ---------------------------------------------------------------- market view


@app.get("/api/market")
async def market(timeframe: str = "4h") -> dict[str, Any]:
    """Current regime and headline features per symbol, from stored bars."""
    store, repo = _store(), _repo()
    tf = TF.parse(timeframe)
    out: list[dict[str, Any]] = []
    for row in await _read(store.symbols(), []):
        bars = await repo.get_bars(
            int(row["id"]), str(row["symbol"]), tf,
            datetime.now(UTC) - tf.delta * 400, datetime.now(UTC),
        )
        if len(bars) < 250:
            continue
        rows = build_features(bars, tf)
        index = len(rows) - 1
        state = classify(rows, index)
        feature = rows[index]
        out.append({
            "symbol": row["symbol"],
            "price": float(bars[-1].close),
            "regime": state.regime,
            "regime_reason": state.reason,
            "allows_trading": state.allows_new_risk,
            "adx": feature.get("adx14"),
            "rsi": feature.get("rsi14"),
            "atr_pct": feature.get("atr_pct"),
            "trend_quality": feature.get("trend_q20"),
            "momentum_24": feature.get("mom24"),
            "volatility": feature.get("vol24"),
            "last_bar": bars[-1].ts.isoformat(),
        })
    return {"timeframe": tf.value, "symbols": out}


@app.get("/api/data")
async def data_coverage() -> dict[str, Any]:
    return {"coverage": rows_to_jsonable(await _read(_store().data_coverage(), []))}


# ------------------------------------------------------------------------ lab


class BacktestRequest(BaseModel):
    symbol: str
    timeframe: str = "4h"
    strategy: str = "trend_follow"
    start: str = "2022-01-01"
    end: str | None = None
    risk_profile: str = "balanced"
    venue: str = "local_irt_generic"
    cost_multiplier: float = Field(default=1.0, ge=0.5, le=5.0)
    use_regime_filter: bool = True


@app.get("/api/strategies")
def strategies() -> dict[str, Any]:
    return {
        "strategies": sorted(ALL_STRATEGIES),
        "benchmarks": sorted(BENCHMARKS),
        "profiles": [p.value for p in RiskProfile],
        "timeframes": [t.value for t in TF],
    }


@app.post("/api/lab/backtest")
async def lab_backtest(req: BacktestRequest) -> dict[str, Any]:
    """Run a backtest on stored history and return the result plus its benchmarks.

    Lab work never touches the trading session: it reads bars, computes, and
    returns. Nothing here can open a position.
    """
    repo, store = _repo(), _store()
    catalogue = await _read(store.symbols(), None)
    if catalogue is None:
        raise DatabaseUnavailable
    symbols = {str(s["symbol"]): int(s["id"]) for s in catalogue}
    symbol_id = symbols.get(req.symbol)
    if symbol_id is None:
        raise HTTPException(status_code=404, detail=f"unknown symbol {req.symbol!r}")
    try:
        tf = TF.parse(req.timeframe)
        risk_profile = RiskProfile(req.risk_profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    start = datetime.fromisoformat(req.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(req.end).replace(tzinfo=UTC) if req.end else datetime.now(UTC)
    bars = await repo.get_bars(symbol_id, req.symbol, tf, start, end)
    if len(bars) < 300:
        raise HTTPException(
            status_code=400,
            detail=f"only {len(bars)} bars stored for {req.symbol} {tf.value}; "
                   "load more history from the Data tab",
        )

    venue = profile(req.venue)
    cost = CostModel(fees=venue.fees, spread_bps=venue.typical_spread_bps).scaled(
        Decimal(str(req.cost_multiplier))
    )
    config = BacktestConfig(
        starting_equity=_paper_balance(),
        cost=cost,
        use_regime_filter=req.use_regime_filter,
    )
    risk = RiskEngine(limits_for(risk_profile))
    rows = build_features(bars, tf)

    def run_one(name: str) -> dict[str, Any]:
        result = Backtester(risk, config).run(bars, build(name), rows=rows)
        m = result.metrics
        return {
            "strategy": name,
            "trades": m.n_trades,
            "total_return": m.total_return,
            "cagr": m.cagr,
            "sharpe": m.sharpe,
            "deflated_sharpe": m.deflated_sharpe,
            "sortino": m.sortino if m.sortino != float("inf") else None,
            "max_drawdown": m.max_drawdown,
            "win_rate": m.win_rate,
            "profit_factor": m.profit_factor if m.profit_factor != float("inf") else None,
            "fee_drag": m.fee_drag,
            "exposure": m.exposure,
            "equity": result.equity[:: max(1, len(result.equity) // 400)],
            "stamps": [s.isoformat() for s in result.stamps[:: max(1, len(result.stamps) // 400)]],
            "rejections": result.rejections,
        }

    primary = run_one(req.strategy)
    benchmarks = [run_one(name) for name in sorted(BENCHMARKS)]
    beaten = all(primary["total_return"] > b["total_return"] for b in benchmarks)
    return {
        "symbol": req.symbol,
        "timeframe": tf.value,
        "bars": len(bars),
        "period": {"from": bars[0].ts.isoformat(), "to": bars[-1].ts.isoformat()},
        "cost_multiplier": req.cost_multiplier,
        "round_trip_cost": float(venue.round_trip_cost()) * req.cost_multiplier,
        "result": primary,
        "benchmarks": benchmarks,
        "beats_all_benchmarks": beaten,
    }


# ------------------------------------------------------------------- settings


class SettingsPatch(BaseModel):
    paper_balance: float | None = Field(default=None, gt=0)
    risk_profile: str | None = None


@app.get("/api/settings")
def read_settings() -> dict[str, Any]:
    settings = get_settings()
    limits = limits_for(_profile())
    return {
        "mode": settings.mode,
        "risk_profile": _profile(),
        "paper_balance": float(_paper_balance()),
        "profiles": [p.value for p in RiskProfile],
        "limits": {k: str(v) for k, v in limits.model_dump().items()},
        "data_source": settings.public_data_base,
    }


@app.patch("/api/settings")
def patch_settings(patch: SettingsPatch) -> dict[str, Any]:
    """Change the paper wallet size or the active risk profile.

    Applies to lab runs immediately; the trader picks it up when it restarts,
    because changing risk limits underneath open positions is how a 1% risk
    setting silently becomes a 4% one.
    """
    if patch.risk_profile is not None:
        try:
            _STATE["risk_profile"] = RiskProfile(patch.risk_profile).value
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if patch.paper_balance is not None:
        _STATE["paper_balance"] = patch.paper_balance
    return read_settings()


@app.get("/api/wallet")
def wallet() -> dict[str, Any]:
    """Wallet and venue connection status.

    Real-money venues are operator-installed plugins. Credentials are read from
    the environment only — this endpoint reports whether they are present and
    never returns, accepts, or stores their values. An API that accepts a
    withdrawal-capable key over HTTP is a liability, not a feature.
    """
    settings = get_settings()
    plugins = available_plugins()
    configured = settings.venue_api_key is not None and settings.venue_api_secret is not None
    return {
        "paper": {
            "balance": float(_paper_balance()),
            "currency": "IRT",
            "editable": True,
        },
        "real": {
            "plugin_configured": settings.venue_plugin is not None,
            "plugin_name": settings.venue_plugin,
            "credentials_present": configured,
            "installed_plugins": plugins,
            "unlocked": settings.live_approval_token is not None,
            "how_to_connect": [
                "Install a venue adapter package exposing the 'simin.adapters' entry point.",
                "Set SIMIN_VENUE_PLUGIN, SIMIN_VENUE_API_KEY, SIMIN_VENUE_API_SECRET in the "
                "environment or a Docker secret. Never in the repo, never through this UI.",
                "Use a trade-only key. Disable withdrawal permission at the venue.",
                "Pass all 12 Go/No-Go gates, then set SIMIN_LIVE_APPROVAL_TOKEN and "
                "SIMIN_MODE=live. Start at 2% of intended capital.",
            ],
        },
        "venues": [
            {
                "code": code,
                "display": profile(code).display,
                "maker": float(profile(code).fees.maker),
                "taker": float(profile(code).fees.taker),
                "spread_bps": float(profile(code).typical_spread_bps),
                "round_trip_cost": float(profile(code).round_trip_cost()),
                "supports_short": profile(code).supports_short,
                "notes": profile(code).notes,
            }
            for code in PROFILES
        ],
    }


@app.get("/api/gates")
async def gates() -> dict[str, Any]:
    """Live-trading checklist with whatever evidence currently exists."""
    store = _store()
    run = await _read(store.active_run(), None)
    paper_days = 0
    closed_trades = 0
    if run is not None:
        started = run["started_at"]
        paper_days = max(0, (datetime.now(UTC) - started).days)
        closed = await _read(store.closed_positions(uuid.UUID(str(run["id"])), limit=5000), [])
        closed_trades = len(closed)
    checklist = [
        (1, "walk-forward consistency", ">=70% of windows profitable", None),
        (2, "worst walk-forward window", ">=-15%", None),
        (3, "deflated Sharpe (out of sample)", ">=0.95", None),
        (4, "Monte Carlo probability of ruin", "<=1%", None),
        (5, "survives 2x modelled cost", "still profitable", None),
        (6, "beats every benchmark", "strictly better", None),
        (7, "drawdown within Monte Carlo p95", "no worse", None),
        (8, "meaningful trade count", ">=100 backtested trades", None),
        (9, "paper trading duration", ">=60 days", paper_days >= 60),
        (10, "paper closed trades", ">=200", closed_trades >= 200),
        (11, "operational stability", "0 exceptions, slippage <=1.5x", None),
        (12, "human approval", "explicit sign-off", False),
    ]
    return {
        "gates": [
            {"n": n, "name": name, "required": req, "passed": passed}
            for n, name, req, passed in checklist
        ],
        "evidence": {"paper_days": paper_days, "paper_closed_trades": closed_trades},
        "note": (
            "Gates 1-8 are filled in by running a backtest in the Lab tab. Gates 9-11 accrue "
            "while paper trading runs. Gate 12 is a person, not a computation."
        ),
        "initial_live_allocation": "2% of intended capital",
    }


@app.get("/api/feasibility")
def feasibility(
    monthly_pct: float = 200.0, trades_per_month: int = 60, venue: str = "local_irt_generic"
) -> dict[str, Any]:
    if trades_per_month <= 0:
        raise HTTPException(status_code=400, detail="trades_per_month must be positive")
    return target_feasibility(
        monthly_pct / 100.0, float(profile(venue).round_trip_cost()), trades_per_month
    )


@app.get("/api/bot")
async def bot_status() -> dict[str, Any]:
    """Everything needed to answer "is it running, and what has it done?"."""
    settings = get_settings()
    store = _store()
    await _ensure_control(store)
    state = await _read(store.get_control(), {"paused": False, "reason": None})
    run = await _read(store.active_run(), None)

    since = datetime.now(UTC) - timedelta(days=7)
    activity: dict[str, Any] = {}
    open_positions: list[dict[str, Any]] = []
    latest: dict[str, Any] | None = None
    if run is not None:
        run_id = uuid.UUID(str(run["id"]))
        activity = await _read(store.session_activity(run_id, since), {})
        open_positions = await _read(store.open_positions(run_id), [])
        latest = await _read(store.latest_equity(run_id), None)

    # Only this session's events decide the status. A halt from a previous run
    # is history, not the current state of the bot.
    session_start = run["started_at"] if run else None
    recent_events = await _read(store.recent_risk_events(20, since=session_start), [])
    halted = any(e.get("kind") == "halted" for e in recent_events[:3])
    paused = bool(state.get("paused"))

    if paused:
        status_label = "PAUSED"
    elif halted:
        status_label = "HALTED"
    elif run is not None:
        status_label = "RUNNING"
    else:
        status_label = "NOT STARTED"

    return {
        "status": status_label,
        "paused": paused,
        "pause_reason": state.get("reason"),
        "mode": settings.mode,
        "can_control": settings.mode is not RunMode.LIVE,
        "session_started_at": run["started_at"].isoformat() if run else None,
        "strategies": list(TraderConfig().strategies),
        "symbols": list(TraderConfig().symbols),
        "max_hold_hours": TraderConfig().max_hold_bars,
        "equity": float(latest["equity_irt"]) if latest else None,
        "open_positions": len(open_positions),
        "last_7_days": {k: float(v) if v is not None else 0 for k, v in activity.items()},
        "recent_events": rows_to_jsonable(recent_events[:8]),
    }


@app.post("/api/bot/{action}")
async def bot_control(action: str) -> dict[str, Any]:
    """Start or pause the bot.

    Available in LAB modes only. Starting real trading is not a button: it needs
    an approval token issued out of band once the Go/No-Go checklist passes.
    """
    if action not in ("start", "pause"):
        raise HTTPException(status_code=400, detail="action must be 'start' or 'pause'")
    if get_settings().mode is RunMode.LIVE:
        raise HTTPException(
            status_code=403,
            detail="live trading is not controlled from the dashboard; see docs/03",
        )
    store = _store()
    await _ensure_control(store)
    paused = action == "pause"
    try:
        state = await store.set_control(
            paused=paused, reason="paused from dashboard" if paused else None, by="dashboard"
        )
    except (SQLAlchemyError, OSError) as exc:
        raise DatabaseUnavailable from exc
    return {
        "status": "PAUSED" if paused else "RUNNING",
        "paused": bool(state["paused"]),
        "note": (
            "Open positions are still managed while paused: stops and the holding "
            "ceiling keep working. Pause stops new entries."
            if paused
            else "The trader picks this up within one poll cycle (60s)."
        ),
    }


@app.post("/api/kill-switch")
def kill_switch(reason: str = "manual") -> dict[str, Any]:
    """Stop trading. There is intentionally no endpoint that resumes it."""
    _STATE["kill_switch"] = True
    _STATE["kill_reason"] = reason
    return {"kill_switch": True, "reason": reason, "resume": "requires a human restart"}


# ------------------------------------------------------- compatibility + page


@app.get("/health")
async def health() -> dict[str, Any]:
    body = await status()
    return {"status": "ok", **body}


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    index = _WEB / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="dashboard not built")
    return index.read_text(encoding="utf-8")


def _window(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)
