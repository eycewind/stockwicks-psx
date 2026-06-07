#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/stocks/backtest_algos/Algo1_VWAP_Scalper.py

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
# Helpers
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

# -----------------------
# Schwab API fetchers
# -----------------------
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
        day_start = d.replace(hour=9, minute=30); day_end = d.replace(hour=16, minute=0)
        start_ms, end_ms = int(day_start.tz_convert(UTC).timestamp() * 1000), int(day_end.tz_convert(UTC).timestamp() * 1000)
        try:
            data = _schwab_pricehistory(symbol, frequencyType, frequency, start_ms=start_ms, end_ms=end_ms, need_extended=extended_hours)
            candles = data.get("candles", [])
            df = process_candles_to_df(candles)
            if not df.empty:
                df = df.between_time("09:30", "16:00")
                frames.append(df)
            time.sleep(0.25)
        except requests.HTTPError as e:
            logging.warning(f"HTTP Error fetching {symbol} for {d.date()}: {e}")
    return pd.concat(frames).sort_index() if frames else pd.DataFrame()

## --- THIS FUNCTION IS NOW FULLY CORRECTED --- ##
def get_df_for_range(symbol: str, interval: str, start_date: str, end_date: str, extended_hours: bool) -> pd.DataFrame:
    if interval not in ALLOWED_INTERVALS:
        raise ValueError(f"Unsupported interval '{interval}'")

    start_et = pd.Timestamp(start_date, tz=ET)
    end_et = pd.Timestamp(end_date, tz=ET)

    if interval == "1d":
        frequencyType, frequency = SCHWAB_SPEC["1d"]
        try:
            duration_days = (end_et - start_et).days
            duration_years = (duration_days + 5) / 365.25 # Add buffer
            valid_periods = [1, 2, 3, 5, 10, 15, 20]
            request_period = 1
            for p in valid_periods:
                if p >= duration_years:
                    request_period = p
                    break
            else:
                raise ValueError(f"Date range is too large ({duration_years:.1f} years). Max 20.")

            logging.info(f"Fetching {request_period} year(s) of daily data for {symbol} to cover user range.")
            data = _schwab_pricehistory(
                symbol, frequencyType, frequency,
                periodType="year", period=request_period,
                need_extended=False
            )
            full_df = process_candles_to_df(data.get("candles", []))
            if full_df.empty:
                logging.warning("API returned no daily data.")
                return pd.DataFrame()
            
            # Filter the large dataset down to the user's specific range
            filtered_df = full_df.loc[start_et.normalize():end_et.normalize()]
            logging.info(f"Successfully filtered daily data to {len(filtered_df)} rows.")
            return filtered_df
        except (requests.HTTPError, ValueError) as e:
            logging.error(f"[DAILY] Failed to fetch daily data for {symbol}: {e}")
            return pd.DataFrame()
    else:
        # Intraday fetching uses the reliable day-by-day chunking method
        return fetch_range_minute_chunked(symbol, interval, start_et, end_et, extended_hours)

# -----------------------
# Indicators, Backtest Core, Pipeline, CLI...
# -----------------------
def compute_vwap_and_bands(df: pd.DataFrame, std_dev_mult: float = 1.5):
    df = df.copy()

    def calc_vwap_daily(day_df):
        tp = (day_df['high'] + day_df['low'] + day_df['close']) / 3
        tp_vol = tp * day_df['volume']
        cum_tp_vol = tp_vol.cumsum()
        cum_vol = day_df['volume'].cumsum()
        vwap = cum_tp_vol / cum_vol

        # volume-weighted std around VWAP
        std_sq = ((tp - vwap) ** 2) * day_df['volume']
        mean_sq_error = std_sq.cumsum() / cum_vol
        std_dev = np.sqrt(mean_sq_error)

        day_df['VWAP'] = vwap
        day_df['VWAP_UpperBand'] = vwap + std_dev * std_dev_mult
        day_df['VWAP_LowerBand'] = vwap - std_dev * std_dev_mult
        day_df['VWAP_UpperBand2'] = vwap + std_dev * 2.0
        day_df['VWAP_LowerBand2'] = vwap - std_dev * 2.0
        return day_df

    # group by trading day in index timezone (ET)
    df = df.groupby(df.index.normalize(), group_keys=False).apply(calc_vwap_daily)
    return df

