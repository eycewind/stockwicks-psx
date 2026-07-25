from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_default_and_profile_service_topology():
    compose = yaml.safe_load((ROOT / "compose.dev.yml").read_text())
    services = compose["services"]

    assert set(services) == {"web", "postgres", "redis", "worker"}
    assert "profiles" not in services["web"]
    assert "profiles" not in services["postgres"]
    assert services["redis"]["profiles"] == ["worker"]
    assert services["worker"]["profiles"] == ["worker"]
    assert services["worker"]["healthcheck"] == {"disable": True}
    assert all("beat" not in name.lower() for name in services)
    assert "ports" not in services["postgres"]
    assert "ports" not in services["redis"]
    assert services["web"]["ports"][0].startswith("127.0.0.1:")


def test_compose_forces_safe_effective_flags_and_no_real_credentials():
    compose = yaml.safe_load((ROOT / "compose.dev.yml").read_text())
    environment = compose["services"]["web"]["environment"]

    assert environment["TRADING_ENABLED"] == "false"
    assert environment["PAPER_TRADING_ENABLED"] == "false"
    assert environment["LIVE_TRADING_ENABLED"] == "false"
    assert environment["EMERGENCY_STOP"] == "true"
    assert environment["SCHWAB_CLIENT_ID"] == ""
    assert environment["SCHWAB_CLIENT_SECRET"] == ""
    assert "psx_watcher.db" not in (ROOT / "compose.dev.yml").read_text()
