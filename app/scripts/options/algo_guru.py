# app/scripts/options/algo_guru.py
import sys, os, logging, pandas as pd, pandas_ta as ta, requests, pytz
from datetime import datetime, timedelta

# This script reuses many functions from your dashboard. We've consolidated them here.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))
from app.utils.stock.schwab_token import get_valid_access_token

SCHWAB_API_URL = "https://api.schwabapi.com/marketdata/v1"

def get_schwab_headers():
    access_token = get_valid_access_token()
    if not access_token: raise ValueError("Could not get Schwab token.")
    return {"Authorization": f"Bearer {access_token}"}


def get_schwab_intraday_data(symbol, interval='1min', limit=200):
    """
    Fetch intraday OHLCV data from Schwab API with configurable limit.
    Supports 1min, 5min, and 15min intervals.
    Returns a cleaned, time-indexed DataFrame or None.
    """
    intervals = {
        '1min': ('day', 1, 'minute', 1),
        '5min': ('day', 1, 'minute', 5),
        '15min': ('day', 1, 'minute', 15),
    }

    if interval not in intervals:
        logging.error(f"Unsupported interval '{interval}' requested in get_schwab_intraday_data.")
        return None

    periodType, period, frequencyType, frequency = intervals[interval]
    url = f"{SCHWAB_API_URL}/pricehistory"
    params = {
        "symbol": symbol,
        "periodType": periodType,
        "period": period,
        "frequencyType": frequencyType,
        "frequency": frequency,
    }

    headers = get_schwab_headers()
    try:
        resp = requests.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            logging.warning(f"Schwab API returned {resp.status_code} for {symbol} price history.")
            return None

        data = resp.json()
        if not data or 'candles' not in data:
            logging.warning(f"No candles returned for {symbol}.")
            return None

        df = pd.DataFrame(data['candles'])
        if df.empty:
            logging.warning(f"Empty DataFrame for {symbol} {interval}.")
            return None

        # Normalize & index
        df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
        df.set_index('datetime', inplace=True)
        df.columns = [col.lower() for col in df.columns]

        # Keep OHLC columns, tail 'limit' bars
        keep_cols = [c for c in df.columns if c in ('open', 'high', 'low', 'close', 'volume')]
        df = df[keep_cols].sort_index().tail(limit)

        if len(df) < 10:
            logging.warning(f"{symbol}: only {len(df)} bars returned for {interval}")
            return None

        return df

    except Exception as e:
        logging.error(f"Error fetching Schwab intraday data for {symbol}: {e}")
        return None

#
# --- The rest of your functions in this file are correct and do not need changes ---
#

def get_target_expiration(symbol):
    # ... (This function is correct)
    url = f"{SCHWAB_API_URL}/expirationchain"
    params = {"symbol": symbol}
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params)
    data = resp.json() if resp.status_code == 200 else {}
    expirations = data.get("expirationList", [])
    if not expirations: return None
    exp_dates = [datetime.strptime(e["expirationDate"], "%Y-%m-%d").date() for e in expirations]
    today = datetime.now(pytz.timezone('US/Eastern')).date()
    future_exp_dates = [d for d in exp_dates if d >= today]
    if not future_exp_dates: return None
    target_date = today + timedelta(days=14)
    return min(future_exp_dates, key=lambda d: abs(d - target_date))

def fetch_full_option_chain(symbol, expiration_date):
    # ... (This function is correct)
    url = f"{SCHWAB_API_URL}/chains"
    params = {"symbol": symbol, "fromDate": expiration_date.strftime("%Y-%m-%d"), "toDate": expiration_date.strftime("%Y-%m-%d"), "includeUnderlyingQuote": "true"}
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params)
    if resp.status_code != 200: return pd.DataFrame(), None
    chain = resp.json()
    price = chain.get('underlying', {}).get('last')
    options = []
    required = ["symbol", "strikePrice", "bid", "ask", "volatility", "delta", "openInterest"]
    for side, key in [("call", "callExpDateMap"), ("put", "putExpDateMap")]:
        for exp_key, strikes in chain.get(key, {}).items():
            for strike, contracts in strikes.items():
                for contract in contracts:
                    if all(k in contract and contract[k] is not None for k in required):
                        options.append({
                            "symbol": contract["symbol"].replace(" ", ""), "strike": contract["strikePrice"], "side": side,
                            "bid": contract["bid"], "ask": contract["ask"], "iv": contract["volatility"], 
                            "delta": contract["delta"], "oi": contract["openInterest"]
                        })
    return pd.DataFrame(options), price

