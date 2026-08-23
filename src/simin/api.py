"""HTTP API and dashboard host.

Read-mostly by design. The only mutating endpoint is the kill switch, which can
always stop trading and can never start it: enabling live trading is a deliberate
out-of-band act gated by the Go/No-Go checklist, not a button.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from simin import __version__
from simin.config import RiskProfile, get_settings, limits_for
from simin.exchanges.venues import PROFILES, profile
from simin.validation.gates import target_feasibility

app = FastAPI(title="Simin", version=__version__)

_STATE: dict[str, Any] = {"kill_switch": False, "kill_reason": None}
_WEB = Path(__file__).parent / "web"


@app.get("/health")
def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "mode": settings.mode,
        "risk_profile": settings.risk_profile,
        "trading_enabled": not _STATE["kill_switch"],
        "live_gated": settings.live_approval_token is None,
        "server_time": datetime.now(UTC).isoformat(),
    }


@app.get("/costs")
def costs() -> dict[str, Any]:
    return {
        "venues": [
            {
                "code": code,
                "display": profile(code).display,
                "maker": str(profile(code).fees.maker),
                "taker": str(profile(code).fees.taker),
                "typical_spread_bps": str(profile(code).typical_spread_bps),
                "round_trip_cost": str(profile(code).round_trip_cost()),
                "supports_short": profile(code).supports_short,
                "notes": profile(code).notes,
            }
            for code in PROFILES
        ],
        "note": (
            "A signal whose expected move is below the round-trip cost has negative "
            "expectancy regardless of how convincing the chart looks."
        ),
    }


@app.get("/limits")
def limits(risk_profile: str | None = None) -> dict[str, Any]:
    try:
        selected = RiskProfile(risk_profile) if risk_profile else get_settings().risk_profile
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"unknown risk profile {risk_profile!r}"
        ) from exc
    values = {k: str(v) for k, v in limits_for(selected).model_dump().items()}
    return {"profile": selected, "limits": values}


@app.get("/feasibility")
def feasibility(monthly_pct: float = 200.0, trades_per_month: int = 60,
                venue: str = "local_irt_generic") -> dict[str, Any]:
    """What a monthly target actually requires, recomputed from the live cost model."""
    if trades_per_month <= 0:
        raise HTTPException(status_code=400, detail="trades_per_month must be positive")
    cost = float(profile(venue).round_trip_cost())
    return target_feasibility(monthly_pct / 100.0, cost, trades_per_month)


@app.get("/gates")
def gates() -> dict[str, Any]:
    """The live-trading checklist. Always visible, so the bar stays concrete."""
    return {
        "gates": [
            {"n": 1, "name": "walk-forward consistency", "required": ">=70% of windows profitable"},
            {"n": 2, "name": "worst walk-forward window", "required": ">=-15%"},
            {"n": 3, "name": "deflated Sharpe (out of sample)", "required": ">=0.95"},
            {"n": 4, "name": "Monte Carlo probability of ruin", "required": "<=1%"},
            {"n": 5, "name": "survives 2x modelled cost", "required": "still profitable"},
            {"n": 6, "name": "beats every benchmark incl. hold-USDT",
             "required": "strictly better"},
            {"n": 7, "name": "drawdown within Monte Carlo p95", "required": "no worse"},
            {"n": 8, "name": "meaningful trade count", "required": ">=100"},
            {"n": 9, "name": "paper trading duration", "required": ">=60 days"},
            {"n": 10, "name": "paper closed trades", "required": ">=200"},
            {"n": 11, "name": "operational stability",
         "required": "0 exceptions, slippage <=1.5x"},
            {"n": 12, "name": "human approval", "required": "explicit sign-off"},
        ],
        "initial_live_allocation": "2% of intended capital",
    }


@app.get("/portfolio")
def portfolio() -> dict[str, Any]:
    """Portfolio snapshot.

    Reports equity in both IRT and USDT on purpose: a Toman-only figure cannot
    distinguish trading skill from rial devaluation (docs/01 §0.1).
    """
    settings = get_settings()
    balance = settings.paper_start_balance_irt
    return {
        "mode": settings.mode,
        "balance_irt": str(balance),
        "equity_irt": str(balance),
        "equity_usdt": None,
        "unrealized_pnl": "0",
        "realized_pnl": "0",
        "drawdown": "0",
        "positions": [],
        "note": "No trading engine session is attached; start the trader service to populate.",
    }


@app.post("/kill-switch")
def kill_switch(reason: str = "manual") -> dict[str, Any]:
    """Stop trading. There is intentionally no matching endpoint to resume."""
    _STATE["kill_switch"] = True
    _STATE["kill_reason"] = reason
    return {"kill_switch": True, "reason": reason, "resume": "requires a human restart"}


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    index = _WEB / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="dashboard not built")
    return index.read_text(encoding="utf-8")


def _decimal_str(value: Decimal) -> str:
    return str(value)
