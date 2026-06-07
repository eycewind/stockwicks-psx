#!/usr/bin/env python3
"""
Replay Bot Runner - Continuously runs bot during replay
"""

import os
import sys
import time
from datetime import datetime
import pytz

# Set replay mode
os.environ["REPLAY_MODE"] = "true"

# Import your bot runner
sys.path.insert(0, "/var/www/stockwicks")
from app.scripts.stock_algos.algoMM_runner import run_algoMM_bot_tick

ET = pytz.timezone("America/New_York")

def main(bot_id: int, interval_minutes: int = 5):
    """
    Run bot in a loop, simulating the Celery scheduler.
    
    Args:
        bot_id: Bot ID to run
        interval_minutes: How often to run (matches bot's interval)
    """
    print(f"\n{'='*60}")
    print(f"REPLAY BOT RUNNER")
    print(f"{'='*60}")
    print(f"Bot ID: {bot_id}")
    print(f"Interval: Every {interval_minutes} minutes")
    print(f"Mode: REPLAY")
    print(f"{'='*60}\n")
    
    iteration = 0
    
    try:
        while True:
            iteration += 1
            
            # Get current replay time
            try:
                with open("/tmp/replay_state.json", 'r') as f:
                    import json
                    state = json.load(f)
                    current_time = datetime.fromisoformat(state['current_time'])
                    is_running = state.get('is_running', False)
                    
                    if not is_running:
                        print("\n⚠️  Replay engine stopped. Exiting...")
                        break
                    
                    print(f"\n[{current_time.strftime('%H:%M:%S')}] Iteration #{iteration}")
                    print(f"{'─'*60}")
                    
            except FileNotFoundError:
                print("\n❌ Replay engine not running! Start it first:")
                print("   python app/utils/replay_engine.py --date 2026-04-17 --speed 2.0")
                break
            
            # Run the bot (same as Celery would)
            try:
                result = run_algoMM_bot_tick(bot_id)
                print(f"Result: {result.get('decision', 'UNKNOWN')}")
            except Exception as e:
                print(f"❌ Error: {e}")
            
            # Wait for next interval (in real time, not replay time)
            # Since replay is 2x speed, 5min replay = 2.5min real time
            # But we'll just check every 30 seconds to catch all bars
            print(f"\n⏳ Waiting 30s before next check...")
            time.sleep(30)
            
    except KeyboardInterrupt:
        print("\n\n⏹️  Bot stopped by user")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Replay Bot Runner")
    parser.add_argument("bot_id", type=int, help="Bot ID to run")
    parser.add_argument("--interval", type=int, default=5, help="Check interval in minutes")
    
    args = parser.parse_args()
    
    main(args.bot_id, args.interval)