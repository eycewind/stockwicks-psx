#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/options/test_spx_trend_signals.py
"""
Test script for SPX 0DTE trend/chop logic.

What it does
------------
1. Fetches recent SPX 5-minute price history from Schwab
2. Keeps the last N trading days of regular-session bars
3. Replays the current chop/trend logic bar-by-bar
4. Plots:
   - SPX close
   - candidate CALL entries from logic
   - candidate PUT entries from logic
   - optional actual trade opens from DB (if --user-id is provided)
5. Exports a CSV of all evaluated bars/signals

This is for VALIDATION only. It does not place any trades.

Example
-------
cd /var/www/stockwicks
source venv/bin/activate

python app/scripts/options/test_spx_trend_signals.py \
  --days 2 \
  --user-id 116 \
  --outdir /tmp/spx_signal_test

Then open:
  /tmp/spx_signal_test/spx_signal_plot.png
  /tmp/spx_signal_test/spx_signal_bars.csv
"""

from __future__ import annotations

import os
import sys
import math
import argparse
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List

import requests
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pytz
from datetime import time as dtime

_THIS_FILE = os.path.abspath(__file__)
_THIS_DIR = os.path.dirname(_THIS_FILE)
PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "../../../"))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

ET = pytz.timezone("US/Eastern")

SPX_SYMBOLS_TO_TRY = ["$SPX", "SPX", "$SPX.X", ".SPX"]

LOOKBACK_MINUTES = 30
MAX_CHOP_MOVE_PCT = 0.30
MIN_TREND_MOVE_PCT = 0.60
CONSECUTIVE_DIRECTION_NEEDED = 3

OPTIMAL_WINDOWS = [
    (9, 45, 10, 30),
    (11, 0, 11, 45),
    (13, 30, 14, 30),
]
AVOID_WINDOWS = [
    (9, 30, 9, 45),
    (10, 30, 10, 45),
    (12, 0, 13, 0),
    (14, 45, 15, 0),
    (15, 0, 16, 0),
]


def _is_weekday(ts_et: pd.Timestamp) -> bool:
    return ts_et.weekday() < 5


def _is_regular_session(ts_et: pd.Timestamp) -> bool:
    if not _is_weekday(ts_et):
        return False
    t = ts_et.time()
    return dtime(9, 30) <= t <= dtime(16, 0)


def _is_avoid_window(ts_et: pd.Timestamp) -> bool:
    t = ts_et.time()
    for sh, sm, eh, em in AVOID_WINDOWS:
        if dtime(sh, sm) <= t <= dtime(eh, em):
            return True
    return False


def _is_open_window(ts_et: pd.Timestamp) -> bool:
    if not _is_weekday(ts_et):
        return False
    t = ts_et.time()
    return dtime(9, 40) <= t <= dtime(15, 15) and (not _is_avoid_window(ts_et))


def _is_optimal_window(ts_et: pd.Timestamp) -> bool:
    t = ts_et.time()
    for sh, sm, eh, em in OPTIMAL_WINDOWS:
        if dtime(sh, sm) <= t <= dtime(eh, em):
            return True
    return False


def get_valid_token():
    from app.utils.stock.schwab_token import get_valid_access_token
    return get_valid_access_token()


