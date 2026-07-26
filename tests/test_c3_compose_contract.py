from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_psx_database_is_mounted_read_only_without_new_service_or_port():
    compose = yaml.safe_load((ROOT / "compose.dev.yml").read_text())
    services = compose["services"]
    assert set(services) == {"web", "postgres", "redis", "worker"}
    for name in ("web", "worker"):
        mounts = services[name]["volumes"]
        assert any(
            mount.endswith(":/market-data/psx.db:ro")
            and "PSX_DB_HOST_PATH" in mount
            for mount in mounts
        )
        environment = services[name]["environment"]
        assert environment["PSX_DB_PATH"] == "/market-data/psx.db"
        assert environment["PSX_PRICE_MODE"] == "${PSX_PRICE_MODE:-adjusted}"
    assert "ports" not in services["postgres"]
    assert "ports" not in services["redis"]
    assert all("beat" not in name.lower() for name in services)


def test_psx_mode_keeps_c2_safety_and_blank_schwab_credentials():
    compose = yaml.safe_load((ROOT / "compose.dev.yml").read_text())
    environment = compose["services"]["web"]["environment"]
    assert environment["MARKET_DATA_PROVIDER"] == (
        "${MARKET_DATA_PROVIDER:-psx_sqlite}"
    )
    assert environment["TRADING_ENABLED"] == "false"
    assert environment["PAPER_TRADING_ENABLED"] == "false"
    assert environment["LIVE_TRADING_ENABLED"] == "false"
    assert environment["EMERGENCY_STOP"] == "true"
    assert environment["SCHWAB_CLIENT_ID"] == ""
    assert environment["SCHWAB_CLIENT_SECRET"] == ""
    assert "/home/hassan" not in (ROOT / "compose.dev.yml").read_text()
