# app/utils/data_utils.py

import pandas as pd
import requests
import pytz
import logging
import time
from datetime import datetime, timedelta, time as dtime

from app.utils.stock.schwab_token import get_valid_access_token

_ET = pytz.timezone("US/Eastern")

def is_market_open_now(now_et: datetime | None = None) -> bool:
    """
    Market open: 00:00 to 23:55 ET
    Closed: 23:55 to 00:00 ET
    """
    now_et = now_et or datetime.now(_ET)
    t = now_et.time()
    return dtime(0, 0) <= t < dtime(23, 55)

def max_bar_age_for_interval(interval: str) -> timedelta:
    """Staleness threshold for each interval."""
    interval = (interval or "1min").lower()
    return {
        "1min": timedelta(minutes=3),
        "5min": timedelta(minutes=12),
        "10min": timedelta(minutes=20),
        "15min": timedelta(minutes=35),
        "30min": timedelta(minutes=80),
        "1h": timedelta(minutes=140),
        "1d": timedelta(days=2),
        "1wk": timedelta(days=14),
    }.get(interval, timedelta(minutes=10))


def get_live_bars_from_schwab(symbol: str, days: int = 7) -> pd.DataFrame:
    all_dfs = []
    eastern = pytz.timezone('US/Eastern')
    today = pd.Timestamp.now(tz=eastern).date()

    for i in range(days):
        date = today - pd.Timedelta(days=i)
        if date.weekday() >= 5: continue  # skip weekends

        start = pd.Timestamp(date, tz=eastern).replace(hour=9, minute=30)
        end   = pd.Timestamp(date, tz=eastern).replace(hour=16, minute=0)
        start_ms, end_ms = int(start.timestamp()*1000), int(end.timestamp()*1000)

        token = get_valid_access_token()
        if not token:
            logging.warning("Missing Schwab access token")
            continue

        try:
            resp = requests.get(
                "https://api.schwabapi.com/marketdata/v1/pricehistory",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "symbol": symbol.upper(),
                    "frequencyType": "minute",
                    "frequency": 1,
                    "startDate": start_ms,
                    "endDate": end_ms,
                    "needExtendedHoursData": "false"
                }
            )
            resp.raise_for_status()
            data = resp.json().get("candles", [])
            if not data: continue

            df = pd.DataFrame(data)
            df["timestamp"] = pd.to_datetime(df["datetime"], unit="ms", utc=True).dt.tz_convert(eastern)
            df = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
            all_dfs.append(df)
            time.sleep(0.2)
        except Exception as e:
            logging.error(f"Schwab fetch error for {symbol} on {date}: {e}")
            continue

    if not all_dfs:
        return pd.DataFrame()
    
    df_all = pd.concat(all_dfs).sort_index()
    df_all = df_all[~df_all.index.duplicated(keep='first')]
    return df_all.between_time("09:30", "16:00")


def resample_bars(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    rule_map = {
        "1min": "1min", "5min": "5min", "10min": "10min",
        "15min": "15min", "30min": "30min",
        "1h": "60min", "1d": "1D", "1wk": "1W",
    }
    rule = rule_map.get(interval)
    if not rule:
        logging.warning(f"Unsupported interval: {interval}")
        return pd.DataFrame()

    return df.resample(rule).agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum"
    }).dropna()
