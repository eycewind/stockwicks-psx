#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/stocks/AI_master_evaluator_v2.py
#
# Multi-symbol master evaluator (CSV or single symbol) with parallel runs,
# EOD close toggle, and aggregate leaderboards.
#
# Inspired by/compatible with your existing AI_master_evaluator.py.  (v2)

import sys, os, argparse, logging, subprocess
import pandas as pd
import concurrent.futures

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s (MASTER_V2) %(message)s")

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../"))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DATA_DIR = os.getenv("DATA_DIR", "/var/www/stockwicks/data")

ALGO_SCRIPTS = {
    "Algo1": os.path.join(REPO_ROOT, "app/scripts/stocks/backtest_algos/Algo1_eval.py"),
    "Algo2": os.path.join(REPO_ROOT, "app/scripts/stocks/backtest_algos/Algo2_eval.py"),
    "Algo3": os.path.join(REPO_ROOT, "app/scripts/stocks/backtest_algos/Algo3_eval.py"),
    "AlgoMM": os.path.join(REPO_ROOT, "app/scripts/stocks/backtest_algos/AlgoMM_eval.py"),  # ← NEW
}

DEFAULT_INTERVALS = ["1min", "5min", "10min", "15min", "30min", "1d"]

def run_single_backtest(algo_name, script_path, interval, symbol, trade_size, user_id, eod_close):
    """Run a single algo/interval/symbol backtest and read its summary CSV."""
    try:
        logging.info(f"▶ {symbol} | {algo_name} | {interval} | EOD={eod_close}")
        cmd = [
            "python3", script_path,
            "--symbol", symbol,
            "--interval", interval,
            "--trade-size", str(trade_size),
            "--user-id", user_id,
        ]
        if eod_close:
            cmd.append("--eod-close")

        # Important: cwd=REPO_ROOT to keep paths consistent with your eval scripts
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=180, cwd=REPO_ROOT)

        # Per your evaluators, the summary is saved here:
        summary_file = os.path.join(DATA_DIR, user_id, f"{user_id}_{symbol}_{interval}_summary.csv")
        if not os.path.exists(summary_file):
            logging.warning(f"⚠ No summary produced for {symbol} {algo_name} {interval}")
            return None

        df = pd.read_csv(summary_file).fillna(0)

        # Total_Profit can be a string like "$123.45"
        if "Total_profit" in df.columns:
            prof = df["Total_profit"]
        elif "Total_Profit" in df.columns:
            prof = df["Total_Profit"]
        else:
            prof = pd.Series([0.0])

        total_profit = (
            prof.astype(str)
                .str.replace(r"[\$,]", "", regex=True)
                .astype(float)
                .sum()
        )

        # SuccessRate may be a percent string like "86.10%"
        if "SuccessRate" in df.columns:
            sr = (
                df["SuccessRate"]
                    .astype(str).str.replace("%", "", regex=False)
                    .astype(float).mean()
            )
        else:
            sr = 0.0

        return {
            "Symbol": symbol,
            "Algorithm": algo_name,
            "Interval": interval,
            "Total_Profit": total_profit,
            "Success_Rate": sr,
        }

    except subprocess.CalledProcessError as e:
        logging.error(f"✖ Backtest failed: {symbol} {algo_name} {interval}\nSTDERR:\n{e.stderr}")
        return None
    except Exception as e:
        logging.error(f"✖ Unexpected error: {symbol} {algo_name} {interval}: {e}")
        return None


def load_symbols(args) -> list[str]:
    """Support either --symbol (repeatable) or --symbols-file + --symbols-column."""
    if args.symbol:
        return list(dict.fromkeys(args.symbol))  # de-dupe, preserve order
    if args.symbols_file and args.symbols_column:
        df = pd.read_csv(args.symbols_file)
        if args.symbols_column not in df.columns:
            raise ValueError(f"Column '{args.symbols_column}' not found in {args.symbols_file}")
        syms = df[args.symbols_column].dropna().astype(str).str.strip()
        syms = syms[syms != ""].tolist()
        return list(dict.fromkeys(syms))
    raise ValueError("Provide either --symbol ... or both --symbols-file and --symbols-column.")


