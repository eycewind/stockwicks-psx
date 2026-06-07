#app/scripts/options/scalping_screener_smi_mfi.py
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))
import logging
from datetime import datetime, timedelta, date
import numpy as np
import pandas as pd
import pandas_ta as ta
import requests
import pytz
from dotenv import load_dotenv

# === ENVIRONMENT & LOGGING ===================================
load_dotenv()
BASE_DIR = "/var/www/stockwicks"
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "scalping_screener.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

# === CONFIG ================================================
SYMBOL = sys.argv[1].upper() if len(sys.argv) > 1 else "SPY"
INTERVAL = '5min' # <-- TRUE DAY TRADING INTERVAL
MIN_VOLUME = 100
MIN_OI = 100
MIN_PREMIUM = 10.00
SCHWAB_API_URL = "https://api.schwabapi.com/marketdata/v1"

def get_schwab_headers():
    from app.utils.stock.schwab_token import get_valid_access_token
    access_token = get_valid_access_token()
    if not access_token:
        raise ValueError("SCHWAB_ACCESS_TOKEN missing or could not refresh.")
    return {"Authorization": f"Bearer {access_token}"}

# --- SCALPER'S METHOD: SMI + MFI Confirmation on Intraday Data ---
def get_scalper_signal(symbol, interval):
    """
    Calculates SMI and MFI on intraday data to find a dual-confirmed scalping signal.
    """
    # Define intervals for Schwab API
    intervals = {'1min': ('day', 1, 'minute', 1), '5min': ('day', 1, 'minute', 5), '15min': ('day', 5, 'minute', 15)}
    if interval not in intervals:
        return "Invalid Interval", None, {}
    
    periodType, period, frequencyType, frequency = intervals[interval]
    
    url = f"{SCHWAB_API_URL}/pricehistory"
    params = { 
        "symbol": symbol, 
        "periodType": periodType, 
        "period": period, 
        "frequencyType": frequencyType, 
        "frequency": frequency,
        "needExtendedHoursData": "false"
    }
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params)
    logger.info(f"PriceHistory API [{symbol} - {interval}] status: {resp.status_code}")
    if resp.status_code != 200: return "API Error", None, {}

    data = resp.json()
    if not data or 'candles' not in data or len(data['candles']) < 20:
        logger.warning(f"Insufficient data for {symbol} to calculate indicators on a {interval} interval.")
        return "Insufficient Data", None, {}

    df = pd.DataFrame(data['candles'])
    
    # Calculate Indicators
    df.ta.smi(fast=5, slow=20, signal=5, append=True)
    df.ta.mfi(length=14, append=True)

    # --- FIX: Standardize all columns to lowercase AFTER calculating indicators ---
    df.columns = [col.lower() for col in df.columns]

    smi_col_name = next((col for col in df.columns if 'smi_5_20_5' in col), None)
    if not smi_col_name or 'mfi_14' not in df.columns:
        logger.error(f"Could not find required indicator columns. Available: {df.columns.tolist()}")
        return "Indicator Calc Error", None, {}

    last_smi = df[smi_col_name].iloc[-1]
    last_mfi = df['mfi_14'].iloc[-1]

    analysis = {
        "price": f"${df['close'].iloc[-1]:.2f}",
        "smi": f"{last_smi:.2f}",
        "mfi": f"{last_mfi:.2f}"
    }

    if last_smi > 80 and last_mfi > 80:
        return "Overbought Confirmation (SMI & MFI)", "Sell CALL", analysis
    
    if last_smi < 20 and last_mfi < 20:
        return "Oversold Confirmation (SMI & MFI)", "Sell PUT", analysis

    return "No Confirmation", None, analysis

# ... (Helper functions remain the same) ...
def get_option_expirations(symbol):
    url = f"{SCHWAB_API_URL}/expirationchain"
    params = {"symbol": symbol}
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params)
    data = resp.json() if resp.status_code == 200 else None
    expirations = []
    if data and "expirationList" in data:
        expirations = [e["expirationDate"] for e in data["expirationList"]]
    return expirations

def get_target_expiration(expirations):
    tz = pytz.timezone('US/Eastern')
    now = datetime.now(tz).date()
    # For day trading, we want the closest expiration, often 0-DTE
    target_date = now
    valid_expirations = []
    for exp_str in expirations:
        try:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            if exp_date >= now: valid_expirations.append(exp_date)
        except Exception: continue
    if not valid_expirations: return None
    # Return the absolute closest expiration date
    return min(valid_expirations, key=lambda d: abs(d - target_date))

