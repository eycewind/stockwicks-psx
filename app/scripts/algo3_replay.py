# scripts/algo3_replay.py
import argparse
import os
import pytz
from datetime import datetime, timedelta

import pandas as pd

from app.utils.stock import schwab_price_history as sph
from app.utils.stock.indicators import compute_smi_blau
from app.scripts.stock_algos.base_wiring import StockBaseRunner
from app.scripts._replay_overlay import Marker, plot_overlay, export_markers_csv, verify_against_db


ET = pytz.timezone("US/Eastern")

def cross_up(s: pd.Series, level: float, k: int) -> bool:
    return len(s) >= k + 1 and float(s.iloc[-k - 1]) < level <= float(s.iloc[-1])

def cross_down(s: pd.Series, level: float, k: int) -> bool:
    return len(s) >= k + 1 and float(s.iloc[-k - 1]) > level >= float(s.iloc[-1])

def cross_over(a: pd.Series, b: pd.Series, k: int) -> bool:
    return (
        len(a) >= k + 1
        and len(b) >= k + 1
        and float(a.iloc[-k - 1]) <= float(b.iloc[-k - 1])
        and float(a.iloc[-1]) > float(b.iloc[-1])
    )

def cross_under(a: pd.Series, b: pd.Series, k: int) -> bool:
    return (
        len(a) >= k + 1
        and len(b) >= k + 1
        and float(a.iloc[-k - 1]) >= float(b.iloc[-k - 1])
        and float(a.iloc[-1]) < float(b.iloc[-1])
    )

