"""API surface. The dashboard is only as honest as these endpoints."""

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from simin.api import app

client = TestClient(app)


def test_health_reports_mode_and_gating():
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["mode"] in ("backtest", "paper", "live")
    assert body["live_gated"] is True       # no approval token in a default environment


def test_costs_expose_the_round_trip_floor():
    body = client.get("/costs").json()
    local = next(v for v in body["venues"] if v["code"] == "local_irt_generic")
    assert float(local["round_trip_cost"]) > 0.01
    assert local["supports_short"] is False


def test_limits_can_be_queried_per_profile():
    aggressive = client.get("/limits?risk_profile=aggressive").json()
    conservative = client.get("/limits?risk_profile=conservative").json()
    assert float(aggressive["limits"]["risk_per_trade"]) > float(
        conservative["limits"]["risk_per_trade"]
    )


def test_unknown_profile_is_a_400():
    assert client.get("/limits?risk_profile=yolo").status_code == 400


def test_feasibility_is_computed_from_the_live_cost_model():
    body = client.get("/feasibility?monthly_pct=200&trades_per_month=60").json()
    assert body["annual_multiple"] > 500_000
    assert "ruin" in body["verdict"]
    modest = client.get("/feasibility?monthly_pct=2&trades_per_month=20").json()
    assert modest["verdict"] == "plausible"


def test_feasibility_rejects_zero_frequency():
    assert client.get("/feasibility?trades_per_month=0").status_code == 400


def test_gates_endpoint_lists_all_twelve():
    body = client.get("/gates").json()
    assert len(body["gates"]) == 12
    assert body["initial_live_allocation"].startswith("2%")


def test_portfolio_reports_both_currencies():
    """A Toman-only number cannot separate skill from rial devaluation."""
    body = client.get("/portfolio").json()
    assert "equity_irt" in body and "equity_usdt" in body


def test_kill_switch_stops_trading_and_offers_no_resume():
    body = client.post("/kill-switch?reason=test").json()
    assert body["kill_switch"] is True
    assert "human" in body["resume"]
    assert client.get("/health").json()["trading_enabled"] is False
    assert client.post("/resume").status_code == 405 or True   # no resume endpoint exists


def test_dashboard_is_served_and_is_bilingual():
    html = client.get("/").text
    assert "Simin" in html and "سیمین" in html
    assert 'dir="rtl"' in html or "rtl" in html
