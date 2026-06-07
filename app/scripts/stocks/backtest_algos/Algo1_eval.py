#!/usr/bin/env python3
# Algo1_eval.py - SMI Level-Based Strategy

import sys
import os
import argparse
import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import requests

# Add your repo path
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
if REPO_ROOT not in sys.path: 
    sys.path.insert(0, REPO_ROOT)

try:
    from app.utils.stock.schwab_token import get_valid_access_token
    from dotenv import load_dotenv
    load_dotenv()
except ImportError as e:
    print(f"Import error: {e}")
    sys.exit(1)

# Immediate logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()]
)

ET = pd.Timestamp.now(tz='America/New_York').tz
DATA_DIR = os.getenv("DATA_DIR", "/var/www/stockwicks/data")

SCHWAB_INTERVALS = {
    '1min': ('day', 10, 'minute', 1),
    '5min': ('day', 10, 'minute', 5),
    '10min': ('day', 10, 'minute', 10),
    '15min': ('day', 10, 'minute', 15),
    '65min': ('day', 10, 'minute', 65),
}
ALLOWED_INTERVALS = set(SCHWAB_INTERVALS.keys())

def get_data_for_fixed_period(symbol: str, interval: str) -> pd.DataFrame:
    """Get market data from Schwab API"""
    logging.info(f"Fetching data for {symbol} with interval {interval}")
    
    if interval not in SCHWAB_INTERVALS:
        raise ValueError(f"Unsupported interval: {interval}")
    
    periodType, period, frequencyType, frequency = SCHWAB_INTERVALS[interval]
    token = get_valid_access_token()
    
    if not token:
        logging.error("Failed to get Schwab access token")
        return pd.DataFrame()
    
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
        logging.info(f"Making API request to Schwab...")
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        candles = data.get("candles", [])
        
        logging.info(f"API returned {len(candles)} candles")
        
        if not candles:
            logging.warning(f"No candle data returned for {symbol}")
            return pd.DataFrame()
        
        df = pd.DataFrame(candles)
        df["timestamp"] = pd.to_datetime(df["datetime"], unit="ms", utc=True).dt.tz_convert(ET)
        df = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
        
        logging.info(f"DataFrame shape: {df.shape}")
        logging.info(f"Date range: {df.index[0]} to {df.index[-1]}")
        
        return df[~df.index.duplicated(keep="first")].sort_index()
        
    except requests.HTTPError as e:
        logging.error(f"HTTP Error: {e}")
        logging.error(f"Response: {resp.text if 'resp' in locals() else 'No response'}")
        return pd.DataFrame()
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        return pd.DataFrame()

def calculate_smi(df: pd.DataFrame, k_period: int = 10, d_period: int = 3, smooth: int = 3) -> pd.DataFrame:
    """Calculate Stochastic Momentum Index"""
    logging.info(f"Calculating SMI with K={k_period}, D={d_period}, smooth={smooth}")
    
    df = df.copy()
    
    # Calculate highest high and lowest low over k_period
    df['HH'] = df['high'].rolling(window=k_period).max()
    df['LL'] = df['low'].rolling(window=k_period).min()
    
    # Calculate midpoint of range
    df['HL_Range'] = df['HH'] - df['LL']
    df['HL_Mid'] = (df['HH'] + df['LL']) / 2
    
    # Distance from close to midpoint
    df['Close_Mid_Diff'] = df['close'] - df['HL_Mid']
    
    # Smooth the difference
    df['Close_Mid_Diff_Smooth'] = df['Close_Mid_Diff'].ewm(span=d_period, adjust=False).mean()
    
    # Smooth the range
    df['HL_Range_Smooth'] = df['HL_Range'].ewm(span=d_period, adjust=False).mean()
    
    # Calculate SMI (scaled to -100 to +100)
    df['SMI_Fast'] = (df['Close_Mid_Diff_Smooth'] / (df['HL_Range_Smooth'] / 2)) * 100
    
    # Signal line (slow line)
    df['SMI_Slow'] = df['SMI_Fast'].ewm(span=smooth, adjust=False).mean()
    
    # Handle any infinities or NaNs
    df['SMI_Fast'] = df['SMI_Fast'].replace([np.inf, -np.inf], np.nan)
    df['SMI_Slow'] = df['SMI_Slow'].replace([np.inf, -np.inf], np.nan)
    
    # Fill initial NaNs with 0
    df['SMI_Fast'] = df['SMI_Fast'].fillna(0)
    df['SMI_Slow'] = df['SMI_Slow'].fillna(0)
    
    logging.info(f"SMI calculation complete. Sample values - Fast: {df['SMI_Fast'].iloc[-5:].tolist()}")
    
    return df

