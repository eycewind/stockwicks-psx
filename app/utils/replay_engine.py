#!/usr/bin/env python3
"""
Live Market Replay Engine

Simulates live market data playback for testing trading bots on weekends.
Feeds historical data as if it's arriving in real-time.
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
import pytz
import os
import json

ET_TZ = pytz.timezone("America/New_York")

class MarketReplayEngine:
    """
    Replays historical market data as if it's live.
    
    Usage:
        replay = MarketReplayEngine("2026-04-14", speed=2.0)  # 2x speed
        replay.start()
        
        # In another terminal, your bot runs normally
        # It fetches data via replay API instead of Schwab
    """
    
    def __init__(self, replay_date: str, speed: float = 1.0, data_dir: str = "/var/www/stockwicks/data/replay"):
        """
        Args:
            replay_date: Date to replay (YYYY-MM-DD format)
            speed: Playback speed (1.0 = realtime, 2.0 = 2x, 0.5 = half speed)
            data_dir: Directory containing historical data
        """
        self.replay_date = replay_date
        self.speed = speed
        self.data_dir = data_dir
        
        # State
        self.current_time = None
        self.market_open = None
        self.market_close = None
        self.is_running = False
        
        # Data cache
        self.data_cache = {}
        
        # State file (persists current replay time)
        self.state_file = "/tmp/replay_state.json"
        
    def load_historical_data(self, symbol: str, interval: str) -> pd.DataFrame:
        """Load historical data for replay."""
        file_path = os.path.join(
            self.data_dir, 
            f"{symbol}_{interval}_{self.replay_date}.csv"
        )
        
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Replay data not found: {file_path}")
        
        df = pd.read_csv(file_path, parse_dates=['timestamp'], index_col='timestamp')
        df.index = df.index.tz_localize('UTC').tz_convert(ET_TZ)
        
        print(f"✓ Loaded {len(df)} bars for {symbol} {interval}")
        return df
    
    def start(self):
        """Start the replay engine."""
        print(f"\n{'='*60}")
        print(f"MARKET REPLAY ENGINE")
        print(f"{'='*60}")
        print(f"Date: {self.replay_date}")
        print(f"Speed: {self.speed}x")
        print(f"{'='*60}\n")
        
        # Set market hours for replay date
        replay_dt = datetime.strptime(self.replay_date, "%Y-%m-%d")
        self.market_open = ET_TZ.localize(datetime.combine(replay_dt.date(), datetime.strptime("09:30", "%H:%M").time()))
        self.market_close = ET_TZ.localize(datetime.combine(replay_dt.date(), datetime.strptime("16:00", "%H:%M").time()))
        
        self.current_time = self.market_open
        self.is_running = True
        
        # Save initial state
        self._save_state()
        
        print(f"▶ Replay started at {self.current_time.strftime('%H:%M:%S')}")
        print(f"Press Ctrl+C to stop\n")
        
        try:
            while self.current_time < self.market_close:
                # Update current time
                self.current_time += timedelta(seconds=1/self.speed)
                
                # Save state every second
                self._save_state()
                
                # Print progress every minute
                if self.current_time.second == 0:
                    elapsed = self.current_time - self.market_open
                    total = self.market_close - self.market_open
                    pct = (elapsed.total_seconds() / total.total_seconds()) * 100
                    
                    print(f"[{self.current_time.strftime('%H:%M:%S')}] {pct:.1f}% complete", end='\r')
                
                # Sleep to maintain speed
                time.sleep(1.0)
            
            print(f"\n\n✓ Replay complete!")
            
        except KeyboardInterrupt:
            print(f"\n\n⏸ Replay paused at {self.current_time.strftime('%H:%M:%S')}")
        finally:
            self.is_running = False
            self._save_state()
    
    def _save_state(self):
        """Save current replay state to file."""
        state = {
            'current_time': self.current_time.isoformat(),
            'replay_date': self.replay_date,
            'speed': self.speed,
            'is_running': self.is_running,
        }
        with open(self.state_file, 'w') as f:
            json.dump(state, f)
    
    def get_current_time(self) -> datetime:
        """Get current replay time."""
        if os.path.exists(self.state_file):
            with open(self.state_file, 'r') as f:
                state = json.load(f)
                return datetime.fromisoformat(state['current_time'])
        return None


# CLI for starting replay
if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Market Replay Engine")
    parser.add_argument("--date", required=True, help="Replay date (YYYY-MM-DD)")
    parser.add_argument("--speed", type=float, default=1.0, help="Playback speed (1.0 = realtime)")
    parser.add_argument("--data-dir", default="/var/www/stockwicks/data/replay", help="Data directory")
    
    args = parser.parse_args()
    
    replay = MarketReplayEngine(args.date, args.speed, args.data_dir)
    replay.start()