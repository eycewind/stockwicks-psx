# app/scripts/validate_history_pipeline.py
import argparse
import sys
from typing import Optional
import numpy as np
import pandas as pd
import pytz
from datetime import datetime, timedelta

from app.utils.stock import schwab_price_history as sph
from app.utils.stock.indicators import compute_smi, compute_smi_blau  # <- uses both

_ET = pytz.timezone("US/Eastern")

def _ok(df: Optional[pd.DataFrame]) -> bool:
    return df is not None and isinstance(df, pd.DataFrame) and not df.empty

def normalize_from_csv(path: str) -> pd.DataFrame:
    """
    Load CSV (columns can be either: timestamp,open,high,low,close,volume
    or Schwab 'datetime' in ms). Returns tz-aware ET index with O/H/L/C/Vol.
    """
    df = pd.read_csv(path)
    if "timestamp" in df.columns:
        ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        if ts.dt.tz is None:
            ts = ts.dt.tz_localize("UTC")
        ts = ts.dt.tz_convert(_ET)
    elif "datetime" in df.columns:
        ts = pd.to_datetime(df["datetime"], unit="ms", utc=True).dt.tz_convert(_ET)
    else:
        raise SystemExit("CSV must have 'timestamp' or 'datetime' column")

    cols = ["open", "high", "low", "close", "volume"]
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise SystemExit(f"CSV missing columns: {missing}")

    out = df.copy()
    out.index = ts
    out = out[cols]
    for c in cols:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=["open", "high", "low", "close"])
    out.index.name = "timestamp"
    return out.sort_index()

def validate_df(df: pd.DataFrame, interval: str, label: str, smi_mode: str = "legacy"):
    print(f"\n[VALIDATE] {label} interval={interval} rows={len(df)} tz={df.index.tz}")

    if str(df.index.tz) != "US/Eastern":
        print("[WARN] index tz != US/Eastern")

    for c in ["open", "high", "low", "close", "volume"]:
        if c not in df.columns:
            print(f"[ERR] missing column {c}")
        elif not np.issubdtype(df[c].dtype, np.number):
            print(f"[ERR] column {c} not numeric: {df[c].dtype}")

    if not df.index.is_monotonic_increasing:
        print("[WARN] index not monotonic increasing; sorting")
        df.sort_index(inplace=True)
    dup = df.index.duplicated().sum()
    if dup:
        print(f"[WARN] found {dup} duplicate timestamps")

    latest = df.index[-1]
    now_et = datetime.now(_ET)
    age = now_et - latest
    max_age = {
        "1min": timedelta(minutes=3),
        "5min": timedelta(minutes=12),
        "15min": timedelta(minutes=35),
        "30min": timedelta(minutes=70),
        "1d": timedelta(days=3),
    }.get(interval, timedelta(minutes=10))
    print(f"[AGE] latest={latest} age={age} stale={age>max_age}")

    # Indicator quick check
    try:
        if smi_mode == "blau":
            k, d = compute_smi_blau(df, length=10, r=3, s=3, sig=10, ma="ema")
            print("[SMI-K] last5:", [round(float(x), 2) for x in k.tail(5).tolist()])
            if d is not None:
                print("[SMI-D] last5:", [round(float(x), 2) for x in d.tail(5).tolist()])
        else:
            smi = compute_smi(df)
            if smi is None or len(smi) < 5:
                print("[SMI] not enough points")
            else:
                print("[SMI] last5:", [round(float(x), 2) for x in smi.tail(5).tolist()])
    except Exception as e:
        print(f"[SMI] compute failed: {e}")

def resample_from_1min(min1: pd.DataFrame, target: str) -> pd.DataFrame:
    """
    Canonical resample: right-closed 5/15/30-min bars.
    Drop the last INCOMPLETE bucket so we match native completed bars.
    """
    rule_map = {"5min": "5min", "15min": "15min", "30min": "30min"}
    rule = rule_map[target]
    agg = {"open":"first", "high":"max", "low":"min", "close":"last", "volume":"sum"}

    out = (
        min1.resample(rule, label="right", closed="right")
            .agg(agg)
            .dropna(subset=["open","high","low","close"])
    )

    # Clip to the last FULL right-edge so we don’t include an incomplete bar
    latest_1m = min1.index[-1]
    last_full_right = latest_1m.floor(rule)
    out = out[out.index <= last_full_right]
    return out

