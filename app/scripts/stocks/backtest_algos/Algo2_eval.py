#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/stocks/backtest_algos/Algo2_eval.py

import sys, os, argparse, logging, pandas as pd, requests, time
import numpy as np, pandas_ta as ta
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timedelta
from dotenv import load_dotenv

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
if REPO_ROOT not in sys.path: sys.path.insert(0, REPO_ROOT)
from app.utils.stock.schwab_token import get_valid_access_token

# --- Config & Data Fetching ---
load_dotenv(); logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
ET = pd.Timestamp.now(tz='America/New_York').tz
DATA_DIR = os.getenv("DATA_DIR", "/var/www/stockwicks/data")
SCHWAB_INTERVALS = {'1min': ('day', 10, 'minute', 1), '5min': ('day', 10, 'minute', 5), '10min': ('day', 10, 'minute', 10), '15min': ('day', 10, 'minute', 15), '30min': ('day', 10, 'minute', 30), '1d': ('year', 5, 'daily', 1)}
ALLOWED_INTERVALS = set(SCHWAB_INTERVALS.keys())

# --- Data Fetching (Keep existing get_data_for_fixed_period) ---
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
        df = pd.DataFrame(candles); df["timestamp"] = pd.to_datetime(df["datetime"], unit="ms", utc=True).dt.tz_convert(ET)
        df = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
        return df[~df.index.duplicated(keep="first")].sort_index()
    except requests.HTTPError as e: logging.error(f"Failed fetch {symbol} {interval}: {e}"); return pd.DataFrame()

# --- Improved SuperTrend Calculation ---
def calculate_supertrend(df: pd.DataFrame, atr_period: int = 1, multiplier: float = 4.0) -> pd.DataFrame:
    """
    Improved SuperTrend calculation using pandas_ta library for accuracy
    """
    df = df.copy()
    
    # Use pandas_ta for more reliable SuperTrend calculation
    supertrend_result = ta.supertrend(df['high'], df['low'], df['close'], 
                                    length=atr_period, multiplier=multiplier)
    
    if supertrend_result is not None:
        # pandas_ta returns a DataFrame with multiple columns
        df['supertrend'] = supertrend_result[f'SUPERT_{atr_period}_{multiplier}']
        df['trend'] = supertrend_result[f'SUPERTd_{atr_period}_{multiplier}']
    else:
        # Fallback calculation if pandas_ta fails
        logging.warning("pandas_ta SuperTrend failed, using fallback calculation")
        df = calculate_supertrend_fallback(df, atr_period, multiplier)
    
    return df

def calculate_supertrend_fallback(df: pd.DataFrame, atr_period: int = 1, multiplier: float = 4.0) -> pd.DataFrame:
    """
    Fallback SuperTrend calculation
    """
    # Calculate ATR
    df['tr'] = ta.true_range(df['high'], df['low'], df['close'])
    df['atr'] = df['tr'].rolling(window=atr_period).mean()
    
    # Calculate basic upper and lower bands
    hl2 = (df['high'] + df['low']) / 2
    df['upper_band'] = hl2 + (multiplier * df['atr'])
    df['lower_band'] = hl2 - (multiplier * df['atr'])
    
    # Initialize SuperTrend columns
    df['supertrend'] = 0.0
    df['trend'] = 1  # 1 for uptrend, -1 for downtrend
    
    # Calculate SuperTrend
    for i in range(1, len(df)):
        if i < atr_period:
            continue
            
        current_close = df['close'].iloc[i]
        current_upper = df['upper_band'].iloc[i]
        current_lower = df['lower_band'].iloc[i]
        prev_close = df['close'].iloc[i-1]
        prev_supertrend = df['supertrend'].iloc[i-1]
        prev_trend = df['trend'].iloc[i-1]
        
        if prev_trend == 1:
            supertrend = max(current_lower, prev_supertrend)
        else:
            supertrend = min(current_upper, prev_supertrend)
        
        # Determine trend
        if current_close > supertrend:
            trend = 1
            supertrend = current_lower
        else:
            trend = -1
            supertrend = current_upper
            
        df.iloc[i, df.columns.get_loc('supertrend')] = supertrend
        df.iloc[i, df.columns.get_loc('trend')] = trend
    
    return df

