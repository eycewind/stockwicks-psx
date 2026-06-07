#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/stocks/backtest_algos/Algo1_eval.py

import sys, os, argparse, logging, pandas as pd, requests
import numpy as np
from datetime import datetime, timedelta
from dotenv import load_dotenv

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
if REPO_ROOT not in sys.path: sys.path.insert(0, REPO_ROOT)
from app.utils.stock.schwab_token import get_valid_access_token

# --- Config ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
ET = pd.Timestamp.now(tz='America/New_York').tz
DATA_DIR = os.getenv("DATA_DIR", "/var/www/stockwicks/data")
SCHWAB_INTERVALS = {
    '1min': ('day', 10, 'minute', 1),
    '5min': ('day', 10, 'minute', 5),
    '10min': ('day', 10, 'minute', 10),
    '15min': ('day', 10, 'minute', 15),
    '30min': ('day', 10, 'minute', 30),
    '1d': ('year', 20, 'daily', 1)  # Increased to 20 years for more data
}
ALLOWED_INTERVALS = set(SCHWAB_INTERVALS.keys())

# --- Data Fetching ---
def get_data_for_fixed_period(symbol: str, interval: str) -> pd.DataFrame:
    if interval not in SCHWAB_INTERVALS:
        raise ValueError(f"Unsupported interval: {interval}")
    periodType, period, frequencyType, frequency = SCHWAB_INTERVALS[interval]
    token = get_valid_access_token()
    if not token:
        raise RuntimeError("Failed to get Schwab access token")
    url = "https://api.schwabapi.com/marketdata/v1/pricehistory"
    params = {
        "symbol": symbol.upper(),
        "periodType": periodType,
        "period": period,
        "frequencyType": frequencyType,
        "frequency": frequency,
        "needExtendedHoursData": "false"
    }
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        candles = resp.json().get("candles", [])
        if not candles:
            logging.warning(f"API returned no candles for {symbol} on {interval}")
            return pd.DataFrame()
        df = pd.DataFrame(candles)
        df["timestamp"] = pd.to_datetime(df["datetime"], unit="ms", utc=True).dt.tz_convert(ET)
        df = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
        df = df[~df.index.duplicated(keep="first")].sort_index()
        return df
    except requests.HTTPError as e:
        logging.error(f"Failed fetch {symbol} {interval}: {e}")
        return pd.DataFrame()

