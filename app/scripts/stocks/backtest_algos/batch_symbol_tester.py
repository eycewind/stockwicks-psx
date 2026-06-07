#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/stock_algos/batch_symbol_tester.py
"""
Batch Symbol Tester - Updated for new AlgoMM_eval CLI parameters with last close price
"""

import os
import sys
import subprocess
import pandas as pd
from datetime import datetime
import argparse
import logging

# Add repo path for imports
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app.scripts.stock_algos.base_wiring import StockBaseRunner

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s [BatchTester] %(message)s')
logger = logging.getLogger("BatchSymbolTester")

# Default parameters - UPDATED for new thresholds
DEFAULT_PARAMS = {
    'quantity': 100,
    'user_id': 116,
    'builder_days': 60,
    'long_threshold': 0.60, 
    'short_threshold': 0.40,
    'long_exit_threshold': 0.55,  # NEW
    'short_exit_threshold': 0.45,  # NEW
    'min_volume_multiplier': 1.2,
    'min_prob_advantage': 0.15,
    'fixed_stop_loss': 200,
    'trailing_stop_activation': 0.8,
    'trailing_stop_distance': 1.0,
    'daily_loss_limit_usd': 500.0,
    'take_profit_percent': 1.5,
    'cooldown_sec': 60,
    'eod_close': True,
    'use_raw_prob': False  # NEW
}

def get_last_close_price(symbol):
    """Get the last close price for a symbol"""
    try:
        runner = StockBaseRunner()
        df_raw = runner.fetch_source_bars(symbol)
        if df_raw is not None and not df_raw.empty:
            last_close = float(df_raw['close'].iloc[-1])
            logger.info(f"📊 {symbol} last close: ${last_close:.2f}")
            return last_close
    except Exception as e:
        logger.error(f"❌ Error fetching last close for {symbol}: {e}")
    return 0.0

def read_symbols_from_file(filename):
    """Read symbols from a text file"""
    symbols = []
    try:
        with open(filename, 'r') as f:
            for line in f:
                symbol = line.strip()
                if symbol and not symbol.startswith('#'):
                    symbols.append(symbol)
        logger.info(f"📖 Read {len(symbols)} symbols from {filename}")
        return symbols
    except Exception as e:
        logger.error(f"❌ Error reading symbol file: {e}")
        return []

def run_single_backtest(symbol, interval, params):
    """Run backtest for a single symbol"""
    
    # Get last close price first
    last_close = get_last_close_price(symbol)
    
    backtest_cmd = [
        "python3", "/var/www/stockwicks/app/scripts/stocks/backtest_algos/AlgoMM_eval.py",
        "-s", symbol,
        "-i", interval,
        "-q", str(params['quantity']),
        "-u", str(params['user_id']),
        "--auto-train",
        "--builder-days", str(params['builder_days']),
        "--long-threshold", str(params['long_threshold']),
        "--short-threshold", str(params['short_threshold']),
        "--long-exit-threshold", str(params['long_exit_threshold']),  # NEW
        "--short-exit-threshold", str(params['short_exit_threshold']),  # NEW
        "--min-volume-multiplier", str(params['min_volume_multiplier']),
        "--min-prob-advantage", str(params['min_prob_advantage']),
        "--fixed-stop-loss", str(params['fixed_stop_loss']),
        "--trailing-stop-activation", str(params['trailing_stop_activation']),
        "--trailing-stop-distance", str(params['trailing_stop_distance']),
        "--daily-loss-limit-usd", str(params['daily_loss_limit_usd']),
        "--take-profit-percent", str(params['take_profit_percent']),
        "--cooldown-sec", str(params['cooldown_sec']),
    ]
    
    # Add boolean flags
    if params.get('eod_close', True):
        backtest_cmd.append("--eod-close")
    
    if params.get('use_raw_prob', False):
        backtest_cmd.append("--use-raw-prob")
    
    logger.info(f"🔬 Testing {symbol} {interval}...")
    
    try:
        result = subprocess.run(backtest_cmd, capture_output=True, text=True, timeout=300)
        
        if result.returncode == 0:
            backtest_result = parse_output_fixed(result.stdout, symbol, interval)
            if backtest_result:
                backtest_result['last_close'] = last_close
            return backtest_result
        else:
            logger.error(f"❌ Backtest failed for {symbol}")
            logger.error(f"Error output: {result.stderr}")
            return None
            
    except subprocess.TimeoutExpired:
        logger.error(f"⏰ Backtest timeout for {symbol}")
        return None
    except Exception as e:
        logger.error(f"❌ Error running backtest for {symbol}: {e}")
        return None

