#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/stocks/backtest_algos/Algo1_eval.py

import sys, os, argparse, logging, pandas as pd, requests, time
import numpy as np
from datetime import datetime, timedelta
from dotenv import load_dotenv

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
if REPO_ROOT not in sys.path: sys.path.insert(0, REPO_ROOT)
from app.utils.stock.schwab_token import get_valid_access_token

# --- Config ---
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

# --- Data Fetching ---
def get_data_for_fixed_period(symbol: str, interval: str) -> pd.DataFrame:
    if interval not in SCHWAB_INTERVALS: raise ValueError(f"Unsupported interval: {interval}")
    periodType, period, frequencyType, frequency = SCHWAB_INTERVALS[interval]
    token = get_valid_access_token()
    if not token: raise RuntimeError("Failed to get Schwab access token")
    url = "https://api.schwabapi.com/marketdata/v1/pricehistory"
    params = {"symbol": symbol.upper(), "periodType": periodType, "period": period, "frequencyType": frequencyType, "frequency": frequency, "needExtendedHoursData": "false"}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        candles = resp.json().get("candles", [])
        if not candles: logging.warning(f"API returned no candles for {symbol} on {interval}"); return pd.DataFrame()
        df = pd.DataFrame(candles)
        df["timestamp"] = pd.to_datetime(df["datetime"], unit="ms", utc=True).dt.tz_convert(ET)
        df = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
        return df[~df.index.duplicated(keep="first")].sort_index()
    except requests.HTTPError as e:
        logging.error(f"Failed to fetch data for {symbol} on {interval}: {e}"); return pd.DataFrame()

# --- Algorithm-Specific Logic ---
def compute_vwap_and_bands(df: pd.DataFrame, std_dev_mult: float = 1.5):
    df = df.copy()
    def calc_vwap_daily(day_df):
        if 'volume' not in day_df.columns or day_df['volume'].sum() == 0: return day_df
        tp = (day_df['high'] + day_df['low'] + day_df['close']) / 3
        cum_tp_vol = (tp * day_df['volume']).cumsum()
        cum_vol = day_df['volume'].cumsum()
        vwap = cum_tp_vol / cum_vol
        std_sq = ((tp - vwap) ** 2) * day_df['volume']
        mean_sq_error = std_sq.cumsum() / cum_vol
        std_dev = np.sqrt(mean_sq_error)
        day_df['VWAP'] = vwap
        day_df['VWAP_UpperBand'] = vwap + std_dev * std_dev_mult
        day_df['VWAP_LowerBand'] = vwap - std_dev * std_dev_mult
        return day_df
    return df.groupby(df.index.normalize(), group_keys=False).apply(calc_vwap_daily)

def determine_signals(df: pd.DataFrame) -> pd.DataFrame:
    """IMPROVED VWAP Mean Reversion with a 200-period EMA trend filter."""
    df = df.copy()
    df = compute_vwap_and_bands(df)
    df["EMA200"] = df['close'].ewm(span=200, adjust=False).mean()
    price_crosses_up = (df["close"].shift(1) < df["VWAP_LowerBand"].shift(1)) & (df["close"] > df["VWAP_LowerBand"])
    price_crosses_down = (df["close"].shift(1) > df["VWAP_UpperBand"].shift(1)) & (df["close"] < df["VWAP_UpperBand"])
    df["Buy_Signal"] = price_crosses_up & (df["close"] > df["EMA200"])
    df["Sell_Signal"] = price_crosses_down & (df["close"] < df["EMA200"])
    long_exit = (df["close"].shift(1) <= df["VWAP"].shift(1)) & (df["close"] > df["VWAP"])
    short_exit = (df["close"].shift(1) >= df["VWAP"].shift(1)) & (df["close"] < df["VWAP"])
    df["Sell_Signal"] = df["Sell_Signal"] | (long_exit & ~df['Sell_Signal'])
    df["Buy_Signal"] = df["Buy_Signal"] | (short_exit & ~df['Buy_Signal'])
    df.loc[df['Buy_Signal'] & df['Sell_Signal'], 'Sell_Signal'] = False
    return df

# --- Universal Backtest Engine & Pipeline ---
# In Algo1_eval.py (and others)

# The function now accepts the eod_close flag
def evaluate_performance(df: pd.DataFrame, interval: str, trade_size: float, symbol: str, eod_close: bool = False):
    long_trades, short_trades = [], []
    position, entry_price, entry_time = None, None, None
    qty = float(trade_size)

    for i in range(1, len(df)):
        # --- Using the more realistic execution price ---
        t, execution_price = df.index[i-1], float(df["close"].iloc[i-1])

        prev_buy, prev_sell = bool(df["Buy_Signal"].iloc[i - 1]), bool(df["Sell_Signal"].iloc[i - 1])
        if pd.isna(execution_price): continue

        # --- Standard Exit Logic ---
        if position == "long" and prev_sell:
            pl = (execution_price - entry_price) * qty
            long_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(execution_price, 4), round(pl, 2), "Win" if pl > 0 else "Loss"])
            position = None
        elif position == "short" and prev_buy:
            pl = (entry_price - execution_price) * qty
            short_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(execution_price, 4), round(pl, 2), "Win" if pl > 0 else "Loss"])
            position = None
        
        # --- NEW END-OF-DAY CLOSE LOGIC ---
        # Check if it's the last bar of the day and if we should close positions
        is_last_bar_of_day = (i + 1 == len(df)) or (df.index[i].date() != df.index[i-1].date())
        if eod_close and position is not None and is_last_bar_of_day:
            exit_price = float(df['close'].iloc[i-1]) # Close at the EOD bar's close
            if position == "long":
                pl = (exit_price - entry_price) * qty
                long_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(exit_price, 4), round(pl, 2), "Win" if pl > 0 else "Loss"])
            elif position == "short":
                pl = (entry_price - exit_price) * qty
                short_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(exit_price, 4), round(pl, 2), "Win" if pl > 0 else "Loss"])
            position = None # We are now flat

        # --- Entry Logic (only if flat) ---
        if position is None:
            if prev_buy: entry_price, entry_time, position = execution_price, t, "long"
            elif prev_sell: entry_price, entry_time, position = execution_price, t, "short"

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
    if df.empty or interval == '1d':
        if interval == '1d': logging.warning("VWAP algo not suitable for '1d'.")
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
    logging.info(f"Summary for Algo1 on {interval} saved. {summary_file}")

def parse_args():
    p = argparse.ArgumentParser(description="Algo1: VWAP Scalper (AI Eval)")
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