# --- SuperTrend Algorithm Logic ---
def calculate_supertrend(df: pd.DataFrame, atr_mult: float = 1.0, n_atr: int = 4) -> pd.DataFrame:
    """
    Calculate SuperTrend indicator - FIXED VERSION
    """
    df = df.copy()
    
    # Calculate True Range
    df['prev_close'] = df['close'].shift(1)
    df['tr1'] = df['high'] - df['low']
    df['tr2'] = abs(df['high'] - df['prev_close'])
    df['tr3'] = abs(df['low'] - df['prev_close'])
    df['true_range'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
    
    # Calculate ATR using Wilder's smoothing (standard for SuperTrend)
    df['atr'] = df['true_range'].ewm(alpha=1/n_atr, adjust=False).mean()
    
    # Calculate HL2 (average of high and low)
    df['hl2'] = (df['high'] + df['low']) / 2
    
    # Calculate Upper and Lower bands
    df['upper_band'] = df['hl2'] + (atr_mult * df['atr'])
    df['lower_band'] = df['hl2'] - (atr_mult * df['atr'])
    
    # Initialize SuperTrend column
    df['supertrend'] = np.nan
    df['supertrend_trend'] = 1  # 1 for uptrend, -1 for downtrend
    
    # Calculate SuperTrend (iterative calculation)
    for i in range(len(df)):
        if i == 0:
            df.iloc[i, df.columns.get_loc('supertrend')] = df['hl2'].iloc[i]
            df.iloc[i, df.columns.get_loc('supertrend_trend')] = 1
        else:
            prev_st = df['supertrend'].iloc[i-1]
            prev_trend = df['supertrend_trend'].iloc[i-1]
            current_close = df['close'].iloc[i]
            upper_band = df['upper_band'].iloc[i]
            lower_band = df['lower_band'].iloc[i]
            
            if prev_trend == 1:  # Previous uptrend
                if current_close > lower_band:
                    # Continue uptrend
                    df.iloc[i, df.columns.get_loc('supertrend')] = max(lower_band, prev_st)
                    df.iloc[i, df.columns.get_loc('supertrend_trend')] = 1
                else:
                    # Switch to downtrend
                    df.iloc[i, df.columns.get_loc('supertrend')] = upper_band
                    df.iloc[i, df.columns.get_loc('supertrend_trend')] = -1
            else:  # Previous downtrend
                if current_close < upper_band:
                    # Continue downtrend
                    df.iloc[i, df.columns.get_loc('supertrend')] = min(upper_band, prev_st)
                    df.iloc[i, df.columns.get_loc('supertrend_trend')] = -1
                else:
                    # Switch to uptrend
                    df.iloc[i, df.columns.get_loc('supertrend')] = lower_band
                    df.iloc[i, df.columns.get_loc('supertrend_trend')] = 1
    
    # Calculate cross signals - FIXED: Use current bar for Thinkorswim compatibility
    df['prev_supertrend'] = df['supertrend'].shift(1)
    df['prev_close'] = df['close'].shift(1)
    
    # Cross up: close crosses above supertrend (CURRENT BAR)
    df['cross_up'] = (df['prev_close'] <= df['prev_supertrend']) & (df['close'] > df['supertrend'])
    
    # Cross down: close crosses below supertrend (CURRENT BAR)  
    df['cross_down'] = (df['prev_close'] >= df['prev_supertrend']) & (df['close'] < df['supertrend'])
    
    # Trading signals - FIXED: Execute on CURRENT BAR open (like Thinkorswim)
    df['Buy_Signal'] = df['cross_up']
    df['Sell_Signal'] = df['cross_down']
    
    # Clean up temporary columns
    df = df.drop(['prev_close', 'tr1', 'tr2', 'tr3', 'prev_supertrend'], axis=1, errors='ignore')
    
    return df

def determine_signals(df: pd.DataFrame, atr_mult: float = 1.0, n_atr: int = 4, allow_shorts: bool = True) -> pd.DataFrame:
    """
    SuperTrend signal generation
    """
    df = calculate_supertrend(df, atr_mult, n_atr)
    
    # If shorts are not allowed, remove sell signals
    if not allow_shorts:
        df['Sell_Signal'] = False
    
    # Add dummy columns for compatibility with existing backtest framework
    df['LongBandStop'] = False
    df['ShortBandStop'] = False
    
    return df

# --- Backtest Core - FIXED for Thinkorswim Timing ---
def evaluate_performance(
    df: pd.DataFrame,
    interval: str,
    trade_size: float,
    symbol: str,
    fixed_stop_loss_amount: float = 0.0,
    eod_close: bool = False,
    commission_per_side: float = 1.00,  # Realistic commission
    slippage_abs_per_side: float = 0.01,  # Realistic slippage
    allow_shorts: bool = True
):
    required = {"Buy_Signal", "Sell_Signal", "open", "high", "low", "close"}
    assert required.issubset(df.columns), f"Missing required columns: {required - set(df.columns)}"

    long_trades, short_trades = [], []
    position, entry_price, entry_time, stop_loss_price = None, None, None, None
    qty = float(trade_size)
    
    # Only set stop loss if amount is provided and positive
    use_stop_loss = fixed_stop_loss_amount > 0
    stop_loss_per_share = fixed_stop_loss_amount / qty if use_stop_loss and qty > 0 else 0.0

    for i in range(1, len(df)):
        t = df.index[i]
        o = float(df["open"].iloc[i])
        h = float(df["high"].iloc[i])
        l = float(df["low"].iloc[i])
        c = float(df["close"].iloc[i])

        # FIXED: Use CURRENT BAR signals (like Thinkorswim), execute on CURRENT open
        current_buy  = bool(df["Buy_Signal"].iloc[i])
        current_sell = bool(df["Sell_Signal"].iloc[i])

        if any(pd.isna(x) for x in (o, h, l, c)):
            continue

        # ================= 1) FIXED $ STOP-LOSS ====================
        if use_stop_loss and position is not None and stop_loss_price is not None:
            if position == "long" and l <= stop_loss_price:
                exit_price = stop_loss_price
                gross = (exit_price - entry_price) * qty
                pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
                long_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(exit_price, 4), pl, "Loss", "Stop Loss Hit"])
                position = entry_price = entry_time = stop_loss_price = None
                continue
            elif position == "short" and h >= stop_loss_price:
                exit_price = stop_loss_price
                gross = (entry_price - exit_price) * qty
                pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
                short_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(exit_price, 4), pl, "Loss", "Stop Loss Hit"])
                position = entry_price = entry_time = stop_loss_price = None
                continue

        # ===================== 2) EOD CLOSE ========================
        is_last_bar_of_day = (i + 1 == len(df)) or (df.index[i].date() != df.index[i+1].date())
        if eod_close and position is not None and is_last_bar_of_day and interval != '1d':
            exit_price = c
            if position == "long":
                gross = (exit_price - entry_price) * qty
                pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
                long_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(exit_price, 4), pl, "Win" if pl > 0 else "Loss", "End of Day Close"])
            else:
                gross = (entry_price - exit_price) * qty
                pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
                short_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(exit_price, 4), pl, "Win" if pl > 0 else "Loss", "End of Day Close"])
            position = entry_price = entry_time = stop_loss_price = None
            continue

        # ================= 3) SIGNAL EXITS ==========
        if position == "long" and current_sell:
            exit_price = o - slippage_abs_per_side  # Slippage on exit
            gross = (exit_price - entry_price) * qty
            pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
            long_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(exit_price, 4), pl, "Win" if pl > 0 else "Loss", "Signal Exit"])
            position = entry_price = entry_time = stop_loss_price = None
            continue
        elif position == "short" and current_buy and allow_shorts:
            exit_price = o + slippage_abs_per_side  # Slippage on exit
            gross = (entry_price - exit_price) * qty
            pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
            short_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(exit_price, 4), pl, "Win" if pl > 0 else "Loss", "Signal Exit"])
            position = entry_price = entry_time = stop_loss_price = None
            continue

        # =========================== 4) ENTRIES ===============================
        if position is None:
            if current_buy:
                entry_price = o + slippage_abs_per_side  # Slippage on entry
                entry_time = t
                position = "long"
                if use_stop_loss:
                    stop_loss_price = entry_price - stop_loss_per_share
            elif current_sell and allow_shorts:
                entry_price = o - slippage_abs_per_side  # Slippage on entry
                entry_time = t
                position = "short"
                if use_stop_loss:
                    stop_loss_price = entry_price + stop_loss_per_share

    cols = ['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Exit_date_time', 'Exit_price', 'Profit', 'Status', 'Exit_Reason']
    return pd.DataFrame(long_trades, columns=cols), pd.DataFrame(short_trades, columns=cols)

