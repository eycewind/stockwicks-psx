from __future__ import annotations

import math
import re
import sqlite3
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from .errors import (
    MarketDataConfigurationError,
    MarketDataProviderError,
    MarketDataQualityError,
    MarketDataRequestError,
    UnknownSymbolError,
    UnsupportedMarketDataInterval,
)
from .models import AuditBar, MarketDataRequest, MarketDataResult, QualitySummary

PSX_TIMEZONE = ZoneInfo("Asia/Karachi")
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]+(?:[.-][A-Z0-9]+)*$")
_REQUIRED_COLUMNS = {
    "trade_date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "ldcp",
    "open_missing",
    "open_adj",
    "high_adj",
    "low_adj",
    "close_adj",
    "volume_adj",
    "adj_factor",
}
_SELECT = """
SELECT trade_date, open, high, low, close, volume, ldcp, open_missing,
       open_adj, high_adj, low_adj, close_adj, volume_adj, adj_factor
FROM daily_ohlc
WHERE symbol = ? AND trade_date >= ? AND trade_date <= ?
ORDER BY trade_date ASC
"""


def normalize_symbol(symbol: str) -> str:
    normalized = (symbol or "").strip().upper()
    if not normalized or not _SYMBOL_PATTERN.fullmatch(normalized):
        raise MarketDataRequestError(
            "symbol must contain only PSX symbol letters, digits, '.', or '-'"
        )
    return normalized


def trade_date_to_epoch_ms(value: date) -> int:
    midnight = datetime.combine(value, time.min, tzinfo=PSX_TIMEZONE)
    return int(midnight.timestamp() * 1000)


class PsxSqliteMarketDataProvider:
    """Read-only daily-bar provider for psx-stock-watcher SQLite data."""

    def __init__(self, db_path: str | Path, price_mode: str = "adjusted"):
        path = Path(db_path)
        if not path.is_absolute():
            raise MarketDataConfigurationError("PSX_DB_PATH must be absolute")
        if price_mode.lower() != "adjusted":
            raise MarketDataConfigurationError(
                "C3 supports only PSX_PRICE_MODE=adjusted"
            )
        if not path.is_file():
            raise MarketDataConfigurationError("PSX database file does not exist")
        self.db_path = path
        self.price_mode = "adjusted"
        self._validate_schema()

    def _connect(self) -> sqlite3.Connection:
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, isolation_level=None)
            connection.execute("PRAGMA query_only = ON")
            connection.row_factory = sqlite3.Row
            return connection
        except sqlite3.Error as exc:
            raise MarketDataProviderError(
                f"Unable to open PSX database read-only: {exc}"
            ) from exc

    def _validate_schema(self) -> None:
        try:
            with self._connect() as connection:
                columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(daily_ohlc)")
                }
        except MarketDataProviderError:
            raise
        except sqlite3.Error as exc:
            raise MarketDataConfigurationError(
                f"PSX database schema could not be read: {exc}"
            ) from exc
        missing = sorted(_REQUIRED_COLUMNS - columns)
        if missing:
            raise MarketDataConfigurationError(
                f"PSX database daily_ohlc is missing required columns: {', '.join(missing)}"
            )

    def fetch(self, request: MarketDataRequest) -> MarketDataResult:
        symbol = normalize_symbol(request.symbol)
        if request.frequency_type.lower() != "daily" or request.frequency != 1:
            raise UnsupportedMarketDataInterval(
                "PSX SQLite supports only frequencyType=daily, frequency=1"
            )
        if request.start_date > request.end_date:
            raise MarketDataRequestError("start date must be on or before end date")

        summary = QualitySummary(
            symbol=symbol,
            start_date=request.start_date.isoformat(),
            end_date=request.end_date.isoformat(),
        )
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    _SELECT,
                    (
                        symbol,
                        request.start_date.isoformat(),
                        request.end_date.isoformat(),
                    ),
                ).fetchall()
                if not rows:
                    exists = connection.execute(
                        "SELECT 1 FROM daily_ohlc WHERE symbol = ? LIMIT 1", (symbol,)
                    ).fetchone()
                    if not exists:
                        raise UnknownSymbolError(f"Unknown PSX symbol: {symbol}")
                    return MarketDataResult(symbol, [], [], summary)
        except UnknownSymbolError:
            raise
        except sqlite3.Error as exc:
            raise MarketDataProviderError(f"PSX database query failed: {exc}") from exc

        candles: list[dict[str, int | float]] = []
        audit_bars: list[AuditBar] = []
        seen_dates: set[date] = set()
        for row in rows:
            trade_date = date.fromisoformat(row["trade_date"])
            if trade_date in seen_dates:
                summary.record("duplicate_date", trade_date)
                self._fail_quality("duplicate symbol/trade_date row", summary)
            seen_dates.add(trade_date)

            adjusted = [
                row["open_adj"],
                row["high_adj"],
                row["low_adj"],
                row["close_adj"],
            ]
            if row["open_missing"] and row["open_adj"] is None:
                adjusted[0] = row["close_adj"]
                summary.record("missing_open_fallback", trade_date)
            if any(value is None for value in adjusted):
                summary.record("missing_required_adjusted_field", trade_date)
                self._fail_quality("missing required adjusted OHLC", summary)
            volume = row["volume_adj"]
            numeric = [*adjusted, volume]
            if any(
                value is None
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                for value in numeric
            ):
                summary.record("non_finite_ohlcv", trade_date)
                self._fail_quality("non-finite adjusted OHLCV", summary)

            open_value, high, low, close = map(float, adjusted)
            volume_value = float(volume)
            if any(value <= 0 for value in (open_value, high, low, close)):
                summary.record("non_positive_ohlc", trade_date)
                self._fail_quality("non-positive adjusted OHLC", summary)
            if high < low:
                summary.record("high_below_low", trade_date)
                self._fail_quality("adjusted high is below adjusted low", summary)
            if volume_value < 0:
                summary.record("negative_volume", trade_date)
                self._fail_quality("negative adjusted volume", summary)
            if open_value < low or open_value > high:
                summary.record("open_outside_range", trade_date)
            if close < low or close > high:
                summary.record("close_outside_range", trade_date)

            factor = row["adj_factor"]
            if factor is None:
                summary.record("missing_adj_factor", trade_date)
            elif not math.isfinite(float(factor)) or float(factor) <= 0:
                summary.record("non_positive_adj_factor", trade_date)
            ldcp = row["ldcp"]
            if ldcp is None:
                summary.record("missing_ldcp", trade_date)
            elif not math.isfinite(float(ldcp)) or float(ldcp) <= 0:
                summary.record("invalid_ldcp", trade_date)

            candles.append(
                {
                    "datetime": trade_date_to_epoch_ms(trade_date),
                    "open": open_value,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume_value,
                }
            )
            audit_bars.append(
                AuditBar(
                    trade_date=trade_date,
                    raw_open=row["open"],
                    raw_high=row["high"],
                    raw_low=row["low"],
                    raw_close=row["close"],
                    raw_volume=row["volume"],
                    adjusted_open=open_value,
                    adjusted_high=high,
                    adjusted_low=low,
                    adjusted_close=close,
                    adjusted_volume=volume_value,
                    adj_factor=factor,
                    ldcp=ldcp,
                    open_missing=bool(row["open_missing"]),
                )
            )

        summary.returned_bar_count = len(candles)
        return MarketDataResult(symbol, candles, audit_bars, summary)

    @staticmethod
    def _fail_quality(message: str, summary: QualitySummary) -> None:
        raise MarketDataQualityError(message, summary.as_dict())
