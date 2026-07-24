#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/replay/data_ingest.py
"""
Replay Data Ingester
====================

Downloads N days of 1-minute bars for a symbol via Schwab and saves to:

    {DATA_DIR}/{user_id}/replay/{SYMBOL}_1min.csv
    {DATA_DIR}/{user_id}/replay/{SYMBOL}_1min.meta.json

One file per (user, symbol). Intervals 5/10/15/1D are derived on the fly
by ReplayDataProvider — no separate files per interval.

Idempotent: if the file exists and was downloaded < FRESH_HOURS ago,
re-use it unless --force is passed.

CLI
---
    python -m app.scripts.replay.data_ingest --user-id 116 --symbol AAPL
    python -m app.scripts.replay.data_ingest --user-id 116 --symbol AAPL --days 30 --force
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import pytz

from app.scripts.stock_algos.base_wiring import get_schwab_history


# =============================================================================
# Config
# =============================================================================
ET = pytz.timezone("US/Eastern")
UTC = pytz.UTC

DATA_DIR = os.getenv("DATA_DIR", "/var/www/stockwicks/data")
MAX_DAYS = 120
DEFAULT_DAYS = 30
FRESH_HOURS = 6            # reuse cache if younger than this

logger = logging.getLogger("ReplayDataIngest")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [ReplayIngest] %(message)s"
    ))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# =============================================================================
# Paths
# =============================================================================
def get_replay_dir(user_id: int) -> Path:
    """
    Returns {DATA_DIR}/{user_id}/replay/, creating it if needed.
    """
    p = Path(DATA_DIR) / str(user_id) / "replay"
    p.mkdir(parents=True, exist_ok=True)
    return p


def safe_symbol_for_files(symbol: str) -> str:
    return str(symbol or "").upper().strip().replace("/", "_").replace(" ", "_")


def get_data_paths(user_id: int, symbol: str, interval: str = "1min") -> tuple[Path, Path]:
    """
    Returns (csv_path, meta_path) for a given user+symbol.
    """
    d = get_replay_dir(user_id)
    sym = safe_symbol_for_files(symbol)
    interval = str(interval or "1min").lower().strip()
    return d / f"{sym}_{interval}.csv", d / f"{sym}_{interval}.meta.json"


# =============================================================================
# Freshness / metadata
# =============================================================================
@dataclass
class IngestMeta:
    symbol: str
    user_id: int
    days_requested: int
    downloaded_at: str       # UTC ISO
    first_bar: str           # ET ISO (naive)
    last_bar: str            # ET ISO (naive)
    total_rows: int
    unique_dates: list[str]  # sorted
    source_mode: str = "legacy"

    def to_dict(self) -> dict:
        return asdict(self)


def _load_meta(meta_path: Path) -> Optional[IngestMeta]:
    if not meta_path.exists():
        return None
    try:
        d = json.loads(meta_path.read_text())
        return IngestMeta(**d)
    except Exception as e:
        logger.warning(f"Could not read meta {meta_path}: {e}")
        return None


def is_fresh(
    meta: Optional[IngestMeta],
    max_age_hours: int = FRESH_HOURS,
    minimum_days: int = 1,
) -> bool:
    if meta is None:
        return False
    if getattr(meta, "source_mode", "legacy") != "live_rth_v1":
        return False
    try:
        dl_at = datetime.fromisoformat(meta.downloaded_at)
        return (
            int(getattr(meta, "days_requested", 0) or 0) >= int(minimum_days)
            and (datetime.utcnow() - dl_at) < timedelta(hours=max_age_hours)
        )
    except Exception:
        return False


# =============================================================================
# Fetch
# =============================================================================
def fetch_and_save(
    user_id: int,
    symbol: str,
    days: int = DEFAULT_DAYS,
    force: bool = False,
    interval: str = "1min",
) -> tuple[Path, IngestMeta]:
    """
    Download `days` days of 1-min bars and cache to disk.

    Returns (csv_path, meta).
    Raises on empty/failed download.
    """
    symbol = symbol.upper().strip()
    interval = str(interval or "1min").lower().strip()
    if interval not in {"1min", "5min", "10min", "15min", "30min"}:
        raise ValueError(f"Unsupported replay ingest interval: {interval}")
    days = max(1, min(int(days), MAX_DAYS))

    csv_path, meta_path = get_data_paths(user_id, symbol, interval)
    existing = _load_meta(meta_path)

    if not force and is_fresh(existing, minimum_days=days) and csv_path.exists():
        logger.info(
            f"[{symbol}] Using cached data "
            f"({existing.total_rows} rows, downloaded {existing.downloaded_at}Z)"
        )
        return csv_path, existing

    logger.info(f"[{symbol}] Fetching {days} trading sessions of {interval} bars from Schwab...")
    # Use the same source fetch path as live AlgoMM bots. This keeps replay
    # candles aligned with live candles before the replay engine evaluates them.
    df = get_schwab_history(
        symbol=symbol,
        interval=interval,
        lookback_days=days,
        need_extended_hours=False,
    )

    if df is None or df.empty:
        raise RuntimeError(f"Schwab returned no data for {symbol}")

    # --- Normalize to ET tz-aware, then convert to ET tz-naive for storage ---
    if df.index.tz is None:
        # assume UTC if naive (Schwab returns epoch ms which _to_ohlcv_frame
        # typically already made UTC — this is a safety net)
        df.index = df.index.tz_localize(UTC).tz_convert(ET)
    else:
        df.index = df.index.tz_convert(ET)

    # Keep only the columns we care about
    needed = ["open", "high", "low", "close", "volume"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise RuntimeError(
            f"Schwab response missing columns {missing} for {symbol}. "
            f"Got: {list(df.columns)}"
        )
    df = df[needed].sort_index()
    df = df[~df.index.duplicated(keep="last")]

    # Store as naive ET — simpler to round-trip than tz-aware CSV
    df_out = df.copy()
    df_out.index = df_out.index.tz_localize(None)
    df_out.index.name = "timestamp"
    df_out.to_csv(csv_path)

    # Metadata
    meta = IngestMeta(
        symbol=symbol,
        user_id=int(user_id),
        days_requested=days,
        downloaded_at=datetime.utcnow().isoformat(),
        first_bar=df_out.index[0].isoformat(),
        last_bar=df_out.index[-1].isoformat(),
        total_rows=int(len(df_out)),
        unique_dates=sorted({str(d) for d in df_out.index.date}),
        source_mode="live_rth_v1",
    )
    meta_path.write_text(json.dumps(meta.to_dict(), indent=2, default=str))

    logger.info(
        f"[{symbol}] Saved {meta.total_rows} bars "
        f"({meta.first_bar} → {meta.last_bar}, "
        f"{len(meta.unique_dates)} trading days) to {csv_path}"
    )
    return csv_path, meta


# =============================================================================
# CLI
# =============================================================================
def main():
    p = argparse.ArgumentParser(description="Download 1-min replay data for a symbol.")
    p.add_argument("--user-id", type=int, required=True)
    p.add_argument("--symbol", required=True)
    p.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help=f"Days of history (max {MAX_DAYS})")
    p.add_argument("--force", action="store_true",
                   help="Re-download even if fresh cache exists")
    args = p.parse_args()

    csv_path, meta = fetch_and_save(
        user_id=args.user_id,
        symbol=args.symbol,
        days=args.days,
        force=args.force,
    )

    print(f"\n  CSV : {csv_path}")
    print(f"  Meta:")
    for k, v in meta.to_dict().items():
        if k == "unique_dates":
            print(f"    {k:<16} = [{len(v)} days] {v[0]}..{v[-1]}" if v else f"    {k}: (none)")
        else:
            print(f"    {k:<16} = {v}")


if __name__ == "__main__":
    main()
