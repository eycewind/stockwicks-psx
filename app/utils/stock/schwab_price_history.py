# app/utils/stock/schwab_price_history.py
import datetime as dt
import logging
import os
import pandas as pd
import pytz
import requests

from .schwab_token import get_valid_access_token

log = logging.getLogger("schwab_history")

BASE_URL = "https://api.schwabapi.com/marketdata/v1/pricehistory"
TZ = pytz.timezone("US/Eastern")

# Keep this for compatibility with any callers that reference it
SCHWAB_INTERVAL_MAP = {
    "1min":  {"frequencyType": "minute", "frequency": 1},
    "5min":  {"frequencyType": "minute", "frequency": 5},
    "10min": {"frequencyType": "minute", "frequency": 10},
    "15min": {"frequencyType": "minute", "frequency": 15},
    "30min": {"frequencyType": "minute", "frequency": 30},
    "1d":    {"frequencyType": "daily",  "frequency": 1},
    "1wk":   {"frequencyType": "weekly", "frequency": 1},
}

def _ms(ts: dt.datetime) -> int:
    """Return milliseconds since epoch; ensure tz-aware UTC."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    else:
        ts = ts.astimezone(dt.timezone.utc)
    return int(ts.timestamp() * 1000)

def _request(params: dict) -> pd.DataFrame:
    """Perform Schwab pricehistory request and return ET-tz DataFrame: [open,high,low,close,volume]."""
    access_token = get_valid_access_token()
    if not access_token:
        raise RuntimeError("No valid Schwab access token.")
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

    resp = requests.get(BASE_URL, headers=headers, params=params)
    try:
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"[SCHWAB] HTTP error: {e} params={params} body={getattr(resp, 'text', '')[:500]}")

    data = resp.json()
    candles = data.get("candles", [])
    if not candles:
        log.warning(f"[SCHWAB] No candles; params={params}")
        return pd.DataFrame()

    df = pd.DataFrame(candles)
    # Schwab returns ms in 'datetime'; convert to ET tz-aware index
    df["timestamp"] = pd.to_datetime(df["datetime"], unit="ms", utc=True).dt.tz_convert(TZ)
    return df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]

def _fetch_intraday(symbol: str, frequency_minutes: int, days_back: int = 5,
                    extended: bool = True) -> pd.DataFrame:
    """
    Intraday helper that uses ONLY startDate/endDate (no periodType/period),
    ensuring 'today' is included (premarket/post included when extended=True).
    """
    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(days=days_back)
    params = {
        "symbol": symbol.upper(),
        "frequencyType": "minute",
        "frequency": int(frequency_minutes),
        # IMPORTANT: do not include 'periodType'/'period' with date bounds
        "startDate": _ms(start),
        "endDate": _ms(now),
        "needExtendedHoursData": str(bool(extended)).lower(),
        "needPreviousClose": "false",
    }
    return _request(params)

def _fetch_daily(symbol: str, months_back: int = 12) -> pd.DataFrame:
    """Daily helper using date bounds for consistency."""
    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(days=months_back * 30)
    params = {
        "symbol": symbol.upper(),
        "frequencyType": "daily",
        "frequency": 1,
        "startDate": _ms(start),
        "endDate": _ms(now),
        "needExtendedHoursData": "false",
        "needPreviousClose": "false",
    }
    return _request(params)

# === Public helpers ===

def get_schwab_1min(symbol: str, period: int = 5) -> pd.DataFrame:
    """Last `period` days of 1-minute bars, ending 'now', incl. pre/post."""
    return _fetch_intraday(symbol, 1, days_back=period, extended=True)

def get_schwab_5min(symbol: str, period: int = 10) -> pd.DataFrame:
    """Last `period` days of 5-minute bars, ending 'now', incl. pre/post."""
    return _fetch_intraday(symbol, 5, days_back=period, extended=True)

def get_schwab_15min(symbol: str, period: int = 10) -> pd.DataFrame:
    """Last `period` days of 15-minute bars, ending 'now', incl. pre/post."""
    return _fetch_intraday(symbol, 15, days_back=period, extended=True)

def get_schwab_30min(symbol: str, period: int = 10) -> pd.DataFrame:
    """Last `period` days of 30-minute bars, ending 'now', incl. pre/post."""
    return _fetch_intraday(symbol, 30, days_back=period, extended=True)

def get_schwab_daily(symbol: str, period: int = 12) -> pd.DataFrame:
    """Last `period` months of daily bars (approx)."""
    return _fetch_daily(symbol, months_back=period)

# === Legacy passthroughs (kept for compatibility) ===

def get_schwab_history(
    symbol,
    periodType="day",
    period=10,
    frequencyType="minute",
    frequency=1,
    startDate=None,
    endDate=None,
    needExtendedHoursData=False,
    needPreviousClose=False
):
    """
    Raw passthrough returning JSON (legacy).
    NOTE: For intraday with today's bars, prefer date-bounded fetch via helpers above.
    """
    access_token = get_valid_access_token()
    if not access_token:
        raise RuntimeError("No valid Schwab access token.")

    params = {
        "symbol": symbol.upper(),
        "frequencyType": frequencyType,
        "frequency": frequency,
        "needExtendedHoursData": str(bool(needExtendedHoursData)).lower(),
        "needPreviousClose": str(bool(needPreviousClose)).lower(),
    }

    # Use either dates OR periodType/period. Date-bounded is recommended.
    if startDate is not None and endDate is not None:
        params["startDate"] = startDate
        params["endDate"] = endDate
    else:
        params["periodType"] = periodType
        params["period"] = period

    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    try:
        resp = requests.get(BASE_URL, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        raise RuntimeError(f"Error in get_schwab_history API passthrough: {e}")

def get_schwab_intraday_multi_day(symbol, interval, num_days, user_id=None, save_file=False):
    """
    Compatibility wrapper for older code.
    Pulls intraday bars for up to num_days using the date-bounded intraday fetch.
    """
    key = str(interval).lower().replace(" ", "")
    freq = SCHWAB_INTERVAL_MAP.get(key, {}).get("frequency")
    if not freq:
        log.error(f"Unsupported interval: {interval}")
        return pd.DataFrame()
    df = _fetch_intraday(symbol, freq, days_back=int(num_days), extended=True)
    if df is None or df.empty:
        return pd.DataFrame()
    return df