def main():
    p = argparse.ArgumentParser(description="Master AI Evaluator (multi-symbol v2)")
    # Your desired CLI style:
    p.add_argument("--symbols-file", help="CSV with a column of symbols")
    p.add_argument("--symbols-column", help="Column name in the CSV that contains symbols")
    p.add_argument("--symbol", action="append", help="Test a single symbol (repeatable)")

    # Positional like your existing script: trade_size user_id
    p.add_argument("trade_size", type=float, help="Per-trade quantity/size")
    p.add_argument("user_id", help="User ID for output folders")

    p.add_argument("--intervals", nargs="+", default=DEFAULT_INTERVALS, help="Intervals to test")
    p.add_argument("--algos", nargs="+", default=list(ALGO_SCRIPTS.keys()), choices=list(ALGO_SCRIPTS.keys()))
    p.add_argument("--eod-close", action="store_true", help="Force end-of-day flat for intraday backtests")
    p.add_argument("--max-workers", type=int, default=8)

    args = p.parse_args()

    symbols = load_symbols(args)
    if not symbols:
        logging.error("No symbols to evaluate.")
        sys.exit(1)

    # Ensure user dir exists
    user_dir = os.path.join(DATA_DIR, args.user_id)
    os.makedirs(user_dir, exist_ok=True)

    # Parallel submit
    all_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as ex:
        futures = []
        for sym in symbols:
            for algo in args.algos:
                script = ALGO_SCRIPTS[algo]
                for iv in args.intervals:
                    futures.append(ex.submit(
                        run_single_backtest, algo, script, iv, sym, args.trade_size, args.user_id, args.eod_close
                    ))
        for fut in concurrent.futures.as_completed(futures):
            res = fut.result()
            if res:
                all_results.append(res)

    if not all_results:
        logging.error("No successful backtests completed.")
        sys.exit(2)

    # DataFrames
    full_df = pd.DataFrame(all_results).fillna(0)

    # Save a per-symbol report (sorted by SR then Profit)
    for sym in symbols:
        sym_df = full_df[full_df["Symbol"] == sym].copy()
        if sym_df.empty:  # skip symbols that failed completely
            continue
        sym_df = sym_df.sort_values(by=["Success_Rate", "Total_Profit"], ascending=False)
        out_sym = os.path.join(user_dir, f"{args.user_id}_{sym}_AI_evaluation_full.csv")
        sym_df.to_csv(out_sym, index=False)

        # Best single row for that symbol:
        best_row = sym_df.iloc[0:1]
        best_out = os.path.join(user_dir, f"{args.user_id}_{sym}_AI_recommend.csv")
        best_row.to_csv(best_out, index=False)

    # Aggregate leaderboards across ALL symbols
    # 1) Overall leaderboard
    overall = (
        full_df.groupby(["Algorithm", "Interval"], as_index=False)
               .agg(Total_Profit=("Total_Profit", "sum"),
                    Success_Rate=("Success_Rate", "mean"),
                    Symbols_Test=("Symbol", "nunique"))
               .sort_values(by=["Success_Rate", "Total_Profit"], ascending=False)
    )
    overall_out = os.path.join(user_dir, f"{args.user_id}_ALL_AI_leaderboard.csv")
    overall.to_csv(overall_out, index=False)

    # 2) Per-interval leaderboard
    per_iv = (
        full_df.groupby(["Interval", "Algorithm"], as_index=False)
               .agg(Total_Profit=("Total_Profit", "sum"),
                    Success_Rate=("Success_Rate", "mean"),
                    Symbols_Test=("Symbol", "nunique"))
               .sort_values(by=["Interval", "Success_Rate", "Total_Profit"], ascending=[True, False, False])
    )
    per_iv_out = os.path.join(user_dir, f"{args.user_id}_BY_INTERVAL_leaderboard.csv")
    per_iv.to_csv(per_iv_out, index=False)

    # 3) Per-symbol best pick snapshot
    picks = []
    for sym in symbols:
        sym_df = full_df[full_df["Symbol"] == sym]
        if sym_df.empty: continue
        picks.append(sym_df.sort_values(by=["Success_Rate", "Total_Profit"], ascending=False).iloc[0])
    picks_df = pd.DataFrame(picks)
    picks_out = os.path.join(user_dir, f"{args.user_id}_PER_SYMBOL_best.csv")
    picks_df.to_csv(picks_out, index=False)

    logging.info("✅ Saved:")
    logging.info(f"  - Per-symbol full reports/recommendations in {user_dir}")
    logging.info(f"  - Overall leaderboard:   {overall_out}")
    logging.info(f"  - By-interval leaderboard: {per_iv_out}")
    logging.info(f"  - Per-symbol best picks: {picks_out}")

if __name__ == "__main__":
    main()