def get_market_context():
    # ... (This function is correct)
    spy_df = get_schwab_intraday_data("SPY")
    if spy_df is None or len(spy_df) < 2: return "Unknown"
    spy_df.ta.vwap(append=True)
    last_price = spy_df['close'].iloc[-1]
    vwap_col = next((col for col in spy_df.columns if 'vwap' in col.lower()), None)
    if not vwap_col: return "Unknown"
    last_vwap = spy_df[vwap_col].iloc[-1]
    return "Uptrend" if last_price > last_vwap else "Downtrend"

def analyze_symbol_technicals(df):
    # ... (This function is correct)
    if df is None or len(df) < 26: return None
    df.ta.macd(append=True)
    df.ta.vwap(append=True)
    vwap_col = next((col for col in df.columns if 'vwap' in col.lower()), None)
    if not vwap_col: return None
    last = df.iloc[-1]
    return {
        "price": last['close'], "vwap": last[vwap_col],
        "trend": "Uptrend" if last['close'] > last[vwap_col] else "Downtrend",
        "momentum": "Bullish" if last['MACD_12_26_9'] > last['MACDs_12_26_9'] else "Bearish"
    }

def find_oi_walls(options_df, price):
    # ... (This function is correct)
    walls = {"put": None, "call": None}
    puts = options_df[(options_df['side'] == 'put') & (options_df['strike'] < price)]
    if not puts.empty: walls['put'] = puts.loc[puts['oi'].idxmax()]
    calls = options_df[(options_df['side'] == 'call') & (options_df['strike'] > price)]
    if not calls.empty: walls['call'] = calls.loc[calls['oi'].idxmax()]
    return walls

def calculate_sell_score(option, tech, market_trend, walls):
    # ... (This function is correct)
    score = 0
    if option['iv'] > 75: score += 40
    elif option['iv'] > 50: score += 30
    elif option['iv'] > 30: score += 15
    if option['side'] == 'put':
        if tech['trend'] == "Uptrend" and tech['momentum'] == "Bullish": score += 30
        elif tech['trend'] == "Uptrend": score += 15
    elif option['side'] == 'call':
        if tech['trend'] == "Downtrend" and tech['momentum'] == "Bearish": score += 30
        elif tech['trend'] == "Downtrend": score += 15
    if option['side'] == 'put' and walls['put'] is not None:
        distance = option['strike'] - walls['put']['strike']
        if distance > 20: score += 20
        elif distance > 10: score += 10
    elif option['side'] == 'call' and walls['call'] is not None:
        distance = walls['call']['strike'] - option['strike']
        if distance > 20: score += 20
        elif distance > 10: score += 10
    if option['side'] == 'put' and market_trend == "Uptrend": score += 10
    elif option['side'] == 'call' and market_trend == "Downtrend": score += 10
    return score

def find_guru_choice(symbol: str):
    # ... (This function is correct)
    try:
        market_trend = get_market_context()
        symbol_df = get_schwab_intraday_data(symbol)
        technicals = analyze_symbol_technicals(symbol_df)
        if not technicals: return None
        expiration = get_target_expiration(symbol)
        if not expiration: return None
        options_df, price = fetch_full_option_chain(symbol, expiration)
        if options_df.empty: return None
        walls = find_oi_walls(options_df, price)
        otm_puts = options_df[(options_df['side'] == 'put') & (options_df['strike'] < price)].sort_values('strike', ascending=False).head(3)
        otm_calls = options_df[(options_df['side'] == 'call') & (options_df['strike'] > price)].sort_values('strike', ascending=True).head(3)
        target_options = pd.concat([otm_puts, otm_calls])
        results = []
        for index, option_data in target_options.iterrows():
            option_data['sell_score'] = calculate_sell_score(option_data, technicals, market_trend, walls)
            prob_profit = (1 - abs(option_data['delta'])) * 100
            option_data['prob_profit_sort'] = prob_profit
            results.append(option_data)
        if not results: return None
        results_df = pd.DataFrame(results)
        sorted_df = results_df.sort_values(by=['sell_score', 'prob_profit_sort', 'bid'], ascending=[False, False, False])
        guru_choice = sorted_df.iloc[0].to_dict()
        guru_choice['profit_target'] = round(guru_choice['bid'] * 0.95, 2)
        guru_choice['stop_loss'] = round(guru_choice['bid'] * 1.10, 2)
        guru_choice['expiration'] = expiration.strftime('%Y-%m-%d')
        return guru_choice
    except Exception as e:
        logging.error(f"Error in find_guru_choice for {symbol}: {e}")
        return None