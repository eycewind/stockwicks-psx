#!/usr/bin/env python3
import subprocess
import pandas as pd
import sys
from typing import List, Dict

TEST_CONFIGS = [
    {"name": "OLD (Symmetric 0.50)", "exit_long": 0.50, "exit_short": 0.50},
    {"name": "Conservative (0.45/0.55)", "exit_long": 0.45, "exit_short": 0.55},
    {"name": "Moderate (0.40/0.60)", "exit_long": 0.40, "exit_short": 0.60},
    {"name": "Balanced (0.35/0.65)", "exit_long": 0.35, "exit_short": 0.65},
    {"name": "Aggressive (0.30/0.70)", "exit_long": 0.30, "exit_short": 0.70},
]

SYMBOL = "TSLA"
INTERVAL = "1min"
TRADE_SIZE = 100.0
USER_ID = 1
BUILDER_DAYS = 25
K_FORWARD = 3
FIXED_STOP_LOSS = 300.0


def print_usage():
    print("""
Usage: python AlgoMM_threshold_optimizer.py [SYMBOL] [INTERVAL]

Examples:
  python AlgoMM_threshold_optimizer.py                    # Test TSLA 1min (default)
  python AlgoMM_threshold_optimizer.py AAPL               # Test AAPL 1min
  python AlgoMM_threshold_optimizer.py SPY 5min           # Test SPY 5min
  python AlgoMM_threshold_optimizer.py NVDA 15min         # Test NVDA 15min
    """)


def run_backtest(config, symbol, interval, user_id):
    cmd = [
        "python",
        "/var/www/stockwicks/app/scripts/stocks/backtest_algos/AlgoMM_eval.py",
        "-s", symbol,
        "-i", interval,
        "-q", str(TRADE_SIZE),
        "-u", str(user_id),
        "--builder-days", str(BUILDER_DAYS),
        "--k-forward", str(K_FORWARD),
        "--long-threshold", "0.60",
        "--short-threshold", "0.40",
        "--exit-long-threshold", str(config["exit_long"]),
        "--exit-short-threshold", str(config["exit_short"]),
        "--fixed-stop-loss", str(FIXED_STOP_LOSS),
        "--save-trades",
    ]
    
    print(f"\nTesting: {config['name']}")
    print(f"Exit Long: {config['exit_long']:.2f}, Exit Short: {config['exit_short']:.2f}")
    print("Running backtest...")
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"FAILED: {result.stderr[:200]}")
        return None
    
    csv_path = f"/var/www/stockwicks/data/{user_id}/{user_id}_{symbol}_{interval}_summary.csv"
    
    try:
        df = pd.read_csv(csv_path)
        overall = df[df['Trade_Type'] == 'Overall'].iloc[0]
        
        dollar_sign = "$"
        percent_sign = "%"
        
        profit_str = str(overall['Total_profit']).replace(dollar_sign, "")
        total_profit = float(profit_str)
        
        wr_str = str(overall['SuccessRate']).replace(percent_sign, "")
        win_rate = float(wr_str) / 100
        
        largest_win_str = str(overall['Largest_Win']).replace(dollar_sign, "")
        largest_win = float(largest_win_str)
        
        largest_loss_str = str(overall['Largest_Loss']).replace(dollar_sign, "")
        largest_loss = float(largest_loss_str)
        
        result_dict = {
            "config": config["name"],
            "exit_long": config["exit_long"],
            "exit_short": config["exit_short"],
            "total_trades": int(overall['Total_Trades']),
            "wins": int(overall['Wins']),
            "losses": int(overall['Losses']),
            "win_rate": win_rate,
            "total_profit": total_profit,
            "largest_win": largest_win,
            "largest_loss": largest_loss,
        }
        
        print(f"OK - Trades: {result_dict['total_trades']}, "
              f"P&L: ${result_dict['total_profit']:.2f}, "
              f"WR: {result_dict['win_rate']*100:.1f}%")
        
        return result_dict
        
    except Exception as e:
        print(f"Error parsing: {e}")
        return None


def main():
    symbol = SYMBOL
    interval = INTERVAL
    
    if len(sys.argv) > 1:
        if sys.argv[1] in ["-h", "--help", "help"]:
            print_usage()
            return
        symbol = sys.argv[1].upper()
    
    if len(sys.argv) > 2:
        interval = sys.argv[2].lower()
    
    results = []
    
    print("\n" + "="*80)
    print("ALGOMM THRESHOLD OPTIMIZER")
    print(f"Symbol: {symbol}, Interval: {interval}")
    print(f"Entry: Long@0.60, Short@0.40")
    print(f"Testing {len(TEST_CONFIGS)} configurations...")
    print("="*80)
    
    for config in TEST_CONFIGS:
        result = run_backtest(config, symbol, interval, USER_ID)
        if result:
            results.append(result)
    
    if not results:
        print("\nNo results collected!")
        return
    
    print("\n" + "="*80)
    print("RESULTS")
    print("="*80)
    print(f"{'Config':<30} {'Trades':<8} {'WR':<8} {'P&L':<12} {'Avg/Trade':<12}")
    print("-"*80)
    
    for r in results:
        avg = r["total_profit"] / r["total_trades"] if r["total_trades"] > 0 else 0
        print(
            f"{r['config']:<30} "
            f"{r['total_trades']:<8} "
            f"{r['win_rate']*100:>6.1f}% "
            f"${r['total_profit']:>10.2f} "
            f"${avg:>10.2f}"
        )
    
    best_pnl = max(results, key=lambda x: x["total_profit"])
    best_ratio = max(results, key=lambda x: x["total_profit"] / max(1, x["total_trades"]))
    
    print("-"*80)
    print(f"\n[BEST P&L] {best_pnl['config']}")
    print(f"   Exit: Long={best_pnl['exit_long']:.2f}, Short={best_pnl['exit_short']:.2f}")
    print(f"   P&L: ${best_pnl['total_profit']:.2f}")
    print(f"   Win Rate: {best_pnl['win_rate']*100:.1f}%")
    print(f"   Trades: {best_pnl['total_trades']}")
    
    print(f"\n[BEST RATIO] {best_ratio['config']}")
    avg_ratio = best_ratio['total_profit'] / max(1, best_ratio['total_trades'])
    print(f"   Exit: Long={best_ratio['exit_long']:.2f}, Short={best_ratio['exit_short']:.2f}")
    print(f"   Avg/Trade: ${avg_ratio:.2f}")
    
    print("\n" + "="*80)
    print("RECOMMENDATION:")
    print("="*80)
    print(f"--exit-long-threshold {best_pnl['exit_long']:.2f}")
    print(f"--exit-short-threshold {best_pnl['exit_short']:.2f}")
    print("="*80 + "\n")


if __name__ == "__main__":
    main()