def compare_5min(native5: pd.DataFrame, from1: pd.DataFrame, tolerance_price=1e-4, tolerance_vol=1):
    # Align on intersection of timestamps to avoid comparing apples-to-oranges
    idx = native5.index.intersection(from1.index)
    if len(idx) == 0:
        print("[DIFF] No overlapping timestamps after alignment.")
        return
    left = native5.reindex(idx)
    right = from1.reindex(idx)

    both = pd.concat([left, right], axis=1, keys=["L","R"]).dropna()
    if both.empty:
        print("[DIFF] No overlapping non-NaN rows after alignment.")
        return

    for col in ["open","high","low","close"]:
        diff = (both["L",col] - both["R",col]).abs()
        nbad = int((diff > tolerance_price).sum())
        mx = float(diff.max()) if len(diff) else 0.0
        print(f"[DIFF] {col}: max_abs_diff={mx:.6f} bad={nbad}/{len(diff)}")

    vdiff = (both["L","volume"] - both["R","volume"]).abs()
    nbad_v = int((vdiff > tolerance_vol).sum())
    mxv = float(vdiff.max()) if len(vdiff) else 0.0
    print(f"[DIFF] volume: max_abs_diff={mxv:.2f} bad={nbad_v}/{len(vdiff)}")



def compare_series(native5: pd.DataFrame, from1_5: pd.DataFrame,
                   tolerance_price=1e-4, tolerance_vol=1):
    # Align on intersection of timestamps
    idx = native5.index.intersection(from1_5.index)
    if len(idx) == 0:
        print("[DIFF] No overlapping timestamps after alignment.")
        return
    left = native5.reindex(idx)
    right = from1_5.reindex(idx)
    both = pd.concat([left, right], axis=1, keys=["L","R"]).dropna()
    if both.empty:
        print("[DIFF] No overlapping non-NaN rows after alignment.")
        return

    for col in ["open","high","low","close"]:
        diff = (both["L",col] - both["R",col]).abs()
        nbad = int((diff > tolerance_price).sum())
        mx = float(diff.max()) if len(diff) else 0.0
        print(f"[DIFF] {col}: max_abs_diff={mx:.6f} bad={nbad}/{len(diff)}")

    vdiff = (both["L","volume"] - both["R","volume"]).abs()
    nbad_v = int((vdiff > tolerance_vol).sum())
    mxv = float(vdiff.max()) if len(vdiff) else 0.0
    print(f"[DIFF] volume: max_abs_diff={mxv:.2f} bad={nbad_v}/{len(vdiff)}")

def main():
    ap = argparse.ArgumentParser(description="Validate Schwab history parsing & resampling.")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--save1", help="Save cleaned 1min to CSV")
    ap.add_argument("--save5", help="Save cleaned 5min (native) to CSV")
    ap.add_argument("--from-csv-1m", help="Optional: path to a previously saved 1m CSV")
    ap.add_argument("--from-csv-5m", help="Optional: path to a previously saved 5m CSV")
    ap.add_argument("--days1", type=int, default=7, help="days back for 1m pull")
    ap.add_argument("--days5", type=int, default=10, help="days back for 5m pull")
    ap.add_argument("--smi", choices=["legacy","blau"], default="legacy",
                    help="which SMI to print in validation output")
    args = ap.parse_args()

    symbol = args.symbol.upper()

    # 1m
    if args.from_csv_1m:
        m1 = normalize_from_csv(args.from_csv_1m)
        print(f"[LOAD] 1m from CSV: {args.from_csv_1m}")
    else:
        m1 = sph.get_schwab_1min(symbol, period=args.days1)
        if not _ok(m1):
            print("[ERR] No 1m data from Schwab")
            sys.exit(2)
        print(f"[FETCH] 1m Schwab rows={len(m1)} latest={m1.index[-1]}")
    validate_df(m1, "1min", "1m", smi_mode=args.smi)

    # 5m native
    if args.from_csv_5m:
        m5 = normalize_from_csv(args.from_csv_5m)
        print(f"[LOAD] 5m from CSV: {args.from_csv_5m}")
    else:
        m5 = sph.get_schwab_5min(symbol, period=args.days5)
        if not _ok(m5):
            print("[ERR] No 5m data from Schwab")
            sys.exit(3)
        print(f"[FETCH] 5m Schwab rows={len(m5)} latest={m5.index[-1]}")
    validate_df(m5, "5min", "5m (native)", smi_mode=args.smi)

    # 1m -> 5m resample (drop last incomplete) and compare
    m1_to_5 = resample_from_1min(m1, "5min")
    validate_df(m1_to_5, "5min", "5m (from 1m)", smi_mode=args.smi)
    compare_series(m5, m1_to_5)

    if args.save1:
        m1.to_csv(args.save1, float_format="%.6f")
        print(f"[SAVE] wrote {args.save1}")
    if args.save5:
        m5.to_csv(args.save5, float_format="%.6f")
        print(f"[SAVE] wrote {args.save5}")

if __name__ == "__main__":
    main()
