#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/stocks/backtest_algos/Algo3_backtest.py

import sys, os

# Put the repo root (/var/www/stockwicks) on sys.path
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import argparse
import logging
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

from app.utils.stock.schwab_token import get_valid_access_token

load_dotenv()
os.environ['PYTHONIOENCODING'] = 'utf-8'

# -----------------------
# Config / Globals
# -----------------------
np.random.seed(42)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ET = pd.Timestamp.now(tz='America/New_York').tz
UTC = pd.Timestamp.now(tz='UTC').tz

ALLOWED_INTERVALS = {"1min", "5min", "10min", "15min", "30min", "1d"}

SCHWAB_SPEC = {
    "1min": ("minute", 1), "5min": ("minute", 5), "10min": ("minute", 10),
    "15min": ("minute", 15), "30min": ("minute", 30), "1d": ("daily", 1),
}

DATA_DIR = os.getenv("DATA_DIR", "/var/www/stockwicks/data")

# -----------------------
# Data Fetching (Using the final, corrected version)
# -----------------------
def iter_et_trading_days(start_et: pd.Timestamp, end_et: pd.Timestamp) -> List[pd.Timestamp]:
    days = []
    cur = start_et.normalize()
    end_norm = end_et.normalize()
    nyse_holidays = {pd.Timestamp(f"{cur.year}-{d}", tz=ET).normalize() for d in ["01-01", "01-20", "02-17", "04-18", "05-26", "06-19", "07-04", "09-01", "11-27", "12-25"]}
    while cur <= end_norm:
        if cur.weekday() < 5 and cur.normalize() not in nyse_holidays:
            days.append(cur)
        cur += pd.Timedelta(days=1)
    return days

def _schwab_pricehistory(symbol: str, frequencyType: str, frequency: int, start_ms: Optional[int] = None, end_ms: Optional[int] = None, periodType: Optional[str] = None, period: Optional[int] = None, need_extended: bool = False) -> Dict:
    token = get_valid_access_token()
    if not token: raise RuntimeError("Failed to get Schwab access token")
    url = "https://api.schwabapi.com/marketdata/v1/pricehistory"
    params = {"symbol": symbol.upper(), "frequencyType": frequencyType, "frequency": frequency}
    if frequencyType == "minute":
        params["needExtendedHoursData"] = "true" if need_extended else "false"
    if periodType and period:
        params["periodType"] = periodType
        params["period"] = period
    elif start_ms is not None and end_ms is not None:
        params["startDate"] = start_ms
        params["endDate"] = end_ms
    else:
        raise ValueError("Either startDate/endDate or periodType/period must be provided.")
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        logging.error(f"[SCHWAB ERROR] payload: {resp.text}")
        raise
    return resp.json()

def process_candles_to_df(candles: List[Dict]) -> pd.DataFrame:
    if not candles: return pd.DataFrame()
    df = pd.DataFrame(candles)
    df["timestamp"] = pd.to_datetime(df["datetime"], unit="ms", utc=True).dt.tz_convert(ET)
    df = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
    df = df[~df.index.duplicated(keep="first")]
    return df.sort_index()

def fetch_range_minute_chunked(symbol: str, interval: str, start_et: pd.Timestamp, end_et: pd.Timestamp, extended_hours: bool) -> pd.DataFrame:
    frequencyType, frequency = SCHWAB_SPEC[interval]
    frames = []
    for d in iter_et_trading_days(start_et, end_et):
        day_start, day_end = d.replace(hour=9, minute=30), d.replace(hour=16, minute=0)
        start_ms, end_ms = int(day_start.tz_convert(UTC).timestamp() * 1000), int(day_end.tz_convert(UTC).timestamp() * 1000)
        try:
            data = _schwab_pricehistory(symbol, frequencyType, frequency, start_ms=start_ms, end_ms=end_ms, need_extended=extended_hours)
            df = process_candles_to_df(data.get("candles", []))
            if not df.empty:
                df = df.between_time("09:30", "16:00")
                frames.append(df)
            time.sleep(0.25)
        except requests.HTTPError as e:
            logging.warning(f"HTTP Error fetching {symbol} for {d.date()}: {e}")
    return pd.concat(frames).sort_index() if frames else pd.DataFrame()

