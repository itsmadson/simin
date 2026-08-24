"""API surface behind the dashboard.

These run against a live app with no database, which is the state a fresh
install is in: every endpoint must degrade to an honest empty answer rather
than a 500.
"""

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from simin.api import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_status_reports_mode_and_lock_state(client):
    body = client.get("/api/status").json()
    assert body["mode"] in ("backtest", "paper", "live")
    assert body["mode_label"] in ("LAB", "REAL")
    assert body["real_mode_unlocked"] is False   # no approval token by default


def test_health_alias_still_works(client):
    assert client.get("/health").json()["status"] == "ok"


def test_wallet_never_returns_credentials(client):
    body = client.get("/api/wallet").json()
    serialized = str(body)
    assert "secret" not in serialized.lower() or "api_secret" not in serialized
    assert body["real"]["unlocked"] is False
    assert body["paper"]["editable"] is True
    assert len(body["real"]["how_to_connect"]) >= 4


def test_wallet_lists_venue_costs(client):
    body = client.get("/api/wallet").json()
    local = next(v for v in body["venues"] if v["code"] == "local_irt_generic")
    assert local["round_trip_cost"] > 0.01
    assert local["supports_short"] is False


def test_paper_balance_and_profile_are_editable(client):
    updated = client.patch(
        "/api/settings", json={"paper_balance": 5_000_000, "risk_profile": "aggressive"}
    ).json()
    assert updated["paper_balance"] == 5_000_000
    assert updated["risk_profile"] == "aggressive"
    # and it is reflected everywhere the number is shown
    assert client.get("/api/wallet").json()["paper"]["balance"] == 5_000_000
    client.patch("/api/settings", json={"risk_profile": "balanced"})


def test_invalid_profile_is_rejected(client):
    assert client.patch("/api/settings", json={"risk_profile": "yolo"}).status_code == 400


def test_negative_paper_balance_is_rejected(client):
    assert client.patch("/api/settings", json={"paper_balance": -5}).status_code == 422


def test_strategies_endpoint_lists_what_the_lab_can_run(client):
    body = client.get("/api/strategies").json()
    assert "trend_follow" in body["strategies"]
    assert "buy_and_hold" in body["benchmarks"]
    assert "4h" in body["timeframes"]


def test_gates_endpoint_lists_all_twelve_with_evidence(client):
    body = client.get("/api/gates").json()
    assert len(body["gates"]) == 12
    assert body["gates"][-1]["passed"] is False      # human approval, never automatic
    assert "paper_days" in body["evidence"]


def test_feasibility_uses_the_live_cost_model(client):
    body = client.get("/api/feasibility?monthly_pct=200&trades_per_month=60").json()
    assert body["annual_multiple"] > 500_000
    assert "ruin" in body["verdict"]


def test_feasibility_rejects_zero_frequency(client):
    assert client.get("/api/feasibility?trades_per_month=0").status_code == 400


def test_kill_switch_stops_and_offers_no_resume(client):
    body = client.post("/api/kill-switch?reason=test").json()
    assert body["kill_switch"] is True
    assert "human" in body["resume"]
    assert client.get("/api/status").json()["trading_enabled"] is False
    assert not any(r.path == "/api/resume" for r in app.routes)


def test_lab_rejects_an_unknown_symbol(client):
    body = {"symbol": "NOTACOIN", "timeframe": "4h", "strategy": "trend_follow"}
    assert client.post("/api/lab/backtest", json=body).status_code in (404, 503)


def test_dashboard_is_served_with_sidebar_tabs_and_both_languages(client):
    html = client.get("/").text
    assert "سیمین" in html and "Simin" in html
    for page in ("overview", "positions", "lab", "wallet", "golive", "settings"):
        assert page in html
    assert 'dir="rtl"' in html or "rtl" in html
    assert "cdn" not in html.lower()          # no external requests, ever
