# app/scripts/fetch_history_probe.py
import argparse
import datetime as dt
import logging
import sys
from typing import Tuple

import pandas as pd
import pytz
import requests

# Reuse your token getter
from app.utils.stock.schwab_token import get_valid_access_token

log = logging.getLogger("fetch_probe")
logging.basicConfig(level=logging.INFO, format="%(message)s")

BASE_URL = "https://api.schwabapi.com/marketdata/v1/pricehistory"
_ET = pytz.timezone("US/Eastern")

INTERVALS = {
    "1min":  ("minute", 1),
    "5min":  ("minute", 5),
    "15min": ("minute", 15),
    "30min": ("minute", 30),
    "1d":    ("daily", 1),
}

def _ms(ts: dt.datetime) -> int:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    else:
        ts = ts.astimezone(dt.timezone.utc)
    return int(ts.timestamp() * 1000)

def _request(params: dict) -> pd.DataFrame:
    token = get_valid_access_token()
    if not token:
        raise RuntimeError("No valid Schwab access token.")

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    r = requests.get(BASE_URL, headers=headers, params=params, timeout=30)
    try:
        r.raise_for_status()
    except Exception as e:
        body = r.text[:800] if hasattr(r, "text") else ""
        raise RuntimeError(f"HTTP {r.status_code}: {e}\nparams={params}\nbody={body}") from e

    data = r.json()
    candles = data.get("candles", [])
    if not candles:
        log.warning(f"[WARN] No candles returned. params={params}")
        return pd.DataFrame()

    df = pd.DataFrame(candles)
    # normalize index to ET tz-aware
    ts = pd.to_datetime(df["datetime"], unit="ms", utc=True).dt.tz_convert(_ET)
    df = df.set_index(ts)[["open", "high", "low", "close", "volume"]]
    df.index.name = "timestamp"
    return df

def build_params_date(symbol: str, freq_type: str, freq: int,
                      days_back: int, extended: bool) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(days=days_back)
    return {
        "symbol": symbol.upper(),
        "frequencyType": freq_type,
        "frequency": freq,
        "startDate": _ms(start),
        "endDate": _ms(now),
        "needExtendedHoursData": str(bool(extended)).lower(),
        "needPreviousClose": "false",
    }

def build_params_period(symbol: str, interval: str,
                        period: int, extended: bool) -> dict:
    freq_type, freq = INTERVALS[interval]
    # PeriodType/period semantics:
    # - intraday: use periodType="day" and `period` in days
    # - daily:    use periodType="year" with a small year window or month equiv
    if freq_type == "minute":
        periodType = "day"
        periodVal = period  # days
    elif freq_type == "daily":
        periodType = "year"
        periodVal = max(1, int(round(period / 12)))  # crude: months->years
    else:
        periodType = "day"
        periodVal = period

    return {
        "symbol": symbol.upper(),
        "periodType": periodType,
        "period": periodVal,
        "frequencyType": freq_type,
        "frequency": freq,
        "needExtendedHoursData": str(bool(extended)).lower(),
        "needPreviousClose": "false",
    }

def max_bar_age_for_interval(interval: str) -> dt.timedelta:
    mapping = {
        "1min": dt.timedelta(minutes=3),
        "5min": dt.timedelta(minutes=12),
        "15min": dt.timedelta(minutes=35),
        "30min": dt.timedelta(minutes=70),
        "1d": dt.timedelta(days=3),
    }
    return mapping.get(interval, dt.timedelta(minutes=10))

def summarize(df: pd.DataFrame, interval: str) -> Tuple[int, str, bool, dt.timedelta]:
    if df is None or df.empty:
        return 0, "None", True, dt.timedelta.max
    now_et = dt.datetime.now(_ET)
    latest = df.index[-1]
    age = now_et - latest
    stale = age > max_bar_age_for_interval(interval)
    return len(df), latest.strftime("%Y-%m-%d %H:%M:%S%z"), stale, age

def main():
    ap = argparse.ArgumentParser(description="Probe Schwab pricehistory for a symbol/interval.")
    ap.add_argument("--symbol", required=True, help="e.g. AAPL")
    ap.add_argument("--interval", default="1min", choices=list(INTERVALS.keys()))
    ap.add_argument("--days", type=int, default=5, help="Days back (intraday) or months (for 1d if method=date)")
    ap.add_argument("--method", choices=["date", "period"], default="date",
                    help="date: start/end dates (recommended intraday). period: periodType/period.")
    ap.add_argument("--extended", action="store_true",
                    help="Include extended hours (premarket/post) for intraday.")
    ap.add_argument("--save", metavar="CSV_PATH", help="Optional: save DataFrame to CSV")
    args = ap.parse_args()

    symbol = args.symbol.upper()
    interval = args.interval
    freq_type, freq = INTERVALS[interval]

    # Build params
    if args.method == "date":
        if interval == "1d":
            # daily via date bounds: treat --days as months back
            now = dt.datetime.now(dt.timezone.utc)
            start = now - dt.timedelta(days=args.days * 30)
            params = {
                "symbol": symbol,
                "frequencyType": "daily",
                "frequency": 1,
                "startDate": _ms(start),
                "endDate": _ms(now),
                "needExtendedHoursData": "false",
                "needPreviousClose": "false",
            }
        else:
            params = build_params_date(symbol, freq_type, freq, args.days, args.extended)
    else:
        # period method
        period_val = args.days if interval != "1d" else max(1, args.days // 30)
        params = build_params_period(symbol, interval, period_val, args.extended)

    print(f"[REQ] method={args.method} symbol={symbol} interval={interval} "
          f"freq={freq_type}x{freq} days={args.days} extended={args.extended}")
    print(f"[REQ] params={params}")

    try:
        df = _request(params)
    except Exception as e:
        print(f"[ERROR] request failed: {e}")
        sys.exit(2)

    n, latest_str, stale, age = summarize(df, interval)
    print(f"[DATA] rows={n} latest={latest_str} stale={stale} age={age}")

    if n:
        print("[HEAD]")
        print(df.head(3))
        print("[TAIL]")
        print(df.tail(3))

    if args.save and n:
        out = args.save
        df.to_csv(out, float_format="%.6f")
        print(f"[SAVE] wrote {out}")

if __name__ == "__main__":
    main()
