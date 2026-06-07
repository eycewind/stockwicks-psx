import argparse
import pytz
from datetime import datetime, timedelta

import pandas as pd
import matplotlib.pyplot as plt

from app.utils.stock import schwab_price_history as sph
from app.utils.stock.indicators import (
    compute_smi_blau,
    compute_smi_blau_smooth,
    compute_vwmi,
)
from app.database.connection import SessionLocal
from app.models.trade_position import TradePosition
from app.models.paper_trading_bot import PaperStockBotTradeHistory

ET = pytz.timezone("US/Eastern")


def get_candles(symbol: str, interval: str, date: str, days: int = 10):
    fetch = {
        "1min": sph.get_schwab_1min,
        "5min": sph.get_schwab_5min,
        "15min": sph.get_schwab_15min,
        "30min": sph.get_schwab_30min,
        "1d": sph.get_schwab_daily,
    }[interval]

    df = fetch(symbol.upper(), period=days)
    if df is None or df.empty:
        return df

    target_date = pd.to_datetime(date).date()
    df = df[df.index.date == target_date]
    return df


def plot(symbol, interval, df, k_series, d_series, k_smooth, d_smooth, trades, output_file: str | None = None):
    # Ensure volume for VWMI (fallback to 1 if missing)
    if "volume" not in df.columns:
        df = df.copy()
        df["volume"] = 1.0

    # ---- VWMI (SMI around VWAP)
    vwmi, vwmi_sig, vwap = compute_vwmi(df, n=14, m=5, s=5)

    # ---- 4 stacked subplots (share X)
    fig, (ax1, ax2, ax3, ax4) = plt.subplots(4, 1, figsize=(14, 12), sharex=True)

    # --- Price + VWAP + trades ---
    ax1.set_title(f"{symbol} {interval} with bot trades")
    ax1.plot(df.index, df["close"], label="Close")
    ax1.vlines(df.index, df["low"], df["high"], linewidth=0.5, alpha=0.6)
    ax1.plot(df.index, vwap, label="VWAP", linewidth=1.2)

    for tr in trades:
        # entry time from either model
        if hasattr(tr, "created_at") and tr.created_at:
            t_entry = tr.created_at.astimezone(ET)
        else:
            t_entry = tr.entry_time.astimezone(ET) if getattr(tr, "entry_time", None) else None

        if t_entry is not None:
            ax1.scatter(t_entry, tr.entry_price, marker="o", color="blue", s=80, zorder=5)

        t_exit = tr.exit_time.astimezone(ET) if getattr(tr, "exit_time", None) else None
        if t_exit is not None:
            marker = "^" if tr.trade_type.lower() == "buy" else "v"
            pnl = getattr(tr, "profit", None)
            if pnl is None:
                pnl = getattr(tr, "profit_loss", 0)
            color = "green" if (pnl or 0) >= 0 else "red"
            ax1.scatter(t_exit, tr.exit_price or tr.entry_price, marker=marker, color=color, s=80, zorder=5)

    ax1.legend()

    # --- SMI (raw) ---
    ax2.plot(df.index, k_series, label="K (raw)")
    ax2.plot(df.index, d_series, label="D (raw)")
    ax2.axhline(20, linestyle="--", alpha=0.5)
    ax2.axhline(-20, linestyle="--", alpha=0.5)
    ax2.legend()
    ax2.set_title("SMI (Raw)")

    # --- SMI (smoothed) ---
    ax3.plot(df.index, k_smooth, label="K (smooth)")
    ax3.plot(df.index, d_smooth, label="D (smooth)")
    ax3.axhline(20, linestyle="--", alpha=0.5)
    ax3.axhline(-20, linestyle="--", alpha=0.5)
    ax3.legend()
    ax3.set_title("SMI (Smoothed)")

    # --- VWMI panel ---
    # # --- VWMI panel ---
    # vwmi_plot = vwmi.clip(-150, 150)
    # sig_plot  = vwmi_sig.clip(-150, 150)
    # ax4.plot(df.index, vwmi_plot, label="VWMI")
    # ax4.plot(df.index, sig_plot,  label="Signal")
    # ax4.axhline(60, linestyle="--", alpha=0.5)
    # ax4.axhline(-60, linestyle="--", alpha=0.5)
    # ax4.set_ylim(-160, 160)  # optional, keeps the panel consistent
    # ax4.legend()
    # ax4.set_title("VWMI (VWAP-Momentum Index)")


    plt.tight_layout()

    if output_file:
        plt.savefig(output_file, dpi=130)
        plt.close(fig)
    else:
        plt.show()


def main():
    ap = argparse.ArgumentParser(description="Plot trades on candles + SMI + VWMI")
    ap.add_argument("--bot-id", type=int, help="Bot ID (if available)")
    ap.add_argument("--user-id", type=int, help="User ID")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--interval", default="1min")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD trading date (ET)")
    ap.add_argument("--days", type=int, default=10, help="Days of candles to fetch")
    args = ap.parse_args()

    if not args.bot_id and not args.user_id:
        ap.error("You must provide either --bot-id or --user-id")

    start = ET.localize(datetime.fromisoformat(args.date))
    end = start + timedelta(days=1)

    df = get_candles(args.symbol, args.interval, args.date, args.days)
    if df is None or df.empty:
        print("No data found.")
        return

    k_series, d_series = compute_smi_blau(df)
    k_smooth, d_smooth = compute_smi_blau_smooth(df)

    # --- Fetch trades (paper first; fallback real positions) ---
    session = SessionLocal()
    try:
        trades = (
            session.query(PaperStockBotTradeHistory)
            .filter(PaperStockBotTradeHistory.symbol == args.symbol.upper())
            .filter(PaperStockBotTradeHistory.user_id == args.user_id)
            .filter(PaperStockBotTradeHistory.entry_time >= start)
            .filter(PaperStockBotTradeHistory.entry_time <= end)
            .all()
        )

        if not trades:
            trades = (
                session.query(TradePosition)
                .filter(TradePosition.symbol == args.symbol.upper())
                .filter(TradePosition.user_id == args.user_id)
                .filter(TradePosition.created_at >= start)
                .filter(TradePosition.created_at <= end)
                .all()
            )
    finally:
        session.close()

    plot(args.symbol, args.interval, df, k_series, d_series, k_smooth, d_smooth, trades)


if __name__ == "__main__":
    main()