def clean_currency_value_fixed(value_str):
    """Fixed currency parsing with better error handling"""
    try:
        if value_str is None:
            return 0.0

        # Basic cleanup
        clean = str(value_str).strip()

        # Normalize unicode minus to ASCII
        clean = clean.replace('−', '-')

        # Remove dollar sign and commas
        clean = clean.replace('$', '').replace(',', '').strip()

        # Handle parentheses negative format: (123.45) or ($123.45)
        if clean.startswith('(') and clean.endswith(')'):
            clean = '-' + clean[1:-1].strip()

        result = float(clean)
        return result
        
    except (ValueError, TypeError) as e:
        logger.error(f"❌ Currency conversion failed for '{value_str}': {e}")
        return 0.0

def parse_output_fixed(output, symbol, interval):
    """Fixed parsing with correct column indices for AlgoMM_eval output"""
    
    results = {
        'symbol': symbol,
        'interval': interval,
        'total_trades': 0,
        'total_wins': 0, 
        'success_rate': 0.0,
        'total_profit': 0.0,
        'largest_win': 0.0,
        'largest_loss': 0.0,
        'last_close': 0.0  # Will be populated later
    }
    
    lines = output.split('\n')

    # Optional: capture header to be more robust if you change columns later
    header_map = None
    for raw in lines:
        line = raw.strip()

        # Detect header line
        if line.startswith("Symbol ") and "Total_Trades" in line and "Total_profit" in line:
            header_cols = line.split()
            header_map = {name: idx for idx, name in enumerate(header_cols)}
            logger.info(f"🧩 Detected header: {header_map}")
            continue

        # Look for the Overall results line for this symbol
        if symbol in line and 'Overall' in line:
            logger.info(f"📊 Found Overall line for {symbol}")
            parts = line.split()
            logger.info(f"🔍 Tokens: {parts}")

            try:
                if header_map:
                    # Use header-based indices when available
                    idx_trades  = header_map.get("Total_Trades", 3)
                    idx_wins    = header_map.get("Wins", 4)
                    idx_srate   = header_map.get("SuccessRate", 6)
                    idx_profit  = header_map.get("Total_profit", 7)
                    idx_lwin    = header_map.get("Largest_Win", 9)
                    idx_lloss   = header_map.get("Largest_Loss", 10)
                else:
                    # Fallback: fixed indices based on current AlgoMM_eval format
                    idx_trades, idx_wins = 3, 4
                    idx_srate, idx_profit = 6, 7
                    idx_lwin, idx_lloss = 9, 10

                # Total trades & wins
                results['total_trades'] = int(parts[idx_trades])
                results['total_wins'] = int(parts[idx_wins])

                # Success rate (strip %)
                success_str = parts[idx_srate].replace('%', '')
                results['success_rate'] = float(success_str)

                # Total profit
                results['total_profit'] = clean_currency_value_fixed(parts[idx_profit])

                # Largest win/loss
                if len(parts) > idx_lwin:
                    results['largest_win'] = clean_currency_value_fixed(parts[idx_lwin])
                if len(parts) > idx_lloss:
                    results['largest_loss'] = clean_currency_value_fixed(parts[idx_lloss])

                logger.info(
                    f"✅ {symbol}: {results['total_trades']} trades, "
                    f"${results['total_profit']:.2f} profit, "
                    f"{results['success_rate']:.2f}% success rate"
                )
                return results

            except (ValueError, IndexError) as e:
                logger.error(f"❌ Parse error for {symbol}: {e}")
                # Keep looping in case some weird extra line matched

    logger.warning(f"⚠️  No Overall results found for {symbol}")
    return results