def determine_signals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df = compute_vwap_and_bands(df, std_dev_mult=1.5)

    df["SMA20"] = df["close"].rolling(window=20).mean()

    was_oversold = (df["close"].shift(1) < df["VWAP_LowerBand"].shift(1))
    above_sma20 = (df["close"] > df["SMA20"])
    crosses_back_up = (df["close"] > df["VWAP_LowerBand"])
    df["Buy_Signal"] = was_oversold & above_sma20 & crosses_back_up

    was_overbought = (df["close"].shift(1) > df["VWAP_UpperBand"].shift(1))
    below_sma20 = (df["close"] < df["SMA20"])
    crosses_back_down = (df["close"] < df["VWAP_UpperBand"])
    df["Sell_Signal"] = was_overbought & below_sma20 & crosses_back_down

    long_exit_trigger = (df["close"].shift(1) <= df["VWAP"].shift(1)) & (df["close"] > df["VWAP"])
    short_exit_trigger = (df["close"].shift(1) >= df["VWAP"].shift(1)) & (df["close"] < df["VWAP"])
    df['Exit_Long_Signal'] = (long_exit_trigger & (~df['Sell_Signal']))
    df['Exit_Short_Signal'] = (short_exit_trigger & (~df['Buy_Signal']))

    # combine exits to reduce whipsaw
    df["Sell_Signal"] = df["Sell_Signal"] | df['Exit_Long_Signal']
    df["Buy_Signal"] = df["Buy_Signal"] | df['Exit_Short_Signal']

    # if both fire, prefer Buy (clear Sell)
    df.loc[(df['Buy_Signal']) & (df['Sell_Signal']), 'Sell_Signal'] = False
    return df

def evaluate_performance(
    df: pd.DataFrame,
    interval: str,
    trade_size: float,
    symbol: str,
    commission_per_side: float = 0.0,
    slippage_abs_per_side: float = 0.0
):
    assert {"Buy_Signal", "Sell_Signal", "open", "high", "low", "VWAP_UpperBand2", "VWAP_LowerBand2"}.issubset(df.columns)

    long_trades, short_trades, open_positions = [], [], []
    position, entry_price, entry_time = None, None, None
    qty = float(trade_size)

    for i in range(1, len(df)):
        t = df.index[i]
        current_open = float(df["open"].iloc[i])
        current_high = float(df["high"].iloc[i])
        current_low = float(df["low"].iloc[i])

        prev_buy = bool(df["Buy_Signal"].iloc[i - 1])
        prev_sell = bool(df["Sell_Signal"].iloc[i - 1])

        if pd.isna(current_open) or pd.isna(current_high) or pd.isna(current_low):
            continue

        # Check stops first if we have a position
        if position is not None:
            stop_loss_triggered = False
            if position == "long":
                stop_price = float(df["VWAP_LowerBand2"].iloc[i])
                if not pd.isna(stop_price) and current_low <= stop_price:
                    gross = (stop_price - entry_price) * qty
                    pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
                    long_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(stop_price, 4), pl, "Loss"])
                    stop_loss_triggered = True
            elif position == "short":
                stop_price = float(df["VWAP_UpperBand2"].iloc[i])
                if not pd.isna(stop_price) and current_high >= stop_price:
                    gross = (entry_price - stop_price) * qty
                    pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
                    short_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(stop_price, 4), pl, "Loss"])
                    stop_loss_triggered = True

            if stop_loss_triggered:
                position = entry_price = entry_time = None
                continue

        # Exit signals at next bar open
        if position == "long" and prev_sell:
            gross = (current_open - entry_price) * qty
            pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
            long_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(current_open, 4), pl, "Win" if pl > 0 else "Loss"])
            position = entry_price = entry_time = None
        elif position == "short" and prev_buy:
            gross = (entry_price - current_open) * qty
            pl = round(gross - 2*commission_per_side - 2*slippage_abs_per_side*qty, 2)
            short_trades.append([symbol, interval, entry_time, round(entry_price, 4), t, round(current_open, 4), pl, "Win" if pl > 0 else "Loss"])
            position = entry_price = entry_time = None

        # Entries at next bar open if flat
        if position is None:
            if prev_buy:
                entry_price, entry_time, position = current_open + slippage_abs_per_side, t, "long"
            elif prev_sell:
                entry_price, entry_time, position = current_open - slippage_abs_per_side, t, "short"

    # If something still open, record it
    if position is not None:
        open_positions.append([symbol, interval, entry_time, round(entry_price, 4), "open", position])

    df_cols = ['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Exit_date_time', 'Exit_price', 'Profit', 'Status']
    open_cols = ['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Status', 'Type']

    long_df = pd.DataFrame(long_trades, columns=df_cols)
    short_df = pd.DataFrame(short_trades, columns=df_cols)
    open_df = pd.DataFrame(open_positions, columns=open_cols)
    return long_df, short_df, open_df