# --- ORIGINAL Signal Generation (for comparison) ---
def determine_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    Original signal generation (with potential lookahead bias)
    """
    df = df.copy()
    
    # Calculate SuperTrend with ATR=1.0 and NATR=4.0
    df = calculate_supertrend(df, atr_period=1, multiplier=4.0)
    
    # Generate signals - more responsive to trend changes
    df['prev_trend'] = df['trend'].shift(1)
    df['prev_supertrend'] = df['supertrend'].shift(1)
    
    # Buy signal: price crosses above SuperTrend OR trend changes to uptrend
    df['Buy_Signal'] = (
        (df['close'] > df['supertrend']) & 
        (df['close'].shift(1) <= df['supertrend'].shift(1))
    ) | (
        (df['trend'] == 1) & (df['prev_trend'] == -1)  # Trend change to uptrend
    )
    
    # Sell signal: price crosses below SuperTrend OR trend changes to downtrend
    df['Sell_Signal'] = (
        (df['close'] < df['supertrend']) & 
        (df['close'].shift(1) >= df['supertrend'].shift(1))
    ) | (
        (df['trend'] == -1) & (df['prev_trend'] == 1)  # Trend change to downtrend
    )
    
    return df

# --- REALISTIC Signal Generation ---
def determine_signals_realistic(df: pd.DataFrame) -> pd.DataFrame:
    """
    Generate signals based on COMPLETED candles only (no lookahead bias)
    """
    df = df.copy()
    
    # Calculate SuperTrend with ATR=1.0 and NATR=4.0
    df = calculate_supertrend(df, atr_period=1, multiplier=4.0)
    
    # Generate signals based on PREVIOUS completed candle
    df['prev_trend'] = df['trend'].shift(1)
    df['prev_supertrend'] = df['supertrend'].shift(1)
    df['prev_close'] = df['close'].shift(1)
    
    # Buy signal: Previous candle closed above SuperTrend OR trend changed to uptrend
    df['Buy_Signal'] = (
        (df['prev_close'] > df['prev_supertrend']) & 
        (df['close'].shift(2) <= df['supertrend'].shift(2))
    ) | (
        (df['prev_trend'] == 1) & (df['trend'].shift(2) == -1)  # Trend change detected on previous candle
    )
    
    # Sell signal: Previous candle closed below SuperTrend OR trend changed to downtrend
    df['Sell_Signal'] = (
        (df['prev_close'] < df['prev_supertrend']) & 
        (df['close'].shift(2) >= df['supertrend'].shift(2))
    ) | (
        (df['prev_trend'] == -1) & (df['trend'].shift(2) == 1)  # Trend change detected on previous candle
    )
    
    return df

# --- ORIGINAL Backtest Execution ---
def evaluate_performance(
    df: pd.DataFrame, interval: str, trade_size: float, symbol: str,
    fixed_stop_loss_amount: float,
    eod_close: bool = False,
    commission_per_side: float = 0.0, slippage_abs_per_side: float = 0.0
):
    """
    Original execution (with potential lookahead bias)
    """
    assert {"Buy_Signal", "Sell_Signal", "open", "high", "low", "close"}.issubset(df.columns), "Missing required columns"
    long_trades, short_trades = [], []
    position, entry_price, entry_time, stop_loss_price = None, None, None, None
    qty = float(trade_size)
    stop_loss_per_share = fixed_stop_loss_amount / qty

    for i in range(1, len(df)):
        t = df.index[i]
        current_open = float(df["open"].iloc[i])
        current_high = float(df["high"].iloc[i])
        current_low = float(df["low"].iloc[i])
        current_close = float(df["close"].iloc[i])
        
        # Use current bar signals for more responsive trading
        current_buy_signal = bool(df["Buy_Signal"].iloc[i])
        current_sell_signal = bool(df["Sell_Signal"].iloc[i])
        
        execution_price = current_open
        if pd.isna(execution_price) or pd.isna(current_high) or pd.isna(current_low): 
            continue
            
        stop_loss_triggered = False
        exit_reason = ""

        # 1. Check Stop-Loss
        if position is not None and stop_loss_price is not None:
            if position == "long" and current_low <= stop_loss_price:
                exit_price = max(stop_loss_price, current_low)
                gross = (exit_price - entry_price) * qty
                pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
                long_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, 
                                  round(exit_price, 4), pl, "Loss", "Stop Loss Hit"])
                stop_loss_triggered = True
                
            elif position == "short" and current_high >= stop_loss_price:
                exit_price = min(stop_loss_price, current_high)
                gross = (entry_price - exit_price) * qty
                pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
                short_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, 
                                   round(exit_price, 4), pl, "Loss", "Stop Loss Hit"])
                stop_loss_triggered = True
                
            if stop_loss_triggered:
                position = entry_price = entry_time = stop_loss_price = None
                continue

        # 2. Check EOD Close
        is_last_bar_of_day = (i + 1 == len(df)) or (df.index[i].date() != df.index[i+1].date())
        if eod_close and position is not None and is_last_bar_of_day and interval != '1d':
            exit_price = current_close
            exit_time = t
            exit_reason = "End of Day Close"
            
            if position == "long":
                gross = (exit_price - entry_price) * qty
                pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
                long_trades.append([symbol, interval, entry_time, round(entry_price, 4), exit_time, 
                                  round(exit_price, 4), pl, "Win" if pl > 0 else "Loss", exit_reason])
            elif position == "short":
                gross = (entry_price - exit_price) * qty
                pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
                short_trades.append([symbol, interval, entry_time, round(entry_price, 4), exit_time, 
                                   round(exit_price, 4), pl, "Win" if pl > 0 else "Loss", exit_reason])
            
            position = entry_price = entry_time = stop_loss_price = None
            continue

        # 3. Check Regular Exit (Opposite Signal) - Use current signals
        if position == "long" and current_sell_signal:
            exit_price = execution_price - slippage_abs_per_side
            gross = (exit_price - entry_price) * qty
            pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
            long_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, 
                              round(exit_price, 4), pl, "Win" if pl > 0 else "Loss", "Signal Exit"])
            position = entry_price = entry_time = stop_loss_price = None
            
        elif position == "short" and current_buy_signal:
            exit_price = execution_price + slippage_abs_per_side
            gross = (entry_price - exit_price) * qty
            pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
            short_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, 
                               round(exit_price, 4), pl, "Win" if pl > 0 else "Loss", "Signal Exit"])
            position = entry_price = entry_time = stop_loss_price = None

        # 4. Check Entry - Use current signals
        if position is None:
            if current_buy_signal:
                entry_price = execution_price + slippage_abs_per_side
                entry_time = t
                position = "long"
                stop_loss_price = entry_price - stop_loss_per_share
                
            elif current_sell_signal:
                entry_price = execution_price - slippage_abs_per_side
                entry_time = t
                position = "short"
                stop_loss_price = entry_price + stop_loss_per_share

    df_cols = ['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Exit_date_time', 'Exit_price', 'Profit', 'Status', 'Exit_Reason']
    return pd.DataFrame(long_trades, columns=df_cols), pd.DataFrame(short_trades, columns=df_cols)

# --- REALISTIC Backtest Execution ---
def evaluate_performance_realistic(
    df: pd.DataFrame, interval: str, trade_size: float, symbol: str,
    fixed_stop_loss_amount: float,
    eod_close: bool = False,
    commission_per_side: float = 0.0, slippage_abs_per_side: float = 0.0
):
    """
    Realistic execution: 
    - Entry: NEXT candle's open after signal
    - Exit: NEXT candle's open after signal  
    - Uses only completed candle data
    """
    assert {"Buy_Signal", "Sell_Signal", "open", "high", "low", "close"}.issubset(df.columns), "Missing required columns"
    long_trades, short_trades = [], []
    position, entry_price, entry_time, stop_loss_price = None, None, None, None
    qty = float(trade_size)
    stop_loss_per_share = fixed_stop_loss_amount / qty

    for i in range(2, len(df)):  # Start from 2 to have previous completed candle
        t = df.index[i]
        current_open = float(df["open"].iloc[i])
        current_high = float(df["high"].iloc[i])
        current_low = float(df["low"].iloc[i])
        current_close = float(df["close"].iloc[i])
        
        # Use PREVIOUS completed candle signals (realistic)
        prev_buy_signal = bool(df["Buy_Signal"].iloc[i-1])  # Signal from completed candle
        prev_sell_signal = bool(df["Sell_Signal"].iloc[i-1])  # Signal from completed candle
        
        execution_price = current_open  # Execute at current candle's open
        
        if pd.isna(execution_price) or pd.isna(current_high) or pd.isna(current_low): 
            continue
            
        stop_loss_triggered = False
        exit_reason = ""

        # 1. Check Stop-Loss (intra-candle)
        if position is not None and stop_loss_price is not None:
            if position == "long" and current_low <= stop_loss_price:
                exit_price = max(stop_loss_price, current_low)
                gross = (exit_price - entry_price) * qty
                pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
                long_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, 
                                  round(exit_price, 4), pl, "Loss", "Stop Loss Hit"])
                stop_loss_triggered = True
                
            elif position == "short" and current_high >= stop_loss_price:
                exit_price = min(stop_loss_price, current_high)
                gross = (entry_price - exit_price) * qty
                pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
                short_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, 
                                   round(exit_price, 4), pl, "Loss", "Stop Loss Hit"])
                stop_loss_triggered = True
                
            if stop_loss_triggered:
                position = entry_price = entry_time = stop_loss_price = None
                continue

        # 2. Check EOD Close
        is_last_bar_of_day = (i + 1 == len(df)) or (df.index[i].date() != df.index[i+1].date())
        if eod_close and position is not None and is_last_bar_of_day and interval != '1d':
            exit_price = current_close
            gross = (exit_price - entry_price) * qty if position == "long" else (entry_price - exit_price) * qty
            pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
            trade_df = long_trades if position == "long" else short_trades
            trade_df.append([symbol, interval, entry_time, round(entry_price, 4), t, 
                           round(exit_price, 4), pl, "Win" if pl > 0 else "Loss", "EOD Close"])
            position = entry_price = entry_time = stop_loss_price = None
            continue

        # 3. Check Regular Exit (NEXT candle after signal)
        if position == "long" and prev_sell_signal:
            exit_price = execution_price - slippage_abs_per_side
            gross = (exit_price - entry_price) * qty
            pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
            long_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, 
                              round(exit_price, 4), pl, "Win" if pl > 0 else "Loss", "Signal Exit"])
            position = entry_price = entry_time = stop_loss_price = None
            
        elif position == "short" and prev_buy_signal:
            exit_price = execution_price + slippage_abs_per_side
            gross = (entry_price - exit_price) * qty
            pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
            short_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, 
                               round(exit_price, 4), pl, "Win" if pl > 0 else "Loss", "Signal Exit"])
            position = entry_price = entry_time = stop_loss_price = None

        # 4. Check Entry (NEXT candle after signal)
        if position is None:
            if prev_buy_signal:
                entry_price = execution_price + slippage_abs_per_side
                entry_time = t
                position = "long"
                stop_loss_price = entry_price - stop_loss_per_share
                
            elif prev_sell_signal:
                entry_price = execution_price - slippage_abs_per_side
                entry_time = t
                position = "short"
                stop_loss_price = entry_price + stop_loss_per_share

    df_cols = ['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Exit_date_time', 'Exit_price', 'Profit', 'Status', 'Exit_Reason']
    return pd.DataFrame(long_trades, columns=df_cols), pd.DataFrame(short_trades, columns=df_cols)

# --- Enhanced Success Rate Calculations ---
def calculate_success_and_profit(df: pd.DataFrame):
    """Calculate success rate and profit metrics"""
    if df.empty:
        return 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    
    total = len(df)
    wins = (df["Status"] == "Win").sum()
    losses = total - wins
    success_rate = wins / total if total > 0 else 0.0
    total_profit = float(df["Profit"].sum())
    
    # Additional metrics
    avg_profit = df["Profit"].mean() if total > 0 else 0.0
    win_avg_profit = df[df["Status"] == "Win"]["Profit"].mean() if wins > 0 else 0.0
    loss_avg_profit = df[df["Status"] == "Loss"]["Profit"].mean() if losses > 0 else 0.0
    max_win = df["Profit"].max() if total > 0 else 0.0
    max_loss = df["Profit"].min() if total > 0 else 0.0
    
    return total, wins, losses, success_rate, total_profit, avg_profit, win_avg_profit, loss_avg_profit, max_win, max_loss

# --- Comprehensive Logging and Plotting ---
def save_comprehensive_log(df: pd.DataFrame, long_trades: pd.DataFrame, short_trades: pd.DataFrame, 
                          symbol: str, interval: str, user_id: str, user_dir: str):
    """
    Save comprehensive log with candles data and trade entries/exits
    """
    log_file = os.path.join(user_dir, f"{user_id}_{symbol}_{interval}_comprehensive_log.csv")
    
    # Create a copy of the dataframe for logging
    log_df = df.copy()
    
    # Add trade markers to the log
    log_df['Long_Entry'] = False
    log_df['Long_Exit'] = False
    log_df['Short_Entry'] = False
    log_df['Short_Exit'] = False
    log_df['Trade_Type'] = ''
    log_df['Trade_Profit'] = 0.0
    
    # Mark long trades
    for _, trade in long_trades.iterrows():
        entry_time = trade['Entry_date_time']
        exit_time = trade['Exit_date_time']
        profit = trade['Profit']
        
        if entry_time in log_df.index:
            log_df.loc[entry_time, 'Long_Entry'] = True
            log_df.loc[entry_time, 'Trade_Type'] = 'Long'
        if exit_time in log_df.index:
            log_df.loc[exit_time, 'Long_Exit'] = True
            log_df.loc[exit_time, 'Trade_Profit'] = profit
    
    # Mark short trades
    for _, trade in short_trades.iterrows():
        entry_time = trade['Entry_date_time']
        exit_time = trade['Exit_date_time']
        profit = trade['Profit']
        
        if entry_time in log_df.index:
            log_df.loc[entry_time, 'Short_Entry'] = True
            log_df.loc[entry_time, 'Trade_Type'] = 'Short'
        if exit_time in log_df.index:
            log_df.loc[exit_time, 'Short_Exit'] = True
            log_df.loc[exit_time, 'Trade_Profit'] = profit
    
    # Save comprehensive log
    log_df.to_csv(log_file)
    logging.info(f"Comprehensive log saved: {log_file}")
    
    return log_df

def plot_trading_results(df: pd.DataFrame, long_trades: pd.DataFrame, short_trades: pd.DataFrame,
                        symbol: str, interval: str, user_id: str, user_dir: str):
    """
    Create comprehensive trading visualization
    """
    try:
        plt.style.use('dark_background')
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(15, 12), 
                                      gridspec_kw={'height_ratios': [3, 1]})
        
        # Plot 1: Price and Trades
        ax1.plot(df.index, df['close'], label='Close Price', color='white', linewidth=1, alpha=0.7)
        ax1.plot(df.index, df['supertrend'], label='SuperTrend', color='cyan', linewidth=1.5, alpha=0.8)
        
        # Plot long trades (green)
        for _, trade in long_trades.iterrows():
            entry_time = trade['Entry_date_time']
            exit_time = trade['Exit_date_time']
            entry_price = trade['Entry_price']
            exit_price = trade['Exit_price']
            profit = trade['Profit']
            
            color = 'lime' if profit > 0 else 'red'
            ax1.plot([entry_time, exit_time], [entry_price, exit_price], 
                    color=color, linewidth=2, marker='o', markersize=4)
            ax1.scatter(entry_time, entry_price, color='lime', marker='^', s=100, label='Long Entry' if _ == 0 else "")
            ax1.scatter(exit_time, exit_price, color=color, marker='v', s=100, label='Long Exit' if _ == 0 else "")
        
        # Plot short trades (orange/red)
        for _, trade in short_trades.iterrows():
            entry_time = trade['Entry_date_time']
            exit_time = trade['Exit_date_time']
            entry_price = trade['Entry_price']
            exit_price = trade['Exit_price']
            profit = trade['Profit']
            
            color = 'orange' if profit > 0 else 'darkred'
            ax1.plot([entry_time, exit_time], [entry_price, exit_price], 
                    color=color, linewidth=2, marker='o', markersize=4)
            ax1.scatter(entry_time, entry_price, color='orange', marker='v', s=100, label='Short Entry' if _ == 0 else "")
            ax1.scatter(exit_time, exit_price, color=color, marker='^', s=100, label='Short Exit' if _ == 0 else "")
        
        ax1.set_title(f'{symbol} {interval} - SuperTrend Trading Results', fontsize=16, fontweight='bold')
        ax1.set_ylabel('Price ($)', fontsize=12)
        ax1.legend()
        ax1.grid(True, alpha=0.3)
        
        # Format x-axis
        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M'))
        plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45)
        
        # Plot 2: Cumulative P&L
        all_trades = pd.concat([long_trades, short_trades]).sort_values('Exit_date_time')
        if not all_trades.empty:
            cumulative_pnl = all_trades['Profit'].cumsum()
            ax2.plot(all_trades['Exit_date_time'], cumulative_pnl, 
                    color='yellow', linewidth=2, marker='o', markersize=3)
            ax2.fill_between(all_trades['Exit_date_time'], cumulative_pnl, 
                           alpha=0.3, color='yellow')
            
            # Add final P&L annotation
            final_pnl = cumulative_pnl.iloc[-1]
            ax2.annotate(f'Final P&L: ${final_pnl:.2f}', 
                        xy=(all_trades['Exit_date_time'].iloc[-1], final_pnl),
                        xytext=(10, 10), textcoords='offset points',
                        bbox=dict(boxstyle='round,pad=0.3', facecolor='green' if final_pnl > 0 else 'red', alpha=0.7),
                        fontweight='bold')
        
        ax2.set_title('Cumulative Profit & Loss', fontsize=14, fontweight='bold')
        ax2.set_ylabel('P&L ($)', fontsize=12)
        ax2.set_xlabel('Time', fontsize=12)
        ax2.grid(True, alpha=0.3)
        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M'))
        plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45)
        
        plt.tight_layout()
        
        # Save plot
        plot_file = os.path.join(user_dir, f"{user_id}_{symbol}_{interval}_trading_plot.png")
        plt.savefig(plot_file, dpi=150, bbox_inches='tight')
        plt.close()
        
        logging.info(f"Trading plot saved: {plot_file}")
        
    except Exception as e:
        logging.error(f"Error creating plot: {e}")

def run(symbol: str, interval: str, trade_size: float, user_id: str, eod_close: bool, fixed_stop_loss_amount: float, realistic: bool = False):
    user_dir = os.path.join(DATA_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    df = get_data_for_fixed_period(symbol, interval)
    df_cols = ['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Exit_date_time', 'Exit_price', 'Profit', 'Status', 'Exit_Reason']
    
    # Add date range logging
    if not df.empty:
        start_date = df.index[0].strftime('%Y-%m-%d %H:%M')
        end_date = df.index[-1].strftime('%Y-%m-%d %H:%M')
        trading_days = len(df.index.normalize().unique())
        total_bars = len(df)
        logging.info(f"Data range: {start_date} to {end_date} ({trading_days} trading days, {total_bars} bars)")
    
    if df.empty or len(df) < 15:
        logging.warning(f"No/Insufficient data ({len(df)} rows). Min 15 required.")
        long_df, short_df = pd.DataFrame(columns=df_cols), pd.DataFrame(columns=df_cols)
    else:
        # Choose between realistic or original version
        if realistic:
            logging.info("Using REALISTIC signal generation (no lookahead bias)")
            df = determine_signals_realistic(df)
            long_df, short_df = evaluate_performance_realistic(df, interval, trade_size, symbol, fixed_stop_loss_amount, eod_close)
        else:
            logging.info("Using ORIGINAL signal generation")
            df = determine_signals(df)
            long_df, short_df = evaluate_performance(df, interval, trade_size, symbol, fixed_stop_loss_amount, eod_close)
        
        # Save comprehensive log and create plot
        if not long_df.empty or not short_df.empty:
            log_df = save_comprehensive_log(df, long_df, short_df, symbol, interval, user_id, user_dir)
            plot_trading_results(df, long_df, short_df, symbol, interval, user_id, user_dir)
    
    # Debug: Print signal counts
    buy_signals = df['Buy_Signal'].sum() if not df.empty else 0
    sell_signals = df['Sell_Signal'].sum() if not df.empty else 0
    logging.info(f"Buy signals: {buy_signals}, Sell signals: {sell_signals}")
    
    # Calculate detailed success rates
    long_total, long_wins, long_losses, long_sr, long_profit, long_avg, long_win_avg, long_loss_avg, long_max_win, long_max_loss = calculate_success_and_profit(long_df)
    short_total, short_wins, short_losses, short_sr, short_profit, short_avg, short_win_avg, short_loss_avg, short_max_win, short_max_loss = calculate_success_and_profit(short_df)
    
    # Combined metrics
    total_trades = long_total + short_total
    total_wins = long_wins + short_wins
    total_profit = long_profit + short_profit
    overall_sr = total_wins / total_trades if total_trades > 0 else 0.0
    
    # Create detailed summary
    summary_rows = [
        # Long trades summary
        [symbol, interval, trade_size, long_total, long_wins, long_losses, 
         f"{long_sr*100:.1f}%", f"${long_profit:.2f}", f"${long_avg:.2f}", 
         f"${long_win_avg:.2f}", f"${long_loss_avg:.2f}", f"${long_max_win:.2f}", 
         f"${long_max_loss:.2f}", "Long"],
        
        # Short trades summary  
        [symbol, interval, trade_size, short_total, short_wins, short_losses,
         f"{short_sr*100:.1f}%", f"${short_profit:.2f}", f"${short_avg:.2f}",
         f"${short_win_avg:.2f}", f"${short_loss_avg:.2f}", f"${short_max_win:.2f}",
         f"${short_max_loss:.2f}", "Short"],
        
        # Combined summary
        [symbol, interval, trade_size, total_trades, total_wins, total_trades - total_wins,
         f"{overall_sr*100:.1f}%", f"${total_profit:.2f}", f"${(long_avg + short_avg)/2:.2f}" if total_trades > 0 else "$0.00",
         f"${(long_win_avg + short_win_avg)/2:.2f}" if long_wins + short_wins > 0 else "$0.00", 
         f"${(long_loss_avg + short_loss_avg)/2:.2f}" if (long_total - long_wins) + (short_total - short_wins) > 0 else "$0.00",
         f"${max(long_max_win, short_max_win):.2f}", f"${min(long_max_loss, short_max_loss):.2f}", "Combined"]
    ]
    
    summary_columns = [
        'Symbol', 'Interval', 'Trade_size', 'Total_Trades', 'Wins', 'Losses', 
        'Success_Rate', 'Total_Profit', 'Avg_Profit', 'Avg_Win', 'Avg_Loss', 
        'Max_Win', 'Max_Loss', 'Trade_Type'
    ]
    
    summary_df = pd.DataFrame(summary_rows, columns=summary_columns)
    summary_file = os.path.join(user_dir, f"{user_id}_{symbol}_{interval}_summary.csv")
    summary_df.to_csv(summary_file, index=False)
    
    # Also save detailed trades for analysis
    trades_file = os.path.join(user_dir, f"{user_id}_{symbol}_{interval}_trades.csv")
    all_trades = pd.concat([long_df, short_df], ignore_index=True)
    all_trades.to_csv(trades_file, index=False)
    
    # Log detailed success rate information
    logging.info(f"Summary for Algo2 (Improved SuperTrend) on {interval} saved.")
    logging.info(f"=== SUCCESS RATE ANALYSIS ===")
    logging.info(f"Long Trades: {long_total} total, {long_wins} wins, {long_losses} losses, SR: {long_sr*100:.1f}%")
    logging.info(f"Short Trades: {short_total} total, {short_wins} wins, {short_losses} losses, SR: {short_sr*100:.1f}%")
    logging.info(f"Overall: {total_trades} total, {total_wins} wins, SR: {overall_sr*100:.1f}%")
    logging.info(f"=== PROFIT ANALYSIS ===")
    logging.info(f"Long P&L: ${long_profit:.2f}, Avg: ${long_avg:.2f}")
    logging.info(f"Short P&L: ${short_profit:.2f}, Avg: ${short_avg:.2f}")
    logging.info(f"Total P&L: ${total_profit:.2f}")
    logging.info(f"=== TRADE QUALITY ===")
    if long_wins > 0:
        logging.info(f"Long - Avg Win: ${long_win_avg:.2f}, Avg Loss: ${long_loss_avg:.2f}")
    if short_wins > 0:
        logging.info(f"Short - Avg Win: ${short_win_avg:.2f}, Avg Loss: ${short_loss_avg:.2f}")

# --- CLI ---
def parse_args():
    p = argparse.ArgumentParser(description="Algo2: Improved SuperTrend (AI Eval with Fixed $ Stop)")
    p.add_argument("--symbol", "-s", required=True)
    p.add_argument("--interval", "-i", required=True, choices=sorted(ALLOWED_INTERVALS))
    p.add_argument("--trade-size", "-q", type=float, default=100.0)
    p.add_argument("--user-id", "-u", required=True)
    p.add_argument("--eod-close", action="store_true", help="Force close positions at EOD.")
    p.add_argument("--fixed-stop-loss", type=float, default=300.0, help="Fixed dollar stop-loss amount (e.g., 500)")
    p.add_argument("--realistic", action="store_true", help="Use realistic signal generation (no lookahead bias)")
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()
    eod_flag = args.eod_close if args.interval != '1d' else False
    run(symbol=args.symbol, interval=args.interval, trade_size=args.trade_size, 
        user_id=args.user_id, eod_close=eod_flag, fixed_stop_loss_amount=args.fixed_stop_loss,
        realistic=args.realistic)