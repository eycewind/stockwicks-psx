#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/stocks/AI_master_evaluator.py
# how to use
# python3 /var/www/stockwicks/app/scripts/stocks/AI_master_evaluator.py \
#   --symbols-file /var/www/stockwicks/app/scripts/stocks/qqq_list.csv \
#   --symbols-column Symbol \
#   100 116 --eod-close

# python3 /var/www/stockwicks/app/scripts/stocks/AI_master_evaluator.py \
#   NVDA 100 116 --eod-close

import sys
import os
import subprocess
import pandas as pd
import logging
import concurrent.futures
import argparse
from typing import List, Optional, Dict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s (MASTER_EVAL) %(message)s")

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../"))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DATA_DIR = os.getenv("DATA_DIR", "/var/www/stockwicks/data")

ALGO_SCRIPTS = {
    "Algo1": os.path.join(REPO_ROOT, "app/scripts/stocks/backtest_algos/Algo1_eval.py"),
    "Algo2": os.path.join(REPO_ROOT, "app/scripts/stocks/backtest_algos/Algo2_eval.py"),
    "Algo3": os.path.join(REPO_ROOT, "app/scripts/stocks/backtest_algos/Algo3_eval.py"),
}
INTERVALS_TO_TEST = ["1min", "5min", "10min", "15min", "30min", "1d"]

def _num(x: pd.Series, pct: bool = False) -> pd.Series:
    """Convert column (possibly with $ or %) to float; missing→0."""
    s = x.astype(str)
    if pct:
        s = s.str.replace('%', '', regex=False)
    s = s.str.replace(r'[\$,]', '', regex=True)
    return pd.to_numeric(s, errors="coerce").fillna(0.0)

def run_single_backtest(
    algo_name: str,
    script_path: str,
    interval: str,
    symbol: str,
    trade_size: float,
    user_id: str,
    eod_close: bool
) -> Optional[Dict]:
    """
    Runs an individual backtest script as a subprocess, conditionally adding --eod-close,
    then reads the per-interval summary to compute Total_Profit and Success_Rate.
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

        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=REPO_ROOT
        )
        if result.stderr:
            logging.debug(result.stderr.strip())

        logging.info(f"Finished test: {algo_name} on {interval} for {symbol}")

        summary_file = os.path.join(DATA_DIR, user_id, f"{user_id}_{symbol}_{interval}_summary.csv")
        if not os.path.exists(summary_file):
            logging.warning(f"Summary file not found for {algo_name} on {interval} despite success.")
            return None

        df = pd.read_csv(summary_file).fillna(0)

        total_profit = _num(df.get('Total_profit', pd.Series(dtype=object))).sum()
        # Accept alternate spelling too, just in case:
        if total_profit == 0.0 and 'Total_Profit' in df.columns:
            total_profit = _num(df['Total_Profit']).sum()

        success_rate = _num(df.get('SuccessRate', pd.Series(dtype=object)), pct=True).mean()
        if success_rate == 0.0 and 'Success_Rate' in df.columns:
            success_rate = _num(df['Success_Rate'], pct=True).mean()

        return {
            "Symbol": symbol,
            "Algorithm": algo_name,
            "Interval": interval,
            "Total_Profit": float(total_profit),
            "Success_Rate": float(success_rate),
        }

    except subprocess.CalledProcessError as e:
        logging.error(f"Failed AI backtest for {algo_name} on {interval} [{symbol}]. STDERR: {e.stderr}")
        return None
    except Exception as e:
        logging.error(f"Unexpected error running backtest for {algo_name} on {interval} [{symbol}]: {e}")
        return None

def load_symbols_from_file(path: str, column: str = "Symbol") -> List[str]:
    """
    Load symbols from a CSV (expects a column name, default 'Symbol') or from a plain txt (one per line).
    Uppercases, trims, de-duplicates preserving order.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Symbols file not found: {path}")

    ext = os.path.splitext(path)[1].lower()
    symbols: List[str] = []
    if ext in (".csv", ".tsv"):
        df = pd.read_csv(path)
        if column not in df.columns:
            raise ValueError(f"Column '{column}' not found in {path}. Columns: {list(df.columns)}")
        vals = df[column].astype(str).str.strip().str.upper().tolist()
        symbols = [s for s in vals if s and s != "NAN"]
    else:
        # treat as plain text; one symbol per line
        with open(path, "r") as f:
            vals = [line.strip().upper() for line in f.readlines()]
        symbols = [s for s in vals if s and s != "NAN"]

    # de-duplicate preserving order
    seen = set()
    uniq = []
    for s in symbols:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq

