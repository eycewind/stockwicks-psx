#!/usr/bin/env python3
"""
Prepare historical data for replay.

Downloads data from Schwab and saves it in the format needed by replay engine.
Run this once to prepare a day for replay.
"""

import os
import sys
import pandas as pd
from datetime import datetime, timedelta

# Add project root
REPO_ROOT = "/var/www/stockwicks"
sys.path.insert(0, REPO_ROOT)

from app.utils.stock.schwab_token import get_valid_access_token
import requests
import pytz

ET_TZ = pytz.timezone("America/New_York")

def download_day_data(symbol: str, interval: str, date: str, output_dir: str):
    """
    Download one day of data from Schwab and save for replay.
    
    Note: Schwab API for intraday data returns the MOST RECENT trading day,
    not a specific historical date. So this will download the last trading day's data.
    """
    
    print(f"Downloading {symbol} {interval} for {date}...")
    
    # Schwab API params - FIXED VERSION
    intervals = {
        "1min": ("day", 1, "minute", 1),
        "5min": ("day", 1, "minute", 5),
        "10min": ("day", 1, "minute", 10),
        "15min": ("day", 1, "minute", 15),
        "30min": ("day", 1, "minute", 30),
    }
    
    if interval not in intervals:
        raise ValueError(f"Unsupported interval: {interval}")
    
    periodType, period, freqType, freq = intervals[interval]
    
    # Fetch from Schwab
    token = get_valid_access_token()
    url = "https://api.schwabapi.com/marketdata/v1/pricehistory"
    
    # Use periodType/period instead of startDate/endDate
    params = {
        "symbol": symbol.upper(),
        "periodType": periodType,
        "period": period,
        "frequencyType": freqType,
        "frequency": freq,
        "needExtendedHoursData": "false",
    }
    headers = {"Authorization": f"Bearer {token}"}
    
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    
    if resp.status_code != 200:
        print(f"  ✗ Error: {resp.status_code} - {resp.text}")
        return
    
    data = resp.json()
    
    candles = data.get("candles", [])
    if not candles:
        print(f"  ⚠ No data returned")
        return
    
    # Convert to DataFrame
    df = pd.DataFrame(candles)
    df["timestamp"] = pd.to_datetime(df["datetime"], unit="ms", utc=True).dt.tz_convert(ET_TZ)
    df = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
    df = df[~df.index.duplicated(keep="last")].sort_index()
    
    # Get the actual date from the data
    actual_date = df.index[0].strftime("%Y-%m-%d")
    
    print(f"  ℹ️  Data is from: {actual_date} ({len(df)} bars)")
    
    # Save
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f"{symbol}_{interval}_{actual_date}.csv")
    df.to_csv(output_file)
    
    print(f"  ✓ Saved to {output_file}")
    
    return actual_date


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Prepare replay data")
    parser.add_argument("--date", help="Date label (not used for API, just for reference)")
    parser.add_argument("--symbols", nargs="+", default=["TSLA", "MU", "AAPL", "QQQ"], help="Symbols to download")
    parser.add_argument("--intervals", nargs="+", default=["5min"], help="Intervals to download")
    parser.add_argument("--output-dir", default="/var/www/stockwicks/data/replay", help="Output directory")
    
    args = parser.parse_args()
    
    print(f"\n{'='*60}")
    print(f"DOWNLOADING REPLAY DATA")
    print(f"{'='*60}")
    print(f"Note: Schwab API returns the MOST RECENT trading day")
    print(f"{'='*60}\n")
    
    actual_dates = set()
    
    for symbol in args.symbols:
        for interval in args.intervals:
            try:
                actual_date = download_day_data(symbol, interval, args.date or "latest", args.output_dir)
                if actual_date:
                    actual_dates.add(actual_date)
            except Exception as e:
                print(f"  ✗ Error: {e}")
    
    if actual_dates:
        print(f"\n{'='*60}")
        print(f"✓ Download complete!")
        print(f"Data dates: {', '.join(sorted(actual_dates))}")
        print(f"\nTo replay this data:")
        print(f"  python app/utils/replay_engine.py --date {sorted(actual_dates)[0]} --speed 2.0")
        print(f"{'='*60}\n")