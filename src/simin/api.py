"""Read-mostly HTTP API.

Phase 0 surface: health and the cost/target reality checks that the dashboard
shows on its landing page. Trading endpoints arrive with the trading engine;
until then this service cannot place an order, by construction.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from simin import __version__
from simin.config import get_settings
from simin.exchanges.venues import PROFILES, profile

app = FastAPI(title="Simin", version=__version__)


@app.get("/health")
def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "mode": settings.mode,
        "risk_profile": settings.risk_profile,
        "trading_enabled": False,  # no trading engine wired yet — Phase 5+
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
            }
            for code in PROFILES
        ],
        "note": (
            "A signal whose expected move is below the round-trip cost has negative "
            "expectancy regardless of how convincing the chart looks."
        ),
    }


@app.get("/limits")
def limits() -> dict[str, str | int]:
    lim = get_settings().limits
    return {k: str(v) for k, v in lim.model_dump().items()}
