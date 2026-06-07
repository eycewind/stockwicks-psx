#trades_working.py
import sys
import pandas as pd
import numpy as np
import requests
import logging
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
import json

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
DATA_DIR = os.getenv('DATA_DIR', '/var/www/stonxs/data')
user_data_dir = os.path.join(DATA_DIR, str(user_id))
os.makedirs(user_data_dir, exist_ok=True)

DURATION_MAPPING = {
    '1min': '1',
    '2min': '2',
    '3min': '3',
    '4min': '4',
    '5min': '5',
    '15min': '15',
    '60min': '60',
    '120min': '120',
    '240min': '240',
    '1D': '1D',
}
API_TOKEN = os.getenv('API_TOKEN')

def get_stock_data(symbol, interval):
    if interval == '1D':
        days = 360
    else:
        days = 1

    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=days)
    start_date_str = start_date.strftime('%Y-%m-%dT%H:%M:%SZ')
    end_date_str = end_date.strftime('%Y-%m-%dT%H:%M:%SZ')
    api_interval = DURATION_MAPPING.get(interval)
    if not api_interval:
        raise ValueError(f"Unsupported interval. Supported intervals are: {list(DURATION_MAPPING.keys())}")

    url = f"https://api.marketdata.app/v1/stocks/candles/{api_interval}/{symbol}?from={start_date_str}&to={end_date_str}&token={API_TOKEN}"
    logging.info(f"Requesting URL: {url}")
    response = requests.get(url, headers={'Accept': 'application/json'})

    if response.status_code == 200:
        logging.info(f"Data successfully fetched for {symbol} at interval {interval}")
        return response.json()
    else:
        logging.error(f"Failed to retrieve data: {response.status_code} {response.text}")
        return None

# Compute SMI (Stochastic Momentum Index)
def compute_smi(df, period=14, smooth_k=3, smooth_d=3):
    df['max_high'] = df['high'].rolling(window=period).max()
    df['min_low'] = df['low'].rolling(window=period).min()
    df['midpoint'] = (df['max_high'] + df['min_low']) / 2
    df['diff'] = df['max_high'] - df['min_low']
    df['smi_raw'] = (df['close'] - df['midpoint']) / (df['diff'] / 2) * 100
    df['SMI'] = df['smi_raw'].rolling(window=smooth_k).mean()
    df['SMI_Signal'] = df['SMI'].rolling(window=smooth_d).mean()
    return df['SMI'], df['SMI_Signal']

# Function to determine buy/sell signals based on SMI with trend reversal logic
# Function to determine buy/sell signals based on SMI crossover
def determine_signals(df):
    # VWAP (Volume Weighted Average Price)
    df['vwap_num'] = (df['close'] * df['volume']).cumsum()
    df['vwap_den'] = df['volume'].cumsum()
    df['vwap'] = df['vwap_num'] / df['vwap_den']

    # ATR (Average True Range)
    df['H-L'] = df['high'] - df['low']
    df['H-PC'] = abs(df['high'] - df['close'].shift(1))
    df['L-PC'] = abs(df['low'] - df['close'].shift(1))
    df['TR'] = df[['H-L', 'H-PC', 'L-PC']].max(axis=1)
    df['ATR'] = df['TR'].rolling(window=14).mean()
    atr_multiplier = 2.5

    df['upper_breakout'] = df['vwap'] + atr_multiplier * df['ATR']
    df['lower_breakout'] = df['vwap'] - atr_multiplier * df['ATR']

    # Volume spike (1.5x 20-period avg volume)
    df['avg_vol'] = df['volume'].rolling(window=20).mean()
    df['vol_spike'] = df['volume'] > 1.5 * df['avg_vol']

    # Signal logic: breakout + volume confirmation + avoid duplicate fires
    df['Buy_Signal'] = (
        (df['close'] > df['upper_breakout']) &
        df['vol_spike'] &
        (df['close'].shift(1) <= df['upper_breakout'].shift(1))
    )

    df['Sell_Signal'] = (
        (df['close'] < df['lower_breakout']) &
        df['vol_spike'] &
        (df['close'].shift(1) >= df['lower_breakout'].shift(1))
    )

    return df