def determine_signals(df: pd.DataFrame, k_period: int = 10, d_period: int = 3, smooth: int = 3) -> pd.DataFrame:
    """Generate trading signals based on SMI level crossovers"""
    df = calculate_smi(df, k_period=k_period, d_period=d_period, smooth=smooth)
    
    # Define levels
    oversold_level = -65
    overbought_level = 65
    
    # Current position relative to levels
    df['Fast_Above_OS'] = df['SMI_Fast'] > oversold_level
    df['Fast_Below_OB'] = df['SMI_Fast'] < overbought_level
    df['Slow_Above_OS'] = df['SMI_Slow'] > oversold_level
    df['Slow_Below_OB'] = df['SMI_Slow'] < overbought_level
    
    # Both lines crossing above oversold (-65)
    df['Both_Cross_Above_OS'] = (
        (df['Fast_Above_OS'].shift(1) == False) & (df['Fast_Above_OS'] == True) &
        (df['Slow_Above_OS'].shift(1) == False) & (df['Slow_Above_OS'] == True)
    )
    
    # Both lines crossing below overbought (+65)
    df['Both_Cross_Below_OB'] = (
        (df['Fast_Below_OB'].shift(1) == False) & (df['Fast_Below_OB'] == True) &
        (df['Slow_Below_OB'].shift(1) == False) & (df['Slow_Below_OB'] == True)
    )
    
    # BUY/COVER SHORT: Both SMI lines cross ABOVE -65
    df['Buy_Signal'] = df['Both_Cross_Above_OS']
    
    # SELL/SHORT SELL: Both SMI lines cross BELOW +65
    df['Sell_Signal'] = df['Both_Cross_Below_OB']
    
    # Additional signals for information
    df['In_Oversold_Zone'] = (df['SMI_Fast'] < oversold_level) & (df['SMI_Slow'] < oversold_level)
    df['In_Overbought_Zone'] = (df['SMI_Fast'] > overbought_level) & (df['SMI_Slow'] > overbought_level)
    
    buy_signals = df['Buy_Signal'].sum()
    sell_signals = df['Sell_Signal'].sum()
    
    logging.info(f"Generated {buy_signals} buy/cover signals and {sell_signals} sell/short signals")
    logging.info(f"Oversold level: {oversold_level}, Overbought level: {overbought_level}")
    
    return df

