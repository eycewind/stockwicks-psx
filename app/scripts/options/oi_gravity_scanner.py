#app/scripts/options/oi_gravity_scanner.py
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))
import logging
from datetime import datetime, timedelta
import pandas as pd
import pandas_ta as ta
import requests
import pytz
from dotenv import load_dotenv

# === ENVIRONMENT & LOGGING ===================================
load_dotenv()
LOG_DIR = "/var/www/stockwicks/logs"
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# === CONFIG ================================================
SYMBOL = sys.argv[1].upper() if len(sys.argv) > 1 else "SPY"
INTERVAL = '1min'
SCHWAB_API_URL = "https://api.schwabapi.com/marketdata/v1"

def get_schwab_headers():
    from app.utils.stock.schwab_token import get_valid_access_token
    access_token = get_valid_access_token()
    if not access_token:
        raise ValueError("SCHWAB_ACCESS_TOKEN missing or could not refresh.")
    return {"Authorization": f"Bearer {access_token}"}

def get_intraday_data(symbol, interval):
    intervals = {'1min': ('day', 1, 'minute', 1)}
    periodType, period, frequencyType, frequency = intervals[interval]
    
    url = f"{SCHWAB_API_URL}/pricehistory"
    params = { "symbol": symbol, "periodType": periodType, "period": period, "frequencyType": frequencyType, "frequency": frequency }
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params)
    logger.info(f"PriceHistory API [{symbol} - {interval}] status: {resp.status_code}")
    if resp.status_code != 200: return None

    data = resp.json()
    if not data or 'candles' not in data: return None
    
    df = pd.DataFrame(data['candles'])
    df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
    df.set_index('datetime', inplace=True)
    df.columns = [col.lower() for col in df.columns]
    
    if not all(c in df.columns for c in ['high', 'low', 'close', 'volume']):
        return None
    return df

def fetch_option_chain(symbol, expiration_date):
    url = f"{SCHWAB_API_URL}/chains"
    params = { "symbol": symbol, "fromDate": expiration_date.strftime("%Y-%m-%d"), "toDate": expiration_date.strftime("%Y-%m-%d") }
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params)
    logger.info(f"Chains API [{symbol}] status: {resp.status_code}")
    if resp.status_code != 200:
        return pd.DataFrame()

    chain = resp.json()
    options = []
    required_keys = ["strikePrice", "openInterest"]
    for side, exp_map_key in [("call", "callExpDateMap"), ("put", "putExpDateMap")]:
        exp_map = chain.get(exp_map_key, {})
        for exp_key, strikes in exp_map.items():
            for strike, contracts in strikes.items():
                for contract in contracts:
                    if all(key in contract and contract[key] is not None for key in required_keys):
                        options.append({
                            "strike": contract["strikePrice"], "side": side,
                            "oi": contract["openInterest"]
                        })
    
    logger.info(f"Parsed {len(options)} contracts from API response.")
    return pd.DataFrame(options)

def get_target_expiration(symbol):
    url = f"{SCHWAB_API_URL}/expirationchain"
    params = {"symbol": symbol}
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params)
    data = resp.json() if resp.status_code == 200 else {}
    expirations = data.get("expirationList", [])
    if not expirations: return None

    exp_dates = []
    for e in expirations:
        try: exp_dates.append(datetime.strptime(e["expirationDate"], "%Y-%m-%d").date())
        except (ValueError, TypeError): continue
    
    today = datetime.now(pytz.timezone('US/Eastern')).date()
    future_exp_dates = [d for d in exp_dates if d >= today]
    if not future_exp_dates: return None
    target_date = today + timedelta(days=14)
    return min(future_exp_dates, key=lambda d: abs(d - target_date))

# --- NEW: OI Wall Analysis Function ---
def find_oi_walls(options_df, price):
    walls = {"put_wall": None, "call_wall": None}

    # Find Put Wall (Support)
    puts = options_df[(options_df['side'] == 'put') & (options_df['strike'] < price)].copy()
    if not puts.empty:
        put_wall = puts.loc[puts['oi'].idxmax()]
        walls['put_wall'] = {"strike": put_wall['strike'], "oi": int(put_wall['oi'])}

    # Find Call Wall (Resistance)
    calls = options_df[(options_df['side'] == 'call') & (options_df['strike'] > price)].copy()
    if not calls.empty:
        call_wall = calls.loc[calls['oi'].idxmax()]
        walls['call_wall'] = {"strike": call_wall['strike'], "oi": int(call_wall['oi'])}
        
    return walls

if __name__ == "__main__":
    logger.info(f"--- OI Gravity Scanner for {SYMBOL} ---")
    
    price_df = get_intraday_data(SYMBOL, INTERVAL)
    if price_df is None or price_df.empty:
        print("Could not retrieve intraday price data. Exiting.")
        sys.exit(1)
        
    price_df.ta.vwap(append=True)
    vwap_col_name = next((col for col in price_df.columns if 'vwap' in col.lower()), None)
    if not vwap_col_name:
        print("Could not calculate VWAP. Exiting."); sys.exit(1)
        
    last_price = price_df['close'].iloc[-1]
    vwap = price_df[vwap_col_name].iloc[-1]
    
    intraday_trend = "Uptrend" if last_price > vwap else "Downtrend"
    
    print(f"\nAnalysis for {SYMBOL}:")
    print(f"- Current Price: ${last_price:.2f}")
    print(f"- VWAP: ${vwap:.2f}")
    print(f"- Intraday Trend: {intraday_trend}")

    expiration = get_target_expiration(SYMBOL)
    if not expiration:
        print("\nCould not find a valid options expiration date. Exiting."); sys.exit(1)
    
    print(f"- Analyzing OI for expiration: {expiration}")
        
    options_df = fetch_option_chain(SYMBOL, expiration)
    if options_df.empty:
        print("\nCould not retrieve a valid option chain. Exiting."); sys.exit(1)
        
    oi_walls = find_oi_walls(options_df, last_price)
    
    print("\n" + "="*40)
    print("           OPEN INTEREST WALLS")
    print("="*40)
    
    if oi_walls['put_wall']:
        put_wall = oi_walls['put_wall']
        put_highlight = " <--- Relevant Support" if intraday_trend == "Downtrend" else ""
        print(f"🧱 Put Wall (Support):   ${put_wall['strike']:.2f} (OI: {put_wall['oi']:,}){put_highlight}")
    else:
        print("🧱 Put Wall (Support):   Not Found")

    if oi_walls['call_wall']:
        call_wall = oi_walls['call_wall']
        call_highlight = " <--- Relevant Resistance" if intraday_trend == "Uptrend" else ""
        print(f"🧱 Call Wall (Resistance): ${call_wall['strike']:.2f} (OI: {call_wall['oi']:,}){call_highlight}")
    else:
        print("🧱 Call Wall (Resistance): Not Found")
    print("="*40)