# Function to process data for the given interval
# Function to process data for the given interval
def process_interval(symbol, interval):
    try:
        data = get_stock_data(symbol, interval)
        if data is None:
            return None
        logging.info(f"Data fetched for symbol={symbol}, interval={interval}")
    except Exception as e:
        logging.error(f"Error fetching data: {e}")
        return None

    # Convert to DataFrame
    if data['s'] == 'ok':
        df = pd.DataFrame({
            'timestamp': pd.to_datetime(data['t'], unit='s', utc=True),
            'open': data['o'],
            'high': data['h'],
            'low': data['l'],
            'close': data['c'],
            'volume': data['v']
        })
        df.set_index('timestamp', inplace=True)
        df.index = df.index.tz_convert('US/Eastern')
        if interval != '1D':
            df = df.between_time('09:30', '16:00')
    else:
        logging.error("Data not in expected format")
        return None

    logging.info(f"Computing indicators for {interval} interval")

    # Compute SMI
    df['SMI'], df['SMI_Signal'] = compute_smi(df)
    df['SMI'] = df['SMI'].ewm(span=3, adjust=False).mean()
    df['SMI_Signal'] = df['SMI_Signal'].ewm(span=3, adjust=False).mean()

    # Load previous SMI state
    smi_state_path = os.path.join(user_data_dir, f"{symbol}_{interval}_last_smi.json")
    prev_smi = None
    try:
        if os.path.exists(smi_state_path):
            with open(smi_state_path, "r") as f:
                state = json.load(f)
                prev_smi = float(state.get("smi", -999))
    except Exception as e:
        logging.warning(f"Could not load SMI state file: {e}")

    # Determine buy/sell signals using previous SMI state
    df = determine_signals(df)


    # Save updated SMI state
    try:
        last_smi = float(df.iloc[-1]['SMI'])
        state = {
            "smi": last_smi,
            "timestamp": str(df.index[-1])
        }
        with open(smi_state_path, "w") as f:
            json.dump(state, f)
    except Exception as e:
        logging.warning(f"Failed to save SMI state: {e}")

    return df

# Process the specified interval
df = process_interval(symbol, interval)

if df is not None:
    # Save the processed data
    csv_file_path = os.path.join(user_data_dir, f"{user_id}_{symbol}_{interval}_data.csv")
    df.to_csv(csv_file_path)
    logging.info(f"Data for {interval} interval saved to {csv_file_path}")

    # Function to evaluate the accuracy of trend predictions and record trades
    def evaluate_performance(df, interval, trade_size, symbol):
        long_trades = []
        short_trades = []
        open_positions = []

        long_entry_price = None
        short_entry_price = None
        long_entry_time = None
        short_entry_time = None

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
                long_entry_price = None  # Reset entry price after trade is closed

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
                short_entry_price = None  # Reset entry price after trade is closed

        # Handle open positions at the end of the script execution
        if long_entry_price is not None:
            open_positions.append([symbol, interval, long_entry_time, long_entry_price, 'open', 'long'])
        if short_entry_price is not None:
            open_positions.append([symbol, interval, short_entry_time, short_entry_price, 'open', 'short'])

        long_df = pd.DataFrame(long_trades, columns=['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Exit_date_time', 'Exit_price', 'Profit', 'Status'])
        short_df = pd.DataFrame(short_trades, columns=['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Exit_date_time', 'Exit_price', 'Profit', 'Status'])
        open_df = pd.DataFrame(open_positions, columns=['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Status', 'Type'])

        return long_df, short_df, open_df

    long_df, short_df, open_df = evaluate_performance(df, interval, trade_size, symbol)

    # Save trade data
    long_df.to_csv(os.path.join(user_data_dir, f"{user_id}_{symbol}_long_trades.csv"), index=False)
    short_df.to_csv(os.path.join(user_data_dir, f"{user_id}_{symbol}_short_trades.csv"), index=False)
    open_df.to_csv(os.path.join(user_data_dir, f"{user_id}_{symbol}_notify_open_positions.csv"), index=False)

    # Calculate the success rate and trade summary for the specified interval
    def calculate_success_and_profit(df):
        total_trades = len(df)
        wins = (df['Status'] == 'Win').sum()
        losses = (df['Status'] == 'Loss').sum()
        success_rate = wins / total_trades if total_trades > 0 else 0
        total_profit = df['Profit'].sum()
        return total_trades, wins, losses, success_rate, total_profit

    long_summary = calculate_success_and_profit(long_df)
    short_summary = calculate_success_and_profit(short_df)

    # Save summary to CSV file
    summary_file_path = os.path.join(user_data_dir, f"{user_id}_{symbol}_summary.csv")
    summary_data = [
        [symbol, interval, trade_size, long_summary[0], long_summary[1], long_summary[2], f"{long_summary[3] * 100:.2f}%", f"${long_summary[4]:.2f}", "Long"],
        [symbol, interval, trade_size, short_summary[0], short_summary[1], short_summary[2], f"{short_summary[3] * 100:.2f}%", f"${short_summary[4]:.2f}", "Short"]
    ]

    summary_df = pd.DataFrame(summary_data, columns=['Symbol', 'Interval', 'Trade_size', 'Total_Trades', 'Wins', 'Losses', 'SuccessRate', 'Total_profit', 'Trade_Type'])
    summary_df.to_csv(summary_file_path, index=False)

    # Display open positions
    print("Open Positions:")
    print(open_df)