def evaluate_one_symbol(symbol: str, trade_size: float, user_id: str, eod_close: bool) -> pd.DataFrame:
    """
    Evaluate all algos x intervals for a single symbol (internal parallelism),
    return a DataFrame with rows for that symbol.
    """
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(run_single_backtest, name, path, iv, symbol, trade_size, user_id, eod_close)
            for name, path in ALGO_SCRIPTS.items()
            for iv in INTERVALS_TO_TEST
        ]
        for fut in concurrent.futures.as_completed(futures):
            res = fut.result()
            if res:
                rows.append(res)

    if not rows:
        logging.error(f"No successful backtests for {symbol}. Writing empty placeholder files.")
        user_dir = os.path.join(DATA_DIR, user_id)
        os.makedirs(user_dir, exist_ok=True)
        # Maintain legacy per-symbol outputs:
        pd.DataFrame([{
            "Algorithm": "N/A", "Interval": "N/A",
            "Total_Profit": 0.0, "Success_Rate": 0.0
        }]).to_csv(os.path.join(user_dir, f"{user_id}_{symbol}_AI_recommend.csv"), index=False)
        pd.DataFrame().to_csv(os.path.join(user_dir, f"{user_id}_{symbol}_AI_evaluation_full.csv"), index=False)
        return pd.DataFrame()  # empty for combined

    df = pd.DataFrame(rows).fillna(0)

    # Save per-symbol full report (legacy behavior)
    user_dir = os.path.join(DATA_DIR, user_id)
    os.makedirs(user_dir, exist_ok=True)
    full_path = os.path.join(user_dir, f"{user_id}_{symbol}_AI_evaluation_full.csv")
    df.sort_values(by=["Success_Rate", "Total_Profit"], ascending=False).to_csv(full_path, index=False)

    # Save per-symbol best recommendation (legacy behavior)
    best_row = df.sort_values(by=["Success_Rate", "Total_Profit"], ascending=False).iloc[0]
    pd.DataFrame([best_row.to_dict()]).to_csv(
        os.path.join(user_dir, f"{user_id}_{symbol}_AI_recommend.csv"),
        index=False
    )

    return df

def main():
    parser = argparse.ArgumentParser(description="Master AI Evaluator for StockWicks Bots")
    # Mode A: single symbol (legacy)
    parser.add_argument("symbol", nargs="?", help="Single stock symbol to evaluate (e.g., AAPL)")
    # Mode B: multi-symbol from file
    parser.add_argument("--symbols-file", help="Path to CSV/TXT of symbols to evaluate")
    parser.add_argument("--symbols-column", default="Symbol", help="Column name in CSV (default: Symbol)")

    parser.add_argument("trade_size", type=float, help="Size of each trade for the simulation")
    parser.add_argument("user_id", help="User ID for storing data")

    parser.add_argument("--eod-close", action="store_true", help="Enable end-of-day closing for all backtests")

    # Combined outputs for multi-symbol mode
    parser.add_argument("--combined-full", default="ALL_symbols_AI_evaluation_full.csv",
                        help="Filename for combined full report (all rows)")
    parser.add_argument("--combined-best", default="ALL_symbols_AI_recommend.csv",
                        help="Filename for combined best-per-symbol report")

    args = parser.parse_args()

    user_data_dir = os.path.join(DATA_DIR, args.user_id)
    os.makedirs(user_data_dir, exist_ok=True)

    # Determine run mode
    symbols: List[str] = []
    if args.symbols_file:
        symbols = load_symbols_from_file(args.symbols_file, column=args.symbols_column)
        if not symbols:
            logging.error(f"No symbols found in {args.symbols_file}")
            sys.exit(1)
        logging.info(f"Loaded {len(symbols)} symbols from file: {', '.join(symbols[:10])}{'...' if len(symbols) > 10 else ''}")
    else:
        # Legacy single-symbol mode requires positional 'symbol'
        if not args.symbol:
            logging.error("Provide either a single SYMBOL or --symbols-file.")
            sys.exit(1)
        symbols = [args.symbol.strip().upper()]

    # Evaluate one symbol at a time (sequential at the symbol level)
    combined_full_rows: List[pd.DataFrame] = []
    combined_best_rows: List[dict] = []

    for sym in symbols:
        logging.info(f"=== Evaluating {sym} ===")
        df_sym = evaluate_one_symbol(sym, args.trade_size, args.user_id, args.eod_close)

        if df_sym is None or df_sym.empty:
            continue

        # Ensure numeric
        df_sym["Total_Profit"] = pd.to_numeric(df_sym["Total_Profit"], errors="coerce").fillna(0.0)
        df_sym["Success_Rate"] = pd.to_numeric(df_sym["Success_Rate"], errors="coerce").fillna(0.0)

        # Append to combined full (already contains Symbol column)
        combined_full_rows.append(df_sym)

        # Best row for this symbol
        best_row = df_sym.sort_values(by=["Success_Rate", "Total_Profit"], ascending=False).iloc[0]
        combined_best_rows.append(best_row.to_dict())

    # Write combined outputs (only if multi-symbol or even for one symbol—it’s fine)
    if combined_full_rows:
        combined_full = pd.concat(combined_full_rows, ignore_index=True)
        combined_full.sort_values(by=["Symbol", "Success_Rate", "Total_Profit"], ascending=[True, False, False], inplace=True)
        combined_full.to_csv(os.path.join(user_data_dir, args.combined_full), index=False)
        logging.info(f"✅ Combined full report saved to {os.path.join(user_data_dir, args.combined_full)}")

    if combined_best_rows:
        combined_best_df = pd.DataFrame(combined_best_rows)
        combined_best_df.sort_values(by=["Success_Rate", "Total_Profit"], ascending=False, inplace=True)
        combined_best_df.to_csv(os.path.join(user_data_dir, args.combined_best), index=False)
        logging.info(f"✅ Combined best-per-symbol report saved to {os.path.join(user_data_dir, args.combined_best)}")

    logging.info("✅ All done.")

if __name__ == "__main__":
    main()
