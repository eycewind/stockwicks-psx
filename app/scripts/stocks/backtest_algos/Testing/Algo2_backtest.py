#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/stocks/backtest_algos/Algo2_backtest.py

import sys, os
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
if REPO_ROOT not in sys.path: sys.path.insert(0, REPO_ROOT)

import argparse, logging, time
from datetime import datetime, timedelta
import numpy as np, pandas as pd, requests
from dotenv import load_dotenv
from app.utils.stock.schwab_token import get_valid_access_token

# --- Config ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
ET = pd.Timestamp.now(tz='America/New_York').tz
DATA_DIR = os.getenv("DATA_DIR", "/var/www/stockwicks/data")
SCHWAB_INTERVALS = {
    '1min': ('day', 1, 'minute', 1), '5min': ('day', 5, 'minute', 5),
    '10min': ('day', 10, 'minute', 10), '15min': ('day', 10, 'minute', 15),
    '30min': ('day', 10, 'minute', 30), '1d': ('year', 1, 'daily', 1),
}
ALLOWED_INTERVALS = set(SCHWAB_INTERVALS.keys())

# --- Data Fetching ---
def get_data_for_fixed_period(symbol: str, interval: str) -> pd.DataFrame:
    if interval not in SCHWAB_INTERVALS:
        raise ValueError(f"Unsupported interval: {interval}")
    
    periodType, period, frequencyType, frequency = SCHWAB_INTERVALS[interval]
    token = get_valid_access_token()
    if not token: raise RuntimeError("Failed to get Schwab access token")

    url = "https://api.schwabapi.com/marketdata/v1/pricehistory"
    params = {
        "symbol": symbol.upper(), "periodType": periodType, "period": period,
        "frequencyType": frequencyType, "frequency": frequency, "needExtendedHoursData": "false",
    }
    headers = {"Authorization": f"Bearer {token}"}
    
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        candles = data.get("candles", [])
        if not candles:
            logging.warning(f"API returned no candles for {symbol} on {interval}")
            return pd.DataFrame()
        df = pd.DataFrame(candles)
        df["timestamp"] = pd.to_datetime(df["datetime"], unit="ms", utc=True).dt.tz_convert(ET)
        df = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
        return df[~df.index.duplicated(keep="first")].sort_index()
    except requests.HTTPError as e:
        logging.error(f"Failed to fetch data for {symbol} on {interval}: {e}")
        return pd.DataFrame()

# --- Algorithm-Specific Logic ---
def determine_signals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # Ensure enough data for 200-period MA
    if len(df) < 200:
        logging.warning(f"Not enough data ({len(df)} rows) for 200-period MA, no signals generated.")
        df['Buy_Signal'] = False
        df['Sell_Signal'] = False
        return df

    df['SMA50'] = df['close'].rolling(window=50).mean()
    df['SMA200'] = df['close'].rolling(window=200).mean()
    df['Buy_Signal'] = (df['SMA50'].shift(1) < df['SMA200'].shift(1)) & (df['SMA50'] > df['SMA200'])
    df['Sell_Signal'] = (df['SMA50'].shift(1) > df['SMA200'].shift(1)) & (df['SMA50'] < df['SMA200'])
    return df

