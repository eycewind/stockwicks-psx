#app.routes.options_picker.py
# app/routes/options_picker.py
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import logging
from dotenv import load_dotenv

# Schwab token utilities
from app.utils.stock.schwab_token import get_valid_access_token

load_dotenv()
DATA_DIR = os.getenv('DATA_DIR', '/var/www/stockwicks/data')

logging.basicConfig(level=logging.INFO)

SCHWAB_INTERVALS = {
    '1min':   ('day', 1, 'minute', 1),
    '2min':   ('day', 1, 'minute', 2),
    '3min':   ('day', 1, 'minute', 3),
    '5min':   ('day', 1, 'minute', 5),
    '15min':  ('day', 5, 'minute', 15),
    '30min':  ('day', 10, 'minute', 30),
    '1d':     ('year', 1, 'daily', 1),
}

def get_stock_data(symbol, interval, access_token):
    if interval not in SCHWAB_INTERVALS:
        raise ValueError(f"Unsupported interval. Supported: {list(SCHWAB_INTERVALS.keys())}")
    periodType, period, frequencyType, frequency = SCHWAB_INTERVALS[interval]
    url = "https://api.schwabapi.com/marketdata/v1/pricehistory"
    params = {
        "symbol": symbol.upper(),
        "periodType": periodType,
        "period": period,
        "frequencyType": frequencyType,
        "frequency": frequency,
        "needExtendedHoursData": "false",
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get(url, headers=headers, params=params)
    if response.status_code != 200:
        logging.error(f"Schwab API error: {response.status_code} {response.text}")
        return pd.DataFrame()
    data = response.json()
    candles = data.get("candles", [])
    if not candles:
        logging.error("No candles returned from Schwab")
        return pd.DataFrame()
    return pd.DataFrame({
        'timestamp': pd.to_datetime([c["datetime"] for c in candles], unit='ms'),
        'open': [c["open"] for c in candles],
        'high': [c["high"] for c in candles],
        'low': [c["low"] for c in candles],
        'close': [c["close"] for c in candles],
        'volume': [c["volume"] for c in candles]
    })


def get_expiration_date_for_week(symbol, weeks_ahead, access_token):
    url = "https://api.schwabapi.com/marketdata/v1/expirationchain"
    params = {"symbol": symbol.upper()}
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(url, headers=headers, params=params)
    if resp.status_code != 200:
        logging.error(f"Schwab expirationchain API error: {resp.status_code} {resp.text}")
        return None
    # Log full response for debugging
    try:
        logging.info(f"Schwab expirationchain API response for {symbol}: {resp.text[:1000]}")
        expiration_list = resp.json().get("expirationList", [])
    except Exception as e:
        logging.error(f"Error parsing expirationchain JSON for {symbol}: {e}")
        return None
    fridays = [
        e["expirationDate"] for e in expiration_list
        if datetime.strptime(e["expirationDate"], "%Y-%m-%d").weekday() == 4
    ]
    if len(fridays) < weeks_ahead:
        logging.error(f"Not enough fridays in expiration chain for {symbol}! Got: {fridays}")
        return None
    return datetime.strptime(fridays[weeks_ahead-1], "%Y-%m-%d")



def fetch_options_data(symbol, expiration_date, access_token):
    """
    Fetch and flatten options data from Schwab /chains endpoint for a given expiration.
    Handles empty/malformed responses robustly, returns a filtered DataFrame.
    """
    import pandas as pd
    url = "https://api.schwabapi.com/marketdata/v1/chains"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {
        "symbol": symbol.upper(),
        "fromDate": expiration_date.strftime("%Y-%m-%d"),
        "toDate": expiration_date.strftime("%Y-%m-%d"),
        "includeUnderlyingQuote": "true",
        "contractType": "ALL"
    }
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
    except Exception as e:
        logging.error(f"Error fetching Schwab chains: {e}")
        return pd.DataFrame()
    if resp.status_code != 200:
        logging.error(f"Schwab chains API error: {resp.status_code} {resp.text}")
        return pd.DataFrame()
    try:
        chain = resp.json()
        logging.info(f"Schwab /chains API response for {symbol} {expiration_date}: {str(chain)[:1000]}")
    except Exception as e:
        logging.error(f"Error parsing chains JSON for {symbol}: {e}")
        return pd.DataFrame()
    # Both call and put maps
    options = []
    for side, exp_map_key in [("call", "callExpDateMap"), ("put", "putExpDateMap")]:
        exp_map = chain.get(exp_map_key, {})
        if not exp_map:
            logging.warning(f"No {side} contracts found in chains response for {symbol}")
            continue
        for exp_key, strikes in exp_map.items():
            try:
                expiration, dte = exp_key.split(":")
                dte = int(dte)
            except Exception:
                expiration = exp_key
                dte = None
            for strike, contracts in strikes.items():
                for contract in contracts:
                    options.append({
                        "optionSymbol": contract.get("symbol"),
                        "strike": contract.get("strikePrice"),
                        "side": side,
                        "ask": contract.get("askPrice"),
                        "bid": contract.get("bidPrice"),
                        "volume": contract.get("totalVolume"),
                        "openInterest": contract.get("openInterest"),
                        "expiration": expiration,
                        "dte": dte if dte is not None else contract.get("daysToExpiration"),
                        "iv": contract.get("volatility"),
                        "delta": contract.get("delta"),
                        "gamma": contract.get("gamma"),
                        "theta": contract.get("theta"),
                        "mark": contract.get("mark"),
                    })
    df = pd.DataFrame(options)
    # Filter for only usable rows
    if df.empty:
        logging.error(f"No options contracts found for {symbol} on {expiration_date}.")
        return df
    df = df.dropna(subset=["optionSymbol", "strike", "side", "ask", "bid", "openInterest", "expiration"])
    df = df[(df["strike"] > 0) & (df["ask"] > 0) & (df["bid"] >= 0)]
    logging.info(f"Returning {len(df)} contracts for {symbol} {expiration_date}")
    return df.reset_index(drop=True)



def detect_market_trend(df):
    if len(df) < 60:
        logging.warning("Insufficient data to detect trend accurately.")
        return "neutral"
    df['SMA10'] = df['close'].rolling(window=10).mean()
    df['SMA30'] = df['close'].rolling(window=30).mean()
    df['SMA60'] = df['close'].rolling(window=60).mean()
    df['RSI'] = compute_rsi(df['close'])
    df['MACD'], df['MACD_signal'] = compute_macd(df['close'])
    sma10, sma30, sma60 = df['SMA10'].iloc[-1], df['SMA30'].iloc[-1], df['SMA60'].iloc[-1]
    rsi, macd, macd_sig = df['RSI'].iloc[-1], df['MACD'].iloc[-1], df['MACD_signal'].iloc[-1]
    if pd.isna(sma10) or pd.isna(sma30) or pd.isna(rsi) or pd.isna(macd):
        logging.warning("Trend indicators have NaN values. Defaulting to neutral trend.")
        return "neutral"
    bullish = sma10 > sma30 > sma60 and rsi > 55 and macd > macd_sig
    bearish = sma10 < sma30 < sma60 and rsi < 45 and macd < macd_sig
    if bullish:
        return "uptrend"
    elif bearish:
        return "downtrend"
    else:
        return "neutral"

def compute_macd(series, span_short=12, span_long=26, span_signal=9):
    ema_short = series.ewm(span=span_short, adjust=False).mean()
    ema_long = series.ewm(span=span_long, adjust=False).mean()
    macd = ema_short - ema_long
    macd_signal = macd.ewm(span=span_signal, adjust=False).mean()
    return macd, macd_signal

def compute_rsi(series, window=14):
    delta = series.diff().dropna()
    gain = delta.where(delta > 0, 0).rolling(window).mean()
    loss = -delta.where(delta < 0, 0).rolling(window).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_recommendations(options_data, stock_price, trend="neutral"):
    required = ['delta', 'iv', 'gamma', 'theta', 'ask', 'bid', 'volume',
                'strike', 'side', 'dte', 'expiration', 'openInterest']
    for col in required:
        if col not in options_data.columns:
            logging.warning(f"Missing column {col} in options data.")
            return pd.DataFrame({'Message': [f"Missing required data ({col}) from API."]})
    opts = options_data.copy()
    opts['spread']      = opts['ask'] - opts['bid']
    opts['mid']         = (opts['ask'] + opts['bid']) / 2
    opts['spread_pct']  = opts['spread'] / opts['mid']
    opts.loc[opts['side'] == 'put', 'delta'] *= -1
    filtered = opts[
        (opts['dte'].between(5, 120)) &
        (opts['strike'].between(stock_price * 0.60, stock_price * 1.40)) &
        (opts['openInterest'] > 100) &
        (opts['spread_pct'] < 0.25) &
        (opts['iv'] > 0)
    ].dropna(subset=['delta', 'gamma', 'theta'])
    if filtered.empty:
        return pd.DataFrame({'Message': ['No contracts after adaptive filter.']})
    if trend == "uptrend":
        slice_ = filtered[(filtered['side'] == 'call') & (filtered['delta'] > 0.25)]
    elif trend == "downtrend":
        slice_ = filtered[(filtered['side'] == 'put')  & (filtered['delta'] < -0.25)]
    else:
        slice_ = filtered
    if slice_.empty:
        slice_ = filtered
    slice_['liquidity']   = slice_['openInterest'] / slice_['spread_pct']
    slice_['risk_reward'] = (slice_['gamma'].abs() * slice_['delta'].abs()) / slice_['theta'].abs()
    top = slice_.nlargest(5, 'risk_reward').copy()
    if trend == "uptrend":
        top['Action'] = 'Buy Call'
    elif trend == "downtrend":
        top['Action'] = 'Buy Put'
    else:
        top['Action'] = np.where(top['side'] == 'call', 'Sell Call', 'Sell Put')
    top['Stop_Loss']  = (top['ask'] * 0.75).round(2)
    top['Exit_Price'] = (top['ask'] * 1.25).round(2)
    num_cols = ['strike', 'ask', 'bid', 'Stop_Loss', 'Exit_Price']
    top[num_cols] = top[num_cols].astype(float).round(2)
    top['confidence'] = pd.qcut(top['liquidity'], 3, labels=['Low', 'Medium', 'High'])
    return top[['optionSymbol', 'strike', 'side', 'ask', 'bid', 'volume',
                'Stop_Loss', 'Exit_Price', 'expiration', 'confidence', 'Action']]


def get_live_quote(symbol, access_token):
    """
    Fetch live quote (last price) for a symbol using Schwab /quotes endpoint.
    Falls back to closePrice if lastPrice not present.
    """
    url = "https://api.schwabapi.com/marketdata/v1/quotes"
    params = {"symbols": symbol.upper()}
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
    except Exception as e:
        logging.error(f"Error fetching Schwab quote: {e}")
        return None
    if response.status_code != 200:
        logging.error(f"Schwab /quotes API error: {response.status_code} {response.text}")
        return None
    data = response.json()
    quote = data.get(symbol.upper(), {})
    # Try lastPrice first, fallback to closePrice
    price = quote.get("lastPrice") or quote.get("closePrice")
    if not price:
        logging.error(f"No price found in Schwab quote response for {symbol}")
        return None
    return price



if __name__ == "__main__":
    import sys
    try:
        if len(sys.argv) < 4:
            print("Usage: options_picker.py <user_id> <symbol> <weeks>")
            sys.exit(1)
        user_id, symbol, weeks = sys.argv[1], sys.argv[2], int(sys.argv[3])
        # --- Always get latest Schwab token before each run!
        schwab_token = get_valid_access_token()
        if not schwab_token:
            print("No valid Schwab API token.")
            sys.exit(1)
        df = get_stock_data(symbol, '1d', schwab_token)
        if df.empty:
            logging.error("No stock data retrieved.")
            sys.exit(1)
        stock_price = df['close'].iloc[-1]
        trend = detect_market_trend(df)
        expiration_date = get_expiration_date_for_week(symbol, weeks, schwab_token)
        if not expiration_date:
            logging.error("No expiration date found for this week count.")
            sys.exit(1)
        options_data = fetch_options_data(symbol, expiration_date, schwab_token)
        if options_data is None or options_data.empty:
            logging.error("No options data retrieved or empty options set.")
            sys.exit(1)
        recs = calculate_recommendations(options_data, stock_price, trend)
        file_path = os.path.join(DATA_DIR, str(user_id), f"{symbol}_options_recommendations.csv")
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        try:
            recs.to_csv(file_path, index=False)
            logging.info(f"✅ Recommendations saved to {file_path}")
        except PermissionError:
            fallback_path = f"/tmp/{symbol}_options_recommendations.csv"
            recs.to_csv(fallback_path, index=False)
            logging.warning(f"⚠️ Permission denied for {file_path}. Saved to fallback: {fallback_path}")
    except Exception as e:
        import traceback
        logging.error(f"Script crashed: {e}\n{traceback.format_exc()}")
        print("ERROR:", str(e))
        sys.exit(2)
