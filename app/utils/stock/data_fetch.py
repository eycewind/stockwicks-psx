#/var/www/stockwicks/app/utils/stock/data_fetch.py
import pandas as pd
import pytz
import requests
import logging
import time
from app.utils.stock.schwab_token import get_valid_access_token

def get_schwab_1min_history(symbol, num_days=7):
    """Fetch 1-min OHLCV data from Schwab API for the last N days."""
    eastern = pytz.timezone('US/Eastern')
    all_dfs = []
    today = pd.Timestamp.now(tz=eastern).date()

    for days_back in range(num_days):
        day = today - pd.Timedelta(days=days_back)
        if day.weekday() >= 5:  # skip weekends
            continue

        start_dt = pd.Timestamp(day, tz=eastern).replace(hour=9, minute=30)
        end_dt = pd.Timestamp(day, tz=eastern).replace(hour=16, minute=0)
        start_ms, end_ms = int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)

        access_token = get_valid_access_token()
        if not access_token:
            continue

        url = "https://api.schwabapi.com/marketdata/v1/pricehistory"
        params = {
            "symbol": symbol.upper(), "frequencyType": "minute", "frequency": 1,
            "startDate": start_ms, "endDate": end_ms, "needExtendedHoursData": "false"
        }
        headers = {"Authorization": f"Bearer {access_token}"}

        try:
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            if not data.get("candles"):
                continue

            day_df = pd.DataFrame(data["candles"])
            if "datetime" in day_df.columns:
                day_df["timestamp"] = pd.to_datetime(
                    day_df["datetime"], unit='ms', utc=True
                ).dt.tz_convert(eastern)
                day_df = day_df.set_index("timestamp")

            all_dfs.append(day_df[["open", "high", "low", "close", "volume"]])
            time.sleep(0.2)
        except Exception as e:
            logging.warning(f"[DATA-FETCH] Error for {symbol}: {e}")
            continue

    if not all_dfs:
        return pd.DataFrame()

    df_all = pd.concat(all_dfs).sort_index()
    df_all = df_all[~df_all.index.duplicated(keep='first')]
    return df_all.between_time('09:30', '16:00')


def process_interval(df, interval, symbol):
    """Resample OHLCV data to desired interval."""
    tz = pytz.timezone('US/Eastern')
    if df.empty:
        return None, None, None
    try:
        if not pd.api.types.is_datetime64_any_dtype(df.index):
            df.index = pd.to_datetime(df.index, utc=True)
        elif df.index.tz is None:
            df.index = df.index.tz_localize('UTC')
        df.index = df.index.tz_convert(tz)
    except Exception as e:
        logging.warning(f"[DATA-PROCESS] {symbol} tz parse error: {e}")
        return None, None, None

    rule_map = {
        "1min": "1min", "5min": "5min", "10min": "10min", "15min": "15min",
        "30min": "30min", "1d": "1D", "1wk": "1W"
    }
    rule = rule_map.get(interval)
    if not rule:
        return None, None, None

    resampled = df.resample(rule).agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
    }).dropna()
    if resampled.empty:
        return None, None, None

    return resampled.index[-1], None, resampled