def fetch_spx_5m_history(days: int = 2, verbose: bool = True) -> Tuple[pd.DataFrame, str]:
    token = get_valid_token()
    if not token:
        raise RuntimeError("Could not get valid Schwab token.")

    base = os.getenv("SCHWAB_API_URL", "https://api.schwabapi.com/marketdata/v1").rstrip("/")
    url = f"{base}/pricehistory"
    headers = {"Authorization": f"Bearer {token}"}
    req_period = max(5, days + 3)

    last_err = None
    for sym in SPX_SYMBOLS_TO_TRY:
        params = {
            "symbol": sym,
            "periodType": "day",
            "period": req_period,
            "frequencyType": "minute",
            "frequency": 5,
            "needExtendedHoursData": "false",
        }
        try:
            r = requests.get(url, headers=headers, params=params, timeout=20)
            if verbose:
                print(f"[fetch] symbol={sym} status={r.status_code}")
            if r.status_code != 200:
                last_err = f"{r.status_code} {r.text[:300] if r.text else ''}"
                continue

            js = r.json() if r.content else {}
            candles = js.get("candles") or []
            if not candles:
                last_err = f"No candles for {sym}"
                continue

            df = pd.DataFrame(candles).copy()
            required = {"datetime", "open", "high", "low", "close"}
            if not required.issubset(set(df.columns)):
                last_err = f"Missing required columns for {sym}: {list(df.columns)}"
                continue

            df["dt_utc"] = pd.to_datetime(df["datetime"], unit="ms", utc=True)
            df["dt_et"] = df["dt_utc"].dt.tz_convert(ET)

            for col in ["open", "high", "low", "close", "volume"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            df = df.dropna(subset=["open", "high", "low", "close"]).copy()
            df = df[df["dt_et"].apply(_is_regular_session)].copy()
            df["trade_date_et"] = df["dt_et"].dt.date

            unique_dates = sorted(df["trade_date_et"].dropna().unique())
            if not unique_dates:
                last_err = f"No regular-session rows for {sym}"
                continue

            keep_dates = set(unique_dates[-days:])
            df = df[df["trade_date_et"].isin(keep_dates)].copy()
            df = df.sort_values("dt_et").reset_index(drop=True)
            return df, sym

        except Exception as e:
            last_err = str(e)
            continue

    raise RuntimeError(f"Failed to fetch SPX 5m history. Last error: {last_err}")


def detect_signal_from_history_window(closes: np.ndarray) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "regime": "CHOP",
        "direction": "NONE",
        "should_trade": False,
        "move_30m_pct": None,
        "consecutive_dir": 0,
        "trend_strength": 0.0,
        "reason": "",
    }

    need = max(6, int(math.ceil(LOOKBACK_MINUTES / 5)))
    if len(closes) < need + 1:
        out["reason"] = "not_enough_bars"
        return out

    current = float(closes[-1])
    past = float(closes[-(need + 1)])
    move_pct = ((current - past) / past) * 100.0 if past else 0.0
    out["move_30m_pct"] = round(move_pct, 3)

    recent = closes[-(CONSECUTIVE_DIRECTION_NEEDED + 1):]
    rets = np.diff(recent)
    if len(rets) == CONSECUTIVE_DIRECTION_NEEDED:
        if np.all(rets > 0):
            out["consecutive_dir"] = CONSECUTIVE_DIRECTION_NEEDED
        elif np.all(rets < 0):
            out["consecutive_dir"] = -CONSECUTIVE_DIRECTION_NEEDED

    abs_move = abs(move_pct)

    if abs_move < MAX_CHOP_MOVE_PCT:
        out["regime"] = "CHOP"
        out["reason"] = f"CHOP: 30m move {move_pct:.2f}% < {MAX_CHOP_MOVE_PCT:.2f}%"
        return out

    if abs_move >= MIN_TREND_MOVE_PCT and abs(out["consecutive_dir"]) >= CONSECUTIVE_DIRECTION_NEEDED:
        out["should_trade"] = True
        out["trend_strength"] = min(1.0, abs_move / 1.0)
        if move_pct > 0:
            out["regime"] = "STRONG_UPTREND"
            out["direction"] = "CALL"
            out["reason"] = f"STRONG UPTREND: {move_pct:.2f}% with {CONSECUTIVE_DIRECTION_NEEDED} green candles"
        else:
            out["regime"] = "STRONG_DOWNTREND"
            out["direction"] = "PUT"
            out["reason"] = f"STRONG DOWNTREND: {move_pct:.2f}% with {CONSECUTIVE_DIRECTION_NEEDED} red candles"
        return out

    out["trend_strength"] = min(0.8, abs_move / 1.5)
    if move_pct > 0:
        out["regime"] = "WEAK_UPTREND"
        out["direction"] = "CALL"
        out["reason"] = f"WEAK UPTREND: {move_pct:.2f}%, insufficient candle consistency"
    else:
        out["regime"] = "WEAK_DOWNTREND"
        out["direction"] = "PUT"
        out["reason"] = f"WEAK DOWNTREND: {move_pct:.2f}%, insufficient candle consistency"
    return out


