#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/stocks/backtest_algos/Algo2_eval.py

import sys, os, argparse, logging, pandas as pd, requests, time
import numpy as np, pandas_ta as ta # Added pandas_ta
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

# --- Algorithm-Specific Logic ---
def compute_atr_trailing_stop(df: pd.DataFrame, period: int = 14, multiplier: float = 3.0) -> pd.Series:
    # Keep existing logic
    df = df.copy(); df['h-l'], df['h-pc'], df['l-pc'] = df['high']-df['low'], abs(df['high']-df['close'].shift(1)), abs(df['low']-df['close'].shift(1))
    df['tr'] = df[['h-l', 'h-pc', 'l-pc']].max(axis=1); df['atr'] = df['tr'].ewm(span=period, adjust=False).mean()
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
    df['atr_stop_signal'] = compute_atr_trailing_stop(df, multiplier=3.0) # Used for signals
    df.ta.atr(length=14, append=True) # Calculate ATRr_14 separately for risk stop
    df['Buy_Signal'] = (df['close'].shift(1) <= df['atr_stop_signal'].shift(1)) & (df['close'] > df['atr_stop_signal'])
    df['Sell_Signal'] = (df['close'].shift(1) >= df['atr_stop_signal'].shift(1)) & (df['close'] < df['atr_stop_signal'])
    return df

# --- Backtest Core (UPDATED with Fixed ATR Stop & EOD Close) ---
def evaluate_performance(
    df: pd.DataFrame, interval: str, trade_size: float, symbol: str,
    atr_multiplier: float = 3.0, # ATR multiplier for FIXED stop-loss
    eod_close: bool = False,
    commission_per_side: float = 0.0, slippage_abs_per_side: float = 0.0
):
    # This uses the same unified evaluation function as Algo1_eval.py
    assert {"Buy_Signal", "Sell_Signal", "open", "high", "low", "close", "ATRr_14"}.issubset(df.columns), "Missing required columns"
    long_trades, short_trades = [], []
    position, entry_price, entry_time, stop_loss_price = None, None, None, None
    qty = float(trade_size)

    for i in range(1, len(df)):
        t = df.index[i]; current_open=float(df["open"].iloc[i]); current_high=float(df["high"].iloc[i]); current_low=float(df["low"].iloc[i]); current_close=float(df["close"].iloc[i])
        prev_buy_signal=bool(df["Buy_Signal"].iloc[i-1]); prev_sell_signal=bool(df["Sell_Signal"].iloc[i-1])
        execution_price=current_open # Execute at open of next bar
        if pd.isna(execution_price) or pd.isna(current_high) or pd.isna(current_low): continue
        stop_loss_triggered = False; exit_reason = ""

        # 1. Check Fixed Stop-Loss
        if position is not None and stop_loss_price is not None:
            if position == "long" and current_low <= stop_loss_price:
                exit_price=stop_loss_price; gross=(exit_price - entry_price) * qty; pl=round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
                long_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(exit_price, 4), pl, "Loss", "Stop Loss Hit"]); stop_loss_triggered=True; exit_reason="Stop Loss Hit"
            elif position == "short" and current_high >= stop_loss_price:
                exit_price=stop_loss_price; gross=(entry_price - exit_price) * qty; pl=round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
                short_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(exit_price, 4), pl, "Loss", "Stop Loss Hit"]); stop_loss_triggered=True; exit_reason="Stop Loss Hit"
            if stop_loss_triggered: position=entry_price=entry_time=stop_loss_price=None; continue

        # 2. Check EOD Close
        is_last_bar_of_day=(i + 1 == len(df)) or (df.index[i].date() != df.index[i+1].date())
        if eod_close and position is not None and is_last_bar_of_day and interval != '1d': # EOD only for intraday
            exit_price=current_close; exit_time=t; exit_reason="End of Day Close"
            if position == "long": gross=(exit_price - entry_price) * qty; pl=round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2); long_trades.append([symbol, interval, entry_time, round(entry_price, 4), exit_time, round(exit_price, 4), pl, "Win" if pl > 0 else "Loss", exit_reason])
            elif position == "short": gross=(entry_price - exit_price) * qty; pl=round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2); short_trades.append([symbol, interval, entry_time, round(entry_price, 4), exit_time, round(exit_price, 4), pl, "Win" if pl > 0 else "Loss", exit_reason])
            position=entry_price=entry_time=stop_loss_price=None; continue

        # 3. Check Regular Exit (Opposite Signal based on ATR Trailing Stop)
        if position == "long" and prev_sell_signal:
            exit_price=execution_price - slippage_abs_per_side; gross=(exit_price - entry_price) * qty; pl=round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
            long_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(exit_price, 4), pl, "Win" if pl > 0 else "Loss", "Signal Exit"]); position=entry_price=entry_time=stop_loss_price=None
        elif position == "short" and prev_buy_signal:
            exit_price=execution_price + slippage_abs_per_side; gross=(entry_price - exit_price) * qty; pl=round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
            short_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(exit_price, 4), pl, "Win" if pl > 0 else "Loss", "Signal Exit"]); position=entry_price=entry_time=stop_loss_price=None

        # 4. Check Entry
        if position is None:
            atr_value = float(df["ATRr_14"].iloc[i-1]) if not pd.isna(df["ATRr_14"].iloc[i-1]) else None # Use ATRr_14 for stop loss calc
            if prev_buy_signal and atr_value is not None:
                entry_price=execution_price + slippage_abs_per_side; entry_time=t; position="long"; stop_loss_amount=atr_value*atr_multiplier; stop_loss_price=entry_price-stop_loss_amount
            elif prev_sell_signal and atr_value is not None:
                entry_price=execution_price - slippage_abs_per_side; entry_time=t; position="short"; stop_loss_amount=atr_value*atr_multiplier; stop_loss_price=entry_price+stop_loss_amount

    df_cols = ['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Exit_date_time', 'Exit_price', 'Profit', 'Status', 'Exit_Reason']
    return pd.DataFrame(long_trades, columns=df_cols), pd.DataFrame(short_trades, columns=df_cols)

