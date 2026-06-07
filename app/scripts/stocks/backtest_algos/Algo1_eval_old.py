#!/usr/bin/env python3
# Adaptive VWAP Strategy - Only trades in favorable conditions

import sys, os, argparse, logging, pandas as pd, requests
import numpy as np, pandas_ta as ta
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timedelta
from dotenv import load_dotenv

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
if REPO_ROOT not in sys.path: sys.path.insert(0, REPO_ROOT)
from app.utils.stock.schwab_token import get_valid_access_token

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
ET = pd.Timestamp.now(tz='America/New_York').tz
DATA_DIR = os.getenv("DATA_DIR", "/var/www/stockwicks/data")
SCHWAB_INTERVALS = {
    '1min': ('day', 10, 'minute', 1), 
    '5min': ('day', 10, 'minute', 5), 
    '10min': ('day', 10, 'minute', 10), 
    '15min': ('day', 10, 'minute', 15), 
    '30min': ('day', 10, 'minute', 30)
}
ALLOWED_INTERVALS = set(SCHWAB_INTERVALS.keys())

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
        return df[~df.index.duplicated(keep="first")].sort_index()
    except requests.HTTPError as e:
        logging.error(f"Failed fetch {symbol} {interval}: {e}")
        return pd.DataFrame()

def compute_vwap_and_bands(df: pd.DataFrame, std_dev_mult: float = 1.5):
    """Calculate intraday VWAP with standard deviation bands"""
    df = df.copy()
    
    def calc_vwap_daily(day_df):
        if 'volume' not in day_df.columns or day_df['volume'].sum() == 0:
            return day_df
        
        tp = (day_df['high'] + day_df['low'] + day_df['close']) / 3
        cum_tp_vol = (tp * day_df['volume']).cumsum()
        cum_vol = day_df['volume'].cumsum()
        vwap = cum_tp_vol / cum_vol
        
        std_sq = ((tp - vwap) ** 2) * day_df['volume']
        mean_sq_error = std_sq.cumsum() / cum_vol
        std_dev = np.sqrt(mean_sq_error)
        
        day_df['VWAP'] = vwap
        day_df['VWAP_Upper'] = vwap + std_dev * std_dev_mult
        day_df['VWAP_Lower'] = vwap - std_dev * std_dev_mult
        return day_df
    
    return df.groupby(df.index.normalize(), group_keys=False).apply(calc_vwap_daily)