def generate_summary_report(results, output_file):
    """Generate a comprehensive summary report"""
    
    # Filter out symbols with no trades
    valid_results = [r for r in results if r and r['total_trades'] > 0]
    
    if not valid_results:
        logger.error("❌ No symbols had any trades")
        return
    
    # Create summary DataFrame
    summary_data = []
    
    for result in valid_results:
        profit_per_trade = result['total_profit'] / max(1, result['total_trades'])
        win_rate = (result['total_wins'] / result['total_trades']) * 100
        
        summary_data.append({
            'Symbol': result['symbol'],
            'Interval': result['interval'],
            'Last_Close($)': round(result['last_close'], 2),
            'Total_Trades': result['total_trades'],
            'Total_Wins': result['total_wins'],
            'Success_Rate(%)': round(result['success_rate'], 2),
            'Win_Rate(%)': round(win_rate, 2),
            'Total_Profit($)': round(result['total_profit'], 2),
            'Profit_Per_Trade($)': round(profit_per_trade, 2),
            'Largest_Win($)': round(result['largest_win'], 2),
            'Largest_Loss($)': round(result['largest_loss'], 2),
            'Profit_Per_Dollar(%)': round((result['total_profit'] / (result['last_close'] * 100)) * 100, 2) if result['last_close'] > 0 else 0
        })
    
    df = pd.DataFrame(summary_data)
    
    # Print console summary
    print("\n" + "="*120)
    print("BATCH SYMBOL TESTING SUMMARY REPORT")
    print("="*120)
    print(df.to_string(index=False))
    
    # Print overall statistics
    print("\n" + "="*80)
    print("OVERALL STATISTICS")
    print("="*80)
    print(f"Total Symbols Tested: {len(results)}")
    print(f"Symbols With Trades: {len(valid_results)}")
    print(f"Average Success Rate: {df['Success_Rate(%)'].mean():.2f}%")
    print(f"Average Win Rate: {df['Win_Rate(%)'].mean():.2f}%")
    print(f"Total Profit: ${df['Total_Profit($)'].sum():.2f}")
    print(f"Most Profitable: {df.loc[df['Total_Profit($)'].idxmax(), 'Symbol']} (${df['Total_Profit($)'].max():.2f})")
    print(f"Highest Win Rate: {df.loc[df['Win_Rate(%)'].idxmax(), 'Symbol']} ({df['Win_Rate(%)'].max():.2f}%)")
    print(f"Most Active: {df.loc[df['Total_Trades'].idxmax(), 'Symbol']} ({df['Total_Trades'].max()} trades)")
    print(f"Best Profit/Trade: {df.loc[df['Profit_Per_Trade($)'].idxmax(), 'Symbol']} (${df['Profit_Per_Trade($)'].max():.2f})")
    print(f"Best Return %: {df.loc[df['Profit_Per_Dollar(%)'].idxmax(), 'Symbol']} ({df['Profit_Per_Dollar(%)'].max():.2f}%)")
    
    # Save to CSV
    if output_file:
        df.to_csv(output_file, index=False)
        logger.info(f"💾 Summary saved to: {output_file}")
    
    return df

def main():
    parser = argparse.ArgumentParser(description='Batch Symbol Tester - Updated for AlgoMM_eval v4')
    parser.add_argument('--symbols-file', required=True, help='File containing list of symbols')
    parser.add_argument('--interval', required=True, help='Trading interval (e.g., 5min, 1min)')
    parser.add_argument('--output', help='Output CSV file for results')
    
    # NEW parameters for separate exit thresholds
    parser.add_argument('--long-threshold', type=float, default=0.60, help='Long entry probability threshold')
    parser.add_argument('--short-threshold', type=float, default=0.40, help='Short entry probability threshold')
    parser.add_argument('--long-exit-threshold', type=float, default=0.55, help='Long exit probability threshold')
    parser.add_argument('--short-exit-threshold', type=float, default=0.45, help='Short exit probability threshold')
    
    # Additional parameters
    parser.add_argument('--min-volume-multiplier', type=float, default=1.2, help='Minimum volume multiplier')
    parser.add_argument('--min-prob-advantage', type=float, default=0.15, help='Minimum probability advantage')
    parser.add_argument('--fixed-stop-loss', type=float, default=200.0, help='Fixed stop loss in USD')
    parser.add_argument('--daily-loss-limit', type=float, default=500.0, help='Daily loss limit in USD')
    parser.add_argument('--use-raw-prob', action='store_true', help='Use raw probabilities instead of smoothed')
    parser.add_argument('--no-eod-close', action='store_true', help='Disable end-of-day closing')
    
    args = parser.parse_args()
    
    # Update parameters with command line values
    params = DEFAULT_PARAMS.copy()
    params.update({
        'long_threshold': args.long_threshold,
        'short_threshold': args.short_threshold,
        'long_exit_threshold': args.long_exit_threshold,
        'short_exit_threshold': args.short_exit_threshold,
        'min_volume_multiplier': args.min_volume_multiplier,
        'min_prob_advantage': args.min_prob_advantage,
        'fixed_stop_loss': args.fixed_stop_loss,
        'daily_loss_limit_usd': args.daily_loss_limit,
        'use_raw_prob': args.use_raw_prob,
        'eod_close': not args.no_eod_close
    })
    
    logger.info(f"⚙️  Using parameters: Long Entry={params['long_threshold']}, Exit={params['long_exit_threshold']}")
    logger.info(f"⚙️  Using parameters: Short Entry={params['short_threshold']}, Exit={params['short_exit_threshold']}")
    
    # Read symbols
    symbols = read_symbols_from_file(args.symbols_file)
    if not symbols:
        logger.error("❌ No symbols to test")
        return
    
    # Run backtests
    results = []
    
    for symbol in symbols:
        result = run_single_backtest(symbol, args.interval, params)
        if result:
            results.append(result)
    
    # Generate report
    if results:
        output_file = args.output or f"batch_test_results_{args.interval}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        generate_summary_report(results, output_file)
        
        symbols_with_trades = sum(1 for r in results if r['total_trades'] > 0)
        print(f"\n🎯 Testing Complete: {symbols_with_trades}/{len(symbols)} symbols had trades")
    else:
        logger.error("❌ No successful backtests to report")

if __name__ == "__main__":
    main()