# --- Universal Backtest Engine & Pipeline ---
def evaluate_performance(df: pd.DataFrame, interval: str, trade_size: float, symbol: str):
    long_trades, short_trades, open_positions = [], [], []
    position, entry_price, entry_time = None, None, None
    qty = float(trade_size)
    day_trade_intervals = {'1min', '5min', '10min'}

    for i in range(1, len(df)):
        t, execution_price = df.index[i], float(df["open"].iloc[i])
        prev_buy, prev_sell = bool(df["Buy_Signal"].iloc[i - 1]), bool(df["Sell_Signal"].iloc[i - 1])
        if pd.isna(execution_price): continue

        if position == "long" and prev_sell:
            pl = (execution_price - entry_price) * qty
            long_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(execution_price, 4), round(pl, 2), "Win" if pl > 0 else "Loss"])
            position = None
        elif position == "short" and prev_buy:
            pl = (entry_price - execution_price) * qty
            short_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(execution_price, 4), round(pl, 2), "Win" if pl > 0 else "Loss"])
            position = None
        
        if position is None:
            if prev_buy: entry_price, entry_time, position = execution_price, t, "long"
            elif prev_sell: entry_price, entry_time, position = execution_price, t, "short"

        if interval in day_trade_intervals:
            is_last_bar_of_day = (i + 1 == len(df)) or (df.index[i].date() != df.index[i + 1].date())
            if is_last_bar_of_day and position is not None:
                exit_price = float(df['close'].iloc[i])
                if position == "long":
                    pl = (exit_price - entry_price) * qty
                    long_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(exit_price, 4), round(pl, 2), "Win" if pl > 0 else "Loss"])
                elif position == "short":
                    pl = (entry_price - exit_price) * qty
                    short_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(exit_price, 4), round(pl, 2), "Win" if pl > 0 else "Loss"])
                position = None

    if position is not None: open_positions.append([symbol, interval, entry_time, round(entry_price, 4), "open", position])
    df_cols = ['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Exit_date_time', 'Exit_price', 'Profit', 'Status']
    open_cols = ['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Status', 'Type']
    return pd.DataFrame(long_trades, columns=df_cols), pd.DataFrame(short_trades, columns=df_cols), pd.DataFrame(open_positions, columns=open_cols)

def calculate_success_and_profit(df: pd.DataFrame):
    total_trades = len(df)
    if total_trades == 0: return 0, 0, 0, 0.0, 0.0
    wins = (df["Status"] == "Win").sum()
    return total_trades, wins, total_trades - wins, wins / total_trades, float(df["Profit"].sum())

def run(symbol: str, interval: str, trade_size: float, user_id: str):
    user_dir = os.path.join(DATA_DIR, str(user_id)); os.makedirs(user_dir, exist_ok=True)
    start_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    end_date = datetime.now().strftime("%Y-%m-%d")
    
    df = get_data_for_fixed_period(symbol, interval)
    if not df.empty:
        df = determine_signals(df)
        long_df, short_df, _ = evaluate_performance(df, interval, trade_size, symbol)
    else:
        df_cols = ['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Exit_date_time', 'Exit_price', 'Profit', 'Status']
        long_df, short_df = pd.DataFrame(columns=df_cols), pd.DataFrame(columns=df_cols)

    long_summary = calculate_success_and_profit(long_df)
    short_summary = calculate_success_and_profit(short_df)
    summary_rows = [
        [symbol, interval, trade_size, long_summary[0], long_summary[1], long_summary[2], f"{long_summary[3]*100:.2f}%", f"${long_summary[4]:.2f}", "Long"],
        [symbol, interval, trade_size, short_summary[0], short_summary[1], short_summary[2], f"{short_summary[3]*100:.2f}%", f"${short_summary[4]:.2f}", "Short"],
    ]
    summary_df = pd.DataFrame(summary_rows, columns=['Symbol', 'Interval', 'Trade_size', 'Total_Trades', 'Wins', 'Losses', 'SuccessRate', 'Total_profit', 'Trade_Type'])
    summary_file = os.path.join(user_dir, f"{user_id}_{symbol}_{interval}_{start_date}_{end_date}_summary.csv")
    summary_df.to_csv(summary_file, index=False)
    logging.info(f"Summary saved to {summary_file}")

def parse_args():
    p = argparse.ArgumentParser(description="Algo2: Swing MA Crossover Strategy")
    p.add_argument("--symbol", "-s", required=True); p.add_argument("--interval", "-i", required=True, choices=sorted(ALLOWED_INTERVALS))
    p.add_argument("--trade-size", "-q", type=float, default=100.0); p.add_argument("--user-id", "-u", required=True)
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()
    run(symbol=args.symbol, interval=args.interval, trade_size=args.trade_size, user_id=args.user_id)