# --- Pipeline & Reporting ---
def calculate_success_and_profit(df: pd.DataFrame):
    total = len(df); wins = (df["Status"] == "Win").sum()
    return total, wins, total - wins, wins / total if total > 0 else 0.0, float(df["Profit"].sum())

def run(symbol: str, interval: str, trade_size: float, user_id: str, eod_close: bool, atr_multiplier: float): # Added atr_multiplier
    user_dir = os.path.join(DATA_DIR, str(user_id)); os.makedirs(user_dir, exist_ok=True)
    df = get_data_for_fixed_period(symbol, interval)
    df_cols = ['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Exit_date_time', 'Exit_price', 'Profit', 'Status', 'Exit_Reason'] # Added Exit_Reason
    if df.empty or len(df) < 15: # Need ~15 bars for ATR
        logging.warning(f"No/Insufficient data ({len(df)} rows). Min 15 required.")
        long_df, short_df = pd.DataFrame(columns=df_cols), pd.DataFrame(columns=df_cols)
    else:
        df = determine_signals(df)
        long_df, short_df = evaluate_performance(df, interval, trade_size, symbol, atr_multiplier, eod_close) # Pass ATR multiplier
    long_summary, short_summary = calculate_success_and_profit(long_df), calculate_success_and_profit(short_df)
    summary_rows = [
        [symbol, interval, trade_size, *long_summary[:3], f"{long_summary[3]*100:.2f}%", f"${long_summary[4]:.2f}", "Long"],
        [symbol, interval, trade_size, *short_summary[:3], f"{short_summary[3]*100:.2f}%", f"${short_summary[4]:.2f}", "Short"],
    ]
    summary_df = pd.DataFrame(summary_rows, columns=['Symbol', 'Interval', 'Trade_size', 'Total_Trades', 'Wins', 'Losses', 'SuccessRate', 'Total_profit', 'Trade_Type'])
    summary_file = os.path.join(user_dir, f"{user_id}_{symbol}_{interval}_summary.csv")
    summary_df.to_csv(summary_file, index=False)
    logging.info(f"Summary for Algo2 on {interval} saved.")

# --- CLI ---
def parse_args():
    p = argparse.ArgumentParser(description="Algo2: ATR Trend (AI Eval with Fixed ATR Stop)")
    p.add_argument("--symbol", "-s", required=True)
    p.add_argument("--interval", "-i", required=True, choices=sorted(ALLOWED_INTERVALS))
    p.add_argument("--trade-size", "-q", type=float, default=100.0)
    p.add_argument("--user-id", "-u", required=True)
    p.add_argument("--eod-close", action="store_true", help="Force close positions at EOD.")
    p.add_argument("--atr-multiplier", type=float, default=2.0, help="ATR multiplier for fixed stop-loss (default: 2.0)") # Added ATR arg
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()
    eod_flag = args.eod_close if args.interval != '1d' else False # EOD only for intraday
    run(symbol=args.symbol, interval=args.interval, trade_size=args.trade_size, user_id=args.user_id, eod_close=eod_flag, atr_multiplier=args.atr_multiplier) # Pass ATR multiplier