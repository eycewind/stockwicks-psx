from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from app.config import Settings

from .factory import get_market_data_provider
from .models import MarketDataRequest, MarketDataResult

PSX_TIMEZONE = ZoneInfo("Asia/Karachi")


def epoch_ms_to_psx_date(value: int) -> date:
    return datetime.fromtimestamp(int(value) / 1000, tz=PSX_TIMEZONE).date()


def fetch_psx_history(
    *,
    symbol: str,
    start_date: date,
    end_date: date,
    frequency_type: str = "daily",
    frequency: int = 1,
    config: Settings | None = None,
) -> MarketDataResult:
    active_config = config or Settings()
    provider = get_market_data_provider(active_config)
    if provider is None:
        raise RuntimeError("fetch_psx_history requires MARKET_DATA_PROVIDER=psx_sqlite")
    return provider.fetch(
        MarketDataRequest(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            frequency_type=frequency_type,
            frequency=frequency,
        )
    )


def result_to_dataframe(result: MarketDataResult) -> pd.DataFrame:
    if not result.candles:
        empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        empty.index = pd.DatetimeIndex([], tz=PSX_TIMEZONE, name="timestamp")
        empty.attrs["quality"] = result.quality.as_dict()
        return empty
    frame = pd.DataFrame(result.candles)
    frame["timestamp"] = (
        pd.to_datetime(frame["datetime"], unit="ms", utc=True)
        .dt.tz_convert(PSX_TIMEZONE)
    )
    output = frame.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
    output.attrs["quality"] = result.quality.as_dict()
    return output


def fetch_compatibility_response(params: dict, config: Settings | None = None) -> dict:
    if "startDate" not in params or "endDate" not in params:
        raise ValueError("PSX daily history requires startDate and endDate")
    unsupported = {
        key
        for key in ("periodType", "period")
        if key in params and params[key] is not None
    }
    if unsupported:
        raise ValueError("PSX history does not combine period fields with date bounds")
    if str(params.get("needExtendedHoursData", "false")).lower() == "true":
        raise ValueError("PSX daily history does not support extended-hours data")
    if str(params.get("needPreviousClose", "false")).lower() == "true":
        raise ValueError("PSX C3 does not provide previousClose metadata")
    result = fetch_psx_history(
        symbol=params.get("symbol", ""),
        start_date=epoch_ms_to_psx_date(params["startDate"]),
        end_date=epoch_ms_to_psx_date(params["endDate"]),
        frequency_type=str(params.get("frequencyType", "")),
        frequency=int(params.get("frequency", 0)),
        config=config,
    )
    return result.compatibility_response()
