# app/utils/marketdata_api.py

import os
import requests
import datetime
import pytz
from dotenv import load_dotenv

load_dotenv()

API_TOKEN = os.getenv("API_TOKEN")
if not API_TOKEN:
    raise ValueError("API_TOKEN missing in environment variables.")

HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}

def get_option_price(option_symbol: str):
    print(f"Fetching live price for {option_symbol}")
    base_symbol = option_symbol[:3]

    url = f"https://api.marketdata.app/v1/options/quotes/{option_symbol}"

    try:
        response = requests.get(url, headers=HEADERS)
        response.raise_for_status()
        data = response.json()

        if isinstance(data, dict) and data.get("s") == "error":
            print(f"⚠️ Option expired or not found: {option_symbol}")
            return fetch_tomorrow_option_price(base_symbol, option_symbol)

        if isinstance(data, dict) and "optionSymbol" in data:
            # Correct: all fields are lists, so pick first element
            if data.get('mid') and len(data['mid']) > 0:
                return float(data['mid'][0])
            if data.get('bid') and data.get('ask') and len(data['bid']) > 0 and len(data['ask']) > 0:
                bid = data['bid'][0]
                ask = data['ask'][0]
                return round((bid + ask) / 2, 2)
            if data.get('last') and len(data['last']) > 0:
                return float(data['last'][0])

            print(f"⚠️ No valid price fields found, trying fallback...")
            return fetch_tomorrow_option_price(base_symbol, option_symbol)

        print(f"⚠️ Unknown API response structure")
        return None

    except Exception as e:
        print(f"❗ Error fetching price for {option_symbol}: {e}")
        return None

def fetch_tomorrow_option_price(base_symbol: str, original_option_symbol: str):
    ny_tz = pytz.timezone('America/New_York')
    now_ny = datetime.datetime.now(ny_tz)
    tomorrow = now_ny + datetime.timedelta(days=1)
    expiration_date = tomorrow.strftime("%Y-%m-%d")

    print(f"🛠️ Trying fallback: Fetching {base_symbol} options for {expiration_date}")

    chain_url = f"https://api.marketdata.app/v1/options/chain/{base_symbol}/"
    params = {"expiration": expiration_date}

    try:
        response = requests.get(chain_url, headers=HEADERS, params=params)
        response.raise_for_status()
        options_chain = response.json()

        if isinstance(options_chain, list) and len(options_chain) > 0:
            target_strike = extract_strike_from_symbol(original_option_symbol)
            side = "call" if "C" in original_option_symbol else "put"

            best_match = None
            smallest_diff = float('inf')

            for opt in options_chain:
                if opt['side'] == side:
                    diff = abs(opt['strike'] - target_strike)
                    if diff < smallest_diff:
                        best_match = opt
                        smallest_diff = diff

            if best_match:
                print(f"✅ Found fallback match: {best_match['optionSymbol']} (Strike {best_match['strike']})")
                if best_match.get('mid') is not None:
                    return float(best_match['mid'])
                if best_match.get('bid') is not None and best_match.get('ask') is not None:
                    bid = best_match['bid']
                    ask = best_match['ask']
                    return round((bid + ask) / 2, 2)
                if best_match.get('last') is not None:
                    return float(best_match['last'])

            else:
                print(f"⚠️ No matching option found for fallback.")

        else:
            print(f"⚠️ No options found for {expiration_date}")

    except Exception as e:
        print(f"❗ Error fetching tomorrow options: {e}")

    return None

def extract_strike_from_symbol(option_symbol: str) -> float:
    try:
        strike_part = option_symbol[-8:]  # Last 8 characters
        strike = int(strike_part) / 1000.0
        return strike
    except Exception as e:
        print(f"❗ Error parsing strike from symbol: {e}")
        return 0.0