def get_df_for_range(symbol: str, interval: str, start_date: str, end_date: str, extended_hours: bool) -> pd.DataFrame:
    if interval not in ALLOWED_INTERVALS:
        raise ValueError(f"Unsupported interval '{interval}'")
    start_et, end_et = pd.Timestamp(start_date, tz=ET), pd.Timestamp(end_date, tz=ET)
    if interval == "1d":
        frequencyType, frequency = SCHWAB_SPEC["1d"]
        try:
            duration_days = (end_et - start_et).days
            duration_years = (duration_days + 5) / 365.25
            valid_periods = [1, 2, 3, 5, 10, 15, 20]
            request_period = next((p for p in valid_periods if p >= duration_years), 20)
            data = _schwab_pricehistory(symbol, frequencyType, frequency, periodType="year", period=request_period, need_extended=False)
            full_df = process_candles_to_df(data.get("candles", []))
            return full_df.loc[start_et.normalize():end_et.normalize()] if not full_df.empty else pd.DataFrame()
        except (requests.HTTPError, ValueError) as e:
            logging.error(f"[DAILY] Failed to fetch daily data for {symbol}: {e}")
            return pd.DataFrame()
    else:
        return fetch_range_minute_chunked(symbol, interval, start_et, end_et, extended_hours)

# -----------------------
# Indicators & Signals
# -----------------------
def compute_smi(df: pd.DataFrame, period: int = 14, smooth_k: int = 3, smooth_d: int = 3):
    """
    Calculate a smoothed Stochastic Momentum Index (SMI) using Exponential Moving Averages (EMAs).
    """
    df = df.copy()
    # Highest high and lowest low over the period
    highest_high = df['high'].rolling(window=period).max()
    lowest_low = df['low'].rolling(window=period).min()

    # Calculate the range and midpoint
    price_range = highest_high - lowest_low
    midpoint = (highest_high + lowest_low) / 2
    
    # Raw SMI value (% of close relative to the range's midpoint)
    smi_raw = 100 * (df['close'] - midpoint) / (price_range / 2)
    
    # First smoothing (EMA on the raw value)
    smi_smoothed = smi_raw.ewm(span=smooth_k, adjust=False).mean()
    
    # Second smoothing (EMA on the smoothed value)
    df['SMI'] = smi_smoothed.ewm(span=smooth_d, adjust=False).mean()
    
    # Signal line (often a simple moving average of the SMI)
    df['SMI_Signal'] = df['SMI'].rolling(window=smooth_d).mean()
    
    return df['SMI'], df['SMI_Signal']

def determine_signals(df: pd.DataFrame, threshold: int = 70) -> pd.DataFrame:
    """
    Generate Buy/Sell signals based on SMI crossing the overbought/oversold thresholds.
    """
    df = df.copy()
    
    # Buy Signal: SMI crosses UP from below the oversold line (-70)
    df['Buy_Signal'] = (df['SMI'].shift(1) < -threshold) & (df['SMI'] >= -threshold)

    # Sell Signal: SMI crosses DOWN from above the overbought line (+70)
    df['Sell_Signal'] = (df['SMI'].shift(1) > threshold) & (df['SMI'] <= threshold)

    return df

# -----------------------
# Backtest Core (Realistic Execution)
# -----------------------
def evaluate_performance(df: pd.DataFrame, interval: str, trade_size: float, symbol: str, commission_per_side: float = 0.0, slippage_abs_per_side: float = 0.0):
    assert {"Buy_Signal", "Sell_Signal", "open", "close"}.issubset(df.columns)
    long_trades, short_trades, open_positions = [], [], []
    position, entry_price, entry_time = None, None, None
    qty = float(trade_size)

    for i in range(1, len(df)):
        t, execution_price = df.index[i], float(df["open"].iloc[i])
        prev_buy, prev_sell = bool(df["Buy_Signal"].iloc[i - 1]), bool(df["Sell_Signal"].iloc[i - 1])
        if pd.isna(execution_price): continue

        if position == "long" and prev_sell:
            gross = (execution_price - entry_price) * qty
            pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
            long_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(execution_price, 4), pl, "Win" if pl > 0 else "Loss"])
            position = entry_price = entry_time = None
        elif position == "short" and prev_buy:
            gross = (entry_price - execution_price) * qty
            pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
            short_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(execution_price, 4), pl, "Win" if pl > 0 else "Loss"])
            position = entry_price = entry_time = None

        if position is None:
            if prev_buy:
                entry_price, entry_time, position = execution_price + slippage_abs_per_side, t, "long"
            elif prev_sell:
                entry_price, entry_time, position = execution_price - slippage_abs_per_side, t, "short"

    if position is not None:
        open_positions.append([symbol, interval, entry_time, round(entry_price, 4), "open", position])

    df_cols = ['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Exit_date_time', 'Exit_price', 'Profit', 'Status']
    open_cols = ['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Status', 'Type']
    long_df, short_df, open_df = pd.DataFrame(long_trades, columns=df_cols), pd.DataFrame(short_trades, columns=df_cols), pd.DataFrame(open_positions, columns=open_cols)
    return long_df, short_df, open_df

