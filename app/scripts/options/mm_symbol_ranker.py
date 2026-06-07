#!/usr/bin/env python3
"""Rank symbols for AlgoMM using realized live trade history.

Input format expected from the uploaded log:
#514\tMU\tLong\t100\t$420.13\t2026-01-28 09:30:14\t$427.30\t2026-01-28 09:32:50\t+$717.00
"""

from __future__ import annotations

import argparse
import math
import re
from pathlib import Path

import pandas as pd

TRADE_RE = re.compile(
    r"^#(?P<bot_id>\d+)\t(?P<symbol>[A-Z.]+)\t(?P<side>Long|Short)\t(?P<qty>\d+)\t"
    r"\$(?P<entry>[\d.]+)\t(?P<entry_ts>[\d\-: ]+)\t\$(?P<exit>[\d.]+)\t"
    r"(?P<exit_ts>[\d\-: ]+)\t(?P<pnl>[+\-]?\$?[\d.,]+)"
)


def parse_trade_log(path: str | Path) -> pd.DataFrame:
    rows = []
    for line in Path(path).read_text(errors="ignore").splitlines():
        m = TRADE_RE.match(line.strip())
        if not m:
            continue
        d = m.groupdict()
        pnl_txt = d["pnl"].replace("$", "").replace(",", "")
        rows.append(
            {
                "bot_id": int(d["bot_id"]),
                "symbol": d["symbol"],
                "side": d["side"],
                "qty": int(d["qty"]),
                "entry_price": float(d["entry"]),
                "exit_price": float(d["exit"]),
                "entry_ts": pd.to_datetime(d["entry_ts"]),
                "exit_ts": pd.to_datetime(d["exit_ts"]),
                "pnl": float(pnl_txt),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError(f"No trades parsed from {path}")
    df["won"] = (df["pnl"] > 0).astype(int)
    df["hold_min"] = (df["exit_ts"] - df["entry_ts"]).dt.total_seconds() / 60.0
    return df.sort_values("entry_ts").reset_index(drop=True)


def summarize_symbols(df: pd.DataFrame, recent_days: int = 20) -> pd.DataFrame:
    cutoff = df["entry_ts"].max() - pd.Timedelta(days=recent_days)
    recent = df[df["entry_ts"] >= cutoff].copy()

    records = []
    for symbol, g in df.groupby("symbol"):
        wins = g.loc[g["pnl"] > 0, "pnl"]
        losses = g.loc[g["pnl"] < 0, "pnl"]
        recent_g = recent[recent["symbol"] == symbol]

        gross_win = float(wins.sum())
        gross_loss = float(-losses.sum())
        pf = gross_win / gross_loss if gross_loss > 0 else math.inf
        expectancy = float(g["pnl"].mean())
        stability = float(recent_g["pnl"].mean()) if not recent_g.empty else expectancy
        score = (0.45 * expectancy) + (25.0 * min(pf, 3.0)) + (15.0 * g["won"].mean()) + (0.20 * stability)

        records.append(
            {
                "symbol": symbol,
                "trades": len(g),
                "win_rate": round(float(g["won"].mean()), 4),
                "net_pnl": round(float(g["pnl"].sum()), 2),
                "avg_pnl": round(expectancy, 2),
                "profit_factor": round(float(pf), 3) if math.isfinite(pf) else 99.0,
                "avg_hold_min": round(float(g["hold_min"].mean()), 2),
                "recent_avg_pnl": round(float(stability), 2),
                "score": round(float(score), 2),
            }
        )

    out = pd.DataFrame(records).sort_values(["score", "net_pnl"], ascending=False)
    return out.reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("logfile", help="Path to AlgoMM live trade log export")
    ap.add_argument("--recent-days", type=int, default=20)
    ap.add_argument("--csv", default="")
    args = ap.parse_args()

    df = parse_trade_log(args.logfile)
    ranked = summarize_symbols(df, recent_days=args.recent_days)
    print(ranked.to_string(index=False))
    if args.csv:
        ranked.to_csv(args.csv, index=False)
        print(f"\nSaved: {args.csv}")


if __name__ == "__main__":
    main()