# --- Reporting helpers ---
def calculate_success_and_profit(df: pd.DataFrame):
    if df.empty:
        return 0, 0, 0, 0.0, 0.0
    total = len(df)
    wins = (df["Status"] == "Win").sum() if total else 0
    losses = total - wins
    sr = wins / total if total else 0.0
    profit = float(df["Profit"].sum()) if total else 0.0
    return total, wins, losses, sr, profit

def run(symbol: str, interval: str, trade_size: float, user_id: str, eod_close: bool, 
        fixed_stop_loss_amount: float, atr_mult: float = 1.0, n_atr: int = 4, allow_shorts: bool = True):
    user_dir = os.path.join(DATA_DIR, str(user_id)); os.makedirs(user_dir, exist_ok=True)
    
    # Get more data to match Thinkorswim timeframe
    df = get_data_for_fixed_period(symbol, interval)
    cols = ['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Exit_date_time', 'Exit_price', 'Profit', 'Status', 'Exit_Reason']

    if df.empty or len(df) < 50:
        logging.warning(f"Insufficient data for {symbol} {interval}: {len(df)} rows")
        long_df, short_df = pd.DataFrame(columns=cols), pd.DataFrame(columns=cols)
    else:
        logging.info(f"Processing {len(df)} bars for {symbol} {interval} with SuperTrend")
        df = determine_signals(df, atr_mult=atr_mult, n_atr=n_atr, allow_shorts=allow_shorts)
        
        # Debug: Check signal counts
        total_buy_signals = df['Buy_Signal'].sum()
        total_sell_signals = df['Sell_Signal'].sum()
        logging.info(f"SuperTrend Buy signals: {total_buy_signals}")
        logging.info(f"SuperTrend Sell signals: {total_sell_signals}")
        
        # Filter to match Thinkorswim date range if possible
        thinkorswim_start = pd.Timestamp('2025-10-31 08:39:00', tz=ET)
        thinkorswim_end = pd.Timestamp('2025-11-13 14:54:00', tz=ET)
        
        df_filtered = df[(df.index >= thinkorswim_start) & (df.index <= thinkorswim_end)]
        if len(df_filtered) > 0:
            logging.info(f"Filtered to Thinkorswim date range: {len(df_filtered)} bars")
            df = df_filtered
        
        long_df, short_df = evaluate_performance(
            df, interval, trade_size, symbol,
            fixed_stop_loss_amount, eod_close,
            allow_shorts=allow_shorts
        )

    long_summary  = calculate_success_and_profit(long_df)
    short_summary = calculate_success_and_profit(short_df)

    summary_rows = [
        [symbol, interval, trade_size, *long_summary[:3], f"{long_summary[3]*100:.2f}%", f"${long_summary[4]:.2f}", "Long"],
        [symbol, interval, trade_size, *short_summary[:3], f"{short_summary[3]*100:.2f}%", f"${short_summary[4]:.2f}", "Short"],
    ]
    summary_df = pd.DataFrame(summary_rows, columns=['Symbol','Interval','Trade_size','Total_Trades','Wins','Losses','SuccessRate','Total_profit','Trade_Type'])
    summary_file = os.path.join(user_dir, f"{user_id}_{symbol}_{interval}_supertrend_fixed.csv")
    summary_df.to_csv(summary_file, index=False)
    
    # Save detailed trades
    if not long_df.empty:
        long_df.to_csv(os.path.join(user_dir, f"{user_id}_{symbol}_{interval}_long_trades_fixed.csv"), index=False)
    if not short_df.empty:
        short_df.to_csv(os.path.join(user_dir, f"{user_id}_{symbol}_{interval}_short_trades_fixed.csv"), index=False)
    
    total_profit = long_summary[4] + short_summary[4]
    total_trades = long_summary[0] + short_summary[0]
    
    logging.info(f"=== SUPERFIXED RESULTS ===")
    logging.info(f"Thinkorswim-like timing (current bar execution)")
    logging.info(f"Total trades: {total_trades}")
    logging.info(f"Long trades: {long_summary[0]}, Success: {long_summary[3]*100:.1f}%, Profit: ${long_summary[4]:.2f}")
    logging.info(f"Short trades: {short_summary[0]}, Success: {short_summary[3]*100:.1f}%, Profit: ${short_summary[4]:.2f}")
    logging.info(f"TOTAL PROFIT: ${total_profit:.2f}")
    logging.info(f"Summary saved -> {summary_file}")