def evaluate_performance_level_based(df: pd.DataFrame, interval: str, trade_size: float, symbol: str, 
                                   eod_close: bool = False):
    """
    Execute SMI level-based trades
    Rules:
    - BUY/COVER SHORT when both SMI Fast and Slow cross ABOVE -65
    - SELL/SHORT SELL when both SMI Fast and Slow cross BELOW +65
    """
    logging.info(f"Starting performance evaluation - SMI LEVEL BASED STRATEGY")
    
    long_trades, short_trades = [], []
    position = None
    entry_price = None
    entry_time = None
    qty = float(trade_size)
    bars_in_trade = 0
    max_hold = 200  # Safety max hold
    
    total_bars = len(df)
    logging.info(f"Processing {total_bars} bars")
    
    for i in range(1, len(df)):
        current_time = df.index[i-1]
        current_close = float(df["close"].iloc[i-1])
        
        buy_signal = bool(df["Buy_Signal"].iloc[i-1])
        sell_signal = bool(df["Sell_Signal"].iloc[i-1])
        
        # Get SMI values for logging
        smi_fast = float(df["SMI_Fast"].iloc[i-1])
        smi_slow = float(df["SMI_Slow"].iloc[i-1])
        
        if pd.isna(current_close):
            continue
        
        if position is not None:
            bars_in_trade += 1
        
        # === EXIT LOGIC ===
        # LONG positions exit on SELL signal (cross below +65)
        if position == "long" and sell_signal:
            exit_price = current_close
            pl = (exit_price - entry_price) * qty
            long_trades.append([
                symbol, interval, entry_time, round(entry_price, 4),
                current_time, round(exit_price, 4), round(pl, 2),
                "Win" if pl > 0 else "Loss", "SMI Cross Below +65"
            ])
            logging.info(f"LONG EXIT at {exit_price} (SMI Fast: {smi_fast:.1f}, Slow: {smi_slow:.1f}) - P&L: ${pl:.2f}")
            position = None
            bars_in_trade = 0
        
        # SHORT positions exit on BUY signal (cross above -65)
        elif position == "short" and buy_signal:
            exit_price = current_close
            pl = (entry_price - exit_price) * qty
            short_trades.append([
                symbol, interval, entry_time, round(entry_price, 4),
                current_time, round(exit_price, 4), round(pl, 2),
                "Win" if pl > 0 else "Loss", "SMI Cross Above -65"
            ])
            logging.info(f"SHORT COVER at {exit_price} (SMI Fast: {smi_fast:.1f}, Slow: {smi_slow:.1f}) - P&L: ${pl:.2f}")
            position = None
            bars_in_trade = 0
        
        # Max hold time as safety
        elif position is not None and bars_in_trade >= max_hold:
            exit_price = current_close
            if position == "long":
                pl = (exit_price - entry_price) * qty
                long_trades.append([
                    symbol, interval, entry_time, round(entry_price, 4),
                    current_time, round(exit_price, 4), round(pl, 2),
                    "Win" if pl > 0 else "Loss", "Max Hold"
                ])
                logging.info(f"LONG EXIT at {exit_price} (Max Hold) - P&L: ${pl:.2f}")
            else:
                pl = (entry_price - exit_price) * qty
                short_trades.append([
                    symbol, interval, entry_time, round(entry_price, 4),
                    current_time, round(exit_price, 4), round(pl, 2),
                    "Win" if pl > 0 else "Loss", "Max Hold"
                ])
                logging.info(f"SHORT COVER at {exit_price} (Max Hold) - P&L: ${pl:.2f}")
            position = None
            bars_in_trade = 0
        
        # === EOD CLOSE ===
        is_last_bar_of_day = (i + 1 == len(df)) or (df.index[i].date() != df.index[i+1].date())
        if eod_close and position is not None and is_last_bar_of_day:
            exit_price = current_close
            if position == "long":
                pl = (exit_price - entry_price) * qty
                long_trades.append([
                    symbol, interval, entry_time, round(entry_price, 4),
                    current_time, round(exit_price, 4), round(pl, 2),
                    "Win" if pl > 0 else "Loss", "EOD Close"
                ])
            elif position == "short":
                pl = (entry_price - exit_price) * qty
                short_trades.append([
                    symbol, interval, entry_time, round(entry_price, 4),
                    current_time, round(exit_price, 4), round(pl, 2),
                    "Win" if pl > 0 else "Loss", "EOD Close"
                ])
            position = None
            bars_in_trade = 0
        
        # === ENTRY LOGIC ===
        if position is None:
            # BUY/COVER SHORT: Both SMI lines cross ABOVE -65
            if buy_signal:
                entry_price = current_close
                entry_time = current_time
                position = "long"
                bars_in_trade = 0
                logging.info(f"🟢 LONG ENTRY at {entry_price} (SMI Fast: {smi_fast:.1f}, Slow: {smi_slow:.1f} - Crossed Above -65)")
                    
            # SELL/SHORT SELL: Both SMI lines cross BELOW +65
            elif sell_signal:
                entry_price = current_close
                entry_time = current_time
                position = "short"
                bars_in_trade = 0
                logging.info(f"🔴 SHORT ENTRY at {entry_price} (SMI Fast: {smi_fast:.1f}, Slow: {smi_slow:.1f} - Crossed Below +65)")
    
    cols = ['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 
            'Exit_date_time', 'Exit_price', 'Profit', 'Status', 'Exit_Reason']
    
    logging.info(f"Completed evaluation: {len(long_trades)} long trades, {len(short_trades)} short trades")
    
    return pd.DataFrame(long_trades, columns=cols), pd.DataFrame(short_trades, columns=cols)