def build_signal_table(df: pd.DataFrame) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    prev_trade_dir = "NONE"
    closes = df["close"].astype(float).to_numpy()

    for i in range(len(df)):
        ts = pd.Timestamp(df.iloc[i]["dt_et"])
        row = {
            "dt_et": ts,
            "trade_date_et": df.iloc[i]["trade_date_et"],
            "close": float(df.iloc[i]["close"]),
            "in_open_window": _is_open_window(ts),
            "in_optimal_window": _is_optimal_window(ts),
            "in_avoid_window": _is_avoid_window(ts),
            "regime": "CHOP",
            "direction": "NONE",
            "should_trade": False,
            "move_30m_pct": np.nan,
            "consecutive_dir": 0,
            "trend_strength": 0.0,
            "reason": "",
            "candidate_entry": False,
        }

        if i >= 6:
            sig = detect_signal_from_history_window(closes[: i + 1])
            row.update({
                "regime": sig["regime"],
                "direction": sig["direction"],
                "should_trade": sig["should_trade"],
                "move_30m_pct": sig["move_30m_pct"],
                "consecutive_dir": sig["consecutive_dir"],
                "trend_strength": sig["trend_strength"],
                "reason": sig["reason"],
            })

        would_trade_now = bool(row["in_open_window"] and row["should_trade"] and row["direction"] in ("CALL", "PUT"))
        if would_trade_now:
            if prev_trade_dir != row["direction"]:
                row["candidate_entry"] = True
            prev_trade_dir = row["direction"]
        else:
            prev_trade_dir = "NONE"

        rows.append(row)

    return pd.DataFrame(rows)


def load_actual_trade_opens(user_id: Optional[int], start_et: pd.Timestamp, end_et: pd.Timestamp) -> pd.DataFrame:
    if not user_id:
        return pd.DataFrame(columns=["dt_et", "put_call", "entry_price", "strike", "source"])

    from app.database.connection import SessionLocal
    from app.models.paper_spx_0dte import PaperSPXTradeHistory, PaperSPXOpenTrade

    db = SessionLocal()
    try:
        hist = db.query(PaperSPXTradeHistory).filter(
            PaperSPXTradeHistory.user_id == int(user_id)
        ).all()
        current = db.query(PaperSPXOpenTrade).filter(
            PaperSPXOpenTrade.user_id == int(user_id),
            PaperSPXOpenTrade.status == "OPEN",
        ).all()

        rows = []
        for t in hist:
            if not t.opened_at:
                continue
            ts = pd.Timestamp(t.opened_at)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            ts = ts.tz_convert(ET)
            if start_et <= ts <= end_et:
                rows.append({
                    "dt_et": ts,
                    "put_call": str(t.put_call or ""),
                    "entry_price": float(t.entry_price or 0.0),
                    "strike": float(t.strike or 0.0),
                    "source": "history",
                })

        for t in current:
            if not t.opened_at:
                continue
            ts = pd.Timestamp(t.opened_at)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            ts = ts.tz_convert(ET)
            if start_et <= ts <= end_et:
                rows.append({
                    "dt_et": ts,
                    "put_call": str(t.put_call or ""),
                    "entry_price": float(t.entry_price or 0.0),
                    "strike": float(t.strike or 0.0),
                    "source": "open_trade",
                })

        if not rows:
            return pd.DataFrame(columns=["dt_et", "put_call", "entry_price", "strike", "source"])

        return pd.DataFrame(rows).sort_values("dt_et").reset_index(drop=True)
    finally:
        db.close()


