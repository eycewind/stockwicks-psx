#!/usr/bin/env python3
"""
AlgoMM Parameter Sweep Backtester

Usage example (from your venv):

  python3 AlgoMM_param_sweep.py \
      -s MU \
      -i 5min \
      -q 100 \
      -u 116

What it does:
- For a grid of:
    * long_threshold
    * short_threshold
    * min_prob_advantage
    * min_volume_multiplier
- Calls AlgoMM_eval.py with:
    * --auto-train
    * --eod-close
    * NO fixed stop loss / NO take-profit / NO trailing stop
- After each run, parses the *_run.csv file and reconstructs trades
  based purely on position changes (flat → long/short → flat).
- Computes:
    * Total profit
    * Max drawdown
    * Win rate
    * Profit factor
    * Trades per day
    * Num trades
- Writes:
    * summary CSV: param_sweep_<SYMBOL>_<INTERVAL>_summary.csv
    * trades CSV:  param_sweep_<SYMBOL>_<INTERVAL>_trades.csv
"""

import os
import sys
import argparse
import subprocess
from datetime import datetime

import numpy as np
import pandas as pd


# -----------------------------------------------------------------------------
# Trade extraction & metrics (uses *_run.csv like 116_MU_5min_run.csv)
# -----------------------------------------------------------------------------


def extract_trades_from_run(run_df: pd.DataFrame) -> pd.DataFrame:
    """
    Reconstruct trades from AlgoMM_eval _run.csv.

    Logic:
    - A trade starts when position changes from 'flat' to 'long' or 'short'.
    - A trade ends when position returns to 'flat'.
    - PnL:
        * long:  (exit_price - entry_price) * qty
        * short: (entry_price - exit_price) * qty

    This guarantees **sequential non-overlapping trades**, even if
    earlier summary scripts split them differently.
    """
    if run_df.empty:
        return pd.DataFrame(columns=[
            "side",
            "entry_time",
            "exit_time",
            "entry_price",
            "exit_price",
            "qty",
            "pnl",
            "exit_action",
        ])

    df = run_df.sort_values("timestamp").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    trades = []
    current = {"side": "flat"}

    for _, row in df.iterrows():
        pos = row["position"]
        ts = row["timestamp"]
        price = row["close"]
        qty = row["quantity"]
        action = row["action"]

        # Entry: flat → long/short
        if current["side"] == "flat" and pos in ("long", "short"):
            current = {
                "side": pos,
                "entry_time": ts,
                "entry_price": float(price),
                "qty": float(qty),
            }

        # Exit: long/short → flat
        elif current["side"] in ("long", "short") and pos == "flat":
            side = current["side"]
            entry_price = current["entry_price"]
            entry_time = current["entry_time"]
            qty_entry = current["qty"]
            exit_price = float(price)

            if side == "long":
                pnl = (exit_price - entry_price) * qty_entry
            else:
                pnl = (entry_price - exit_price) * qty_entry

            trades.append(
                {
                    "side": side,
                    "entry_time": entry_time,
                    "exit_time": ts,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "qty": qty_entry,
                    "pnl": float(pnl),
                    "exit_action": action,
                }
            )

            current = {"side": "flat"}

    trades_df = pd.DataFrame(trades)
    if not trades_df.empty:
        trades_df = trades_df.sort_values("entry_time").reset_index(drop=True)
    return trades_df


def compute_metrics_from_trades(trades: pd.DataFrame) -> dict:
    """
    Compute summary metrics from a trades DataFrame:
    - total_profit
    - max_drawdown
    - win_rate
    - profit_factor
    - trades_per_day
    - num_trades
    """
    if trades.empty:
        return {
            "total_profit": 0.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
            "profit_factor": None,
            "trades_per_day": 0.0,
            "num_trades": 0,
        }

    trades = trades.sort_values("entry_time").reset_index(drop=True)
    pnl = trades["pnl"].values

    total_profit = float(pnl.sum())

    # Equity curve and max drawdown
    equity = pnl.cumsum()
    running_max = np.maximum.accumulate(equity)
    drawdowns = running_max - equity
    max_dd = float(drawdowns.max()) if len(drawdowns) > 0 else 0.0

    # Win rate
    num_trades = len(pnl)
    wins = int((pnl > 0).sum())
    win_rate = float(wins / num_trades) if num_trades > 0 else 0.0

    # Profit factor
    gross_profit = pnl[pnl > 0].sum()
    gross_loss = pnl[pnl < 0].sum()
    if gross_loss < 0:
        profit_factor = float(gross_profit / abs(gross_loss))
    else:
        profit_factor = None

    # Trades per day
    days = trades["entry_time"].dt.date.nunique()
    trades_per_day = float(num_trades / days) if days > 0 else 0.0

    return {
        "total_profit": total_profit,
        "max_drawdown": max_dd,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "trades_per_day": trades_per_day,
        "num_trades": num_trades,
    }


