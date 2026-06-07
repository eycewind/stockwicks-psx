# app/scripts/algo2_replay.py
from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
import pytz

from app.utils.stock.indicators import compute_atr
from app.utils.stock import schwab_price_history as sph

ET = pytz.timezone("US/Eastern")


# ---------- ATR Trailing Stops (same logic as Algo2 runner) ----------
def compute_atr_trailing_stops(
    df: pd.DataFrame,
    period: int = 21,
    mult: float = 3.0,
    use_highlow: bool = False,
) -> tuple[pd.Series, pd.Series]:
    """
    Returns (buy_stop, sell_stop):
      buy_stop (green) trails below price in uptrends
      sell_stop (red)  trails above price in downtrends
    """
    close = df["close"].astype(float)
    high  = df["high"].astype(float)
    low   = df["low"].astype(float)
    atr   = compute_atr(df, period=period).astype(float)

    base = (high + low) / 2.0 if use_highlow else close

    basic_upper = base + mult * atr
    basic_lower = base - mult * atr

    upper = pd.Series(index=df.index, dtype=float)  # trailing sell stop (red)
    lower = pd.Series(index=df.index, dtype=float)  # trailing buy stop (green)
    trend = pd.Series(index=df.index, dtype=int)    # +1 uptrend, -1 downtrend

    prev_upper = np.nan
    prev_lower = np.nan
    prev_trend = 0

    for i, ts in enumerate(df.index):
        bu = float(basic_upper.iloc[i])
        bl = float(basic_lower.iloc[i])
        px = float(close.iloc[i])

        if i == 0:
            upper.iloc[i] = bu
            lower.iloc[i] = bl
            trend.iloc[i] = 0
            prev_upper, prev_lower, prev_trend = bu, bl, 0
            continue

        u = bu
        l = bl
        t = prev_trend

        if prev_trend >= 0:  # uptrend/neutral
            l = max(bl, prev_lower if not np.isnan(prev_lower) else -np.inf)
            if px < l:       # flip to downtrend
                t = -1
                u = bu
                l = np.nan
            else:
                t = +1
                u = np.nan
        else:                # downtrend
            u = min(bu, prev_upper if not np.isnan(prev_upper) else np.inf)
            if px > u:       # flip to uptrend
                t = +1
                l = bl
                u = np.nan
            else:
                t = -1
                l = np.nan

        upper.iloc[i] = u
        lower.iloc[i] = l
        trend.iloc[i] = t

        prev_upper, prev_lower, prev_trend = u, l, t

    return lower, upper  # (buy_stop, sell_stop)


# ---------- Cross helpers (with lookback) ----------
def crossed_above(price: pd.Series, level: pd.Series, lookback: int) -> bool:
    """
    Detect price crossing above trailing level within lookback bars.
    Uses the PREVIOUS level only (level at t-1), because on a true flip
    the current bar's opposite stop is often NaN.
    """
    if level is None or price is None or len(price) < 2:
        return False
    lb = min(lookback, len(price) - 1)
    for k in range(1, lb + 1):
        p_prev = float(price.iloc[-(k + 1)])
        p_curr = float(price.iloc[-k])
        l_prev = level.iloc[-(k + 1)]
        if np.isnan(l_prev):
            continue
        if p_prev <= l_prev and p_curr > l_prev:
            return True
    return False

def crossed_below(price: pd.Series, level: pd.Series, lookback: int) -> bool:
    """
    Detect price crossing below trailing level within lookback bars.
    Uses the PREVIOUS level only (level at t-1), for the same reason.
    """
    if level is None or price is None or len(price) < 2:
        return False
    lb = min(lookback, len(price) - 1)
    for k in range(1, lb + 1):
        p_prev = float(price.iloc[-(k + 1)])
        p_curr = float(price.iloc[-k])
        l_prev = level.iloc[-(k + 1)]
        if np.isnan(l_prev):
            continue
        if p_prev >= l_prev and p_curr < l_prev:
            return True
    return False


@dataclass
class Trade:
    side: str          # "long" or "short"
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None

    def closed(self) -> bool:
        return self.exit_time is not None

    def pnl(self, qty: float = 1.0) -> float:
        if not self.closed():
            return 0.0
        if self.side == "long":
            return (self.exit_price - self.entry_price) * qty
        else:
            return (self.entry_price - self.exit_price) * qty