def make_plot(price_df: pd.DataFrame, signal_df: pd.DataFrame, actual_df: pd.DataFrame, out_png: Path, title: str) -> None:
    plt.figure(figsize=(16, 8))
    ax = plt.gca()

    ax.plot(price_df["dt_et"], price_df["close"], linewidth=1.5, label="SPX close")

    logic_calls = signal_df[(signal_df["candidate_entry"]) & (signal_df["direction"] == "CALL")]
    logic_puts = signal_df[(signal_df["candidate_entry"]) & (signal_df["direction"] == "PUT")]

    if not logic_calls.empty:
        ax.scatter(logic_calls["dt_et"], logic_calls["close"], marker="^", s=90, label="Logic CALL entry", zorder=5)
    if not logic_puts.empty:
        ax.scatter(logic_puts["dt_et"], logic_puts["close"], marker="v", s=90, label="Logic PUT entry", zorder=5)

    if actual_df is not None and not actual_df.empty:
        actual_calls = actual_df[actual_df["put_call"].astype(str).str.upper().str.contains("CALL", na=False)]
        actual_puts = actual_df[actual_df["put_call"].astype(str).str.upper().str.contains("PUT", na=False)]

        if not actual_calls.empty:
            call_y = pd.merge_asof(
                actual_calls.sort_values("dt_et"),
                price_df[["dt_et", "close"]].sort_values("dt_et"),
                on="dt_et",
                direction="nearest",
            )
            ax.scatter(call_y["dt_et"], call_y["close"], marker="x", s=80, label="Actual CALL open", zorder=6)

        if not actual_puts.empty:
            put_y = pd.merge_asof(
                actual_puts.sort_values("dt_et"),
                price_df[["dt_et", "close"]].sort_values("dt_et"),
                on="dt_et",
                direction="nearest",
            )
            ax.scatter(put_y["dt_et"], put_y["close"], marker="x", s=80, label="Actual PUT open", zorder=6)

    dates = sorted(price_df["trade_date_et"].unique())
    for d in dates:
        day_rows = price_df[price_df["trade_date_et"] == d]
        if not day_rows.empty:
            ax.axvline(day_rows["dt_et"].iloc[0], linewidth=0.8, alpha=0.35)

    ax.set_title(title)
    ax.set_xlabel("Time (ET)")
    ax.set_ylabel("SPX")
    ax.legend()
    ax.grid(True, alpha=0.25)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M", tz=ET))
    plt.xticks(rotation=30)
    plt.tight_layout()
    plt.savefig(out_png, dpi=180, bbox_inches="tight")
    plt.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=2, help="Number of recent trading days to keep")
    parser.add_argument("--user-id", type=int, default=0, help="Optional: overlay actual trade opens for this user")
    parser.add_argument("--outdir", type=str, default="/tmp/spx_signal_test", help="Output directory")
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    print("[1/4] Fetching SPX 5-minute history...")
    price_df, used_symbol = fetch_spx_5m_history(days=args.days, verbose=True)
    print(f"Fetched {len(price_df)} rows using symbol {used_symbol}")
    print(price_df.tail(10)[["dt_et", "open", "high", "low", "close"]].to_string(index=False))

    print("[2/4] Building signal table...")
    signal_df = build_signal_table(price_df)

    start_et = price_df["dt_et"].min()
    end_et = price_df["dt_et"].max()

    print("[3/4] Loading actual trades from DB...")
    actual_df = load_actual_trade_opens(args.user_id if args.user_id else None, start_et, end_et)
    print(f"Actual trade opens loaded: {len(actual_df)}")

    csv_path = outdir / "spx_signal_bars.csv"
    signal_df.to_csv(csv_path, index=False)

    entry_csv = outdir / "spx_signal_entries_only.csv"
    signal_df[signal_df["candidate_entry"]].to_csv(entry_csv, index=False)

    plot_path = outdir / "spx_signal_plot.png"
    title = f"SPX 5m signal replay | last {args.days} trading day(s) | symbol={used_symbol}"
    print("[4/4] Writing plot...")
    make_plot(price_df, signal_df, actual_df, plot_path, title=title)

    print("\nDone.")
    print(f"Plot:  {plot_path}")
    print(f"CSV:   {csv_path}")
    print(f"Entry: {entry_csv}")

    entries = signal_df[signal_df["candidate_entry"]]
    if not entries.empty:
        print("\nCandidate entries:")
        print(
            entries[["dt_et", "close", "regime", "direction", "move_30m_pct", "consecutive_dir", "reason"]].to_string(index=False)
        )


if __name__ == "__main__":
    main()
