# scripts/_replay_overlay.py
from dataclasses import dataclass
from typing import List, Literal, Optional, Tuple
from datetime import datetime, timedelta, timezone

import pandas as pd
import matplotlib.pyplot as plt

# --- DB imports (SQLAlchemy) ---
from app.database.connection import SessionLocal
from app.models.trade import ClosedPosition as Trade


Side = Literal["BUY", "SELL"]
Pos  = Literal["long", "short"]

@dataclass
class Marker:
    t: pd.Timestamp     # bar timestamp (index from df)
    px: float           # trade price at that bar
    kind: Literal["ENTRY", "EXIT"]
    pos: Pos            # long/short
    side: Side          # BUY/SELL

def plot_overlay(
    df: pd.DataFrame,
    markers: List[Marker],
    title: str = "Replay Overlay",
    save_path: Optional[str] = None,
):
    """
    Draw candles and overlay entry/exit markers.
    - Uses a single matplotlib axes (no seaborn, no custom colors).
    - You can switch to your own app.analysis.visualization if preferred.
    """
    # Basic candlestick with OHLC lines (simple: close line + high/low whiskers)
    # If you already have a plot_candles helper, feel free to replace this block
    fig, ax = plt.subplots(figsize=(12, 6))
    # minimal candle-like: vertical whiskers for high/low, and ticks for open/close
    x = df.index
    ax.plot(x, df["close"], linewidth=1.0)
    for i in range(len(df)):
        ax.vlines(x[i], df["low"].iloc[i], df["high"].iloc[i], linewidth=0.5)
        ax.hlines([df["open"].iloc[i], df["close"].iloc[i]], x[i], x[i], linewidth=3)

    # Markers
    for m in markers:
        marker = "^" if m.kind == "ENTRY" else "v"
        ax.scatter(m.t, m.px, marker=marker, s=100)
        ax.annotate(
            f"{m.kind[:1]}-{m.pos[0].upper()}",
            (m.t, m.px),
            xytext=(0, 8 if m.kind=="ENTRY" else -12),
            textcoords="offset points",
            fontsize=8,
            ha="center",
        )

    ax.set_title(title)
    ax.set_xlabel("Time")
    ax.set_ylabel("Price")
    fig.autofmt_xdate()

    if save_path:
        plt.savefig(save_path, dpi=120, bbox_inches="tight")
    else:
        plt.show()

def export_markers_csv(markers: List[Marker], path: str):
    rows = [{
        "time": m.t.to_pydatetime().isoformat(),
        "price": m.px,
        "kind": m.kind,
        "position": m.pos,
        "side": m.side,
    } for m in markers]
    pd.DataFrame(rows).to_csv(path, index=False)

def verify_against_db(
    *,
    markers: List[Marker],
    bot_id: int,
    user_id: int,
    symbol: str,
    start: datetime,
    end: datetime,
    time_tolerance_sec: int = 90,
    price_tolerance_abs: float = 0.05,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Compares replay markers to actual trade rows for the same bot/user/symbol & time range.
    - Returns (matches_df, mismatches_df).
    - Matching rule: nearest trade within `time_tolerance_sec` and |price diff| <= tolerance.
    """
    session = SessionLocal()
    try:
        q = (
            session.query(Trade)
            .filter(Trade.user_id == user_id)
            .filter(Trade.bot_id == bot_id)
            .filter(Trade.symbol == symbol.upper())
            .filter(Trade.created_at >= start)
            .filter(Trade.created_at <= end)
            .order_by(Trade.created_at.asc())
        )
        db_trades = q.all()

        db_rows = []
        for tr in db_trades:
            db_rows.append({
                "db_time": tr.created_at.replace(tzinfo=timezone.utc).astimezone(tz=None),
                "db_side": tr.side,              # "BUY"/"SELL"
                "db_price": float(tr.price),
            })
        db_df = pd.DataFrame(db_rows)

        out_rows = []
        for m in markers:
            mt = m.t.to_pydatetime()
            # find nearest db row matching side within time window
            cand = db_df[db_df["db_side"] == m.side]
            if cand.empty:
                out_rows.append({
                    "marker_time": mt, "marker_side": m.side, "marker_price": m.px,
                    "match": False, "reason": "no side match", "db_time": None, "db_price": None, "time_diff_s": None, "price_diff": None
                })
                continue
            # nearest by time
            cand["time_diff_s"] = cand["db_time"].apply(lambda dt: abs((dt - mt).total_seconds()))
            row = cand.sort_values("time_diff_s").head(1)
            if row.empty or row["time_diff_s"].iloc[0] > time_tolerance_sec:
                out_rows.append({
                    "marker_time": mt, "marker_side": m.side, "marker_price": m.px,
                    "match": False, "reason": "no time-near db trade", "db_time": None, "db_price": None,
                    "time_diff_s": None, "price_diff": None
                })
                continue
            time_diff = float(row["time_diff_s"].iloc[0])
            db_price = float(row["db_price"].iloc[0])
            price_diff = abs(db_price - m.px)
            match = price_diff <= price_tolerance_abs
            out_rows.append({
                "marker_time": mt,
                "marker_side": m.side,
                "marker_price": m.px,
                "db_time": row["db_time"].iloc[0],
                "db_price": db_price,
                "time_diff_s": time_diff,
                "price_diff": price_diff,
                "match": match,
                "reason": "" if match else "price out of tolerance",
            })

        out = pd.DataFrame(out_rows)
        matches = out[out["match"]]
        mismatches = out[~out["match"]]
        return matches, mismatches
    finally:
        session.close()