def backtest_atr_trailing(
    df: pd.DataFrame,
    atr_period: int = 21,
    atr_mult: float = 3.0,
    lookback: int = 3,
    use_highlow: bool = False,
    qty: float = 1.0,
):
    buy_stop, sell_stop = compute_atr_trailing_stops(df, period=atr_period, mult=atr_mult, use_highlow=use_highlow)

    close = df["close"].astype(float)
    trades: list[Trade] = []
    pos: Optional[Trade] = None

    for i in range(max(atr_period + 5, 50), len(df)):
        window = df.iloc[:i].copy()
        px = float(window["close"].iloc[-1])

        bs = buy_stop.iloc[:i]
        ss = sell_stop.iloc[:i]
        cc = window["close"]

        # Signals with lookback
        long_entry  = crossed_above(cc, ss, lookback)   # cross above sell stop
        short_entry = crossed_below(cc, bs, lookback)   # cross below buy stop
        long_exit   = crossed_below(cc, bs, lookback)   # long exits on cross below buy stop
        short_exit  = crossed_above(cc, ss, lookback)   # short exits on cross above sell stop

        tstamp = window.index[-1]

        if pos is None:
            if long_entry:
                pos = Trade(side="long", entry_time=tstamp, entry_price=px)
                trades.append(pos)
                print(f"[ENTRY LONG]  {tstamp} px={px:.4f}")
            elif short_entry:
                pos = Trade(side="short", entry_time=tstamp, entry_price=px)
                trades.append(pos)
                print(f"[ENTRY SHORT] {tstamp} px={px:.4f}")
        else:
            if pos.side == "long" and long_exit:
                pos.exit_time = tstamp
                pos.exit_price = px
                print(f"[EXIT  LONG]  {tstamp} px={px:.4f}  PnL={pos.pnl(qty):.4f}")
                pos = None
            elif pos.side == "short" and short_exit:
                pos.exit_time = tstamp
                pos.exit_price = px
                print(f"[COVER SHORT] {tstamp} px={px:.4f}  PnL={pos.pnl(qty):.4f}")
                pos = None

    # If position still open, mark notional exit at last bar (optional)
    if pos is not None:
        pos.exit_time = df.index[-1]
        pos.exit_price = float(df["close"].iloc[-1])
        print(f"[FORCE EXIT]   {pos.exit_time} px={pos.exit_price:.4f}  PnL={pos.pnl(qty):.4f}")

    # Summary
    closed = [t for t in trades if t.closed()]
    wins = sum(1 for t in closed if t.pnl(qty) > 0)
    total_pnl = sum(t.pnl(qty) for t in closed)
    print(f"\n[SUMMARY] trades={len(trades)} closed={len(closed)} wins={wins} "
          f"winrate={(wins/len(closed)*100.0 if closed else 0):.1f}% "
          f"total_pnl={total_pnl:.4f} (qty={qty})")

def main():
    ap = argparse.ArgumentParser(description="Replay/backtest ATR Trailing Stop Algo2 (cross with lookback).")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--interval", default="1min", choices=["1min","5min","15min","30min","1d"])
    ap.add_argument("--days", type=int, default=5, help="History depth for Schwab fetch")
    ap.add_argument("--atr-period", type=int, default=21)
    ap.add_argument("--atr-mult", type=float, default=3.0)
    ap.add_argument("--lookback", type=int, default=3)
    ap.add_argument("--use-highlow", action="store_true", help="Base ATR bands on (H+L)/2 instead of Close")
    ap.add_argument("--qty", type=float, default=1.0)
    args = ap.parse_args()

    fetch = {
        "1min": sph.get_schwab_1min,
        "5min": sph.get_schwab_5min,
        "15min": sph.get_schwab_15min,
        "30min": sph.get_schwab_30min,
        "1d": sph.get_schwab_daily,
    }[args.interval]

    df = fetch(args.symbol.upper(), period=args.days)
    if df is None or df.empty:
        print("No data")
        return

    print(f"[DATA] {args.symbol.upper()}@{args.interval} rows={len(df)} latest={df.index[-1]}")
    backtest_atr_trailing(
        df,
        atr_period=args.atr_period,
        atr_mult=args.atr_mult,
        lookback=args.lookback,
        use_highlow=args.use_highlow,
        qty=args.qty,
    )

if __name__ == "__main__":
    main()
