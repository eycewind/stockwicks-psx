#!/usr/bin/env python3
"""
Replay API - Drop-in replacement for Schwab API

Same interface as schwab_api, but returns replay data instead of live data.
Your bot code doesn't need to change at all.
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pytz
import json
import os
import time
import random

ET_TZ = pytz.timezone("America/New_York")

def get_current_replay_time() -> datetime:
    """Get current time from replay engine."""
    state_file = "/tmp/replay_state.json"
    
    if not os.path.exists(state_file):
        raise RuntimeError("Replay engine not running! Start it first: python app/utils/replay_engine.py --date 2026-04-14")
    
    with open(state_file, 'r') as f:
        state = json.load(f)
    
    if not state.get('is_running'):
        raise RuntimeError("Replay engine is not running")
    
    return datetime.fromisoformat(state['current_time'])


def fetch_ohlcv(symbol: str, interval: str, days: int = 10) -> pd.DataFrame:
    """
    REPLAY VERSION of fetch_ohlcv.
    
    Returns historical data up to current replay time.
    Simulates API delay (30-60s like real Schwab).
    
    Interface is IDENTICAL to schwab_api.fetch_ohlcv()
    """
    
    # Get current replay time
    current_time = get_current_replay_time()
    
    # Simulate API delay (30-60 seconds like real Schwab)
    api_delay = random.uniform(30, 60)
    data_cutoff_time = current_time - timedelta(seconds=api_delay)
    
    print(f"[REPLAY API] Fetching {symbol} {interval} as of {data_cutoff_time.strftime('%H:%M:%S')} (delay: {api_delay:.1f}s)")
    
    # Load replay data
    state_file = "/tmp/replay_state.json"
    with open(state_file, 'r') as f:
        state = json.load(f)
    replay_date = state['replay_date']
    
    data_dir = "/var/www/stockwicks/data/replay"
    file_path = os.path.join(data_dir, f"{symbol}_{interval}_{replay_date}.csv")
    
    if not os.path.exists(file_path):
        print(f"[REPLAY API] Warning: No data file for {symbol} {interval}")
        return pd.DataFrame()
    
    # Load full day's data
    df = pd.read_csv(file_path, parse_dates=['timestamp'], index_col='timestamp')
    df.index = df.index.tz_localize('UTC').tz_convert(ET_TZ)
    
    # Filter to only data available at current replay time (with delay)
    df = df[df.index <= data_cutoff_time]
    
    print(f"[REPLAY API] Returning {len(df)} bars (latest: {df.index[-1].strftime('%H:%M:%S') if not df.empty else 'N/A'})")
    
    return df


def get_valid_access_token():
    """Mock - not needed for replay."""
    return "REPLAY_MODE"