# -----------------------------------------------------------------------------
# Parameter Sweep Driver
# -----------------------------------------------------------------------------


def run_algo_for_params(
    symbol: str,
    interval: str,
    trade_size: float,
    user_id: int,
    builder_days: int,
    k_forward: int,
    long_th: float,
    short_th: float,
    min_prob_adv: float,
    vol_mult: float,
    data_dir: str,
    base_dir: str,
) -> pd.DataFrame:
    """
    Call AlgoMM_eval.py for a specific parameter combo and return the run DataFrame.
    Focuses on *live-like* features only:
    - Probability thresholds
    - Min probability advantage
    - Volume multiplier
    - EOD close
    - NO fixed stop loss / NO take-profit / NO trailing stop
    """

    algo_eval_path = os.path.join(
        base_dir,
        "app",
        "scripts",
        "stocks",
        "backtest_algos",
        "AlgoMM_eval.py",
    )

    cmd = [
        sys.executable,
        algo_eval_path,
        "-s",
        symbol,
        "-i",
        interval,
        "-q",
        str(trade_size),
        "-u",
        str(user_id),
        "--builder-days",
        str(builder_days),
        "--k-forward",
        str(k_forward),
        "--long-threshold",
        str(long_th),
        "--short-threshold",
        str(short_th),
        # Exit thresholds – you can tweak, but keep them fixed for now
        "--long-exit-threshold",
        str(long_th - 0.05),
        "--short-exit-threshold",
        str(short_th + 0.05),
        "--min-volume-multiplier",
        str(vol_mult),
        "--min-prob-advantage",
        str(min_prob_adv),
        "--cooldown-sec",
        "60",
        "--eod-close",
        "--auto-train",
        "--save-run",
    ]

    print(
        f"\n=== Running AlgoMM_eval for {symbol} {interval} "
        f"(long={long_th}, short={short_th}, prob_adv={min_prob_adv}, vol_mult={vol_mult}) ==="
    )
    subprocess.run(cmd, check=True)

    # Build run.csv path (like /var/www/stockwicks/data/116/116_MU_5min_run.csv)
    user_dir = os.path.join(data_dir, str(user_id))
    run_filename = f"{user_id}_{symbol}_{interval}_run.csv"
    run_path = os.path.join(user_dir, run_filename)

    if not os.path.exists(run_path):
        raise FileNotFoundError(f"Run file not found at: {run_path}")

    run_df = pd.read_csv(run_path)
    return run_df