def flatten_options_map(exp_map, side):
    records = []
    for exp, strikes in exp_map.items():
        for strike, contracts in strikes.items():
            for c in contracts:
                rec = c.copy()
                rec["side"] = side
                rec["expiration"] = exp.split(":")[0]
                records.append(rec)
    return records

def fetch_options_chain(symbol, expiration):
    url = f"{SCHWAB_API_URL}/chains"
    params = {
        "symbol": symbol,
        "fromDate": expiration.strftime("%Y-%m-%d"),
        "toDate": expiration.strftime("%Y-%m-%d"),
        "contractType": "ALL",
        "includeUnderlyingQuote": "true"
    }
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params)
    if resp.status_code != 200:
        return pd.DataFrame(), None
    chain = resp.json()
    underlying_price = None
    if chain.get('underlying'):
        underlying_price = chain['underlying'].get('last') or chain['underlying'].get('mark') or chain['underlying'].get('close')
    call_map = chain.get("callExpDateMap", {})
    put_map = chain.get("putExpDateMap", {})
    calls = flatten_options_map(call_map, "call")
    puts = flatten_options_map(put_map, "put")
    df = pd.DataFrame(calls + puts)
    return df, underlying_price

def find_sell_candidates(df, price, trade_direction):
    df = df.copy()
    needed = ["bid", "ask", "openInterest", "totalVolume", "strikePrice", "delta", "putCall"]
    if any(col not in df.columns for col in needed): return pd.DataFrame()
    df = df.dropna(subset=needed)

    if trade_direction == "Sell PUT":
        df = df[df["putCall"].str.upper() == "PUT"]
        # For scalping, we often sell closer to the money
        df = df[df["strikePrice"].between(price * 0.98, price * 1.05)]
    elif trade_direction == "Sell CALL":
        df = df[df["putCall"].str.upper() == "CALL"]
        df = df[df["strikePrice"].between(price * 0.95, price * 1.02)]
    else:
        return pd.DataFrame()

    df = df[df["bid"] > MIN_PREMIUM]
    df = df[df["openInterest"] > MIN_OI]
    df = df[df["totalVolume"] > MIN_VOLUME]
    if df.empty: return df

    df["spread_pct"] = (df["ask"] - df["bid"]) / df["bid"]
    df = df[df["spread_pct"] < 0.10] # Tighter spread for scalping
    if df.empty: return df

    df['distance_from_price'] = (df['strikePrice'] - price).abs()
    top = df.sort_values('distance_from_price', ascending=True).head(3)
    if top.empty: return top

    top["Action"] = trade_direction
    top["Exit_Price"] = (top["bid"] - 0.50).round(2)
    top["Stop_Loss"] = (top["bid"] + 1.00).round(2) # Tighter stop for scalping
    return top

if __name__ == "__main__":
    logger.info(f"--- Intraday Scalping Screener (SMI+MFI) for {SYMBOL} on {INTERVAL} interval ---")
    
    signal, trade_direction, analysis = get_scalper_signal(SYMBOL, INTERVAL)
    
    if not trade_direction:
        print(f"\nAnalysis for {SYMBOL}:")
        for key, val in analysis.items():
            print(f"- {key.title()}: {val}")
        print(f"\nConclusion: {signal}. No trade.")
        sys.exit(0)
        
    logger.info(f"Signal for {SYMBOL} is: {signal}")

    expirations = get_option_expirations(SYMBOL)
    if not expirations: sys.exit(1)
        
    target_expiration = get_target_expiration(expirations)
    if not target_expiration: sys.exit(1)

    options_df, price = fetch_options_chain(SYMBOL, target_expiration)
    if options_df.empty or price is None: sys.exit(1)

    result = find_sell_candidates(options_df, price, trade_direction)
    
    print(f"\n📅 {datetime.now(pytz.timezone('US/Eastern')).strftime('%Y-%m-%d %I:%M:%S %p EST')}")
    print(f"🎯 Signal: {signal} for {SYMBOL}")
    for key, val in analysis.items():
        print(f"  - {key.title()}: {val}")
    print("-" * 25)

    if result.empty:
        print("\n⚠️ No valid option contracts found meeting the criteria.\n")
    else:
        show_cols = ["Action", "symbol", "strikePrice", "bid", "ask", "Exit_Price", "Stop_Loss"]
        print(result[show_cols].to_string(index=False))
        snap_path = os.path.join(LOG_DIR, f"scalp_opps_{datetime.now().strftime('%Y%m%d_%H%M')}_{SYMBOL}.csv")
        result.to_csv(snap_path, index=False)