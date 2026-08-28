"""HTTP and WebSocket API.

The frontend talks only to this. Three rules shape it:

* **Credentials never appear in a response.** Not masked, not partial — the
  settings endpoint returns whether a key is *configured*, never any part of it.
* **Every number that could be mistaken for a promise ships next to its
  measurement.** `/api/dial` returns target and measured together, and the
  measured field is explicitly `null` when calibration has not been run, so the
  UI cannot accidentally render a target as if it were a result.
* **Starting REAL mode is a distinct, explicit call** with its own confirmation
  payload. There is no `PATCH /settings {mode: "real"}` shortcut.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from simin.config import Settings, reset_settings_cache, settings as get_settings
from simin.core.types import TF, ExitReason, MarketKind, Mode, Symbol
from simin.exchanges.base import ExchangeError
from simin.exchanges.costs import cost_model
from simin.exchanges.registry import VENUES, adapt_profile, build_exchange, venue_info
from simin.execution.runner import BotState, Runner
from simin.indicators.features import FeatureFrame
from simin.lab.backtest import Backtester
from simin.lab.calibrate import CalibrationStore, calibrate_level, fingerprint
from simin.lab.validation import evaluate_gates, monte_carlo, walk_forward
from simin.logging import configure, get_logger
from simin.risk.dial import all_profiles, ladder, profile
from simin.strategies.base import build_many
from simin.strategies.library import STRATEGIES, strategies_for_level

log = get_logger(__name__)


class AppState:
    """Everything the process holds. One bot at a time, by design.

    Running several bots on one account means several risk engines each sizing
    from an equity figure the others are also spending. The dial's guarantees
    only hold when one engine owns the account.
    """

    def __init__(self) -> None:
        self.runner: Runner | None = None
        self.settings: Settings = get_settings()
        self.store = CalibrationStore(self.settings.data_dir / "calibration.json")
        self.lab_jobs: dict[str, dict[str, Any]] = {}
        self.subscribers: set[WebSocket] = set()

    def require_runner(self) -> Runner:
        if self.runner is None:
            raise HTTPException(409, "no bot is running — start one first")
        return self.runner


state = AppState()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure(state.settings.log_level, state.settings.log_json)
    state.settings.data_dir.mkdir(parents=True, exist_ok=True)
    log.info("simin starting", mode=state.settings.mode.value, venue=state.settings.venue)
    yield
    if state.runner is not None:
        with contextlib.suppress(Exception):
            await state.runner.stop()
    log.info("simin stopped")


app = FastAPI(
    title="Simin",
    description="سیمین — adaptive crypto trading with an honest risk dial",
    version="2.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(state.settings.cors_origins) or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Models ---------------------------------------------------------------


#: REAL mode requires this typed verbatim. A checkbox is too easy to click
#: through on autopilot; typing out a sentence is not.
REAL_CONFIRMATION = "I understand this trades real money"


class StartRequest(BaseModel):
    risk_level: int = Field(ge=1, le=10)
    mode: str = "lab"
    venue: str = "paper"
    symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT"])
    starting_equity: float = 10000.0
    #: Must equal REAL_CONFIRMATION when mode is "real".
    confirmation: str = ""


class RiskLevelRequest(BaseModel):
    level: int = Field(ge=1, le=10)


class StopRequest(BaseModel):
    flatten: bool = False


class BacktestRequest(BaseModel):
    risk_level: int = Field(ge=1, le=10)
    symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT"])
    venue: str = "coinex"
    timeframe: str = ""
    bars: int = Field(default=3000, ge=500, le=20000)
    starting_equity: float = 10000.0
    stress_costs: bool = False
    walk_forward: bool = False


# --- Meta -----------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "time": datetime.now(UTC).isoformat(),
        "mode": state.settings.mode.value,
        "bot": state.runner.state.value if state.runner else "stopped",
    }


@app.get("/api/dial")
async def dial() -> dict[str, Any]:
    """The risk dial: every level, target beside measurement.

    `measured_monthly_return` is null until calibration has run. The UI must
    render that null as "not yet measured", never as zero and never by falling
    back to the target — a target displayed where a measurement belongs is the
    exact failure this endpoint is shaped to prevent.
    """
    measured = state.store.all_empirical()
    levels: list[dict[str, Any]] = []
    for p in all_profiles():
        head = p.headline()
        e = measured.get(p.level)
        if e is not None:
            head.update({
                "measured_monthly_return": e.monthly_return_median,
                "measured_monthly_p05": e.monthly_return_p05,
                "measured_monthly_p95": e.monthly_return_p95,
                "measured_max_drawdown": e.max_drawdown_median,
                "measured_max_drawdown_p95": e.max_drawdown_p95,
                "ruin_probability": e.ruin_probability,
                "win_rate": e.win_rate,
                "profit_factor": e.profit_factor,
                "sharpe": e.sharpe,
                "trades_per_month": e.trades_per_month,
                "calibrated": True,
                "calibrated_at": e.calibrated_at,
                "sample_months": e.sample_months,
                "symbol_scope": e.symbol_scope,
            })
        head.update({
            "description_en": p.description_en,
            "description_fa": p.description_fa,
            "signal_timeframe": p.signal_tf.value,
            "context_timeframe": p.context_tf.value,
            "max_positions": p.max_concurrent_positions,
            "max_trades_per_day": p.max_trades_per_day,
            "min_confluence": p.min_confluence,
            "allow_shorts": p.allow_shorts,
            "daily_loss_halt": float(p.daily_loss_halt),
            "max_drawdown_halt": float(p.max_drawdown_halt),
            "take_profit_r": float(p.take_profit_r),
            "atr_stop_mult": float(p.atr_stop_mult),
            "strategies": list(strategies_for_level(p.level)),
        })
        levels.append(head)
    return {
        "levels": levels,
        "any_calibrated": bool(measured),
        "disclaimer_en": (
            "Target returns describe what each level was designed to attempt. They are "
            "not forecasts. Only the measured column reflects what actually happened in "
            "walk-forward testing, and past results do not predict future ones."
        ),
        "disclaimer_fa": (
            "بازدهی هدف فقط نشان می‌دهد هر سطح برای چه چیزی طراحی شده و پیش‌بینی نیست. "
            "تنها ستون «اندازه‌گیری‌شده» نتیجه واقعی آزمون است و نتایج گذشته آینده را "
            "تضمین نمی‌کند."
        ),
    }


@app.get("/api/venues")
async def venues() -> dict[str, Any]:
    return {
        "venues": [
            {
                "name": v.name,
                "display_name": v.display_name,
                "supports_futures": v.supports_futures,
                "supports_shorts": v.supports_shorts,
                "max_leverage": v.max_leverage,
                "quote_asset": v.quote_asset,
                "notes_en": v.notes_en,
                "notes_fa": v.notes_fa,
                "credentials_configured": state.settings.creds(v.name).present,
                "round_trip_cost": float(cost_model(v.name).round_trip),
            }
            for v in VENUES.values()
        ]
    }


@app.get("/api/strategies")
async def strategies() -> dict[str, Any]:
    return {
        "strategies": [
            {
                "name": cls.name,
                "name_fa": cls.name_fa,
                "regime": cls.regime,
                "description": cls.description,
                "description_fa": cls.description_fa,
                "warmup": cls.warmup,
            }
            for cls in STRATEGIES.values()
        ],
        "by_level": {str(i): list(strategies_for_level(i)) for i in range(1, 11)},
    }


@app.get("/api/settings")
async def read_settings() -> dict[str, Any]:
    s = state.settings
    return {
        "mode": s.mode.value,
        "venue": s.venue,
        "risk_level": s.risk_level,
        "symbols": list(s.symbols),
        "quote": s.quote,
        "starting_equity": float(s.starting_equity),
        "max_capital": float(s.max_capital),
        "poll_seconds": s.poll_seconds,
        "trading_frozen": s.trading_frozen,
        "real_mode_acknowledged": s.real_mode_acknowledged,
        # Whether a credential exists — never any part of its value.
        "credentials": {v: s.creds(v).present for v in VENUES},
        "start_problems": s.validate_for_start(),
    }


# --- Bot control ----------------------------------------------------------


@app.get("/api/bot")
async def bot_status() -> dict[str, Any]:
    if state.runner is None:
        return {
            "state": BotState.STOPPED.value,
            "mode": state.settings.mode.value,
            "running": False,
        }
    r = state.runner
    return {
        **r.status().to_dict(),
        "running": r.state.is_active,
        "positions": r.positions_view(),
        "events": r.recent_events(30),
        "decisions": {
            name: {
                "accepted": d.accepted,
                "regime": d.regime,
                "score": d.raw_score,
                "threshold": d.threshold,
                "reason": d.rejected_because,
                "agreeing": list(d.agreeing),
                "dissenting": list(d.dissenting),
            }
            for name, d in r.last_decisions.items()
        },
        "profile": r.profile.headline(),
    }


@app.post("/api/bot/start")
async def start_bot(req: StartRequest) -> dict[str, Any]:
    if state.runner is not None and state.runner.state.is_active:
        raise HTTPException(409, f"bot already {state.runner.state.value}")

    mode = Mode(req.mode.lower())
    if mode is Mode.REAL and req.confirmation != REAL_CONFIRMATION:
        raise HTTPException(
            400,
            "REAL mode requires the exact confirmation phrase: "
            f'"{REAL_CONFIRMATION}". This step is deliberate — real mode '
            "spends real money.",
        )

    import os

    os.environ["SIMIN_MODE"] = mode.value
    os.environ["SIMIN_VENUE"] = req.venue
    os.environ["SIMIN_RISK_LEVEL"] = str(req.risk_level)
    os.environ["SIMIN_SYMBOLS"] = " ".join(req.symbols)
    os.environ["SIMIN_STARTING_EQUITY"] = str(req.starting_equity)
    reset_settings_cache()
    state.settings = get_settings()

    try:
        # The offline venue needs a moving clock to be worth watching; every
        # real venue supplies its own.
        exchange = build_exchange(
            state.settings, demo_speed=3.0 if req.venue == "offline" else 0.0
        )
    except ExchangeError as exc:
        raise HTTPException(400, str(exc)) from exc

    prof = adapt_profile(profile(req.risk_level), exchange)
    info = venue_info(req.venue)
    kind = MarketKind.FUTURES if info.supports_futures else MarketKind.SPOT

    try:
        venue_symbols = {s.name: s for s in await exchange.symbols()}
    except ExchangeError:
        venue_symbols = {}

    symbols: dict[str, Symbol] = {}
    for name in req.symbols:
        found = venue_symbols.get(name)
        symbols[name] = found or Symbol(
            base=name[:-4], quote=name[-4:], venue=req.venue, venue_symbol=name,
            kind=kind, price_precision=2, qty_precision=6,
            max_leverage=info.max_leverage,
        )

    runner = Runner(
        state.settings, exchange, prof, symbols,
        costs=cost_model(req.venue, kind.value),
    )
    runner.profile_clamped = prof.max_leverage < profile(req.risk_level).max_leverage
    try:
        await runner.start()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    state.runner = runner

    return {
        "started": True,
        "mode": mode.value,
        "venue": req.venue,
        "risk_level": prof.level,
        "profile": prof.headline(),
        "clamped": runner.profile_clamped,
        "warnings_en": list(prof.warnings_en),
        "warnings_fa": list(prof.warnings_fa),
    }


@app.post("/api/bot/stop")
async def stop_bot(req: StopRequest) -> dict[str, Any]:
    runner = state.require_runner()
    await runner.stop(flatten=req.flatten)
    return {"stopped": True, "flattened": req.flatten}


@app.post("/api/bot/pause")
async def pause_bot() -> dict[str, Any]:
    runner = state.require_runner()
    await runner.pause()
    return {"state": runner.state.value}


@app.post("/api/bot/resume")
async def resume_bot() -> dict[str, Any]:
    runner = state.require_runner()
    await runner.resume()
    return {"state": runner.state.value}


@app.post("/api/bot/kill")
async def kill_bot() -> dict[str, Any]:
    runner = state.require_runner()
    await runner.kill()
    return {"state": runner.state.value, "message": "all positions closed, trading disabled"}


@app.post("/api/bot/risk")
async def change_risk(req: RiskLevelRequest) -> dict[str, Any]:
    runner = state.require_runner()
    await runner.set_risk_level(req.level)
    return {"risk_level": req.level, "profile": runner.profile.headline()}


@app.post("/api/bot/flatten")
async def flatten() -> dict[str, Any]:
    runner = state.require_runner()
    closed = await runner.flatten_all(ExitReason.MANUAL)
    return {"closed": closed}


@app.get("/api/equity")
async def equity() -> dict[str, Any]:
    if state.runner is None:
        return {"curve": [], "trades": []}
    r = state.runner
    return {
        "curve": [
            {"ts": p.ts.isoformat(), "equity": float(p.equity),
             "drawdown": float(p.drawdown), "positions": p.open_positions}
            for p in r.curve
        ],
        "trades": [
            {
                "symbol": t.symbol, "direction": t.direction.value,
                "entry_price": float(t.entry_price), "exit_price": float(t.exit_price),
                "opened_at": t.opened_at.isoformat(), "closed_at": t.closed_at.isoformat(),
                "net_pnl": float(t.net_pnl), "r_multiple": float(t.r_multiple),
                "reason": t.reason.value, "strategy": t.strategy,
                "leverage": float(t.leverage),
            }
            for t in r.trades[-200:]
        ],
    }


@app.get("/api/candles")
async def candles(symbol: str = "BTCUSDT", timeframe: str = "2h", limit: int = 300) -> dict[str, Any]:
    """Candles plus the indicator overlays the UI draws."""
    tf = TF.parse(timeframe)
    exchange = build_exchange(state.settings)
    try:
        rows = await exchange.candles(symbol, tf, limit=min(limit, 1000))
    except ExchangeError as exc:
        raise HTTPException(502, f"could not fetch candles: {exc}") from exc
    finally:
        await exchange.close()

    if not rows:
        return {"symbol": symbol, "timeframe": tf.value, "candles": [], "indicators": {}}

    frame = FeatureFrame(symbol, tf, rows)
    keys = ("ema_fast", "ema_slow", "ema_trend", "rsi", "macd", "macd_signal",
            "macd_hist", "atr", "bb_upper", "bb_lower", "bb_mid", "adx",
            "supertrend", "stoch_k", "stoch_d", "vwap")
    return {
        "symbol": symbol,
        "timeframe": tf.value,
        "candles": [
            {"ts": c.ts.isoformat(), "o": float(c.open), "h": float(c.high),
             "l": float(c.low), "c": float(c.close), "v": float(c.volume)}
            for c in rows
        ],
        "indicators": {k: frame.series(k) for k in keys},
        "levels": [
            {"price": lv.price, "touches": lv.touches, "strength": lv.strength,
             "kind": lv.kind.value}
            for lv in frame.row(len(frame) - 1).levels
        ],
        "structure": frame.row(len(frame) - 1).structure.structure.value,
    }


# --- Lab ------------------------------------------------------------------


@app.post("/api/lab/backtest")
async def lab_backtest(req: BacktestRequest) -> dict[str, Any]:
    """Run one configuration over history and report it honestly."""
    prof = profile(req.risk_level)
    tf = TF.parse(req.timeframe) if req.timeframe else prof.signal_tf
    info = venue_info(req.venue)
    kind = MarketKind.FUTURES if info.supports_futures else MarketKind.SPOT

    import os

    os.environ["SIMIN_VENUE"] = req.venue
    os.environ["SIMIN_MODE"] = "lab"
    reset_settings_cache()
    exchange = build_exchange(get_settings())

    frames: dict[str, FeatureFrame] = {}
    symbols: dict[str, Symbol] = {}
    try:
        listed = {s.name: s for s in await exchange.symbols()}
        for name in req.symbols:
            rows = await exchange.candles(name, tf, limit=req.bars)
            if len(rows) < 400:
                raise HTTPException(
                    400,
                    f"{name}: only {len(rows)} {tf.value} candles available; "
                    "need at least 400 for a meaningful backtest",
                )
            frames[name] = FeatureFrame(name, tf, rows)
            symbols[name] = listed.get(name) or Symbol(
                name[:-4], name[-4:], req.venue, name, kind, 2, 6,
                max_leverage=info.max_leverage,
            )
    except ExchangeError as exc:
        raise HTTPException(502, f"could not fetch history: {exc}") from exc
    finally:
        await exchange.close()

    costs = cost_model(req.venue, kind.value)
    if req.stress_costs:
        costs = costs.stressed()

    def _run() -> dict[str, Any]:
        bt = Backtester(prof, build_many(strategies_for_level(req.risk_level)),
                        costs, Decimal(str(req.starting_equity)), keep_decision_log=True)
        result = bt.run(frames, symbols, tf)

        bench = Backtester(profile(4), build_many(["buy_and_hold"]), costs,
                           Decimal(str(req.starting_equity))).run(frames, symbols, tf)
        stressed = Backtester(prof, build_many(strategies_for_level(req.risk_level)),
                              costs.stressed(), Decimal(str(req.starting_equity))
                              ).run(frames, symbols, tf)

        wf = None
        if req.walk_forward:
            try:
                wf = walk_forward(prof, strategies_for_level(req.risk_level), frames,
                                  symbols, tf, costs, Decimal(str(req.starting_equity)))
            except ValueError:
                wf = None

        mc = monte_carlo(
            wf.oos_trades if (wf and len(wf.oos_trades) >= 20) else result.trades,
            Decimal(str(req.starting_equity)), prof, result.metrics.days,
        )
        gates = evaluate_gates(result.metrics, stressed.metrics, wf, mc,
                               bench.metrics, prof)
        return {
            **result.to_dict(),
            "benchmark": bench.metrics.to_dict(),
            "stressed_2x_costs": stressed.metrics.to_dict(),
            "walk_forward": wf.to_dict() if wf else None,
            "monte_carlo": mc.to_dict() if mc else None,
            "gates": gates.to_dict(),
            "target_monthly_return": prof.target_monthly_return,
            "cost_model": {
                "round_trip": float(costs.round_trip),
                "breakeven_move": float(costs.breakeven_move()),
                "venue": req.venue,
            },
        }

    # The backtest is CPU-bound; running it inline would block every other
    # request, including the status polling that keeps the UI alive.
    return await asyncio.to_thread(_run)


@app.post("/api/lab/calibrate")
async def lab_calibrate(req: BacktestRequest) -> dict[str, Any]:
    """Measure one dial level on real history and store the result."""
    tf = TF.parse(req.timeframe) if req.timeframe else profile(req.risk_level).signal_tf
    info = venue_info(req.venue)
    kind = MarketKind.FUTURES if info.supports_futures else MarketKind.SPOT

    import os

    os.environ["SIMIN_VENUE"] = req.venue
    os.environ["SIMIN_MODE"] = "lab"
    reset_settings_cache()
    exchange = build_exchange(get_settings())

    frames: dict[str, FeatureFrame] = {}
    symbols: dict[str, Symbol] = {}
    try:
        listed = {s.name: s for s in await exchange.symbols()}
        for name in req.symbols:
            rows = await exchange.candles(name, tf, limit=req.bars)
            frames[name] = FeatureFrame(name, tf, rows)
            symbols[name] = listed.get(name) or Symbol(
                name[:-4], name[-4:], req.venue, name, kind, 2, 6,
                max_leverage=info.max_leverage,
            )
    except ExchangeError as exc:
        raise HTTPException(502, str(exc)) from exc
    finally:
        await exchange.close()

    costs = cost_model(req.venue, kind.value)

    def _run() -> dict[str, Any]:
        report = calibrate_level(
            req.risk_level, frames, symbols, tf, costs,
            Decimal(str(req.starting_equity)),
        )
        n = min(len(f) for f in frames.values())
        months = n * tf.seconds / 86400 / 30.44
        e = report.empirical(months, ",".join(sorted(frames)))
        state.store.put(req.risk_level, fingerprint(frames, costs), e)
        return report.to_dict()

    return await asyncio.to_thread(_run)


# --- Live stream ----------------------------------------------------------


@app.websocket("/ws")
async def stream(ws: WebSocket) -> None:
    """Push bot status to the UI.

    Pushing rather than polling matters here: a stop moving or a position
    liquidating is not something the user should discover on the next 5-second
    poll.
    """
    await ws.accept()
    state.subscribers.add(ws)
    try:
        while True:
            payload: dict[str, Any] = {"ts": datetime.now(UTC).isoformat()}
            if state.runner is not None:
                r = state.runner
                payload.update({
                    "status": r.status().to_dict(),
                    "positions": r.positions_view(),
                    "events": r.recent_events(10),
                })
            else:
                payload["status"] = {"state": "stopped"}
            await ws.send_json(payload)
            await asyncio.sleep(2)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        state.subscribers.discard(ws)


# --- Static frontend (optional) -------------------------------------------

_static = Path(__file__).parent.parent / "web"
if _static.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_static), html=True), name="web")