def detect_market_regime(df: pd.DataFrame):
    """
    Detect market regime using multiple indicators
    Returns: 'bullish', 'bearish', or 'neutral'
    """
    # Trend indicators
    df['EMA20'] = df['close'].ewm(span=20, adjust=False).mean()
    df['EMA50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['EMA200'] = df['close'].ewm(span=200, adjust=False).mean()
    
    # ADX for trend strength
    adx_data = ta.adx(df['high'], df['low'], df['close'], length=14)
    if adx_data is not None and 'ADX_14' in adx_data.columns:
        df['ADX'] = adx_data['ADX_14']
        df['DI+'] = adx_data['DMP_14']
        df['DI-'] = adx_data['DMN_14']
    else:
        df['ADX'] = 20
        df['DI+'] = 0
        df['DI-'] = 0
    
    # Determine regime
    conditions = []
    
    # Strong trend conditions (ADX > 25)
    strong_trend = df['ADX'] > 25
    
    # Bullish: Price > EMAs and EMA20 > EMA50 > EMA200
    bullish_alignment = (df['close'] > df['EMA20']) & (df['EMA20'] > df['EMA50']) & (df['EMA50'] > df['EMA200'])
    strong_bullish = strong_trend & bullish_alignment & (df['DI+'] > df['DI-'])
    
    # Bearish: Price < EMAs and EMA20 < EMA50 < EMA200
    bearish_alignment = (df['close'] < df['EMA20']) & (df['EMA20'] < df['EMA50']) & (df['EMA50'] < df['EMA200'])
    strong_bearish = strong_trend & bearish_alignment & (df['DI-'] > df['DI+'])
    
    # Assign regime
    df['Regime'] = 'neutral'
    df.loc[strong_bullish, 'Regime'] = 'bullish'
    df.loc[strong_bearish, 'Regime'] = 'bearish'
    
    return df

def determine_signals(df: pd.DataFrame, trade_with_trend: bool = True):
    """
    Adaptive VWAP signals based on market regime
    
    In BULLISH regime: Only take LONG mean reversion trades
    In BEARISH regime: Only take SHORT mean reversion trades  
    In NEUTRAL regime: Take both (classic mean reversion)
    """
    df = df.copy()
    
    # Calculate VWAP bands
    df = compute_vwap_and_bands(df, std_dev_mult=1.5)
    
    # ATR for stops
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    
    # Detect market regime
    df = detect_market_regime(df)
    
    # RSI for additional confirmation
    df['RSI'] = ta.rsi(df['close'], length=14)
    
    # Initialize signals
    df['Buy_Signal'] = False
    df['Sell_Signal'] = False
    
    if trade_with_trend:
        # === ADAPTIVE SIGNALS BASED ON REGIME ===
        
        # LONG signals (mean reversion from lower band)
        long_setup = (df['close'] <= df['VWAP_Lower']) & (df['RSI'] < 40)
        
        # Only take longs in bullish or neutral regimes
        df.loc[long_setup & ((df['Regime'] == 'bullish') | (df['Regime'] == 'neutral')), 'Buy_Signal'] = True
        
        # SHORT signals (mean reversion from upper band)
        short_setup = (df['close'] >= df['VWAP_Upper']) & (df['RSI'] > 60)
        
        # Only take shorts in bearish or neutral regimes
        df.loc[short_setup & ((df['Regime'] == 'bearish') | (df['Regime'] == 'neutral')), 'Sell_Signal'] = True
        
    else:
        # Classic mean reversion (no regime filter)
        df.loc[(df['close'] <= df['VWAP_Lower']), 'Buy_Signal'] = True
        df.loc[(df['close'] >= df['VWAP_Upper']), 'Sell_Signal'] = True
    
    return df

def evaluate_performance(
    df: pd.DataFrame,
    interval: str,
    trade_size: float,
    symbol: str,
    atr_stop_multiplier: float = 1.5,
    profit_target_multiplier: float = 2.0,
    eod_close: bool = False,
    use_trailing_stop: bool = True
):
    """Backtest with ATR-based risk management"""
    
    required_cols = {'Buy_Signal', 'Sell_Signal', 'open', 'high', 'low', 'close', 'ATR'}
    assert required_cols.issubset(df.columns), f"Missing columns: {required_cols - set(df.columns)}"
    
    long_trades, short_trades = [], []
    position = None
    entry_price = None
    entry_time = None
    stop_loss = None
    profit_target = None
    trailing_stop = None
    bars_in_trade = 0
    max_bars = 60  # Maximum bars to hold a position
    qty = float(trade_size)
    
    for i in range(1, len(df)):
        current_time = df.index[i]
        current_open = float(df['open'].iloc[i])
        current_high = float(df['high'].iloc[i])
        current_low = float(df['low'].iloc[i])
        current_close = float(df['close'].iloc[i])
        current_atr = float(df['ATR'].iloc[i])
        
        prev_buy_signal = bool(df['Buy_Signal'].iloc[i-1])
        prev_sell_signal = bool(df['Sell_Signal'].iloc[i-1])
        
        # Skip if invalid data
        if pd.isna([current_open, current_high, current_low, current_close, current_atr]).any():
            continue
        
        # === EXIT LOGIC ===
        if position is not None:
            bars_in_trade += 1
            exit_triggered = False
            exit_price = None
            exit_reason = None
            
            # 1. Maximum holding period
            if bars_in_trade >= max_bars:
                exit_price = current_close
                exit_reason = 'Max Hold Time'
                exit_triggered = True
            
            # 2. Stop Loss
            if not exit_triggered:
                if position == 'long' and current_low <= stop_loss:
                    exit_price = stop_loss
                    exit_reason = 'Stop Loss'
                    exit_triggered = True
                elif position == 'short' and current_high >= stop_loss:
                    exit_price = stop_loss
                    exit_reason = 'Stop Loss'
                    exit_triggered = True
            
            # 3. Profit Target
            if not exit_triggered:
                if position == 'long' and current_high >= profit_target:
                    exit_price = profit_target
                    exit_reason = 'Profit Target'
                    exit_triggered = True
                elif position == 'short' and current_low <= profit_target:
                    exit_price = profit_target
                    exit_reason = 'Profit Target'
                    exit_triggered = True
            
            # 4. Trailing Stop
            if not exit_triggered and use_trailing_stop and trailing_stop is not None:
                if position == 'long' and current_low <= trailing_stop:
                    exit_price = trailing_stop
                    exit_reason = 'Trailing Stop'
                    exit_triggered = True
                elif position == 'short' and current_high >= trailing_stop:
                    exit_price = trailing_stop
                    exit_reason = 'Trailing Stop'
                    exit_triggered = True
            
            # 5. Signal Exit (mean reversion complete - price reached opposite band)
            if not exit_triggered:
                if position == 'long' and prev_sell_signal:
                    exit_price = current_open
                    exit_reason = 'Mean Reversion Complete'
                    exit_triggered = True
                elif position == 'short' and prev_buy_signal:
                    exit_price = current_open
                    exit_reason = 'Mean Reversion Complete'
                    exit_triggered = True
            
            # 6. EOD Close
            if not exit_triggered and eod_close:
                is_eod = (i + 1 == len(df)) or (df.index[i].date() != df.index[i+1].date())
                if is_eod:
                    exit_price = current_close
                    exit_reason = 'EOD Close'
                    exit_triggered = True
            
            # Execute exit
            if exit_triggered:
                if position == 'long':
                    pnl = (exit_price - entry_price) * qty
                    status = 'Win' if pnl > 0 else 'Loss'
                    long_trades.append([
                        symbol, interval, entry_time, round(entry_price, 4),
                        current_time, round(exit_price, 4), round(pnl, 2),
                        status, exit_reason
                    ])
                else:  # short
                    pnl = (entry_price - exit_price) * qty
                    status = 'Win' if pnl > 0 else 'Loss'
                    short_trades.append([
                        symbol, interval, entry_time, round(entry_price, 4),
                        current_time, round(exit_price, 4), round(pnl, 2),
                        status, exit_reason
                    ])
                
                # Reset position
                position = None
                entry_price = None
                entry_time = None
                stop_loss = None
                profit_target = None
                trailing_stop = None
                bars_in_trade = 0
            
            # Update trailing stop
            elif use_trailing_stop and position is not None:
                stop_distance = abs(entry_price - stop_loss)
                
                if position == 'long':
                    # Start trailing after price moves 1x risk in our favor
                    if current_high > entry_price + stop_distance:
                        new_trailing = current_high - stop_distance * 0.75
                        if trailing_stop is None or new_trailing > trailing_stop:
                            trailing_stop = new_trailing
                else:  # short
                    if current_low < entry_price - stop_distance:
                        new_trailing = current_low + stop_distance * 0.75
                        if trailing_stop is None or new_trailing < trailing_stop:
                            trailing_stop = new_trailing
        
        # === ENTRY LOGIC (only if no position) ===
        if position is None:
            if prev_buy_signal:
                entry_price = current_open
                entry_time = current_time
                position = 'long'
                
                # Set ATR-based stops
                stop_distance = current_atr * atr_stop_multiplier
                stop_loss = entry_price - stop_distance
                profit_target = entry_price + (stop_distance * profit_target_multiplier)
                trailing_stop = None
                bars_in_trade = 0
            
            elif prev_sell_signal:
                entry_price = current_open
                entry_time = current_time
                position = 'short'
                
                # Set ATR-based stops
                stop_distance = current_atr * atr_stop_multiplier
                stop_loss = entry_price + stop_distance
                profit_target = entry_price - (stop_distance * profit_target_multiplier)
                trailing_stop = None
                bars_in_trade = 0
    
    # Return trade DataFrames
    cols = ['symbol', 'Interval', 'Entry_date_time', 'Entry_price', 
            'Exit_date_time', 'Exit_price', 'Profit', 'Status', 'Exit_Reason']
    
    return pd.DataFrame(long_trades, columns=cols), pd.DataFrame(short_trades, columns=cols)

def calculate_metrics(df: pd.DataFrame):
    """Calculate comprehensive trading metrics"""
    if df.empty:
        return {
            'total': 0, 'wins': 0, 'losses': 0, 'win_rate': 0.0,
            'total_pnl': 0.0, 'avg_pnl': 0.0, 'avg_win': 0.0, 'avg_loss': 0.0,
            'max_win': 0.0, 'max_loss': 0.0, 'profit_factor': 0.0,
            'expectancy': 0.0
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
    
    # Expectancy (average $ per trade)
    expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)
    
    return {
        'total': total, 'wins': wins, 'losses': losses, 'win_rate': win_rate,
        'total_pnl': total_pnl, 'avg_pnl': avg_pnl, 'avg_win': avg_win, 'avg_loss': avg_loss,
        'max_win': max_win, 'max_loss': max_loss, 'profit_factor': profit_factor,
        'expectancy': expectancy
    }

def plot_results(df: pd.DataFrame, long_trades: pd.DataFrame, short_trades: pd.DataFrame,
                symbol: str, interval: str, user_dir: str, user_id: str):
    """Create visualization with regime overlay"""
    try:
        plt.style.use('dark_background')
        fig = plt.figure(figsize=(16, 12))
        gs = fig.add_gridspec(3, 1, height_ratios=[2, 1, 1], hspace=0.3)
        ax1 = fig.add_subplot(gs[0])
        ax2 = fig.add_subplot(gs[1])
        ax3 = fig.add_subplot(gs[2])
        
        # Plot 1: Price, VWAP, and Regime
        ax1.plot(df.index, df['close'], label='Close', color='white', linewidth=1, alpha=0.8)
        ax1.plot(df.index, df['VWAP'], label='VWAP', color='yellow', linewidth=1.5)
        ax1.plot(df.index, df['VWAP_Upper'], label='Upper', color='red', linewidth=1, alpha=0.6, linestyle='--')
        ax1.plot(df.index, df['VWAP_Lower'], label='Lower', color='lime', linewidth=1, alpha=0.6, linestyle='--')
        
        # Shade regime backgrounds
        bullish_mask = df['Regime'] == 'bullish'
        bearish_mask = df['Regime'] == 'bearish'
        
        for i in range(len(df)-1):
            if bullish_mask.iloc[i]:
                ax1.axvspan(df.index[i], df.index[i+1], alpha=0.1, color='green')
            elif bearish_mask.iloc[i]:
                ax1.axvspan(df.index[i], df.index[i+1], alpha=0.1, color='red')
        
        # Plot trades
        for _, trade in long_trades.iterrows():
            color = 'lime' if trade['Profit'] > 0 else 'red'
            ax1.plot([trade['Entry_date_time'], trade['Exit_date_time']], 
                    [trade['Entry_price'], trade['Exit_price']], 
                    color=color, linewidth=2, alpha=0.7)
            ax1.scatter(trade['Entry_date_time'], trade['Entry_price'], 
                       color='lime', marker='^', s=100, zorder=5, edgecolors='white', linewidths=1)
            ax1.scatter(trade['Exit_date_time'], trade['Exit_price'], 
                       color=color, marker='v', s=100, zorder=5, edgecolors='white', linewidths=1)
        
        for _, trade in short_trades.iterrows():
            color = 'orange' if trade['Profit'] > 0 else 'darkred'
            ax1.plot([trade['Entry_date_time'], trade['Exit_date_time']], 
                    [trade['Entry_price'], trade['Exit_price']], 
                    color=color, linewidth=2, alpha=0.7)
            ax1.scatter(trade['Entry_date_time'], trade['Entry_price'], 
                       color='orange', marker='v', s=100, zorder=5, edgecolors='white', linewidths=1)
            ax1.scatter(trade['Exit_date_time'], trade['Exit_price'], 
                       color=color, marker='^', s=100, zorder=5, edgecolors='white', linewidths=1)
        
        ax1.set_title(f'{symbol} {interval} - Adaptive VWAP (Green=Bullish, Red=Bearish)', 
                     fontsize=16, fontweight='bold')
        ax1.set_ylabel('Price ($)', fontsize=12)
        ax1.legend(loc='best')
        ax1.grid(True, alpha=0.3)
        
        # Plot 2: ADX and Regime Strength
        ax2.plot(df.index, df['ADX'], label='ADX', color='cyan', linewidth=1.5)
        ax2.axhline(y=25, color='yellow', linestyle='--', alpha=0.5, label='Trending Threshold')
        ax2.set_ylabel('ADX', fontsize=12)
        ax2.legend(loc='best')
        ax2.grid(True, alpha=0.3)
        ax2.set_ylim(0, 60)
        
        # Plot 3: Cumulative P&L
        all_trades = pd.concat([long_trades, short_trades]).sort_values('Exit_date_time')
        if not all_trades.empty:
            cum_pnl = all_trades['Profit'].cumsum()
            ax3.plot(all_trades['Exit_date_time'], cum_pnl, 
                    color='yellow', linewidth=2, marker='o', markersize=4)
            ax3.fill_between(all_trades['Exit_date_time'], cum_pnl, alpha=0.3, color='yellow')
            
            final_pnl = cum_pnl.iloc[-1]
            ax3.annotate(f'Final P&L: ${final_pnl:.2f}',
                        xy=(all_trades['Exit_date_time'].iloc[-1], final_pnl),
                        xytext=(10, 10), textcoords='offset points',
                        bbox=dict(boxstyle='round', facecolor='green' if final_pnl > 0 else 'red', alpha=0.7),
                        fontweight='bold', fontsize=10)
        
        ax3.set_title('Cumulative P&L', fontsize=14, fontweight='bold')
        ax3.set_ylabel('P&L ($)', fontsize=12)
        ax3.set_xlabel('Time', fontsize=12)
        ax3.grid(True, alpha=0.3)
        ax3.axhline(y=0, color='white', linestyle='--', alpha=0.5)
        
        # Format dates
        for ax in [ax1, ax2, ax3]:
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M'))
            plt.setp(ax.xaxis.get_majorticklabels(), rotation=45)
        
        plt.tight_layout()
        plot_file = os.path.join(user_dir, f"{user_id}_{symbol}_{interval}_adaptive_vwap.png")
        plt.savefig(plot_file, dpi=150, bbox_inches='tight')
        plt.close()
        logging.info(f"Plot saved: {plot_file}")
    except Exception as e:
        logging.error(f"Error creating plot: {e}")

def run(symbol: str, interval: str, trade_size: float, user_id: str,
        atr_stop_mult: float, profit_target_mult: float, 
        trade_with_trend: bool, use_trailing_stop: bool, eod_close: bool):
    """Main execution"""
    
    user_dir = os.path.join(DATA_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    
    # Fetch data
    df = get_data_for_fixed_period(symbol, interval)
    if df.empty or len(df) < 200:
        logging.error(f"Insufficient data: {len(df)} bars")
        return
    
    start_date = df.index[0].strftime('%Y-%m-%d %H:%M')
    end_date = df.index[-1].strftime('%Y-%m-%d %H:%M')
    logging.info(f"Data range: {start_date} to {end_date} ({len(df)} bars)")
    
    # Generate signals
    df = determine_signals(df, trade_with_trend=trade_with_trend)
    
    buy_signals = df['Buy_Signal'].sum()
    sell_signals = df['Sell_Signal'].sum()
    logging.info(f"Generated {buy_signals} buy signals, {sell_signals} sell signals")
    
    # Analyze regime distribution
    regime_counts = df['Regime'].value_counts()
    logging.info(f"Market Regime Distribution: {dict(regime_counts)}")
    
    # Run backtest
    long_df, short_df = evaluate_performance(
        df, interval, trade_size, symbol,
        atr_stop_multiplier=atr_stop_mult,
        profit_target_multiplier=profit_target_mult,
        eod_close=eod_close,
        use_trailing_stop=use_trailing_stop
    )
    
    # Calculate metrics
    long_metrics = calculate_metrics(long_df)
    short_metrics = calculate_metrics(short_df)
    
    # Combined metrics
    all_trades = pd.concat([long_df, short_df], ignore_index=True)
    combined_metrics = calculate_metrics(all_trades)
    
    # Log results
    logging.info("=" * 70)
    logging.info(f"LONG: {long_metrics['total']} trades, WR: {long_metrics['win_rate']*100:.1f}%, "
                f"P&L: ${long_metrics['total_pnl']:.2f}, PF: {long_metrics['profit_factor']:.2f}, "
                f"Expectancy: ${long_metrics['expectancy']:.2f}")
    
    logging.info(f"SHORT: {short_metrics['total']} trades, WR: {short_metrics['win_rate']*100:.1f}%, "
                f"P&L: ${short_metrics['total_pnl']:.2f}, PF: {short_metrics['profit_factor']:.2f}, "
                f"Expectancy: ${short_metrics['expectancy']:.2f}")
    
    logging.info(f"OVERALL: {combined_metrics['total']} trades, WR: {combined_metrics['win_rate']*100:.1f}%, "
                f"P&L: ${combined_metrics['total_pnl']:.2f}, PF: {combined_metrics['profit_factor']:.2f}, "
                f"Expectancy: ${combined_metrics['expectancy']:.2f}")
    logging.info("=" * 70)
    
    # Save results
    summary_file = os.path.join(user_dir, f"{user_id}_{symbol}_{interval}_adaptive_summary.csv")
    all_trades.to_csv(summary_file, index=False)
    logging.info(f"Summary saved: {summary_file}")
    
    # Plot
    if not all_trades.empty:
        plot_results(df, long_df, short_df, symbol, interval, user_dir, user_id)

def parse_args():
    p = argparse.ArgumentParser(description="Adaptive VWAP Strategy with Regime Detection")
    p.add_argument("--symbol", "-s", required=True, help="Stock symbol")
    p.add_argument("--interval", "-i", required=True, choices=sorted(ALLOWED_INTERVALS))
    p.add_argument("--trade-size", "-q", type=float, default=100.0)
    p.add_argument("--user-id", "-u", required=True)
    p.add_argument("--atr-stop", type=float, default=1.5, help="ATR multiplier for stop (default: 1.5)")
    p.add_argument("--profit-target", type=float, default=2.0, help="Profit target as multiple of risk (default: 2.0)")
    p.add_argument("--ignore-trend", action="store_true", help="Ignore market regime (trade all signals)")
    p.add_argument("--no-trailing-stop", action="store_true", help="Disable trailing stop")
    p.add_argument("--eod-close", action="store_true", help="Close all positions at EOD")
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()
    run(
        symbol=args.symbol,
        interval=args.interval,
        trade_size=args.trade_size,
        user_id=args.user_id,
        atr_stop_mult=args.atr_stop,
        profit_target_mult=args.profit_target,
        trade_with_trend=not args.ignore_trend,
        use_trailing_stop=not args.no_trailing_stop,
        eod_close=args.eod_close
    )