def main():
    ap = argparse.ArgumentParser(description="Replay Algo3 with DB inserts + overlay/verify.")
    ap.add_argument("--bot-id", type=int, required=True)
    ap.add_argument("--user-id", type=int, required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--interval", default="1min", choices=["1min", "5min", "15min", "30min", "1d"])
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--qty", type=float, default=1.0)

    # Algo3 params
    ap.add_argument("--smi-len", type=int, default=int(os.getenv("ALGO3_SMI_LEN", "10")))
    ap.add_argument("--smi-r", type=int, default=int(os.getenv("ALGO3_SMI_R", "3")))
    ap.add_argument("--smi-s", type=int, default=int(os.getenv("ALGO3_SMI_S", "3")))
    ap.add_argument("--smi-sig", type=int, default=int(os.getenv("ALGO3_SMI_SIG", "10")))
    ap.add_argument("--smi-ma", default=os.getenv("ALGO3_SMI_MA", "ema"), choices=["ema", "sma"])
    ap.add_argument("--entry-long", type=float, default=float(os.getenv("ALGO3_ENTRY_LONG_LEVEL", "-60")))
    ap.add_argument("--entry-short", type=float, default=float(os.getenv("ALGO3_ENTRY_SHORT_LEVEL", "60")))
    ap.add_argument("--exit-long", type=float, default=float(os.getenv("ALGO3_EXIT_LONG_LEVEL", "60")))
    ap.add_argument("--exit-short", type=float, default=float(os.getenv("ALGO3_EXIT_SHORT_LEVEL", "-60")))
    ap.add_argument("--lookback", type=int, default=int(os.getenv("ALGO3_CROSS_LOOKBACK", "3")))
    ap.add_argument("--use-signal-gate", action="store_true", default=os.getenv("ALGO3_USE_SIGNAL_GATE", "1").lower() in {"1","true","yes","on"})

    # New: output/verify controls
    ap.add_argument("--plot", action="store_true", help="Show/save overlay chart")
    ap.add_argument("--plot-out", default="", help="Optional path to save PNG instead of showing")
    ap.add_argument("--export-csv", default="", help="Optional path to export markers CSV")
    ap.add_argument("--verify", action="store_true", help="Compare markers vs DB trades")
    ap.add_argument("--verify-start", default="", help="ISO start time (ET) for DB verify")
    ap.add_argument("--verify-end", default="", help="ISO end time (ET) for DB verify")
    ap.add_argument("--time-tol-sec", type=int, default=90)
    ap.add_argument("--px-tol", type=float, default=0.05)

    args = ap.parse_args()

    # fetch history
    fetch = {
        "1min": sph.get_schwab_1min,
        "5min":  sph.get_schwab_5min,
        "15min": sph.get_schwab_15min,
        "30min": sph.get_schwab_30min,
        "1d":   sph.get_schwab_daily,
    }[args.interval]
    df = fetch(args.symbol.upper(), period=args.days)
    if df is None or df.empty:
        print("no data")
        return

    k_series, d_series = compute_smi_blau(
        df, length=args.smi_len, r=args.smi_r, s=args.smi_s, sig=args.smi_sig, ma=args.smi_ma
    )
    df = df.copy()
    df["K"] = k_series
    df["D"] = d_series

    runner = StockBaseRunner()
    pos = None  # None | "long" | "short"
    entries = exits = 0

    markers: list[Marker] = []

    for i in range(max(50, args.smi_len + args.smi_r + args.smi_s), len(df)):
        window = df.iloc[:i].copy()
        K = window["K"].dropna()
        D = window["D"].dropna()
        bar_time = window.index[-1]
        px = float(window["close"].iloc[-1])

        le = cross_up(K, args.entry_long, args.lookback)
        se = cross_down(K, args.entry_short, args.lookback)
        lx = cross_down(K, args.exit_long, args.lookback)
        sx = cross_up(K, args.exit_short, args.lookback)

        kd_long = kd_short = True
        if args.use_signal_gate and not D.empty and len(D) >= args.lookback + 1:
            kd_long = cross_over(K, D, args.lookback)
            kd_short = cross_under(K, D, args.lookback)

        if pos is None:
            if le and kd_long:
                pos = "long"; entries += 1
                print(f"[ENTRY LONG] {bar_time} px={px:.4f}")
                markers.append(Marker(bar_time, px, "ENTRY", "long", "BUY"))
                runner.place_paper_trade_and_bookkeep(
                    user_id=args.user_id, symbol=args.symbol.upper(), side="BUY", price=px,
                    qty=args.qty, bot_id=args.bot_id, algo_name="Algo3Replay",
                )
            elif se and kd_short:
                pos = "short"; entries += 1
                print(f"[ENTRY SHORT] {bar_time} px={px:.4f}")
                markers.append(Marker(bar_time, px, "ENTRY", "short", "SELL"))
                runner.place_paper_trade_and_bookkeep(
                    user_id=args.user_id, symbol=args.symbol.upper(), side="SELL", price=px,
                    qty=args.qty, bot_id=args.bot_id, algo_name="Algo3Replay",
                )
        else:
            if pos == "long" and lx:
                exits += 1
                print(f"[EXIT LONG]  {bar_time} px={px:.4f}")
                markers.append(Marker(bar_time, px, "EXIT", "long", "SELL"))
                pos = None
                runner.place_paper_trade_and_bookkeep(
                    user_id=args.user_id, symbol=args.symbol.upper(), side="SELL", price=px,
                    qty=args.qty, bot_id=args.bot_id, algo_name="Algo3Replay",
                )
            elif pos == "short" and sx:
                exits += 1
                print(f"[COVER SHORT]{bar_time} px={px:.4f}")
                markers.append(Marker(bar_time, px, "EXIT", "short", "BUY"))
                pos = None
                runner.place_paper_trade_and_bookkeep(
                    user_id=args.user_id, symbol=args.symbol.upper(), side="BUY", price=px,
                    qty=args.qty, bot_id=args.bot_id, algo_name="Algo3Replay",
                )

    print(f"\n[SUMMARY] entries={entries} exits={exits} open_pos={pos}")

    # optional exports/plots
    if args.export_csv:
        export_markers_csv(markers, args.export_csv)
        print(f"Exported markers -> {args.export_csv}")

    if args.plot:
        title = f"{args.symbol.upper()} {args.interval} Algo3 replay overlay"
        if args.plot_out:
            plot_overlay(df, markers, title, save_path=args.plot_out)
            print(f"Saved plot -> {args.plot_out}")
        else:
            plot_overlay(df, markers, title)

    if args.verify:
        # derive verify window if not provided: use df range
        if args.verify_start:
            start_et = ET.localize(datetime.fromisoformat(args.verify_start))
        else:
            start_et = ET.localize(df.index[0].to_pydatetime())
        if args.verify_end:
            end_et = ET.localize(datetime.fromisoformat(args.verify_end))
        else:
            end_et = ET.localize(df.index[-1].to_pydatetime())

        matches, mismatches = verify_against_db(
            markers=markers,
            bot_id=args.bot_id,
            user_id=args.user_id,
            symbol=args.symbol,
            start=start_et.astimezone(tz=None),
            end=end_et.astimezone(tz=None),
            time_tolerance_sec=args.time_tol_sec,
            price_tolerance_abs=args.px_tol,
        )
        print("\n[VERIFY] matches:")
        print(matches.to_string(index=False))
        print("\n[VERIFY] mismatches:")
        print(mismatches.to_string(index=False))
        print(f"\n[VERIFY] Summary: matches={len(matches)} mismatches={len(mismatches)}")
        
if __name__ == "__main__":
    main()
