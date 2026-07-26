from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.market_data.errors import UnsupportedMarketDataInterval
from app.main import app
from app.modules.replay import routes as replay_routes
from app.modules.replay.routes import (
    normalize_replay_interval,
    replay_ingest_days,
    start_replay,
)
from app.routes.auth import create_access_token


@pytest.mark.parametrize("alias", ["1d", "1day", "1 day", "daily"])
def test_replay_daily_aliases_normalize_to_canonical_1d(alias: str):
    assert normalize_replay_interval(alias) == "1d"


@pytest.mark.parametrize("interval", ["1min", "5min", "10min", "15min", "30min"])
def test_replay_canonical_intraday_intervals_are_not_converted(interval: str):
    assert normalize_replay_interval(interval) == interval


@pytest.mark.parametrize("interval", ["5 min", "1wk", "hourly", "", None])
def test_replay_unsupported_intervals_return_clear_validation_error(interval):
    with pytest.raises(ValueError, match="Unsupported Replay interval"):
        normalize_replay_interval(interval)


def test_replay_request_boundary_returns_http_400_for_unsupported_interval():
    with pytest.raises(HTTPException) as caught:
        start_replay(
            symbol="DGKC",
            start_date="2026-07-01",
            end_date="2026-07-10",
            interval="1wk",
            db=object(),
            user=SimpleNamespace(id=1),
        )
    assert caught.value.status_code == 400
    assert "Unsupported Replay interval" in caught.value.detail


def test_real_start_route_passes_canonical_1d_to_ingestion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    observed_intervals = []
    daily_csv = tmp_path / "DGKC_1d.csv"

    class FakeDb:
        def __init__(self):
            self.added = []

        def add(self, value):
            self.added.append(value)

        def commit(self):
            return None

        def refresh(self, value):
            if getattr(value, "id", None) is None:
                value.id = 321

        def rollback(self):
            return None

    fake_db = FakeDb()

    def fake_fetch_and_save(*, user_id, symbol, days, force, interval):
        observed_intervals.append(interval)
        daily_csv.write_text(
            "timestamp,open,high,low,close,volume\n"
            "2026-07-10,100,102,99,101,1000\n"
        )
        return daily_csv, SimpleNamespace(total_rows=1)

    class FakeReplayDataProvider:
        def __init__(self, **kwargs):
            assert kwargs["interval"] == "1d"
            assert daily_csv.exists()

    monkeypatch.setattr(replay_routes, "_safe_replay_housekeeping", lambda db: None)
    monkeypatch.setattr(
        replay_routes,
        "get_data_paths",
        lambda user_id, symbol, interval: (daily_csv, daily_csv.with_suffix(".json")),
    )
    monkeypatch.setattr(replay_routes, "fetch_and_save", fake_fetch_and_save)
    monkeypatch.setattr(replay_routes, "ReplayDataProvider", FakeReplayDataProvider)

    from app.tasks import replay_tasks

    monkeypatch.setattr(
        replay_tasks.start_replay_session_task,
        "apply_async",
        lambda **kwargs: SimpleNamespace(id="test-task-1"),
    )

    app.dependency_overrides[replay_routes.get_db] = lambda: fake_db
    app.dependency_overrides[replay_routes.get_current_user] = lambda: SimpleNamespace(
        id=1
    )
    try:
        with TestClient(app) as client:
            client.cookies.set(
                "access_token",
                create_access_token({"sub": "c3-replay-route-test"}),
            )
            response = client.post(
                "/replay-simulator/start",
                data={
                    "symbol": "DGKC",
                    "start_date": "2026-07-10",
                    "end_date": "2026-07-10",
                    "interval": "1 day",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "QUEUED"
    assert observed_intervals == ["1d"]
    assert daily_csv.name == "DGKC_1d.csv"


def test_replay_templates_display_1_day_but_submit_1d():
    root = Path(__file__).resolve().parents[1]
    expected = '<option value="1d">1 day</option>'
    for template in ("replay.html", "td_replay.html"):
        text = (root / "app" / "templates" / template).read_text()
        assert expected in text
        assert 'value="1 day"' not in text
        assert 'value="1day"' not in text
        assert 'value="daily"' not in text
        assert 'interval === "1d" ? 730 : 30' in text
        assert "The 1 day interval supports up to 2 years" in text


def test_daily_replay_supports_multi_month_range_but_intraday_stays_short():
    start = date(2026, 1, 1)
    end = date(2026, 7, 10)
    assert replay_ingest_days("1d", start, end) == 191

    with pytest.raises(ValueError, match="Intraday Replay range cannot exceed 30"):
        replay_ingest_days("5min", start, end)


def test_daily_replay_range_has_clear_two_year_limit():
    with pytest.raises(ValueError, match="Daily Replay range cannot exceed 730"):
        replay_ingest_days("1d", date(2024, 1, 1), date(2026, 7, 10))


def test_daily_replay_ingest_forwards_1d_and_uses_daily_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.scripts.replay import data_ingest, replay_data_provider

    monkeypatch.setattr(data_ingest, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(replay_data_provider, "DATA_DIR", str(tmp_path))
    observed = []

    def fake_history(*, symbol, interval, lookback_days, need_extended_hours):
        observed.append(interval)
        index = pd.DatetimeIndex(
            ["2024-01-01", "2024-01-02"], tz="Asia/Karachi", name="timestamp"
        )
        return pd.DataFrame(
            {
                "open": [100.0, 101.0],
                "high": [102.0, 103.0],
                "low": [99.0, 100.0],
                "close": [101.0, 102.0],
                "volume": [1000.0, 1100.0],
            },
            index=index,
        )

    monkeypatch.setattr(data_ingest, "get_schwab_history", fake_history)
    csv_path, meta = data_ingest.fetch_and_save(
        user_id=7,
        symbol="DGKC",
        days=2,
        force=True,
        interval="1d",
    )

    assert observed == ["1d"]
    assert csv_path.name == "DGKC_1d.csv"
    assert meta.source_mode == "psx_daily_v1"
    assert meta.first_bar.startswith("2024-01-01")

    provider = replay_data_provider.ReplayDataProvider(
        user_id=7,
        symbol="DGKC",
        start_date="2024-01-01",
        end_date="2024-01-02",
        interval="1d",
    )
    assert provider.csv_path == csv_path
    assert provider.total_bars == 2
    assert list(provider.bars["close"]) == [101.0, 102.0]


def test_psx_runner_boundary_remains_strict_about_canonical_1d(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.scripts.stock_algos.base_wiring import get_schwab_history

    monkeypatch.setenv("MARKET_DATA_PROVIDER", "psx_sqlite")
    with pytest.raises(UnsupportedMarketDataInterval, match="only the 1d interval"):
        get_schwab_history("DGKC", "1 day", lookback_days=2)


def test_web_process_does_not_reap_worker_namespace_pid(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.services import replay_process

    class QueryMustNotRun:
        def query(self, *args, **kwargs):
            raise AssertionError("web must not inspect worker-owned Replay PIDs")

    monkeypatch.setenv("REPLAY_PID_LIVENESS_CHECK", "false")
    assert replay_process.pid_liveness_checks_enabled() is False
    assert replay_process.reap_stale_sessions(QueryMustNotRun()) == 0
