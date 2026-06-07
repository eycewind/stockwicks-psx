#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/replay/replay_data_provider.py
"""
ReplayDataProvider
==================

Loads the 1-min CSV produced by data_ingest.py, filters to a date range,
and resamples to the requested interval (1min / 5min / 10min / 15min / 1d).

This is what the orchestrator uses to walk bars during a replay session.

Typical usage (inside orchestrator)
-----------------------------------
    provider = ReplayDataProvider(
        user_id=116, symbol="AAPL",
        start_date="2026-03-20", end_date="2026-04-18",
        interval="5min",
    )

    for i in range(provider.total_bars):
        bar_time = provider.bar_time(i)
        df_so_far = provider.bars_up_to(i)     # what the algo sees as "history"
        run_algoMM_replay_tick(session_id, anchor_dt=bar_time, df=df_so_far)
        sleep(provider.tick_seconds(speed))

CLI (for quick inspection)
--------------------------
    python -m app.scripts.replay.replay_data_provider \
        --user-id 116 --symbol AAPL \
        --start 2026-03-20 --end 2026-04-18 --interval 5min
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Optional

import pandas as pd
import pytz


# =============================================================================
# Config
# =============================================================================
ET = pytz.timezone("US/Eastern")
DATA_DIR = os.getenv("DATA_DIR", "/var/www/stockwicks/data")

# Supported intervals and their pandas resample rules
_INTERVAL_RULES = {
    "1min":  "1min",
    "5min":  "5min",
    "10min": "10min",
    "15min": "15min",
    "1d":    "1D",
}

# Seconds per bar for pacing (orchestrator uses this for sleep time)
_INTERVAL_SECONDS = {
    "1min":  60,
    "5min":  300,
    "10min": 600,
    "15min": 900,
    "1d":    6.5 * 3600,   # ~6.5h of RTH per "bar" for 1d
}

logger = logging.getLogger("ReplayDataProvider")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [ReplayProvider] %(message)s"
    ))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# =============================================================================
# Provider
# =============================================================================
class ReplayDataProvider:
    """
    Loads 1-min CSV → filters by date range → resamples to target interval.

    All bar timestamps are exposed as timezone-NAIVE ET pandas Timestamps, so
    they match what the existing paper_trading models store. The orchestrator
    writes these same timestamps to replay_open_trades.entry_time etc.
    """

    def __init__(
        self,
        user_id: int,
        symbol: str,
        start_date: str,    # YYYY-MM-DD
        end_date: str,      # YYYY-MM-DD (inclusive)
        interval: str = "5min",
        rth_only: bool = True,
    ):
        self.user_id = int(user_id)
        self.symbol = symbol.upper().strip()
        self.start_date = start_date
        self.end_date = end_date
        self.interval = interval.lower().strip()
        self.rth_only = bool(rth_only)

        if self.interval not in _INTERVAL_RULES:
            raise ValueError(
                f"Unsupported interval '{interval}'. "
                f"Supported: {list(_INTERVAL_RULES)}"
            )

        self.csv_path = self._csv_path()
        if not self.csv_path.exists():
            raise FileNotFoundError(
                f"No replay data for {self.symbol} (user {self.user_id}).\n"
                f"  Expected: {self.csv_path}\n"
                f"  Fix: python -m app.scripts.replay.data_ingest "
                f"--user-id {self.user_id} --symbol {self.symbol}"
            )

        self.bars: pd.DataFrame = self._load_filter_resample()

    # ------------------------------------------------------------ paths
    def _csv_path(self) -> Path:
        return Path(DATA_DIR) / str(self.user_id) / "replay" / f"{self.symbol}_1min.csv"

    # --------------------------------------------------- load + transform
    def _load_filter_resample(self) -> pd.DataFrame:
        # 1) Load CSV (naive ET timestamps, per data_ingest.py)
        df = pd.read_csv(
            self.csv_path,
            parse_dates=["timestamp"],
            index_col="timestamp",
        )

        # If somehow tz-aware, drop tz; we're working in naive ET throughout
        if getattr(df.index, "tz", None) is not None:
            df.index = df.index.tz_convert(ET).tz_localize(None)

        # 2) Date-range filter (inclusive on both ends)
        start_ts = pd.Timestamp(self.start_date)
        end_ts = (
            pd.Timestamp(self.end_date)
            + pd.Timedelta(days=1)
            - pd.Timedelta(seconds=1)
        )
        df = df[(df.index >= start_ts) & (df.index <= end_ts)]
        if df.empty:
            raise ValueError(
                f"No 1-min bars between {self.start_date} and {self.end_date} "
                f"in {self.csv_path}. Check that the ingester grabbed this range."
            )

        # 3) RTH filter (skip for 1d since we're aggregating the whole day anyway)
        if self.rth_only and self.interval != "1d":
            df = df.between_time("09:30", "16:00")
            if df.empty:
                raise ValueError(
                    "No RTH bars in the selected date range. "
                    "Pass rth_only=False if you want extended hours."
                )

        # 4) Resample
        rule = _INTERVAL_RULES[self.interval]
        if self.interval == "1min":
            out = df.copy()
        else:
            agg = {
                "open":   "first",
                "high":   "max",
                "low":    "min",
                "close":  "last",
                "volume": "sum",
            }
            out = (
                df.resample(rule, label="left", closed="left")
                  .agg(agg)
                  .dropna(subset=["open"])
            )

        out = out.sort_index()
        out = out[~out.index.duplicated(keep="last")]

        logger.info(
            f"{self.symbol} {self.interval} {self.start_date}→{self.end_date}: "
            f"{len(out)} bars "
            f"({out.index[0]} → {out.index[-1]})"
        )
        return out

    # ------------------------------------------------------------ access
    @property
    def total_bars(self) -> int:
        return int(len(self.bars))

    def bar_time(self, idx: int) -> pd.Timestamp:
        """Timestamp at index idx (naive ET)."""
        if idx < 0:
            idx = len(self.bars) + idx
        return self.bars.index[idx]

    def bars_up_to(self, idx: int) -> pd.DataFrame:
        """
        Slice of bars from start up to and including index idx.
        This is what the algo 'sees' as its history at tick idx.
        """
        return self.bars.iloc[: idx + 1]

    def tick_seconds(self, speed: float) -> float:
        """
        How long the orchestrator should sleep between ticks given a speed
        multiplier. E.g. 5-min bars at 20x = 15 seconds per tick.
        """
        base = _INTERVAL_SECONDS.get(self.interval, 60)
        s = float(speed) if speed and speed > 0 else 1.0
        return max(0.1, base / s)

    def __repr__(self) -> str:
        return (
            f"<ReplayDataProvider {self.symbol} {self.interval} "
            f"{self.start_date}→{self.end_date} bars={self.total_bars}>"
        )


# =============================================================================
# CLI
# =============================================================================
def main():
    p = argparse.ArgumentParser(description="Inspect a replay data range.")
    p.add_argument("--user-id", type=int, required=True)
    p.add_argument("--symbol", required=True)
    p.add_argument("--start", required=True, help="YYYY-MM-DD (inclusive)")
    p.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive)")
    p.add_argument("--interval", default="5min",
                   choices=list(_INTERVAL_RULES.keys()))
    p.add_argument("--speed", type=float, default=1.0,
                   help="Inspect tick pacing at this speed")
    args = p.parse_args()

    provider = ReplayDataProvider(
        user_id=args.user_id,
        symbol=args.symbol,
        start_date=args.start,
        end_date=args.end,
        interval=args.interval,
    )

    print(f"\n  {provider}")
    print(f"\n  Total bars : {provider.total_bars}")
    print(f"  First bar  : {provider.bar_time(0)}")
    print(f"  Last bar   : {provider.bar_time(-1)}")
    print(f"  Unique days: {provider.bars.index.normalize().nunique()}")
    print(f"  Tick pace  : {provider.tick_seconds(args.speed):.2f}s @ speed={args.speed}x")

    print("\n  HEAD")
    print(provider.bars.head().to_string())
    print("\n  TAIL")
    print(provider.bars.tail().to_string())


if __name__ == "__main__":
    main()