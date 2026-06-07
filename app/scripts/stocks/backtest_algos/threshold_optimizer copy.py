#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/stock_algos/threshold_optimizer.py

import os
import sys
import subprocess
import pandas as pd
import itertools
from datetime import datetime
import argparse
import logging

# Add repo path for imports
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s [ThresholdOpt] %(message)s')
logger = logging.getLogger("ThresholdOptimizer")

def generate_threshold_combinations():
    """Generate different threshold combinations to test"""
    
    # Define ranges for thresholds
    long_entry_range = [0.55, 0.60, 0.65, 0.70]
    long_exit_range = [0.50, 0.55, 0.60]
    short_entry_range = [0.30, 0.35, 0.40, 0.45]
    short_exit_range = [0.35, 0.40, 0.45, 0.50]
    
    combinations = []
    
    for long_ent, long_ex, short_ent, short_ex in itertools.product(
        long_entry_range, long_exit_range, short_entry_range, short_exit_range
    ):
        # Ensure exit thresholds are lower than entry thresholds
        if long_ex < long_ent and short_ex > short_ent:
            combinations.append({
                'long_threshold': long_ent,
                'long_exit_threshold': long_ex,
                'short_threshold': short_ent,
                'short_exit_threshold': short_ex
            })
    
    logger.info(f"🧪 Generated {len(combinations)} threshold combinations")
    return combinations

def run_optimization_test(symbols_file, interval, threshold_combo, output_dir):
    """Run batch test with specific threshold combination"""
    
    combo_id = f"L{threshold_combo['long_threshold']:.2f}-LE{threshold_combo['long_exit_threshold']:.2f}-S{threshold_combo['short_threshold']:.2f}-SE{threshold_combo['short_exit_threshold']:.2f}"
    
    logger.info(f"🔬 Testing combination: {combo_id}")
    
    # Run batch tester with these thresholds
    cmd = [
        "python3", "/var/www/stockwicks/app/scripts/stock_algos/batch_symbol_tester.py",
        "--symbols-file", symbols_file,
        "--interval", interval,
        "--output", f"{output_dir}/results_{combo_id}.csv",
        "--long-threshold", str(threshold_combo['long_threshold']),
        "--long-exit-threshold", str(threshold_combo['long_exit_threshold']),
        "--short-threshold", str(threshold_combo['short_threshold']),
        "--short-exit-threshold", str(threshold_combo['short_exit_threshold']),
        "--min-volume-multiplier", "1.2",
        "--min-prob-advantage", "0.15",
        "--fixed-stop-loss", "200",
        "--daily-loss-limit", "500"
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)  # 30min timeout
        
        if result.returncode == 0:
            return analyze_combo_results(f"{output_dir}/results_{combo_id}.csv", threshold_combo, combo_id)
        else:
            logger.error(f"❌ Combination {combo_id} failed")
            return None
            
    except subprocess.TimeoutExpired:
        logger.error(f"⏰ Combination {combo_id} timed out")
        return None

def analyze_combo_results(results_file, thresholds, combo_id):
    """Analyze results for a threshold combination"""
    
    try:
        df = pd.read_csv(results_file)
        
        if df.empty:
            return None
        
        # Calculate aggregate metrics
        total_profit = df['Total_Profit($)'].sum()
        avg_success_rate = df['Success_Rate(%)'].mean()
        avg_win_rate = df['Win_Rate(%)'].mean()
        total_trades = df['Total_Trades'].sum()
        avg_profit_per_trade = df['Profit_Per_Trade($)'].mean()
        avg_profit_per_dollar = df['Profit_Per_Dollar(%)'].mean()
        
        # Count profitable symbols
        profitable_symbols = len(df[df['Total_Profit($)'] > 0])
        total_symbols = len(df)
        
        return {
            'combo_id': combo_id,
            'long_entry': thresholds['long_threshold'],
            'long_exit': thresholds['long_exit_threshold'],
            'short_entry': thresholds['short_threshold'],
            'short_exit': thresholds['short_exit_threshold'],
            'total_profit': total_profit,
            'avg_success_rate': avg_success_rate,
            'avg_win_rate': avg_win_rate,
            'total_trades': total_trades,
            'avg_profit_per_trade': avg_profit_per_trade,
            'avg_profit_per_dollar': avg_profit_per_dollar,
            'profitable_symbols': profitable_symbols,
            'total_symbols': total_symbols,
            'profitability_ratio': profitable_symbols / total_symbols if total_symbols > 0 else 0
        }
        
    except Exception as e:
        logger.error(f"❌ Error analyzing {results_file}: {e}")
        return None

def main():
    parser = argparse.ArgumentParser(description='Threshold Optimizer for Algo Trading')
    parser.add_argument('--symbols-file', required=True, help='File containing list of symbols')
    parser.add_argument('--interval', required=True, help='Trading interval (e.g., 5min)')
    parser.add_argument('--output-dir', default='threshold_optimization', help='Output directory for results')
    parser.add_argument('--max-combinations', type=int, default=20, help='Maximum number of combinations to test')
    
    args = parser.parse_args()
    
    # Create output directory
    os.makedirs(args.output_dir, exist_ok=True)
    
    # Generate threshold combinations
    all_combinations = generate_threshold_combinations()
    
    # Limit number of combinations to test
    combinations_to_test = all_combinations[:args.max_combinations]
    
    logger.info(f"🚀 Starting optimization with {len(combinations_to_test)} combinations")
    
    results = []
    
    for i, combo in enumerate(combinations_to_test, 1):
        logger.info(f"📊 Testing combination {i}/{len(combinations_to_test)}")
        
        result = run_optimization_test(args.symbols_file, args.interval, combo, args.output_dir)
        if result:
            results.append(result)
    
    # Generate optimization report
    if results:
        df_results = pd.DataFrame(results)
        
        # Sort by different metrics to find best combinations
        best_total_profit = df_results.nlargest(5, 'total_profit')
        best_avg_profit_per_dollar = df_results.nlargest(5, 'avg_profit_per_dollar')
        best_profitability_ratio = df_results.nlargest(5, 'profitability_ratio')
        best_avg_win_rate = df_results.nlargest(5, 'avg_win_rate')
        
        # Save detailed results
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        df_results.to_csv(f"{args.output_dir}/optimization_summary_{timestamp}.csv", index=False)
        
        # Print recommendations
        print("\n" + "="*100)
        print("🎯 THRESHOLD OPTIMIZATION RESULTS")
        print("="*100)
        
        print("\n🏆 BEST BY TOTAL PROFIT:")
        print(best_total_profit[['combo_id', 'total_profit', 'avg_profit_per_dollar', 'profitability_ratio']].to_string(index=False))
        
        print("\n💰 BEST BY PROFIT PER DOLLAR (% RETURN):")
        print(best_avg_profit_per_dollar[['combo_id', 'avg_profit_per_dollar', 'total_profit', 'profitability_ratio']].to_string(index=False))
        
        print("\n📈 BEST BY PROFITABILITY RATIO:")
        print(best_profitability_ratio[['combo_id', 'profitability_ratio', 'total_profit', 'avg_profit_per_dollar']].to_string(index=False))
        
        print("\n🎯 BEST BY WIN RATE:")
        print(best_avg_win_rate[['combo_id', 'avg_win_rate', 'total_profit', 'profitability_ratio']].to_string(index=False))
        
        print(f"\n💾 Full results saved to: {args.output_dir}/optimization_summary_{timestamp}.csv")
        
    else:
        logger.error("❌ No successful optimization runs")

if __name__ == "__main__":
    main()