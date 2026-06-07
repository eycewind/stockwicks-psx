#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/stocks/backtest_algos/Algo2_eval.py

import sys, os, argparse, logging, pandas as pd, requests, time
import numpy as np
from datetime import datetime, timedelta
from dotenv import load_dotenv

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
if REPO_ROOT not in sys.path: sys.path.insert(0, REPO_ROOT)
from app.utils.stock.schwab_token import get_valid_access_token

# --- Config & Data Fetching ---
load_dotenv(); logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
ET = pd.Timestamp.now(tz='America/New_York').tz
DATA_DIR = os.getenv("DATA_DIR", "/var/www/stockwicks/data")
# --- CORRECTED DICTIONARY ---
# Fetches the maximum data possible with this function's logic
# (Assumes 10-day limit for intraday and 5 years for daily)
SCHWAB_INTERVALS = {
    '1min': ('day', 10, 'minute', 1),    # Was 1 day, now 10 days
    '5min': ('day', 10, 'minute', 5),    # Was 5 days, now 10 days
    '10min': ('day', 10, 'minute', 10),   # Unchanged, limited by API
    '15min': ('day', 10, 'minute', 15),   # Unchanged, limited by API
    '30min': ('day', 10, 'minute', 30),   # Unchanged, limited by API
    '1d': ('year', 5, 'daily', 1),     # Was 1 year, now 5 years
}
ALLOWED_INTERVALS = set(SCHWAB_INTERVALS.keys())

def get_data_for_fixed_period(symbol: str, interval: str) -> pd.DataFrame:
    if interval not in SCHWAB_INTERVALS: raise ValueError(f"Unsupported interval: {interval}")
    periodType, period, frequencyType, frequency = SCHWAB_INTERVALS[interval]
    token = get_valid_access_token();
    if not token: raise RuntimeError("Failed to get Schwab access token")
    url = "https://api.schwabapi.com/marketdata/v1/pricehistory"
    params = {"symbol": symbol.upper(), "periodType": periodType, "period": period, "frequencyType": frequencyType, "frequency": frequency, "needExtendedHoursData": "false"}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15); resp.raise_for_status()
        candles = resp.json().get("candles", [])
        if not candles: logging.warning(f"API returned no candles for {symbol} on {interval}"); return pd.DataFrame()
        df = pd.DataFrame(candles)
        df["timestamp"] = pd.to_datetime(df["datetime"], unit="ms", utc=True).dt.tz_convert(ET)
        df = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
        return df[~df.index.duplicated(keep="first")].sort_index()
    except requests.HTTPError as e:
        logging.error(f"Failed to fetch data for {symbol} on {interval}: {e}"); return pd.DataFrame()

# --- Algorithm-Specific Logic ---
def compute_atr_trailing_stop(df: pd.DataFrame, period: int = 14, multiplier: float = 3.0) -> pd.Series:
    df = df.copy()
    df['h-l'], df['h-pc'], df['l-pc'] = df['high'] - df['low'], abs(df['high'] - df['close'].shift(1)), abs(df['low'] - df['close'].shift(1))
    df['tr'] = df[['h-l', 'h-pc', 'l-pc']].max(axis=1)
    df['atr'] = df['tr'].ewm(span=period, adjust=False).mean()
    atr_stop = [0.0] * len(df)
    for i in range(1, len(df)):
        close, prev_close, atr, prev_atr_stop = df['close'].iloc[i], df['close'].iloc[i-1], df['atr'].iloc[i], atr_stop[i-1]
        if close > prev_atr_stop and prev_close > prev_atr_stop: atr_stop[i] = max(prev_atr_stop, close - multiplier * atr)
        elif close < prev_atr_stop and prev_close < prev_atr_stop: atr_stop[i] = min(prev_atr_stop, close + multiplier * atr)
        elif close > prev_atr_stop: atr_stop[i] = close - multiplier * atr
        else: atr_stop[i] = close + multiplier * atr
    return pd.Series(atr_stop, index=df.index)

def determine_signals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df['atr_stop'] = compute_atr_trailing_stop(df)
    df['Buy_Signal'] = (df['close'].shift(1) <= df['atr_stop'].shift(1)) & (df['close'] > df['atr_stop'])
    df['Sell_Signal'] = (df['close'].shift(1) >= df['atr_stop'].shift(1)) & (df['close'] < df['atr_stop'])
    return df

# --- Universal Backtest Engine & Pipeline ---
# In both Algo2_eval.py and Algo3_eval.py (REPLACE the old function)

