
#app/scripts/options/nearest_friday_0DTE.py
#app.scripts.options/nearest_friday_0DTE.py
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

# --- MODIFIED: Signal Generation now returns the SMI value ---

# --- MODIFIED: Signal Generation now returns the SMI value and finds the column dynamically ---
def get_smi_signal(symbol):
    """
    Fetches daily data, calculates SMI, and returns a signal and the SMI value.
    Returns: (Signal, SMI Value) -> e.g., ("Overbought", 85.3)
    """
    url = f"{SCHWAB_API_URL}/pricehistory"
    params = { "symbol": symbol, "periodType": "year", "period": 1, "frequencyType": "daily", "frequency": 1 }
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params)
    logger.info(f"PriceHistory API [{symbol}] status: {resp.status_code}")
    if resp.status_code != 200: return "Neutral", None

    data = resp.json()
    if not data or 'candles' not in data: return "Neutral", None

    df = pd.DataFrame(data['candles'])
    if df.empty: return "Neutral", None

    df.columns = [col.lower() for col in df.columns]

    # Calculate SMI with specific parameters to ensure a predictable name
    df.ta.smi(fast=5, slow=20, signal=5, append=True)

    # --- FIX: Find the SMI column name dynamically ---
    smi_col_name = None
    for col in df.columns:
        if 'smi_5_20_5' in col.lower():
            smi_col_name = col
            break # Found it, stop searching

    if not smi_col_name:
         logger.error(f"Could not find SMI column. Available columns: {df.columns.tolist()}")
         return "Neutral", None

    last_smi = df[smi_col_name].iloc[-1]
    logger.info(f"Latest SMI for {symbol} is {last_smi} (from column '{smi_col_name}')")

    if last_smi > 80: return "Overbought", last_smi
    if last_smi < 20: return "Oversold", last_smi

    return "Neutral", last_smi

# ... (get_option_expirations, get_target_expiration, get_quote_price, flatten_options_map, fetch_options_chain functions remain the same) ...
def get_option_expirations(symbol):
    url = f"{SCHWAB_API_URL}/expirationchain"
    params = {"symbol": symbol}
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params)
    logger.info(f"ExpirationChain API [{symbol}] status: {resp.status_code}")
    data = resp.json() if resp.status_code == 200 else None
    expirations = []
    if data and "expirationList" in data:
        expirations = [e["expirationDate"] for e in data["expirationList"]]
    return expirations

def get_target_expiration(expirations):
    tz = pytz.timezone('US/Eastern')
    now = datetime.now(tz).date()
    target_date = now + timedelta(days=14)
    valid_expirations = []
    for exp_str in expirations:
        try:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            if exp_date >= now: valid_expirations.append(exp_date)
        except Exception: continue
    if not valid_expirations: return None
    return min(valid_expirations, key=lambda d: abs(d - target_date))

def get_quote_price(symbol):
    url = f"{SCHWAB_API_URL}/quotes"
    params = {"symbols": symbol}
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params)
    logger.info(f"Quotes API [{symbol}] status: {resp.status_code}")
    if resp.status_code != 200: return None
    data = resp.json()
    q = data.get(symbol, {})
    price = q.get("lastPrice") or q.get("closePrice") or q.get("mark")
    return price

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
        "includeUnderlyingQuote": "true"  # <-- The key addition
    }
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params)
    logger.info(f"Chains API [{symbol}] status: {resp.status_code}")
    if resp.status_code != 200:
        return pd.DataFrame(), None # Return DataFrame and None for price

    chain = resp.json()
    
    # --- New section to extract the price ---
    underlying_price = None
    if chain.get('underlying'):
        underlying_price = chain['underlying'].get('last') or chain['underlying'].get('mark') or chain['underlying'].get('close')
    # --- End new section ---
    
    call_map = chain.get("callExpDateMap", {})
    put_map = chain.get("putExpDateMap", {})
    calls = flatten_options_map(call_map, "call")
    puts = flatten_options_map(put_map, "put")
    
    df = pd.DataFrame(calls + puts)
    
    return df, underlying_price

