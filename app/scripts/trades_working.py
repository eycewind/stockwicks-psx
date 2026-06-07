#/var/www/stockwicks/app/scripts/trades_working.py
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

import pandas as pd
import numpy as np
import requests
import logging
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
import json
from app.utils.stock.schwab_token import get_valid_access_token




load_dotenv()
os.environ['PYTHONIOENCODING'] = 'utf-8'

# Set random seeds for reproducibility
np.random.seed(42)

# Configure logging
logging.basicConfig(level=logging.INFO)

# Extract command-line arguments
symbol = sys.argv[1]
interval = sys.argv[2]
trade_size = float(sys.argv[3])
user_id = sys.argv[4]

logging.info(f"Running script with symbol={symbol} and interval={interval} and trade size={trade_size}")

# Define constants and function to fetch data
DATA_DIR = os.getenv('DATA_DIR', '/var/www/stockwicks/data')
user_data_dir = os.path.join(DATA_DIR, str(user_id))
os.makedirs(user_data_dir, exist_ok=True)

# Schwab mapping: interval -> (periodType, period, frequencyType, frequency)
SCHWAB_INTERVALS = {
    '1min': ('day', 1, 'minute', 1),
    '5min': ('day', 5, 'minute', 5),
    '10min': ('day', 10, 'minute', 10),
    '15min': ('day', 10, 'minute', 15),
    '30min': ('day', 10, 'minute', 30),
    '1d': ('year', 1, 'daily', 1),
    '1wk': ('year', 1, 'weekly', 1),
}

API_TOKEN = os.getenv('API_TOKEN')
import requests
import pandas as pd
from datetime import datetime
import logging

import time


import time

def get_schwab_1min_history(symbol, num_days=7):
    """
    Fetches last num_days of 1min bars (including today) from Schwab API.
    Returns a DataFrame of concatenated bars in time order.
    """
    all_dfs = []
    today = pd.Timestamp.now(tz='US/Eastern').date()

    for days_back in range(num_days):
        day = today - pd.Timedelta(days=days_back)
        start_dt = pd.Timestamp(day, tz='US/Eastern').replace(hour=9, minute=30)
        end_dt   = pd.Timestamp(day, tz='US/Eastern').replace(hour=16, minute=0)
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms   = int(end_dt.timestamp() * 1000)

        access_token = get_valid_access_token()
        if not access_token:
            logging.error("Failed to get Schwab API token")
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

        try:
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            if not data.get("candles"):
                logging.info(f"No data for {day}")
                continue
            day_df = pd.DataFrame(data["candles"])
            if "datetime" in day_df.columns:
                day_df["timestamp"] = pd.to_datetime(day_df["datetime"], unit='ms', utc=True).dt.tz_convert('US/Eastern')
                day_df = day_df.set_index("timestamp")
            all_dfs.append(day_df[["open","high","low","close","volume"]])
            time.sleep(0.2)
        except Exception as e:
            logging.error(f"Error getting bars for {day}: {e}")
            continue

    if not all_dfs:
        return pd.DataFrame()
    df_all = pd.concat(all_dfs).sort_index()
    df_all = df_all[~df_all.index.duplicated(keep='first')]
    df_all = df_all.between_time('09:30', '16:00')
    return df_all

def save_intraday_history_to_file(df, user_data_dir, symbol, user_id, interval):
    file_path = os.path.join(user_data_dir, f"{user_id}_{symbol}_{interval}_ALLDAYS.csv")
    df.to_csv(file_path)
    logging.info(f"Saved {len(df)} rows to {file_path}")
    return file_path


def get_intraday_df_for_n_days(symbol, interval, n_days=7):
    """
    Returns a DataFrame with 1min OHLCV bars for the last n trading days (including today if open).
    """
    data = get_stock_data(symbol, interval)
    if not data or data['s'] != 'ok':
        logging.error("Data not in expected format or fetch failed")
        return None

    df = pd.DataFrame({
        'timestamp': pd.to_datetime(data['t'], unit='s', utc=True),
        'open': data['o'],
        'high': data['h'],
        'low': data['l'],
        'close': data['c'],
        'volume': data['v']
    }).set_index('timestamp')
    df.index = df.index.tz_convert('US/Eastern')
    df = df.between_time('09:30', '16:00')

    # --- FIXED BLOCK ---
    # Find the unique dates, keep only the last n_days
    all_dates = pd.Series(df.index.date)
    last_n_days = sorted(list(set(all_dates)))[-n_days:]
    mask = all_dates.apply(lambda d: d in last_n_days)
    df = df[mask.values]
    # -------------------

    return df