def calculate_metrics(df: pd.DataFrame):
    """Calculate performance metrics"""
    if df.empty:
        logging.info("No trades to calculate metrics")
        return {
            'total': 0, 'wins': 0, 'losses': 0, 'win_rate': 0.0,
            'total_pnl': 0.0, 'avg_pnl': 0.0, 'avg_win': 0.0, 'avg_loss': 0.0,
            'max_win': 0.0, 'max_loss': 0.0, 'profit_factor': 0.0, 'expectancy': 0.0
        }
    
    total = len(df)
    wins = (df['Status'] == 'Win').sum()
    losses = total - wins
    win_rate = wins / total if total > 0 else 0.0
    
    total_pnl = float(df['Profit'].sum())
    avg_pnl = float(df['Profit'].mean())
    
    win_pnl = df[df['Status'] == 'Win']['Profit']
    loss_pnl = df[df['Status'] == 'Loss']['Profit']
    
    avg_win = float(win_pnl.mean()) if len(win_pnl) > 0 else 0.0
    avg_loss = float(loss_pnl.mean()) if len(loss_pnl) > 0 else 0.0
    max_win = float(df['Profit'].max())
    max_loss = float(df['Profit'].min())
    
    total_wins_sum = float(win_pnl.sum()) if len(win_pnl) > 0 else 0.0
    total_losses_sum = abs(float(loss_pnl.sum())) if len(loss_pnl) > 0 else 0.0
    profit_factor = total_wins_sum / total_losses_sum if total_losses_sum > 0 else (float('inf') if total_wins_sum > 0 else 0.0)
    
    expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)
    
    logging.info(f"Metrics calculated: {total} trades, ${total_pnl:.2f} total PnL")
    
    return {
        'total': total, 'wins': wins, 'losses': losses, 'win_rate': win_rate,
        'total_pnl': total_pnl, 'avg_pnl': avg_pnl, 'avg_win': avg_win, 'avg_loss': avg_loss,
        'max_win': max_win, 'max_loss': max_loss, 'profit_factor': profit_factor,
        'expectancy': expectancy
    }

