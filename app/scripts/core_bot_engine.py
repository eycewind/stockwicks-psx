
#/var/www/stockwicks/app/scripts/core_bot_engine.py
# app/scripts/core_bot_engine.py
import sys
import os
import time
import pandas as pd
import requests
import logging
from datetime import datetime, timedelta
import pytz

SCHWAB_INTERVALS = {
    '1min': ('day', 1, 'minute', 1),
    '5min': ('day', 5, 'minute', 5),
    '10min': ('day', 10, 'minute', 10),
    '15min': ('day', 10, 'minute', 15),
    '30min': ('day', 10, 'minute', 30),
    '1d': ('year', 1, 'daily', 1),
    '1wk': ('year', 1, 'weekly', 1),
}

def safe_symbol_for_files(symbol: str) -> str:
    return str(symbol or "").upper().strip().replace("/", "_").replace(" ", "_")


def get_schwab_1min_history(symbol, num_days=7):
    from app.utils.stock.schwab_token import get_valid_access_token

    all_dfs = []
    now_et = pd.Timestamp.now(tz='US/Eastern')
    today = now_et.date()
    holidays = {pd.Timestamp('2025-09-01').date()}  # Labor Day 2025

    for days_back in range(num_days):
        day = today - timedelta(days=days_back)
        if day.weekday() >= 5 or day in holidays:  # Skip weekends and holidays
            logging.info(f"[{symbol}] Skipping non-trading day {day}")
            continue
        start_dt = pd.Timestamp(day, tz='US/Eastern').replace(hour=9, minute=30)
        end_dt = pd.Timestamp(day, tz='US/Eastern').replace(hour=16, minute=0)
        if start_dt > now_et:
            logging.info(f"[{symbol}] Skipping future date {day}")
            continue

        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        access_token = get_valid_access_token()
        if not access_token:
            logging.error(f"[{symbol}] Failed to get Schwab API token")
            continue

        url = "https://api.schwabapi.com/marketdata/v1/pricehistory"
        params = {
            "symbol": symbol.upper(),
            "frequencyType": "minute",
            "frequency": 1,
            "startDate": start_ms,
            "endDate": end_ms,
            "needExtendedHoursData": "false"
        }
        headers = {"Authorization": f"Bearer {access_token}"}

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.get(url, headers=headers, params=params)
                response.raise_for_status()
                data = response.json()
                candles = data.get("candles", [])
                if not candles:
                    logging.warning(f"[{symbol}] No data for {day}")
                    break

                df = pd.DataFrame(candles)
                if not {"open", "high", "low", "close", "datetime"}.issubset(df.columns):
                    logging.error(f"[{symbol}] Invalid data for {day}: missing columns, got {list(df.columns)}")
                    break

                df["datetime"] = pd.to_datetime(df["datetime"], unit="ms").dt.tz_localize("UTC").dt.tz_convert("US/Eastern")
                df.set_index("datetime", inplace=True)
                all_dfs.append(df)
                logging.info(f"[{symbol}] Fetched 1min data for {day}: {len(df)} rows")
                break
            except Exception as e:
                logging.warning(f"[{symbol}] Fetch attempt {attempt}/{max_attempts} for {day} failed: {e}")
                if attempt == max_attempts:
                    logging.error(f"[{symbol}] Failed to fetch 1min data for {day} after {max_attempts} attempts: {e}")
                time.sleep(min(1 * attempt, 10))  # Exponential backoff

    if all_dfs:
        combined_df = pd.concat(all_dfs).sort_index()
        logging.info(f"[{symbol}] Combined 1min data: {len(combined_df)} rows, date_range={combined_df.index.min()} to {combined_df.index.max()}")
        return combined_df
    logging.warning(f"[{symbol}] No valid 1min data fetched")
    return pd.DataFrame()

def get_stock_data(symbol, interval):
    from app.utils.stock.schwab_token import get_valid_access_token

    if interval not in SCHWAB_INTERVALS:
        raise ValueError(f"Unsupported interval: {interval}")

    periodType, period, frequencyType, frequency = SCHWAB_INTERVALS[interval]
    access_token = get_valid_access_token()
    if not access_token:
        logging.error(f"[{symbol}] No access token available")
        return pd.DataFrame()

    url = "https://api.schwabapi.com/marketdata/v1/pricehistory"
    params = {
        "symbol": symbol.upper(),
        "periodType": periodType,
        "period": period,
        "frequencyType": frequencyType,
        "frequency": frequency,
        "needExtendedHoursData": "false"
    }
    headers = {"Authorization": f"Bearer {access_token}"}

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            candles = data.get("candles", [])
            if not candles:
                logging.warning(f"[{symbol}] No {interval} data returned")
                return pd.DataFrame()

            df = pd.DataFrame(candles)
            if not {"open", "high", "low", "close", "datetime"}.issubset(df.columns):
                logging.error(f"[{symbol}] Invalid {interval} data: missing columns, got {list(df.columns)}")
                return pd.DataFrame()

            df["datetime"] = pd.to_datetime(df["datetime"], unit="ms").dt.tz_localize("UTC").dt.tz_convert("US/Eastern")
            df.set_index("datetime", inplace=True)
            logging.info(f"[{symbol}] Fetched {interval} data: {len(df)} rows, date_range={df.index.min()} to {df.index.max()}")
            return df.sort_index()
        except Exception as e:
            logging.warning(f"[{symbol}] Fetch attempt {attempt}/{max_attempts} for {interval} failed: {e}")
            if attempt == max_attempts:
                logging.error(f"[{symbol}] Failed to fetch {interval} data after {max_attempts} attempts: {e}")
            time.sleep(min(1 * attempt, 10))  # Exponential backoff
    return pd.DataFrame()

def save_price_data_to_csv(user_id, symbol, interval, output_dir="/var/www/stockwicks/data"):
    user_dir = os.path.join(output_dir, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    filename = f"{user_id}_{safe_symbol_for_files(symbol)}_{interval}_data.csv"
    filepath = os.path.join(user_dir, filename)

    if interval == '1min':
        df = get_schwab_1min_history(symbol)
    else:
        df = get_stock_data(symbol, interval)

    if df.empty or not {"open", "high", "low", "close"}.issubset(df.columns):
        logging.warning(f"[{symbol}] No valid data to save for {interval}: empty={df.empty}, columns={list(df.columns) if not df.empty else []}")
        return filepath

    df.to_csv(filepath, index=True, index_label="datetime")
    logging.info(f"[{symbol}] Saved {interval} data to {filepath}: {len(df)} rows, date_range={df.index.min()} to {df.index.max()}, sample={df.head(1).to_dict()}")
    return filepath
