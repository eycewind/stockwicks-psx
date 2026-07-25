import os
from pathlib import Path

os.environ.update(
    {
        "APP_ENV": "development",
        "DATABASE_URL": "postgresql://stockwicks_dev:local-only@127.0.0.1:5432/stockwicks_dev",
        "DATA_DIR": str(Path(__file__).resolve().parents[1] / "data"),
        "REDUCED_LOCAL_RUNTIME": "true",
        "TRADING_ENABLED": "false",
        "PAPER_TRADING_ENABLED": "false",
        "LIVE_TRADING_ENABLED": "false",
        "EMERGENCY_STOP": "true",
        "SCHWAB_CLIENT_ID": "",
        "SCHWAB_CLIENT_SECRET": "",
    }
)

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def test_required_safety_state():
    assert settings.reduced_local_runtime is True
    assert settings.trading_enabled is False
    assert settings.paper_trading_enabled is False
    assert settings.live_trading_enabled is False
    assert settings.emergency_stop is True
    assert settings.schwab_client_id == ""
    assert settings.schwab_client_secret == ""


def test_startup_health_login_and_static_without_network(monkeypatch):
    contacted_hosts = []

    def deny_network(*args, **kwargs):
        contacted_hosts.append((args, kwargs))
        raise AssertionError("outbound network was attempted")

    monkeypatch.setattr("socket.create_connection", deny_network)
    monkeypatch.setattr("requests.sessions.Session.request", deny_network)

    with TestClient(app) as client:
        health = client.get("/healthz")
        login = client.get("/auth/login")
        static = client.get("/static/css/style.css")

    assert health.status_code == 200
    assert health.json() == {"ok": True, "service": "stockwicks-commercial-client"}
    assert login.status_code == 200
    assert "<html" in login.text.lower()
    assert static.status_code == 200
    assert static.text
    assert contacted_hosts == []