# --- MODIFIED: Analysis Logic now accepts smi_value and calculates confidence ---
def find_sell_candidates(df, price, signal, smi_value):
    df = df.copy()
    needed = ["bid", "ask", "openInterest", "totalVolume", "strikePrice", "delta", "putCall"]
    if any(col not in df.columns for col in needed):
        logger.error(f"Options DataFrame missing required columns.")
        return pd.DataFrame()

    df = df.dropna(subset=needed)

    if signal == "Overbought":
        trade_side = "CALL"
        df = df[df["putCall"].str.upper() == trade_side]
        df = df[df["strikePrice"] < price] 
    elif signal == "Oversold":
        trade_side = "PUT"
        df = df[df["putCall"].str.upper() == trade_side]
        df = df[df["strikePrice"] > price]
    else:
        return pd.DataFrame()

    df = df[df["bid"] > MIN_PREMIUM]
    df = df[df["openInterest"] > MIN_OI]
    df = df[df["totalVolume"] > MIN_VOLUME]
    if df.empty: return df

    df["spread_pct"] = (df["ask"] - df["bid"]) / df["bid"]
    df = df[df["spread_pct"] < 0.10]

    # --- NEW: Blended Confidence Calculation ---
    market_confidence = np.where(df["putCall"].str.upper() == "CALL", (1 - df["delta"].abs()), df["delta"].abs()) * 100
    if signal == "Overbought":
        signal_confidence = ((smi_value - 80) / 20) * 100
    else: # Oversold
        signal_confidence = ((20 - smi_value) / 20) * 100
    
    df["confidence"] = (market_confidence + signal_confidence) / 2
    # --- End New Section ---
    
    df['distance_from_price'] = (df['strikePrice'] - price).abs()
    top = df.sort_values("distance_from_price", ascending=True).head(3)
    if top.empty: return top

    top["Action"] = f"Sell {trade_side}"
    top["Exit_Price"] = (top["bid"] - 0.75).round(2)
    top["Stop_Loss"] = (top["bid"] * 1.50).round(2)
    
    est_now = datetime.now(pytz.timezone("US/Eastern"))
    top["date"] = est_now.strftime("%Y-%m-%d")
    top["time"] = est_now.strftime("%I:%M:%S %p")
    
    return top
if __name__ == "__main__":
    logger.info(f"🧪 Scalping Screener for {SYMBOL}")
    
    signal, smi_value = get_smi_signal(SYMBOL)
    if signal == "Neutral":
        smi_val_str = f"{smi_value:.2f}" if smi_value is not None else "N/A"
        print(f"⚠️ SMI for {SYMBOL} is neutral ({smi_val_str}). No trading signal.")
        sys.exit(0)
        
    logger.info(f"Signal for {SYMBOL} is: {signal} ({smi_value:.2f})")

    expirations = get_option_expirations(SYMBOL)
    if not expirations:
        logger.error("No option expirations found.")
        sys.exit(1)
        
    target_expiration = get_target_expiration(expirations)
    if not target_expiration:
        logger.error("No valid target expiration found.")
        sys.exit(1)

    # --- MODIFIED: Get options and price in one call ---
    options_df, price = fetch_options_chain(SYMBOL, target_expiration)
    
    if options_df.empty:
        logger.error("Options chain is empty or invalid.")
        sys.exit(1)
        
    if price is None:
        logger.error(f"Could not find underlying price for {SYMBOL} in the option chain response.")
        sys.exit(1)

    result = find_sell_candidates(options_df, price, signal, smi_value)
    
    print(f"\n📅 {datetime.now(pytz.timezone('US/Eastern')).strftime('%Y-%m-%d %I:%M:%S %p EST')}")
    print(f"📊 {signal}-Based Option Selling Screener for {SYMBOL} (Price: ${price:.2f} | SMI: {smi_value:.2f})\n")

    show_cols = ["Action", "symbol", "strikePrice", "bid", "ask", "confidence", "Exit_Price", "Stop_Loss", "totalVolume", "openInterest"]
    cols_present = [c for c in show_cols if c in result.columns]
    result_display = result[cols_present]
    
    if 'confidence' in result_display.columns:
        result_display['confidence'] = result_display['confidence'].map('{:,.2f}%'.format)

    if result_display.empty:
        print("⚠️ No valid option trades found after analysis.\n")
    else:
        print(result_display.to_string(index=False))
        snap_path = os.path.join(LOG_DIR, f"scalping_opps_{datetime.now().strftime('%Y%m%d_%H%M')}_{SYMBOL}.csv")
        result.to_csv(snap_path, index=False)