# --- CLI ---
def parse_args():
    p = argparse.ArgumentParser(description="SuperTrend Strategy - Thinkorswim Compatible")
    p.add_argument("--symbol", "-s", required=True)
    p.add_argument("--interval", "-i", required=True, choices=sorted(ALLOWED_INTERVALS))
    p.add_argument("--trade-size", "-q", type=float, default=100.0)
    p.add_argument("--user-id", "-u", required=True)
    p.add_argument("--eod-close", action="store_true", help="Force close positions at EOD.")
    p.add_argument("--fixed-stop-loss", type=float, default=0.0, help="Fixed dollar stop-loss amount (0 to disable)")
    p.add_argument("--atr-mult", type=float, default=1.0, help="ATR multiplier for SuperTrend")
    p.add_argument("--n-atr", type=int, default=4, help="ATR period for SuperTrend")
    p.add_argument("--no-shorts", action="store_true", help="Disable short selling")
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()
    eod_flag = args.eod_close if args.interval != '1d' else False
    allow_shorts = not args.no_shorts
    
    run(
        symbol=args.symbol,
        interval=args.interval,
        trade_size=args.trade_size,
        user_id=args.user_id,
        eod_close=eod_flag,
        fixed_stop_loss_amount=args.fixed_stop_loss,
        atr_mult=args.atr_mult,
        n_atr=args.n_atr,
        allow_shorts=allow_shorts
    )