def main():
    parser = argparse.ArgumentParser(description="AlgoMM parameter sweep backtester")

    parser.add_argument("-s", "--symbol", required=True, help="Ticker symbol, e.g. MU, AVGO")
    parser.add_argument("-i", "--interval", required=True, help="Interval, e.g. 5min, 15min")
    parser.add_argument("-q", "--trade-size", type=float, required=True, help="Trade size (shares)")
    parser.add_argument("-u", "--user-id", type=int, required=True, help="User ID (e.g. 116)")
    parser.add_argument(
        "--builder-days",
        type=int,
        default=60,
        help="Builder days for feature generation (default: 60)",
    )
    parser.add_argument(
        "--k-forward",
        type=int,
        default=3,
        help="k-forward horizon used in model training (default: 3)",
    )
    parser.add_argument(
        "--data-dir",
        default=os.getenv("DATA_DIR", "/var/www/stockwicks/data"),
        help="Base data directory (default: env DATA_DIR or /var/www/stockwicks/data)",
    )
    parser.add_argument(
        "--base-dir",
        default=os.getenv("REPO_ROOT", "/var/www/stockwicks"),
        help="Repository root (default: env REPO_ROOT or /var/www/stockwicks)",
    )

    args = parser.parse_args()

    symbol = args.symbol.upper()
    interval = args.interval
    trade_size = args.trade_size
    user_id = args.user_id
    builder_days = args.builder_days
    k_forward = args.k_forward
    data_dir = args.data_dir
    base_dir = args.base_dir

    # ---------------------------------------------------------------------
    # PARAMETER GRID (edit these ranges as you like)
    # ---------------------------------------------------------------------

    long_thresholds = [0.60, 0.62, 0.58]
    short_thresholds = [0.40, 0.38, 0.42]
    min_prob_advantages = [0.05, 0.08, 0.10]
    volume_multipliers = [1.0, 1.1, 1.2]

    # ---------------------------------------------------------------------
    # Output files
    # ---------------------------------------------------------------------
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_out = f"param_sweep_{symbol}_{interval}_summary_{timestamp_str}.csv"
    trades_out = f"param_sweep_{symbol}_{interval}_trades_{timestamp_str}.csv"

    all_results = []
    all_trades_rows = []

    combo_id = 0

    for long_th in long_thresholds:
        for short_th in short_thresholds:
            for min_prob_adv in min_prob_advantages:
                for vol_mult in volume_multipliers:
                    combo_id += 1
                    try:
                        run_df = run_algo_for_params(
                            symbol=symbol,
                            interval=interval,
                            trade_size=trade_size,
                            user_id=user_id,
                            builder_days=builder_days,
                            k_forward=k_forward,
                            long_th=long_th,
                            short_th=short_th,
                            min_prob_adv=min_prob_adv,
                            vol_mult=vol_mult,
                            data_dir=data_dir,
                            base_dir=base_dir,
                        )
                    except subprocess.CalledProcessError as e:
                        print(f"AlgoMM_eval failed for combo {combo_id}: {e}")
                        continue
                    except FileNotFoundError as e:
                        print(str(e))
                        continue

                    trades = extract_trades_from_run(run_df)
                    metrics = compute_metrics_from_trades(trades)

                    result_row = {
                        "combo_id": combo_id,
                        "symbol": symbol,
                        "interval": interval,
                        "trade_size": trade_size,
                        "long_threshold": long_th,
                        "short_threshold": short_th,
                        "min_prob_advantage": min_prob_adv,
                        "volume_multiplier": vol_mult,
                        "total_profit": metrics["total_profit"],
                        "max_drawdown": metrics["max_drawdown"],
                        "win_rate": metrics["win_rate"],
                        "profit_factor": metrics["profit_factor"],
                        "trades_per_day": metrics["trades_per_day"],
                        "num_trades": metrics["num_trades"],
                    }
                    all_results.append(result_row)

                    if not trades.empty:
                        trades_copy = trades.copy()
                        trades_copy["combo_id"] = combo_id
                        trades_copy["symbol"] = symbol
                        trades_copy["interval"] = interval
                        trades_copy["long_threshold"] = long_th
                        trades_copy["short_threshold"] = short_th
                        trades_copy["min_prob_advantage"] = min_prob_adv
                        trades_copy["volume_multiplier"] = vol_mult
                        all_trades_rows.append(trades_copy)

    # ---------------------------------------------------------------------
    # Save outputs
    # ---------------------------------------------------------------------
    if all_results:
        summary_df = pd.DataFrame(all_results)
        summary_df = summary_df.sort_values(
            ["total_profit", "profit_factor", "win_rate"], ascending=[False, False, False]
        )
        summary_df.to_csv(summary_out, index=False)
        print(f"\n✅ Summary saved to: {summary_out}")
    else:
        print("\n⚠️ No successful runs, summary not created.")

    if all_trades_rows:
        trades_df = pd.concat(all_trades_rows, ignore_index=True)
        trades_df.to_csv(trades_out, index=False)
        print(f"✅ Trades details saved to: {trades_out}")
    else:
        print("\n⚠️ No trades generated, trades file not created.")


if __name__ == "__main__":
    main()