else:
    logging.error("No data available to process for the given symbol and interval.")
#trades_working.py
import sys
import pandas as pd
import numpy as np
import requests
import logging
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv

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
DATA_DIR = os.getenv('DATA_DIR', '/var/www/stonxs/data')
user_data_dir = os.path.join(DATA_DIR, str(user_id))
os.makedirs(user_data_dir, exist_ok=True)

DURATION_MAPPING = {
    '1min': '1',
    '2min': '2',
    '3min': '3',
    '4min': '4',
    '5min': '5',
    '15min': '15',
    '30min': '30',
    '60min': '60',
    '120min': '120',
    '240min': '240',
    '1D': '1D',
}
API_TOKEN = os.getenv('API_TOKEN')

def get_stock_data(symbol, interval):
    if interval == '1D':
        days = 360
    else:
        days = 30

    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=days)
    start_date_str = start_date.strftime('%Y-%m-%dT%H:%M:%SZ')
    end_date_str = end_date.strftime('%Y-%m-%dT%H:%M:%SZ')
    api_interval = DURATION_MAPPING.get(interval)
    if not api_interval:
        raise ValueError(f"Unsupported interval. Supported intervals are: {list(DURATION_MAPPING.keys())}")

    url = f"https://api.marketdata.app/v1/stocks/candles/{api_interval}/{symbol}?from={start_date_str}&to={end_date_str}&token={API_TOKEN}"
    logging.info(f"Requesting URL: {url}")
    response = requests.get(url, headers={'Accept': 'application/json'})

    if response.status_code == 200:
        logging.info(f"Data successfully fetched for {symbol} at interval {interval}")
        return response.json()
    else:
        logging.error(f"Failed to retrieve data: {response.status_code} {response.text}")
        return None

# Compute SMI (Stochastic Momentum Index)
def compute_smi(df, period=14, smooth_k=3, smooth_d=3):
    df['max_high'] = df['high'].rolling(window=period).max()
    df['min_low'] = df['low'].rolling(window=period).min()
    df['midpoint'] = (df['max_high'] + df['min_low']) / 2
    df['diff'] = df['max_high'] - df['min_low']
    df['smi_raw'] = (df['close'] - df['midpoint']) / (df['diff'] / 2) * 100
    df['SMI'] = df['smi_raw'].rolling(window=smooth_k).mean()
    df['SMI_Signal'] = df['SMI'].rolling(window=smooth_d).mean()
    return df['SMI'], df['SMI_Signal']

# Function to determine buy/sell signals based on SMI with trend reversal logic
def determine_signals(df):
    df['SMI_Change'] = df['SMI'].diff()
    
    # Use a 2-bar confirmation for reversal
    df['Buy_Signal'] = (
        (df['SMI'].shift(2) < -70) &
        (df['SMI_Change'].shift(2) > 0) &
        (df['SMI'].shift(1) > df['SMI'].shift(2)) &
        (df['SMI'].shift(0) > df['SMI'].shift(1))  # strong upward slope
    )

    df['Sell_Signal'] = (
        (df['SMI'].shift(2) > 70) &
        (df['SMI_Change'].shift(2) < 0) &
        (df['SMI'].shift(1) < df['SMI'].shift(2)) &
        (df['SMI'].shift(0) < df['SMI'].shift(1))  # strong downward slope
    )

    return df


