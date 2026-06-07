#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/stocks/AI_master_evaluator.py

import sys
import os
import subprocess
import pandas as pd
import logging
import concurrent.futures
import argparse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s (MASTER_EVAL) %(message)s")
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../"))
if REPO_ROOT not in sys.path: sys.path.insert(0, REPO_ROOT)
DATA_DIR = os.getenv("DATA_DIR", "/var/www/stockwicks/data")

ALGO_SCRIPTS = {
    "Algo1": os.path.join(REPO_ROOT, "app/scripts/stocks/backtest_algos/Algo1_eval.py"),
    "Algo2": os.path.join(REPO_ROOT, "app/scripts/stocks/backtest_algos/Algo2_eval.py"),
    "Algo3": os.path.join(REPO_ROOT, "app/scripts/stocks/backtest_algos/Algo3_eval.py"),
}
INTERVALS_TO_TEST = ["1min", "5min", "10min", "15min", "30min", "1d"]

def run_single_backtest(algo_name, script_path, interval, symbol, trade_size, user_id, eod_close):
    """
    Runs an individual backtest script as a subprocess, conditionally adding the --eod-close flag.
    """
    try:
        logging.info(f"Starting test: {algo_name} on {interval} for {symbol} | EOD Close: {eod_close}")
        
        cmd = [
            "python3", script_path,
            "--symbol", symbol,
            "--interval", interval,
            "--trade-size", str(trade_size),
            "--user-id", user_id,
        ]
        
        if eod_close:
            cmd.append("--eod-close")
        
        # Execute the command. The 'cwd' argument is crucial for background processes.
        # Although we are using subprocess.run here, this structure is robust.
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=180, cwd=REPO_ROOT)
        logging.info(f"Finished test: {algo_name} on {interval} for {symbol}")
        
        summary_file = os.path.join(DATA_DIR, user_id, f"{user_id}_{symbol}_{interval}_summary.csv")
        if not os.path.exists(summary_file):
            logging.warning(f"Summary file not found for {algo_name} on {interval} despite success.")
            return None

        df = pd.read_csv(summary_file).fillna(0)
        
        if 'Total_profit' not in df.columns or df['Total_profit'].empty:
            total_profit = 0.0
        else:
            total_profit = df['Total_profit'].astype(str).str.replace(r'[\$,]', '', regex=True).astype(float).sum()

        if 'SuccessRate' not in df.columns or df['SuccessRate'].empty:
            success_rate = 0.0
        else:
            success_rate = df['SuccessRate'].astype(str).str.replace('%', '').astype(float).mean()
        
        return {"Algorithm": algo_name, "Interval": interval, "Total_Profit": total_profit, "Success_Rate": success_rate}
        
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed AI backtest for {algo_name} on {interval}. STDERR: {e.stderr}")
        return None
    except Exception as e:
        logging.error(f"An unexpected error occurred running backtest for {algo_name} on {interval}: {e}")
        return None

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Master AI Evaluator for Stockwick Bots")
    parser.add_argument("symbol", help="Stock symbol to evaluate (e.g., AAPL)")
    parser.add_argument("trade_size", type=float, help="Size of each trade for the simulation")
    parser.add_argument("user_id", help="User ID for storing data")
    parser.add_argument("--eod-close", action="store_true", help="Enable end-of-day closing for all backtests")
    
    args = parser.parse_args()

    user_data_dir = os.path.join(DATA_DIR, args.user_id)
    os.makedirs(user_data_dir, exist_ok=True)
    all_results = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(run_single_backtest, name, path, iv, args.symbol, args.trade_size, args.user_id, args.eod_close)
            for name, path in ALGO_SCRIPTS.items() for iv in INTERVALS_TO_TEST
        ]
        
        for future in concurrent.futures.as_completed(futures):
            if result := future.result():
                all_results.append(result)

    if not all_results:
        logging.error("No successful backtests completed. Cannot generate recommendation.")
        output_path = os.path.join(user_data_dir, f"{args.user_id}_{args.symbol}_AI_recommend.csv")
        pd.DataFrame([{"Algorithm": "N/A", "Interval": "N/A", "Total_Profit": 0.0, "Success_Rate": 0.0}]).to_csv(output_path, index=False)
        full_report_path = os.path.join(user_data_dir, f"{args.user_id}_{args.symbol}_AI_evaluation_full.csv")
        pd.DataFrame().to_csv(full_report_path, index=False)
        sys.exit(1)

    results_df = pd.DataFrame(all_results)
    results_df.fillna(0, inplace=True)
    
    # --- THIS IS THE CORRECTED SORTING LOGIC ---
    # Sort by Success Rate first, then by Profit as a tie-breaker.
    best_result = results_df.sort_values(by=["Success_Rate", "Total_Profit"], ascending=False).iloc[0]
    
    # Also update the sorting for the full report to match the new logic.
    full_report_df = results_df.sort_values(by=["Success_Rate", "Total_Profit"], ascending=False)
    # ---------------------------------------------
    
    # Save the full report
    full_report_path = os.path.join(user_data_dir, f"{args.user_id}_{args.symbol}_AI_evaluation_full.csv")
    full_report_df.to_csv(full_report_path, index=False)
    
    # Save the single best recommendation
    output_path = os.path.join(user_data_dir, f"{args.user_id}_{args.symbol}_AI_recommend.csv")
    pd.DataFrame([best_result.to_dict()]).to_csv(output_path, index=False)
    
    logging.info("✅ Full report and final AI recommendation saved.")