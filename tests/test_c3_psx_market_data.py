from __future__ import annotations

import os
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.market_data.errors import (
    MarketDataConfigurationError,
    MarketDataQualityError,
    MarketDataRequestError,
    UnknownSymbolError,
    UnsupportedMarketDataInterval,
)
from app.market_data.factory import get_market_data_provider
from app.market_data.history import fetch_compatibility_response
from app.market_data.models import MarketDataRequest
from app.market_data.psx_sqlite import (
    PsxSqliteMarketDataProvider,
    trade_date_to_epoch_ms,
)

SCHEMA = """
CREATE TABLE daily_ohlc (
    trade_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    ldcp REAL,
    open_missing INTEGER DEFAULT 0,
    open_adj REAL,
    high_adj REAL,
    low_adj REAL,
    close_adj REAL,
    volume_adj REAL,
    adj_factor REAL
)
"""


def _row(
    trade_date: str,
    *,
    symbol: str = "OGDC",
    open_value: float | None = 100,
    high: float | None = 110,
    low: float | None = 90,
    close: float | None = 105,
    volume: float | None = 1_000,
    open_missing: int = 0,
    factor: float | None = 1.0,
    ldcp: float | None = 99,
) -> tuple:
    return (
        trade_date,
        symbol,
        open_value,
        high,
        low,
        close,
        volume,
        ldcp,
        open_missing,
        open_value,
        high,
        low,
        close,
        volume,
        factor,
    )


@pytest.fixture
def psx_db(tmp_path: Path) -> Path:
    path = tmp_path / "market.db"
    connection = sqlite3.connect(path)
    connection.execute(SCHEMA)
    connection.executemany(
        "INSERT INTO daily_ohlc VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [_row("2024-01-03"), _row("2024-01-01"), _row("2024-01-02")],
    )
    connection.commit()
    connection.close()
    return path


def _request(
    symbol: str = "OGDC",
    start: date = date(2024, 1, 1),
    end: date = date(2024, 1, 3),
    frequency_type: str = "daily",
    frequency: int = 1,
) -> MarketDataRequest:
    return MarketDataRequest(symbol, start, end, frequency_type, frequency)