def evaluate_performance(df: pd.DataFrame, interval: str, trade_size: float, symbol: str, eod_close: bool = False):
    """
    Evaluates trading performance with realistic execution price and optional EOD close.
    """
    long_trades, short_trades = [], []
    position, entry_price, entry_time = None, None, None
    qty = float(trade_size)

    for i in range(1, len(df)):
        # --- REALISTIC EXECUTION: Use the signal bar's CLOSE price and time ---
        t, execution_price = df.index[i-1], float(df["close"].iloc[i-1])

        prev_buy, prev_sell = bool(df["Buy_Signal"].iloc[i - 1]), bool(df["Sell_Signal"].iloc[i - 1])
        if pd.isna(execution_price): continue

        # --- Standard Exit Logic (based on opposite signal) ---
        if position == "long" and prev_sell:
            pl = (execution_price - entry_price) * qty
            long_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(execution_price, 4), round(pl, 2), "Win" if pl > 0 else "Loss"])
            position = None
        elif position == "short" and prev_buy:
            pl = (entry_price - execution_price) * qty
            short_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(execution_price, 4), round(pl, 2), "Win" if pl > 0 else "Loss"])
            position = None
        
        # --- NEW END-OF-DAY FORCED CLOSE LOGIC ---
        # Check if it's the last bar of the day and if the EOD rule is active
        is_last_bar_of_day = (i + 1 == len(df)) or (df.index[i].date() != df.index[i-1].date())
        if eod_close and position is not None and is_last_bar_of_day:
            exit_price = float(df['close'].iloc[i-1]) # Close at the EOD bar's close price
            exit_time = df.index[i-1] # Use the timestamp of the closing bar

            if position == "long":
                pl = (exit_price - entry_price) * qty
                long_trades.append([symbol, interval, entry_time, round(entry_price, 4), exit_time, round(exit_price, 4), round(pl, 2), "Win" if pl > 0 else "Loss"])
            elif position == "short":
                pl = (entry_price - exit_price) * qty
                short_trades.append([symbol, interval, entry_time, round(entry_price, 4), exit_time, round(exit_price, 4), round(pl, 2), "Win" if pl > 0 else "Loss"])
            
            position = None # We are now flat and won't take a new trade until the next day

        # --- Entry Logic (only if we are currently flat) ---
        if position is None:
            if prev_buy:
                entry_price, entry_time, position = execution_price, t, "long"
            elif prev_sell:
                entry_price, entry_time, position = execution_price, t, "short"

    df_cols = ['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Exit_date_time', 'Exit_price', 'Profit', 'Status']
    return pd.DataFrame(long_trades, columns=df_cols), pd.DataFrame(short_trades, columns=df_cols)
def calculate_success_and_profit(df: pd.DataFrame):
    total = len(df)
    if total == 0: return 0, 0, 0, 0.0, 0.0
    wins = (df["Status"] == "Win").sum()
    return total, wins, total - wins, wins / total, float(df["Profit"].sum())

def run(symbol: str, interval: str, trade_size: float, user_id: str, eod_close: bool): # <-- Add eod_close here
    user_dir = os.path.join(DATA_DIR, str(user_id)); os.makedirs(user_dir, exist_ok=True)
    df = get_data_for_fixed_period(symbol, interval)
    df_cols = ['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Exit_date_time', 'Exit_price', 'Profit', 'Status']
    if df.empty:
        long_df, short_df = pd.DataFrame(columns=df_cols), pd.DataFrame(columns=df_cols)
    else:
        df = determine_signals(df)
        long_df, short_df = evaluate_performance(df, interval, trade_size, symbol, eod_close) # <-- Pass it here
    long_summary, short_summary = calculate_success_and_profit(long_df), calculate_success_and_profit(short_df)
    summary_rows = [
        [symbol, interval, trade_size, *long_summary[:3], f"{long_summary[3]*100:.2f}%", f"${long_summary[4]:.2f}", "Long"],
        [symbol, interval, trade_size, *short_summary[:3], f"{short_summary[3]*100:.2f}%", f"${short_summary[4]:.2f}", "Short"],
    ]
    summary_df = pd.DataFrame(summary_rows, columns=['Symbol', 'Interval', 'Trade_size', 'Total_Trades', 'Wins', 'Losses', 'SuccessRate', 'Total_profit', 'Trade_Type'])
    summary_file = os.path.join(user_dir, f"{user_id}_{symbol}_{interval}_summary.csv")
    summary_df.to_csv(summary_file, index=False)
    logging.info(f"Summary for Algo2 on {interval} saved.")

def parse_args():
    p = argparse.ArgumentParser(description="Algo2: ATR Trend (AI Eval)")
    p.add_argument("--symbol", "-s", required=True)
    p.add_argument("--interval", "-i", required=True, choices=sorted(ALLOWED_INTERVALS))
    p.add_argument("--trade-size", "-q", type=float, default=100.0)
    p.add_argument("--user-id", "-u", required=True)
    # --- NEW ARGUMENT ---
    p.add_argument("--eod-close", action="store_true", help="Force close all positions at the end of each day.")
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()
    # Pass the new argument from args to the run function
    run(symbol=args.symbol, interval=args.interval, trade_size=args.trade_size, user_id=args.user_id, eod_close=args.eod_close)