# -----------------------
# Pipeline & Reporting
# -----------------------
def calculate_success_and_profit(df: pd.DataFrame):
    total_trades = len(df)
    if total_trades == 0: return 0, 0, 0, 0.0, 0.0
    wins, losses = (df["Status"] == "Win").sum(), (df["Status"] == "Loss").sum()
    success_rate = wins / total_trades
    total_profit = float(df["Profit"].sum())
    return total_trades, wins, losses, success_rate, total_profit

def run(symbol: str, interval: str, trade_size: float, user_id: str, start_date: str, end_date: str, extended_hours: bool = False):
    user_dir = os.path.join(DATA_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    logging.info(f"[RUN] {symbol}@{interval} size={trade_size} start={start_date} end={end_date} ext_hours={extended_hours}")
    
    df = get_df_for_range(symbol, interval, start_date, end_date, extended_hours)
    
    df_cols = ['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Exit_date_time', 'Exit_price', 'Profit', 'Status']
    open_cols = ['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Status', 'Type']

    if df.empty:
        logging.error("No data fetched. No trades will be generated.")
        long_df, short_df, open_df = pd.DataFrame(columns=df_cols), pd.DataFrame(columns=df_cols), pd.DataFrame(columns=open_cols)
    else:
        logging.info(f"Data fetched successfully. Processing {len(df)} rows for signals.")
        df['SMI'], df['SMI_Signal'] = compute_smi(df)
        df = determine_signals(df, threshold=70) # Using the requested threshold
        long_df, short_df, open_df = evaluate_performance(df, interval, trade_size, symbol)

    # --- Reporting ---
    long_csv = os.path.join(user_dir, f"{user_id}_{symbol}_{interval}_{start_date}_{end_date}_long_trades.csv")
    short_csv = os.path.join(user_dir, f"{user_id}_{symbol}_{interval}_{start_date}_{end_date}_short_trades.csv")
    open_csv = os.path.join(user_dir, f"{user_id}_{symbol}_{interval}_{start_date}_{end_date}_open_positions.csv")
    summary_csv = os.path.join(user_dir, f"{user_id}_{symbol}_{interval}_{start_date}_{end_date}_summary.csv")

    long_df.to_csv(long_csv, index=False); short_df.to_csv(short_csv, index=False); open_df.to_csv(open_csv, index=False)
    logging.info(f"Saved trade logs to CSV.")

    long_summary = calculate_success_and_profit(long_df)
    short_summary = calculate_success_and_profit(short_df)
    summary_rows = [
        [symbol, interval, trade_size, long_summary[0], long_summary[1], long_summary[2], f"{long_summary[3]*100:.2f}%", f"${long_summary[4]:.2f}", "Long"],
        [symbol, interval, trade_size, short_summary[0], short_summary[1], short_summary[2], f"{short_summary[3]*100:.2f}%", f"${short_summary[4]:.2f}", "Short"],
    ]
    summary_df = pd.DataFrame(summary_rows, columns=['Symbol', 'Interval', 'Trade_size', 'Total_Trades', 'Wins', 'Losses', 'SuccessRate', 'Total_profit', 'Trade_Type'])
    summary_df.to_csv(summary_csv, index=False)
    
    print("\n--- Summary ---"); print(summary_df.to_string(index=False))
    print("\n--- Open Positions ---"); print(open_df.to_string(index=False) if not open_df.empty else "None")

# -----------------------
# CLI
# -----------------------
def parse_args():
    p = argparse.ArgumentParser(description="SMI Reversal Strategy (Algo3) backtest.")
    p.add_argument("--symbol", "-s", required=True, help="Ticker, e.g., TSLA")
    p.add_argument("--interval", "-i", required=True, choices=sorted(list(ALLOWED_INTERVALS)))
    p.add_argument("--trade-size", "-q", type=float, default=100.0)
    p.add_argument("--user-id", "-u", required=True)
    p.add_argument("--start-date", "-S", required=True, help="YYYY-MM-DD (ET)")
    p.add_argument("--end-date", "-E", required=True, help="YYYY-MM-DD (ET)")
    p.add_argument("--extended-hours", "-X", type=int, default=0, help="1 to include extended hours")
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()
    run(symbol=args.symbol, interval=args.interval, trade_size=args.trade_size, user_id=args.user_id,
        start_date=args.start_date, end_date=args.end_date, extended_hours=bool(args.extended_hours))