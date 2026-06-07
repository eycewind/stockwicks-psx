import sys
import os
import logging
import subprocess
import re
import pandas as pd
from io import StringIO
import requests
from dotenv import load_dotenv

# === CONFIG & LOGGING ===================================
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PYTHON_EXECUTABLE = "/var/www/stockwicks/venv/bin/python"
GURU_SCRIPT_PATH = "app.scripts.options.guru_dashboard"

### CHANGE: Replaced the Movers API call with a reliable, curated watchlist.
WATCHLIST = [
    "SPY", "QQQ", "TSLA", "NVDA", "AMD", "AAPL", 
    "MSFT", "AMZN", "GOOGL", "META", "NFLX", "COIN"
]

# === HELPER FUNCTIONS ===================================
# In app/scripts/options/daily_screener.py

def parse_guru_output(output: str):
    """
    Parses the output of the guru_dashboard.py script to find the Guru Choice
    and the Target Expiration Date.
    """
    guru_choice = None
    expiration_date = None
    try:
        # First, find the expiration date from the context block
        context_match = re.search(r"MARKET & SYMBOL CONTEXT\n={50}\n(.*?)\n={50}", output, re.DOTALL)
        if context_match:
            context_block = context_match.group(1)
            for line in context_block.strip().split('\n'):
                if "Target Expiration Date" in line:
                    expiration_date = line.split(':', 1)[1].strip()
                    break # Found it, no need to loop further

        # Next, find the guru choice
        choice_match = re.search(r"🏆 GURU CHOICE 🏆\n={50}\n(.*?)\n={50}", output, re.DOTALL)
        if choice_match:
            guru_choice = {}
            choice_block = choice_match.group(1)
            for line in choice_block.strip().split('\n'):
                if ':' in line:
                    key, value = line.split(':', 1)
                    guru_choice[key.strip()] = value.strip()
            
    except Exception as e:
        logger.error(f"Error parsing guru output: {e}")
    
    return guru_choice, expiration_date

# === MAIN EXECUTION =======================================
if __name__ == "__main__":
    print("\n--- Guru's Daily Options Screener ---")
    
    all_symbols = set(WATCHLIST)
    
    print(f"\nScanning {len(all_symbols)} symbols from the curated watchlist...")
    print("-" * 50)

    best_picks = []
    
    for i, symbol in enumerate(sorted(list(all_symbols))):
        print(f"({i+1}/{len(all_symbols)}) Running analysis for {symbol}...")
        try:
            process = subprocess.run(
                [PYTHON_EXECUTABLE, "-m", GURU_SCRIPT_PATH, symbol],
                capture_output=True, text=True, check=True, cwd="/var/www/stockwicks",
                timeout=60
            )
            
            ### CHANGE: Unpack both the choice and the expiration date
            guru_choice, expiration = parse_guru_output(process.stdout)

            if guru_choice and guru_choice.get("Confidence") in ["High", "Medium"]:
                pick = {
                    "Symbol": symbol,
                    "Action": guru_choice.get("Action"),
                    "Strike": guru_choice.get("Strike"),
                    "Expiration": expiration, ### CHANGE: Add expiration to the pick
                    "Premium (Bid)": guru_choice.get("Premium (Bid)"),
                    "Confidence": guru_choice.get("Confidence"),
                    "Score": guru_choice.get("Sell Score")
                }
                best_picks.append(pick)
                print(f"✅ Found a potential trade for {symbol}!")

        except subprocess.CalledProcessError as e:
            logger.error(f"Guru script failed for {symbol}: {e.stderr}")
        except subprocess.TimeoutExpired:
            logger.error(f"Guru script timed out for {symbol}.")
        except Exception as e:
            logger.error(f"An unexpected error occurred for {symbol}: {e}")

    print("\n" + "="*65)
    print("                   🏆 Today's Top Guru Picks 🏆")
    print("="*65)

    if not best_picks:
        print("No High or Medium confidence trades found today.")
    else:
        sorted_picks = sorted(
            best_picks, 
            key=lambda x: (x['Confidence'] == 'High', int(x['Score'].split('/')[0])), 
            reverse=True
        )
        
        df = pd.DataFrame(sorted_picks)
        print(df.to_string(index=False))

    print("="*65)