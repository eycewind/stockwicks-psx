#app/scripts/options/iv_scalper.py
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
MIN_PREMIUM = 5.00
MIN_IV = 40.0
TARGET_DELTA = 0.30
SCHWAB_API_URL = "https://api.schwabapi.com/marketdata/v1"

def get_schwab_headers():
    from app.utils.stock.schwab_token import get_valid_access_token
    access_token = get_valid_access_token()
    if not access_token:
        raise ValueError("SCHWAB_ACCESS_TOKEN missing or could not refresh.")
    return {"Authorization": f"Bearer {access_token}"}

def get_intraday_data(symbol, interval):
    intervals = {'1min': ('day', 1, 'minute', 1), '5min': ('day', 1, 'minute', 5)}
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
        logger.error("Price history data is missing required columns for VWAP.")
        return None
    return df

def fetch_and_analyze_options(symbol, expiration_date):
    url = f"{SCHWAB_API_URL}/chains"
    params = { "symbol": symbol, "fromDate": expiration_date.strftime("%Y-%m-%d"), "toDate": expiration_date.strftime("%Y-%m-%d"), "includeUnderlyingQuote": "true", "contractType": "ALL" }
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params)
    logger.info(f"Chains API [{symbol}] status: {resp.status_code}")
    if resp.status_code != 200:
        logger.error(f"Chains API Error: {resp.text}")
        return pd.DataFrame(), None

    chain = resp.json()
    price = None
    if chain.get('underlying'):
        price = chain['underlying'].get('last') or chain['underlying'].get('mark')
    logger.info(f"Extracted underlying price from chain: {price}")

    options = []
    required_keys = ["symbol", "strikePrice", "bid", "ask", "volatility", "delta"]
    for side, exp_map_key in [("call", "callExpDateMap"), ("put", "putExpDateMap")]:
        exp_map = chain.get(exp_map_key, {})
        for exp_key, strikes in exp_map.items():
            for strike, contracts in strikes.items():
                for contract in contracts:
                    if all(key in contract and contract[key] is not None for key in required_keys):
                        options.append({
                            "symbol": contract["symbol"].replace(" ", ""), "strike": contract["strikePrice"], "side": side,
                            "bid": contract["bid"], "ask": contract["ask"], "iv": contract["volatility"], "delta": contract["delta"]
                        })
    
    df = pd.DataFrame(options)
    if df.empty:
        logger.warning(f"DataFrame is empty after parsing options.")
    return df, price

def get_target_expiration(symbol):
    url = f"{SCHWAB_API_URL}/expirationchain"
    params = {"symbol": symbol}
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params)
    data = resp.json() if resp.status_code == 200 else {}
    expirations = [e["expirationDate"] for e in data.get("expirationList", [])]
    if not expirations: return None
    
    today = datetime.now(pytz.timezone('US/Eastern')).date()
    future_exp_dates = [datetime.strptime(d, "%Y-%m-%d").date() for d in expirations if datetime.strptime(d, "%Y-%m-%d").date() >= today]
    if not future_exp_dates: return None
    return min(future_exp_dates)

if __name__ == "__main__":
    logger.info(f"--- IV Scalper for {SYMBOL} on {INTERVAL} interval ---")
    
    price_df = get_intraday_data(SYMBOL, INTERVAL)
    if price_df is None or len(price_df) < 26:
        print("Insufficient intraday price data for analysis. Exiting.")
        sys.exit(1)
        
    price_df.ta.macd(append=True)
    price_df.ta.vwap(append=True)
    
    vwap_col_name = next((col for col in price_df.columns if 'vwap' in col.lower()), None)
    if not vwap_col_name:
        print("Could not calculate VWAP. Exiting."); sys.exit(1)
        
    last_row = price_df.iloc[-1]
    last_price = last_row['close']
    macd_line = last_row['MACD_12_26_9']
    signal_line = last_row['MACDs_12_26_9']
    vwap = last_row[vwap_col_name]
    
    intraday_trend = "Uptrend" if last_price > vwap else "Downtrend"
    momentum = "Bullish" if macd_line > signal_line else "Bearish"
    
    print(f"\nAnalysis for {SYMBOL}:")
    print(f"- Current Price: ${last_price:.2f}")
    print(f"- VWAP: ${vwap:.2f}")
    print(f"- Intraday Trend: {intraday_trend} (Price vs VWAP)")
    print(f"- 1-min Momentum: {momentum} (MACD vs Signal)")

    # --- UPGRADED SCALPING LOGIC ---
    trade_direction = None
    signal_message = ""
    if intraday_trend == "Downtrend" and momentum == "Bullish":
        trade_direction = "Sell CALL"
        signal_message = "Fading bounce in a downtrend."
    elif intraday_trend == "Uptrend" and momentum == "Bearish":
        trade_direction = "Sell PUT"
        signal_message = "Selling premium on a dip in an uptrend."
    
    if not trade_direction:
        print("\nConclusion: No high-probability scalping setup found (waiting for dip in uptrend or pop in downtrend).")
        sys.exit(0)

    expiration = get_target_expiration(SYMBOL)
    if not expiration:
        print("\nCould not find a valid options expiration date. Exiting."); sys.exit(1)
        
    options_df, live_price = fetch_and_analyze_options(SYMBOL, expiration)
    if options_df.empty or live_price is None:
        print("\nCould not retrieve a valid option chain. Exiting."); sys.exit(1)
        
    atm_strike = min(options_df['strike'], key=lambda x:abs(x-live_price))
    atm_iv = options_df[options_df['strike'] == atm_strike]['iv'].mean()
    
    print(f"- Target Expiration: {expiration}")
    print(f"- ATM ({atm_strike}) IV: {atm_iv:.2f}%")
    
    if atm_iv < MIN_IV:
        print(f"\nConclusion: IV is too low ({atm_iv:.2f}%) to sell premium. Minimum required: {MIN_IV}%. No trade.")
        sys.exit(0)
    
    print(f"\n🎯 Signal: High IV detected. {signal_message} Proceeding with {trade_direction}.")
    print("-" * 30)

    if trade_direction == "Sell PUT":
        candidates = options_df[options_df['side'] == 'put'].copy()
        candidates['delta_dist'] = (candidates['delta'] - (-TARGET_DELTA)).abs()
    else: # Sell CALL
        candidates = options_df[options_df['side'] == 'call'].copy()
        candidates['delta_dist'] = (candidates['delta'] - (-TARGET_DELTA)).abs()

    candidates = candidates[candidates['bid'] > MIN_PREMIUM]
    if candidates.empty:
        print("\n⚠️ No options found with premium > ${:.2f}.\n".format(MIN_PREMIUM)); sys.exit(0)
        
    top_picks = candidates.sort_values('delta_dist').head(3)

    top_picks["Action"] = trade_direction
    top_picks["Exit_Price"] = (top_picks["bid"] - 0.50).round(2)
    top_picks["Stop_Loss"] = (top_picks["bid"] + 1.00).round(2)
    show_cols = ["Action", "symbol", "strike", "bid", "ask", "delta", "iv", "Exit_Price", "Stop_Loss"]
    
    print(top_picks[show_cols].to_string(index=False))