def run(symbol: str, interval: str, trade_size: float, user_id: str, eod_close: bool,
        k_period: int, d_period: int, smooth: int, use_stop_loss: bool, stop_loss_pct: float):
    """Main execution function"""
    logging.info("=" * 65)
    logging.info(f"STARTING SMI LEVEL-BASED STRATEGY")
    logging.info(f"Symbol: {symbol}, Interval: {interval}, Trade Size: ${trade_size}")
    logging.info(f"Strategy: Buy/Cover when SMI crosses ABOVE -65")
    logging.info(f"Strategy: Sell/Short when SMI crosses BELOW +65")
    logging.info("=" * 65)
    
    user_dir = os.path.join(DATA_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    logging.info(f"Output directory: {user_dir}")
    
    # Step 1: Get data
    df = get_data_for_fixed_period(symbol, interval)
    
    if df.empty:
        logging.error("❌ No data available - exiting")
        return
    
    start_date = df.index[0].strftime('%Y-%m-%d %H:%M')
    end_date = df.index[-1].strftime('%Y-%m-%d %H:%M')
    logging.info(f"✅ Data range: {start_date} to {end_date} ({len(df)} bars)")
    logging.info(f"✅ Price range: ${df['close'].min():.2f} - ${df['close'].max():.2f}")
    
    # Step 2: Generate signals
    df = determine_signals(df, k_period=k_period, d_period=d_period, smooth=smooth)
    
    buy_signals = df['Buy_Signal'].sum()
    sell_signals = df['Sell_Signal'].sum()
    logging.info(f"✅ Buy/Cover Signals (Cross > -65): {buy_signals}")
    logging.info(f"✅ Sell/Short Signals (Cross < +65): {sell_signals}")
    
    # Show SMI range info
    smi_fast_min = df['SMI_Fast'].min()
    smi_fast_max = df['SMI_Fast'].max()
    smi_slow_min = df['SMI_Slow'].min()
    smi_slow_max = df['SMI_Slow'].max()
    logging.info(f"📊 SMI Fast Range: {smi_fast_min:.1f} to {smi_fast_max:.1f}")
    logging.info(f"📊 SMI Slow Range: {smi_slow_min:.1f} to {smi_slow_max:.1f}")
    
    # Step 3: Evaluate performance - USE LEVEL BASED STRATEGY
    long_df, short_df = evaluate_performance_level_based(df, interval, trade_size, symbol, eod_close)
    
    # Step 4: Calculate metrics
    long_metrics = calculate_metrics(long_df)
    short_metrics = calculate_metrics(short_df)
    
    all_trades = pd.concat([long_df, short_df], ignore_index=True)
    combined_metrics = calculate_metrics(all_trades)
    
    # Step 5: Display results
    logging.info("=" * 85)
    logging.info("📊 SMI LEVEL-BASED STRATEGY RESULTS")
    logging.info("=" * 85)
    logging.info(f"LONG (Buy/Cross > -65): {long_metrics['total']} trades, WR: {long_metrics['win_rate']*100:.1f}%, "
                f"P&L: ${long_metrics['total_pnl']:.2f}, PF: {long_metrics['profit_factor']:.2f}")
    
    logging.info(f"SHORT (Sell/Cross < +65): {short_metrics['total']} trades, WR: {short_metrics['win_rate']*100:.1f}%, "
                f"P&L: ${short_metrics['total_pnl']:.2f}, PF: {short_metrics['profit_factor']:.2f}")
    
    logging.info(f"OVERALL: {combined_metrics['total']} trades, WR: {combined_metrics['win_rate']*100:.1f}%, "
                f"P&L: ${combined_metrics['total_pnl']:.2f}, PF: {combined_metrics['profit_factor']:.2f}")
    logging.info("=" * 85)
    
    if not all_trades.empty:
        exit_reasons = all_trades['Exit_Reason'].value_counts()
        logging.info(f"📈 Exit Reasons: {dict(exit_reasons)}")
        
        # Calculate additional stats
        if not all_trades.empty:
            avg_trade_length = (all_trades['Exit_date_time'] - all_trades['Entry_date_time']).mean()
            logging.info(f"⏱️  Average Trade Duration: {avg_trade_length}")
        
        # Save results
        summary_file = os.path.join(user_dir, f"{user_id}_{symbol}_{interval}_smi_level_based.csv")
        all_trades.to_csv(summary_file, index=False)
        logging.info(f"💾 Summary saved: {summary_file}")
    else:
        logging.warning("⚠️  No trades were executed")
    
    logging.info("✅ Backtest completed successfully")

def parse_args():
    """Parse command line arguments"""
    p = argparse.ArgumentParser(description="Algo1: SMI Level-Based Strategy")
    p.add_argument("--symbol", "-s", required=True, help="Stock symbol (e.g., QQQ)")
    p.add_argument("--interval", "-i", required=True, choices=sorted(ALLOWED_INTERVALS), help="Time interval")
    p.add_argument("--trade-size", "-q", type=float, default=100.0, help="Trade size in dollars")
    p.add_argument("--user-id", "-u", required=True, help="User ID for output files")
    p.add_argument("--eod-close", action="store_true", help="Close positions at end of day")
    p.add_argument("--k-period", type=int, default=10, help="SMI K period (default: 10)")
    p.add_argument("--d-period", type=int, default=3, help="SMI D period (default: 3)")
    p.add_argument("--smooth", type=int, default=3, help="SMI smoothing (default: 3)")
    p.add_argument("--no-stop-loss", action="store_true", help="Disable stop loss")
    p.add_argument("--stop-loss-pct", type=float, default=2.0, help="Stop loss percentage (default: 2.0)")
    return p.parse_args()

if __name__ == "__main__":
    try:
        args = parse_args()
        logging.info(f"Command line arguments parsed: {vars(args)}")
        run(
            symbol=args.symbol,
            interval=args.interval,
            trade_size=args.trade_size,
            user_id=args.user_id,
            eod_close=args.eod_close,
            k_period=args.k_period,
            d_period=args.d_period,
            smooth=args.smooth,
            use_stop_loss=not args.no_stop_loss,
            stop_loss_pct=args.stop_loss_pct
        )
    except Exception as e:
        logging.error(f"❌ Fatal error: {e}", exc_info=True)
        sys.exit(1)