# Function to process data for the given interval
def process_interval(symbol, interval):
    try:
        data = get_stock_data(symbol, interval)
        if data is None:
            return None
        logging.info(f"Data fetched for symbol={symbol}, interval={interval}")
    except Exception as e:
        logging.error(f"Error fetching data: {e}")
        return None

    # Convert to DataFrame
    if data['s'] == 'ok':
        df = pd.DataFrame({
            'timestamp': pd.to_datetime(data['t'], unit='s', utc=True),
            'open': data['o'],
            'high': data['h'],
            'low': data['l'],
            'close': data['c'],
            'volume': data['v']
        })
        df.set_index('timestamp', inplace=True)
        df.index = df.index.tz_convert('US/Eastern')
        if interval != '1D':
            df = df.between_time('09:30', '16:00')
    else:
        logging.error("Data not in expected format")
        return None

    logging.info(f"Computing indicators for {interval} interval")

    # Compute SMI
    df['SMI'], df['SMI_Signal'] = compute_smi(df)

    # Determine buy/sell signals
    df = determine_signals(df)
    return df

# Process the specified interval
df = process_interval(symbol, interval)

if df is not None:
    # Save the processed data
    csv_file_path = os.path.join(user_data_dir, f"{user_id}_{symbol}_{interval}_data.csv")
    df.to_csv(csv_file_path)
    logging.info(f"Data for {interval} interval saved to {csv_file_path}")

    # Function to evaluate the accuracy of trend predictions and record trades
    def evaluate_performance(df, interval, trade_size, symbol):
        long_trades = []
        short_trades = []
        open_positions = []

        long_entry_price = None
        short_entry_price = None
        long_entry_time = None
        short_entry_time = None

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
                long_entry_price = None  # Reset entry price after trade is closed

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
                short_entry_price = None  # Reset entry price after trade is closed

        # Handle open positions at the end of the script execution
        if long_entry_price is not None:
            open_positions.append([symbol, interval, long_entry_time, long_entry_price, 'open', 'long'])
        if short_entry_price is not None:
            open_positions.append([symbol, interval, short_entry_time, short_entry_price, 'open', 'short'])

        long_df = pd.DataFrame(long_trades, columns=['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Exit_date_time', 'Exit_price', 'Profit', 'Status'])
        short_df = pd.DataFrame(short_trades, columns=['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Exit_date_time', 'Exit_price', 'Profit', 'Status'])
        open_df = pd.DataFrame(open_positions, columns=['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Status', 'Type'])

        return long_df, short_df, open_df

    long_df, short_df, open_df = evaluate_performance(df, interval, trade_size, symbol)

    # Save trade data
    long_df.to_csv(os.path.join(user_data_dir, f"{user_id}_{symbol}_long_trades.csv"), index=False)
    short_df.to_csv(os.path.join(user_data_dir, f"{user_id}_{symbol}_short_trades.csv"), index=False)
    open_df.to_csv(os.path.join(user_data_dir, f"{user_id}_{symbol}_notify_open_positions.csv"), index=False)

    # Calculate the success rate and trade summary for the specified interval
    def calculate_success_and_profit(df):
        total_trades = len(df)
        wins = (df['Status'] == 'Win').sum()
        losses = (df['Status'] == 'Loss').sum()
        success_rate = wins / total_trades if total_trades > 0 else 0
        total_profit = df['Profit'].sum()
        return total_trades, wins, losses, success_rate, total_profit

    long_summary = calculate_success_and_profit(long_df)
    short_summary = calculate_success_and_profit(short_df)

    # Save summary to CSV file
    summary_file_path = os.path.join(user_data_dir, f"{user_id}_{symbol}_summary.csv")
    summary_data = [
        [symbol, interval, trade_size, long_summary[0], long_summary[1], long_summary[2], f"{long_summary[3] * 100:.2f}%", f"${long_summary[4]:.2f}", "Long"],
        [symbol, interval, trade_size, short_summary[0], short_summary[1], short_summary[2], f"{short_summary[3] * 100:.2f}%", f"${short_summary[4]:.2f}", "Short"]
    ]

    summary_df = pd.DataFrame(summary_data, columns=['Symbol', 'Interval', 'Trade_size', 'Total_Trades', 'Wins', 'Losses', 'SuccessRate', 'Total_profit', 'Trade_Type'])
    summary_df.to_csv(summary_file_path, index=False)

    # Display open positions
    print("Open Positions:")
    print(open_df)
else:
    logging.error("No data available to process for the given symbol and interval.")