def calculate_success_and_profit(df: pd.DataFrame):
    total_trades = len(df)
    if total_trades == 0:
        return 0, 0, 0, 0.0, 0.0
    wins = (df["Status"] == "Win").sum()
    losses = (df["Status"] == "Loss").sum()
    success_rate = wins / total_trades
    total_profit = float(df["Profit"].sum())
    return total_trades, wins, losses, success_rate, total_profit

def run(
    symbol: str,
    interval: str,
    trade_size: float,
    user_id: str,
    start_date: str,
    end_date: str,
    extended_hours: bool = False
):
    user_dir = os.path.join(DATA_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    logging.info(f"[RUN] {symbol}@{interval} size={trade_size} start={start_date} end={end_date} ext_hours={extended_hours}")

    df = get_df_for_range(symbol, interval, start_date, end_date, extended_hours)

    df_cols = ['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Exit_date_time', 'Exit_price', 'Profit', 'Status']
    open_cols = ['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 'Status', 'Type']

    if df.empty:
        logging.error("No data fetched. No trades will be generated.")
        long_df, short_df, open_df = (
            pd.DataFrame(columns=df_cols),
            pd.DataFrame(columns=df_cols),
            pd.DataFrame(columns=open_cols),
        )
    elif interval != '1d':
        logging.info(f"Data fetched successfully. Processing {len(df)} rows for signals.")
        df = determine_signals(df)
        long_df, short_df, open_df = evaluate_performance(df, interval, trade_size, symbol)
    else:
        logging.warning("VWAP Scalper algorithm is not suitable for '1d' interval. No trades will be generated.")
        long_df, short_df, open_df = (
            pd.DataFrame(columns=df_cols),
            pd.DataFrame(columns=df_cols),
            pd.DataFrame(columns=open_cols),
        )

    long_csv = os.path.join(user_dir, f"{user_id}_{symbol}_{interval}_{start_date}_{end_date}_long_trades.csv")
    short_csv = os.path.join(user_dir, f"{user_id}_{symbol}_{interval}_{start_date}_{end_date}_short_trades.csv")
    open_csv = os.path.join(user_dir, f"{user_id}_{symbol}_{interval}_{start_date}_{end_date}_open_positions.csv")
    summary_csv = os.path.join(user_dir, f"{user_id}_{symbol}_{interval}_{start_date}_{end_date}_summary.csv")

    long_df.to_csv(long_csv, index=False)
    short_df.to_csv(short_csv, index=False)
    open_df.to_csv(open_csv, index=False)
    logging.info("Saved trade logs to CSV.")

    long_summary = calculate_success_and_profit(long_df)
    short_summary = calculate_success_and_profit(short_df)

    summary_rows = [
        [symbol, interval, trade_size, long_summary[0], long_summary[1], long_summary[2], f"{long_summary[3]*100:.2f}%", f"${long_summary[4]:.2f}", "Long"],
        [symbol, interval, trade_size, short_summary[0], short_summary[1], short_summary[2], f"{short_summary[3]*100:.2f}%", f"${short_summary[4]:.2f}", "Short"],
    ]
    summary_df = pd.DataFrame(
        summary_rows,
        columns=['Symbol', 'Interval', 'Trade_size', 'Total_Trades', 'Wins', 'Losses', 'SuccessRate', 'Total_profit', 'Trade_Type']
    )
    summary_df.to_csv(summary_csv, index=False)

    print("\n--- Summary ---")
    print(summary_df.to_string(index=False))
    print("\n--- Open Positions ---")
    print(open_df.to_string(index=False) if not open_df.empty else "None")

def parse_args():
    p = argparse.ArgumentParser(description="VWAP Scalper backtest with stop-loss protection.")
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
    run(
        symbol=args.symbol,
        interval=args.interval,
        trade_size=args.trade_size,
        user_id=args.user_id,
        start_date=args.start_date,
        end_date=args.end_date,
        extended_hours=bool(args.extended_hours)
    )