def _replace_rows(path: Path, rows: list[tuple]) -> PsxSqliteMarketDataProvider:
    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM daily_ohlc")
    connection.executemany(
        "INSERT INTO daily_ohlc VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    connection.commit()
    connection.close()
    return PsxSqliteMarketDataProvider(path)


def test_valid_adjusted_bars_are_sorted_inclusive_and_auditable(psx_db: Path):
    result = PsxSqliteMarketDataProvider(psx_db).fetch(_request())

    assert [bar.trade_date.isoformat() for bar in result.audit_bars] == [
        "2024-01-01",
        "2024-01-02",
        "2024-01-03",
    ]
    assert result.candles[0] == {
        "datetime": trade_date_to_epoch_ms(date(2024, 1, 1)),
        "open": 100.0,
        "high": 110.0,
        "low": 90.0,
        "close": 105.0,
        "volume": 1000.0,
    }
    assert result.audit_bars[0].raw_close == 105
    assert result.quality.returned_bar_count == 3

    bounded = PsxSqliteMarketDataProvider(psx_db).fetch(
        _request(start=date(2024, 1, 2), end=date(2024, 1, 2))
    )
    assert len(bounded.candles) == 1


def test_symbol_normalization_rejection_unknown_and_empty_range(psx_db: Path):
    provider = PsxSqliteMarketDataProvider(psx_db)
    assert provider.fetch(_request(" ogdc ")).symbol == "OGDC"
    with pytest.raises(MarketDataRequestError):
        provider.fetch(_request("OGDC' OR 1=1 --"))
    with pytest.raises(UnknownSymbolError):
        provider.fetch(_request("MISSING"))

    empty = provider.fetch(
        _request(start=date(2020, 1, 1), end=date(2020, 1, 2))
    )
    assert empty.candles == []
    assert empty.compatibility_response()["empty"] is True


def test_configuration_schema_and_read_only_enforcement(psx_db: Path, tmp_path: Path):
    with pytest.raises(MarketDataConfigurationError):
        PsxSqliteMarketDataProvider("relative.db")
    with pytest.raises(MarketDataConfigurationError):
        PsxSqliteMarketDataProvider(tmp_path / "missing.db")

    invalid = tmp_path / "invalid.db"
    sqlite3.connect(invalid).close()
    with pytest.raises(MarketDataConfigurationError):
        PsxSqliteMarketDataProvider(invalid)

    provider = PsxSqliteMarketDataProvider(psx_db)
    with provider._connect() as connection:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE forbidden(value INTEGER)")


def test_duplicate_and_structurally_invalid_rows_fail(psx_db: Path):
    cases = [
        (
            [_row("2024-01-01"), _row("2024-01-01")],
            "duplicate_date",
        ),
        (
            [_row("2024-01-01", high=None)],
            "missing_required_adjusted_field",
        ),
        (
            [_row("2024-01-01", close=float("inf"))],
            "non_finite_ohlcv",
        ),
        (
            [_row("2024-01-01", close=0)],
            "non_positive_ohlc",
        ),
        (
            [_row("2024-01-01", high=80, low=90)],
            "high_below_low",
        ),
        (
            [_row("2024-01-01", volume=-1)],
            "negative_volume",
        ),
    ]
    for rows, classification in cases:
        provider = _replace_rows(psx_db, rows)
        with pytest.raises(MarketDataQualityError) as caught:
            provider.fetch(_request(end=date(2024, 1, 1)))
        assert caught.value.quality["counts"][classification] == 1


def test_outside_range_is_preserved_and_machine_reported(psx_db: Path):
    provider = _replace_rows(
        psx_db,
        [
            _row("2024-01-01", open_value=89),
            _row("2024-01-02", close=111),
        ],
    )
    result = provider.fetch(_request(end=date(2024, 1, 2)))

    assert result.candles[0]["open"] == 89
    assert result.candles[1]["close"] == 111
    assert result.quality.counts["open_outside_range"] == 1
    assert result.quality.counts["close_outside_range"] == 1
    assert result.quality.affected_dates["open_outside_range"] == ["2024-01-01"]


def test_authorized_missing_open_falls_back_to_adjusted_close(psx_db: Path):
    provider = _replace_rows(
        psx_db,
        [_row("2024-01-01", open_value=None, open_missing=1, close=104)],
    )
    result = provider.fetch(_request(end=date(2024, 1, 1)))
    assert result.candles[0]["open"] == 104
    assert result.quality.counts["missing_open_fallback"] == 1

    provider = _replace_rows(
        psx_db,
        [_row("2024-01-01", open_value=None, open_missing=0)],
    )
    with pytest.raises(MarketDataQualityError):
        provider.fetch(_request(end=date(2024, 1, 1)))


def test_audit_limitations_are_reported_without_rejecting_candle(psx_db: Path):
    provider = _replace_rows(
        psx_db,
        [_row("2024-01-01", factor=None, ldcp=None)],
    )
    result = provider.fetch(_request(end=date(2024, 1, 1)))
    assert len(result.candles) == 1
    assert result.quality.counts["missing_adj_factor"] == 1
    assert result.quality.counts["missing_ldcp"] == 1


def test_timestamp_is_karachi_midnight_and_host_timezone_independent():
    expected = 1704049200000
    observed = []
    original = os.environ.get("TZ")
    try:
        for host_tz in ("UTC", "America/New_York"):
            os.environ["TZ"] = host_tz
            if hasattr(time, "tzset"):
                time.tzset()
            observed.append(trade_date_to_epoch_ms(date(2024, 1, 1)))
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        if hasattr(time, "tzset"):
            time.tzset()
    assert observed == [expected, expected]
    timestamp = datetime.fromtimestamp(expected / 1000, ZoneInfo("Asia/Karachi"))
    assert timestamp.isoformat() == "2024-01-01T00:00:00+05:00"


def test_unsupported_interval_and_malformed_bounds(psx_db: Path):
    provider = PsxSqliteMarketDataProvider(psx_db)
    with pytest.raises(UnsupportedMarketDataInterval):
        provider.fetch(_request(frequency_type="minute"))
    with pytest.raises(UnsupportedMarketDataInterval):
        provider.fetch(_request(frequency=5))
    with pytest.raises(MarketDataRequestError):
        provider.fetch(
            _request(start=date(2024, 1, 3), end=date(2024, 1, 1))
        )


def test_factory_validation_and_legacy_default(psx_db: Path):
    assert get_market_data_provider(Settings(_env_file=None)) is None
    with pytest.raises(MarketDataConfigurationError):
        get_market_data_provider(
            Settings(_env_file=None, market_data_provider="unknown")
        )
    with pytest.raises(MarketDataConfigurationError):
        get_market_data_provider(
            Settings(_env_file=None, market_data_provider="psx_sqlite")
        )
    provider = get_market_data_provider(
        Settings(
            _env_file=None,
            market_data_provider="psx_sqlite",
            psx_db_path=str(psx_db),
            psx_price_mode="adjusted",
        )
    )
    assert isinstance(provider, PsxSqliteMarketDataProvider)


def test_compatibility_shape_and_no_schwab_access(
    psx_db: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "psx_sqlite")
    monkeypatch.setenv("PSX_DB_PATH", str(psx_db))
    monkeypatch.setenv("PSX_PRICE_MODE", "adjusted")

    def forbidden(*args, **kwargs):
        raise AssertionError("Schwab/OAuth/network path must not run in PSX mode")

    monkeypatch.setattr("requests.get", forbidden)
    monkeypatch.setattr(
        "app.utils.stock.schwab_token.get_valid_access_token", forbidden
    )
    response = fetch_compatibility_response(
        {
            "symbol": "OGDC",
            "frequencyType": "daily",
            "frequency": 1,
            "startDate": trade_date_to_epoch_ms(date(2024, 1, 1)),
            "endDate": trade_date_to_epoch_ms(date(2024, 1, 3)),
            "needExtendedHoursData": "false",
            "needPreviousClose": "false",
        }
    )
    assert set(response) == {"symbol", "empty", "candles", "quality"}
    assert set(response["candles"][0]) == {
        "datetime",
        "open",
        "high",
        "low",
        "close",
        "volume",
    }
    assert response["quality"]["returnedBarCount"] == 3

    with pytest.raises(ValueError, match="extended-hours"):
        fetch_compatibility_response(
            {
                "symbol": "OGDC",
                "frequencyType": "daily",
                "frequency": 1,
                "startDate": trade_date_to_epoch_ms(date(2024, 1, 1)),
                "endDate": trade_date_to_epoch_ms(date(2024, 1, 3)),
                "needExtendedHoursData": "true",
            }
        )


def test_existing_price_history_consumer_routes_to_psx_without_auth(
    psx_db: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "psx_sqlite")
    monkeypatch.setenv("PSX_DB_PATH", str(psx_db))
    monkeypatch.setenv("PSX_PRICE_MODE", "adjusted")

    from app.utils.stock import schwab_price_history

    def forbidden(*args, **kwargs):
        raise AssertionError("Schwab token/network path must not run in PSX mode")

    monkeypatch.setattr(schwab_price_history, "get_valid_access_token", forbidden)
    monkeypatch.setattr(schwab_price_history.requests, "get", forbidden)
    response = schwab_price_history.get_schwab_history(
        "OGDC",
        frequencyType="daily",
        frequency=1,
        startDate=trade_date_to_epoch_ms(date(2024, 1, 1)),
        endDate=trade_date_to_epoch_ms(date(2024, 1, 3)),
    )
    assert response["symbol"] == "OGDC"
    assert len(response["candles"]) == 3