def get_stock_data(symbol, interval):
    if interval not in SCHWAB_INTERVALS:
        raise ValueError(f"Unsupported interval. Supported intervals are: {list(SCHWAB_INTERVALS.keys())}")
    periodType, period, frequencyType, frequency = SCHWAB_INTERVALS[interval]
    access_token = get_valid_access_token()
    if not access_token:
        logging.error("Failed to get Schwab API token")
        return None

    url = "https://api.schwabapi.com/marketdata/v1/pricehistory"
    params = {
        "symbol": symbol.upper(),
        "periodType": periodType,
        "period": period,
        "frequencyType": frequencyType,
        "frequency": frequency,
        "needExtendedHoursData": "false",
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get(url, headers=headers, params=params)
    if response.status_code != 200:
        logging.error(f"Schwab API error: {response.status_code} {response.text}")
        return None
    data = response.json()
    if not data.get("candles"):
        logging.error("No candles returned from Schwab")
        return None

    candles = data["candles"]
    result = {
        "s": "ok",
        "t": [candle["datetime"] // 1000 for candle in candles],
        "o": [candle["open"] for candle in candles],
        "h": [candle["high"] for candle in candles],
        "l": [candle["low"] for candle in candles],
        "c": [candle["close"] for candle in candles],
        "v": [candle["volume"] for candle in candles],
    }
    return result


def get_schwab_realtime_quote(symbol):
    access_token = get_valid_access_token()
    if not access_token:
        logging.error("No valid Schwab access token for real-time quote.")
        return None

    url = f"https://api.schwabapi.com/marketdata/v1/quotes?symbols={symbol.upper()}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }
    try:
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        quote = data.get(symbol.upper())
        if not quote or "lastPrice" not in quote or "quoteTimeInLong" not in quote:
            return None
        # quoteTimeInLong is in ms, convert to s
        return {
            "datetime": int(quote["quoteTimeInLong"]) // 1000,
            "open": quote.get("openPrice", quote["lastPrice"]),
            "high": quote.get("highPrice", quote["lastPrice"]),
            "low": quote.get("lowPrice", quote["lastPrice"]),
            "close": quote["lastPrice"],
            "volume": quote.get("totalVolume", 0)
        }
    except Exception as e:
        logging.error(f"Error fetching Schwab real-time quote: {e}")
        return None


def compute_smi(df, period=14, smooth_k=3, smooth_d=3):
    """Calculate Stochastic Momentum Index (SMI) & signal line."""
    df['max_high'] = df['high'].rolling(window=period).max()
    df['min_low'] = df['low'].rolling(window=period).min()
    df['midpoint'] = (df['max_high'] + df['min_low']) / 2
    df['diff'] = df['max_high'] - df['min_low']
    df['smi_raw'] = (df['close'] - df['midpoint']) / (df['diff'] / 2) * 100
    df['SMI'] = df['smi_raw'].rolling(window=smooth_k).mean()
    df['SMI_Signal'] = df['SMI'].rolling(window=smooth_d).mean()
    return df['SMI'], df['SMI_Signal']

def determine_signals(df):
    """Generate Buy/Sell signals based on SMI reversal logic."""
    df['SMI_Change'] = df['SMI'].diff()
    df['Buy_Signal'] = (
        (df['SMI'].shift(2) < -70) &
        (df['SMI_Change'].shift(2) > 0) &
        (df['SMI'].shift(1) > df['SMI'].shift(2)) &
        (df['SMI'] > df['SMI'].shift(1))
    )
    df['Sell_Signal'] = (
        (df['SMI'].shift(2) > 70) &
        (df['SMI_Change'].shift(2) < 0) &
        (df['SMI'].shift(1) < df['SMI'].shift(2)) &
        (df['SMI'] < df['SMI'].shift(1))
    )
    return df




def process_interval(symbol, interval, user_data_dir=None, user_id=None):
    try:
        if interval == '1min':
            df = get_schwab_1min_history(symbol, num_days=7)
            if user_data_dir and user_id:
                save_intraday_history_to_file(df, user_data_dir, symbol, user_id, interval)
        else:
            data = get_stock_data(symbol, interval)
            if not data or data['s'] != 'ok':
                logging.error("Data not in expected format or fetch failed")
                return None
            df = pd.DataFrame({
                'timestamp': pd.to_datetime(data['t'], unit='s', utc=True),
                'open': data['o'],
                'high': data['h'],
                'low': data['l'],
                'close': data['c'],
                'volume': data['v']
            }).set_index('timestamp')
            df.index = df.index.tz_convert('US/Eastern')
            df = df.between_time('09:30', '16:00')

        print("DF shape after full days fetch:", df.shape)
        print(df.tail())

        df['SMI'], df['SMI_Signal'] = compute_smi(df)
        df = determine_signals(df)
        return df
    except Exception as e:
        logging.error(f"Error in process_interval: {e}")
        return None



def evaluate_performance(df, interval, trade_size, symbol):
    """Backtest: Compute entry/exit points, returns, and open positions."""
    long_trades, short_trades, open_positions = [], [], []
    long_entry_price = short_entry_price = long_entry_time = short_entry_time = None
    for i in range(1, len(df)):
        # Long Trades
        if df['Buy_Signal'].iloc[i-1]:
            long_entry_price = round(df['close'].iloc[i-1], 2)
            long_entry_time = df.index[i-1]
        if df['Sell_Signal'].iloc[i-1] and long_entry_price is not None:
            exit_price = round(df['close'].iloc[i], 2)
            exit_time = df.index[i]
            profit = round((exit_price - long_entry_price) * trade_size, 2)
            status = 'Win' if profit > 0 else 'Loss'
            long_trades.append([symbol, interval, long_entry_time, long_entry_price, exit_time, exit_price, profit, status])
            long_entry_price = None
        # Short Trades
        if df['Sell_Signal'].iloc[i-1]:
            short_entry_price = round(df['close'].iloc[i-1], 2)
            short_entry_time = df.index[i-1]
        if df['Buy_Signal'].iloc[i-1] and short_entry_price is not None:
            exit_price = round(df['close'].iloc[i], 2)
            exit_time = df.index[i]
            profit = round((short_entry_price - exit_price) * trade_size, 2)
            status = 'Win' if profit > 0 else 'Loss'
            short_trades.append([symbol, interval, short_entry_time, short_entry_price, exit_time, exit_price, profit, status])
            short_entry_price = None
    if long_entry_price is not None:
        open_positions.append([symbol, interval, long_entry_time, long_entry_price, 'open', 'long'])
    if short_entry_price is not None:
        open_positions.append([symbol, interval, short_entry_time, short_entry_price, 'open', 'short'])
    long_df = pd.DataFrame(long_trades, columns=['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Exit_date_time', 'Exit_price', 'Profit', 'Status'])
    short_df = pd.DataFrame(short_trades, columns=['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Exit_date_time', 'Exit_price', 'Profit', 'Status'])
    open_df = pd.DataFrame(open_positions, columns=['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Status', 'Type'])
    return long_df, short_df, open_df

def calculate_success_and_profit(df):
    """Summary stats for wins/losses/profit."""
    total_trades = len(df)
    wins = (df['Status'] == 'Win').sum()
    losses = (df['Status'] == 'Loss').sum()
    success_rate = wins / total_trades if total_trades > 0 else 0
    total_profit = df['Profit'].sum() if 'Profit' in df else 0
    return total_trades, wins, losses, success_rate, total_profit

# --- MAIN RUN ---
df = process_interval(symbol, interval)
if df is not None:
    # Save processed OHLC+signals data
    csv_file_path = os.path.join(user_data_dir, f"{user_id}_{symbol}_{interval}_data.csv")
    df.to_csv(csv_file_path)
    logging.info(f"Data for {interval} interval saved to {csv_file_path}")

    long_df, short_df, open_df = evaluate_performance(df, interval, trade_size, symbol)
    # Save trade logs
    long_df.to_csv(os.path.join(user_data_dir, f"{user_id}_{symbol}_long_trades.csv"), index=False)
    short_df.to_csv(os.path.join(user_data_dir, f"{user_id}_{symbol}_short_trades.csv"), index=False)
    open_df.to_csv(os.path.join(user_data_dir, f"{user_id}_{symbol}_notify_open_positions.csv"), index=False)

    # Save summary
    long_summary = calculate_success_and_profit(long_df)
    short_summary = calculate_success_and_profit(short_df)
    summary_data = [
        [symbol, interval, trade_size, long_summary[0], long_summary[1], long_summary[2], f"{long_summary[3] * 100:.2f}%", f"${long_summary[4]:.2f}", "Long"],
        [symbol, interval, trade_size, short_summary[0], short_summary[1], short_summary[2], f"{short_summary[3] * 100:.2f}%", f"${short_summary[4]:.2f}", "Short"]
    ]
    summary_df = pd.DataFrame(summary_data, columns=['Symbol', 'Interval', 'Trade_size', 'Total_Trades', 'Wins', 'Losses', 'SuccessRate', 'Total_profit', 'Trade_Type'])
    summary_file_path = os.path.join(user_data_dir, f"{user_id}_{symbol}_summary.csv")
    summary_df.to_csv(summary_file_path, index=False)

    # Print open positions
    print("Open Positions:")
    print(open_df)
else:
    logging.error("No data available to process for the given symbol and interval.")
