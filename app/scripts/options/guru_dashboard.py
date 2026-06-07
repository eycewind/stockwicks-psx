import sys
import os
import logging
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import pandas_ta as ta
import requests
import pytz
from dotenv import load_dotenv

# Add the project root to Python path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# === CONFIG & LOGGING ===================================
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SYMBOL = "SPY"  # default; overridden in __main__ via argparse
SCHWAB_API_URL = "https://api.schwabapi.com/marketdata/v1"

# === HELPER FUNCTIONS ===================================
def _now_et():
    """Get current time in Eastern Time"""
    return datetime.now(pytz.timezone('US/Eastern'))
def get_schwab_headers():
    from app.utils.stock.schwab_token import get_valid_access_token
    access_token = get_valid_access_token()
    if not access_token: raise ValueError("Could not get Schwab access token.")
    return {"Authorization": f"Bearer {access_token}"}

def get_schwab_price_history(symbol, periodType='day', period=1, frequencyType='minute', frequency=1):
    """Fetches historical price data from Schwab API."""
    url = f"{SCHWAB_API_URL}/pricehistory"
    params = {"symbol": symbol, "periodType": periodType, "period": period, "frequencyType": frequencyType, "frequency": frequency}
    headers = get_schwab_headers()
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        logger.info(f"Historical PriceHistory API [{symbol} - {frequencyType}:{frequency}] status: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        if not data or 'candles' not in data: return None
        df = pd.DataFrame(data['candles'])
        df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
        df.set_index('datetime', inplace=True)
        df.columns = [col.lower() for col in df.columns]
        if not all(c in df.columns for c in ['high', 'low', 'close', 'volume']):
            return None
        return df
    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed for {symbol}: {e}")
        return None

def get_schwab_todays_intraday_data(symbol, frequency=1):
    """
    Fetches intraday price data. Switches between live and historical requests
    based on whether the market is open to prevent API errors.
    """
    eastern = pytz.timezone('US/Eastern')
    now_et = datetime.now(eastern)
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)

    is_market_open = (now_et.weekday() < 5) and (market_open <= now_et <= market_close)

    params = {
        "symbol": symbol,
        "frequencyType": "minute",
        "frequency": frequency
    }

    if is_market_open:
        logger.info(f"Market is OPEN. Fetching live intraday data for {symbol}.")
        date_str = now_et.strftime("%Y-%m-%d")
        params["startDate"] = date_str
        params["endDate"] = date_str
    else:
        logger.info(f"Market is CLOSED. Fetching last completed day's data for {symbol}.")
        params["periodType"] = "day"
        params["period"] = 1
    
    url = f"{SCHWAB_API_URL}/pricehistory"
    headers = get_schwab_headers()
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        logger.info(f"Intraday API [{symbol} - minute:{frequency}] status: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        if not data or 'candles' not in data: return None
        df = pd.DataFrame(data['candles'])
        df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
        df.set_index('datetime', inplace=True)
        df.columns = [col.lower() for col in df.columns]
        if not all(c in df.columns for c in ['high', 'low', 'close', 'volume']):
            return None
        return df
    except requests.exceptions.RequestException as e:
        logger.error(f"API request for intraday data failed for {symbol}: {e}")
        return None

def get_all_expirations(symbol):
    """Get all available expiration dates for a symbol."""
    url = f"{SCHWAB_API_URL}/expirationchain"
    params = {"symbol": symbol}
    headers = get_schwab_headers()
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code != 200:
            logger.error(f"Failed to get expiration chain: {resp.status_code}")
            return []
        data = resp.json()
        expirations = data.get("expirationList", [])
        if not expirations: 
            return []
        exp_dates = [datetime.strptime(e["expirationDate"], "%Y-%m-%d").date() for e in expirations]
        today = datetime.now(pytz.timezone('US/Eastern')).date()
        future_exp_dates = [d for d in exp_dates if d >= today]
        return sorted(future_exp_dates)
    except Exception as e:
        logger.error(f"Error in get_all_expirations: {e}")
        return []

def get_target_expiration(symbol, trade_type="day", target_days=None):
    """
    Get target expiration date for options based on trade type.
    
    Args:
        symbol: The stock symbol
        trade_type: "day" for day trading, "multi" for multi-day/swing trading
        target_days: For multi-day, target days to expiration (e.g., 30, 45, 60)
    """
    all_expirations = get_all_expirations(symbol)
    if not all_expirations:
        return None
    
    today = datetime.now(pytz.timezone('US/Eastern')).date()
    
    if trade_type == "day":
        # For day trading, look for weekly or near-term expirations (0-14 days)
        near_term_dates = [d for d in all_expirations if (d - today).days <= 14]
        if near_term_dates:
            return min(near_term_dates)  # Closest expiration
        else:
            # Fall back to closest expiration
            return min(all_expirations)
    
    elif trade_type == "multi":
        # For multi-day trading, use user-specified target days or default
        if target_days:
            target_date = today + timedelta(days=target_days)
            # Find closest expiration to target date
            return min(all_expirations, key=lambda d: abs((d - today).days - target_days))
        else:
            # Default to 30-45 days out
            target_date = today + timedelta(days=30)
            return min(all_expirations, key=lambda d: abs(d - target_date))
    
    else:
        # Default behavior
        target_date = today + timedelta(days=30)
        return min(all_expirations, key=lambda d: abs(d - target_date))


def fetch_full_option_chain(symbol, expiration_date):
    """Fetch option chain for a specific expiration date."""
    url = f"{SCHWAB_API_URL}/chains"
    params = {
        "symbol": symbol, 
        "fromDate": expiration_date.strftime("%Y-%m-%d"), 
        "toDate": expiration_date.strftime("%Y-%m-%d"), 
        "includeUnderlyingQuote": "true", 
        "strategy": "SINGLE", 
        "range": "ALL"
    }
    headers = get_schwab_headers()
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code != 200: 
            logger.error(f"Option chain API failed: {resp.status_code}")
            return pd.DataFrame(), None
        chain = resp.json()
        price = chain.get('underlying', {}).get('last')
        if not price:
            # Try alternative price source
            quote = chain.get('underlyingQuote', {})
            price = quote.get('lastPrice') or quote.get('askPrice') or quote.get('bidPrice')
        
        options = []
        
        # Get current price for Greeks estimation
        current_price = price or 0
        
        for side, key in [("call", "callExpDateMap"), ("put", "putExpDateMap")]:
            for exp_key, strikes in chain.get(key, {}).items():
                for strike, contracts in strikes.items():
                    for contract in contracts:
                        # Extract values with proper handling of missing/invalid data
                        strike_price = float(contract.get("strikePrice", 0))
                        
                        # Check if delta is valid (not -999 or None)
                        delta_raw = contract.get("delta")
                        if delta_raw is not None and delta_raw != -999:
                            delta = float(delta_raw)
                        else:
                            # Estimate delta based on moneyness if missing/invalid
                            if side == "call":
                                if current_price > 0:
                                    # Simple delta estimation for calls
                                    moneyness = current_price / strike_price
                                    if moneyness > 1.1:  # Deep ITM
                                        delta = 0.85
                                    elif moneyness > 1.05:  # ITM
                                        delta = 0.70
                                    elif moneyness > 0.95:  # ATM
                                        delta = 0.50
                                    elif moneyness > 0.90:  # OTM
                                        delta = 0.30
                                    else:  # Deep OTM
                                        delta = 0.15
                                else:
                                    delta = 0.50  # Default
                            else:  # put
                                if current_price > 0:
                                    # Simple delta estimation for puts (negative for puts)
                                    moneyness = strike_price / current_price
                                    if moneyness > 1.1:  # Deep ITM for puts
                                        delta = -0.85
                                    elif moneyness > 1.05:  # ITM
                                        delta = -0.70
                                    elif moneyness > 0.95:  # ATM
                                        delta = -0.50
                                    elif moneyness > 0.90:  # OTM
                                        delta = -0.30
                                    else:  # Deep OTM
                                        delta = -0.15
                                else:
                                    delta = -0.50  # Default
                        
                        # Check if IV is valid (not -999 or None)
                        iv_raw = contract.get("volatility")
                        if iv_raw is not None and iv_raw != -999 and iv_raw > 0:
                            iv = float(iv_raw)
                        else:
                            # Estimate IV based on typical values for the stock
                            iv = 35.0  # Default 35% IV
                        
                        # Only include if we have basic required fields
                        if (contract.get("symbol") and 
                            contract.get("bid") is not None and 
                            contract.get("ask") is not None):
                            
                            options.append({
                                "symbol": contract["symbol"].replace(" ", ""), 
                                "strike": strike_price, 
                                "side": side,
                                "bid": float(contract["bid"]), 
                                "ask": float(contract["ask"]), 
                                "iv": iv, 
                                "delta": delta, 
                                "oi": int(contract.get("openInterest", 0)),
                                "volume": int(contract.get("totalVolume", 0))
                            })
        
        df = pd.DataFrame(options)
        if not df.empty:
            # Filter out options with unreasonable prices
            df = df[df['bid'] > 0.01]  # Remove options with no bid
            df = df[df['ask'] < 1000]   # Remove options with crazy ask prices
            
            # For day trading, focus on options within reasonable strike range
            if price:
                # Keep strikes within ±30% of current price
                min_strike = price * 0.7
                max_strike = price * 1.3
                df = df[(df['strike'] >= min_strike) & (df['strike'] <= max_strike)]
        
        return df, price
        
    except Exception as e:
        logger.error(f"Error fetching option chain: {e}")
        return pd.DataFrame(), None

# === DAY TRADE ANALYSIS FUNCTIONS ===================================

def get_momentum_score(symbol, current_price):
    """
    Calculate momentum score specifically for day trading options
    Returns score from 0-100 and trend direction
    """
    # Get 15-min data for last 5 days
    df = get_schwab_price_history(symbol, periodType='day', period=5, frequencyType='minute', frequency=15)
    
    if df is None or len(df) < 20:
        logger.warning(f"Insufficient data for momentum analysis on {symbol}")
        return {"score": 50, "trend": "neutral", "volatility": "medium", "atr": 0.015}
    
    # Calculate technical indicators for momentum
    df.ta.rsi(length=14, append=True)
    df.ta.atr(length=14, append=True)
    df.ta.ema(length=9, append=True)
    df.ta.ema(length=20, append=True)
    
    # Initialize scores
    score = 50  # Neutral starting point
    trend = "neutral"
    volatility = "medium"
    atr_value = 0.015  # Default ATR
    
    # 1. RSI momentum (30-70 range is ideal for day trading)
    if 'RSI_14' in df.columns:
        rsi = df['RSI_14'].iloc[-1]
        if not pd.isna(rsi):
            if 40 < rsi < 60:
                score += 15  # Good balance, not overbought/oversold
            elif 30 < rsi < 70:
                score += 10
    
    # 2. EMA alignment (short-term trend)
    if 'EMA_9' in df.columns and 'EMA_20' in df.columns:
        ema9 = df['EMA_9'].iloc[-1]
        ema20 = df['EMA_20'].iloc[-1]
        if not pd.isna(ema9) and not pd.isna(ema20):
            if ema9 > ema20 and df['close'].iloc[-1] > ema9:
                score += 25  # Strong uptrend
                trend = "uptrend"
            elif ema9 < ema20 and df['close'].iloc[-1] < ema9:
                score += 25  # Strong downtrend
                trend = "downtrend"
            else:
                score += 10
                trend = "neutral"
    
    # 3. Volume analysis
    if 'volume' in df.columns:
        recent_volume = df['volume'].tail(10).mean()
        avg_volume = df['volume'].mean()
        if recent_volume > avg_volume * 1.5:
            score += 15  # Above average volume
    
    # 4. ATR volatility (normalized)
    if 'ATRr_14' in df.columns:
        atr = df['ATRr_14'].iloc[-1]
        if not pd.isna(atr):
            atr_value = atr
            if atr > 0.02:  # 2%+ daily range
                score += 10
                volatility = "high"
            elif atr > 0.015:
                score += 5
                volatility = "medium"
            else:
                volatility = "low"
    
    # 5. Price action - recent momentum
    returns = df['close'].pct_change()
    if len(returns) >= 5:
        recent_momentum = returns.tail(5).mean()
        if not pd.isna(recent_momentum) and abs(recent_momentum) > 0.005:  # 0.5% average move
            score += 10
    
    return {
        "score": min(score, 100),
        "trend": trend,
        "volatility": volatility,
        "rsi": rsi if 'rsi' in locals() else None,
        "atr": atr_value
    }

def calculate_stop_loss(option, current_price, momentum_data):
    """
    Calculate intelligent stop loss based on option characteristics and market conditions
    """
    entry_price = option['bid']

    delta = abs(option['delta'])
    iv = option['iv']
    
    # Base stop loss multiplier
    base_stop = 1.50  # 50% loss as default
    
    # Adjust based on Implied Volatility
    if iv > 70:
        iv_factor = 1.3  # Tighter stop for high IV (earnings, events)
    elif iv > 50:
        iv_factor = 1.2
    elif iv > 30:
        iv_factor = 1.0
    else:
        iv_factor = 0.9  # Wider stop for low IV
    
    # Adjust based on Delta (risk exposure)
    if delta > 0.40:
        delta_factor = 1.3  # Tighter stop for high delta
    elif delta > 0.30:
        delta_factor = 1.1
    elif delta > 0.20:
        delta_factor = 1.0
    else:
        delta_factor = 0.8  # Wider stop for low delta
    
    # Adjust based on market volatility
    volatility_factor = {
        "high": 1.3,
        "medium": 1.0,
        "low": 0.8
    }.get(momentum_data['volatility'], 1.0)
    
    # Adjust based on trend alignment
    trend_factor = 1.0
    if momentum_data['trend'] == "uptrend" and option['side'] == "call":
        trend_factor = 1.2  # Tighter stop when selling calls in uptrend
    elif momentum_data['trend'] == "downtrend" and option['side'] == "put":
        trend_factor = 1.2  # Tighter stop when selling puts in downtrend
    elif momentum_data['trend'] == "neutral":
        trend_factor = 0.9  # Wider stop in neutral market
    
    # Calculate final stop multiplier
    stop_multiplier = base_stop * iv_factor * delta_factor * volatility_factor * trend_factor
    
    # Apply bounds
    stop_multiplier = max(1.25, min(stop_multiplier, 2.0))  # Between 25% and 100% loss
    
    stop_price = round(entry_price * stop_multiplier, 2)
    max_loss = stop_price - entry_price
    loss_percentage = ((stop_price - entry_price) / entry_price) * 100
    
    return {
        'stop_price': stop_price,
        'stop_multiplier': stop_multiplier,
        'max_loss_per_contract': max_loss,
        'loss_percentage': loss_percentage,
        'position_size': calculate_position_size(max_loss)
    }



def calculate_stop_loss_buy(option, momentum_data, trade_type="day"):
    """
    Stop loss for BUY-to-open (long option):
    - Entry is ASK (debit)
    - Stop is BELOW entry (max tolerated loss)
    - Adjusts for trade type (day vs multi-day)
    """
    entry_price = option['ask']
    delta = abs(option['delta'])
    iv = option['iv']

    if trade_type == "day":
        # Day trading: tighter stops
        base_loss = 0.45  # 45% premium loss
    else:
        # Multi-day/swing: wider stops
        base_loss = 0.60  # 60% premium loss

    # Higher IV => wider swings => allow slightly more room (but cap)
    if iv > 70:
        iv_factor = 1.15
    elif iv > 50:
        iv_factor = 1.05
    elif iv > 30:
        iv_factor = 1.0
    else:
        iv_factor = 0.95

    # Higher delta => moves faster => can use slightly tighter loss
    if delta > 0.40:
        delta_factor = 0.90
    elif delta > 0.30:
        delta_factor = 0.95
    else:
        delta_factor = 1.00

    vol_factor = {'high': 1.10, 'medium': 1.0, 'low': 0.95}.get(momentum_data.get('volatility'), 1.0)

    loss_frac = base_loss * iv_factor * delta_factor * vol_factor
    
    if trade_type == "day":
        loss_frac = max(0.25, min(loss_frac, 0.60))  # 25% .. 60%
    else:
        loss_frac = max(0.35, min(loss_frac, 0.75))  # 35% .. 75%

    stop_price = round(entry_price * (1.0 - loss_frac), 2)
    max_loss = entry_price - stop_price
    stop_multiplier = round(1.0 - loss_frac, 4)

    return {
        'stop_price': stop_price,
        'loss_fraction': loss_frac,
        'stop_multiplier': stop_multiplier,
        'max_loss_per_contract': max_loss,
        'position_size': calculate_position_size(max_loss),
    }


def calculate_confidence_score_buy(option, momentum_data, trade_type="day"):
    """
    Confidence for BUY-to-open (trend-following):
      - Uptrend favors CALL buys
      - Downtrend favors PUT buys
    - Adjusts for trade type
    """
    score = 0
    trend = momentum_data.get('trend', 'neutral')
    momentum_score = float(momentum_data.get('score', 50))
    option_side = option.get('side')
    delta = abs(option.get('delta', 0.0))
    iv = option.get('iv', 0.0)

    # 1) Trend alignment (40)
    if trend == "uptrend" and option_side == "call":
        score += 40
    elif trend == "downtrend" and option_side == "put":
        score += 40
    elif trend == "neutral":
        score += 20
    else:
        score += 5

    # 2) Momentum strength (20)
    strength = abs(momentum_score - 50) / 50  # 0..1
    if trend in ("uptrend", "downtrend"):
        if strength > 0.6:
            score += 20
        elif strength > 0.4:
            score += 15
        else:
            score += 10
    else:
        score += 8

    # 3) Delta sweet spot - varies by trade type
    if trade_type == "day":
        # Day trading: 0.25-0.55 works well
        if 0.25 <= delta <= 0.55:
            score += 20
        elif 0.20 <= delta <= 0.65:
            score += 14
        else:
            score += 6
    else:
        # Multi-day: can use slightly lower delta for more leverage
        if 0.20 <= delta <= 0.50:
            score += 20
        elif 0.15 <= delta <= 0.60:
            score += 14
        else:
            score += 6

    # 4) Liquidity proxy via bid/ask spread (10)
    bid = float(option.get('bid', 0.0))
    ask = float(option.get('ask', 0.0))
    if bid > 0 and ask > 0:
        spread_pct = (ask - bid) / ((ask + bid) / 2)
        if spread_pct <= 0.08:
            score += 10
        elif spread_pct <= 0.12:
            score += 7
        else:
            score += 3

    # 5) IV sanity (10)
    if 20 <= iv <= 120:
        score += 10
    elif 10 <= iv <= 160:
        score += 6
    else:
        score += 3

    return max(0, min(int(round(score)), 100))


def calculate_position_size(max_loss_per_contract):
    """
    Calculate appropriate position size based on risk tolerance
    """
    max_risk_per_trade = 100  # Don't risk more than $100 per trade
    max_contracts = max(1, int(max_risk_per_trade / max_loss_per_contract))
    
    # Additional constraints
    if max_loss_per_contract > 50:
        max_contracts = 1  # Limit to 1 contract for high-risk trades
    elif max_loss_per_contract > 25:
        max_contracts = min(max_contracts, 2)
    elif max_loss_per_contract > 10:
        max_contracts = min(max_contracts, 3)
    
    return max_contracts

def calculate_confidence_score(option, momentum_data, stop_loss_info):
    """
    Confidence score (0-100) for CREDIT selling (SELL to open).
    Intuition:
      - In uptrend: selling PUTs is generally safer than selling CALLs
      - In downtrend: selling CALLs is generally safer than selling PUTs
      - Prefer liquid contracts (tight spread), sensible delta, and not-crazy IV
    """
    score = 0

    trend = momentum_data.get("trend", "neutral")
    momentum_score = float(momentum_data.get("score", 50))
    option_side = option.get("side", "call")
    delta = abs(float(option.get("delta", 0.0) or 0.0))
    iv = float(option.get("iv", 0.0) or 0.0)

    # 1) Trend alignment (40)
    if trend == "uptrend":
        score += 40 if option_side == "put" else 18
    elif trend == "downtrend":
        score += 40 if option_side == "call" else 18
    else:
        score += 26

    # 2) Momentum strength (10)
    strength = abs(momentum_score - 50) / 50  # 0..1
    if trend in ("uptrend", "downtrend"):
        if strength > 0.6:
            score += 10
        elif strength > 0.4:
            score += 8
        else:
            score += 6
    else:
        score += 6

    # 3) Delta fit (20): for 0DTE/1DTE credit selling, ~0.15-0.35 is typical
    if 0.15 <= delta <= 0.35:
        score += 20
    elif 0.10 <= delta <= 0.45:
        score += 14
    else:
        score += 7

    # 4) Liquidity via spread (20)
    bid = float(option.get("bid", 0.0) or 0.0)
    ask = float(option.get("ask", 0.0) or 0.0)
    if bid > 0 and ask > 0:
        spread_pct = (ask - bid) / ((ask + bid) / 2.0)
        if spread_pct <= 0.06:
            score += 20
        elif spread_pct <= 0.10:
            score += 15
        elif spread_pct <= 0.14:
            score += 10
        else:
            score += 4
    else:
        score += 2

    # 5) IV sanity (10)
    if 20 <= iv <= 120:
        score += 10
    elif 10 <= iv <= 160:
        score += 7
    else:
        score += 4

    return max(0, min(int(round(score)), 100))

def get_confidence_level(score):
    """Convert numeric score to confidence level"""
    if score >= 80:
        return "HIGH"
    elif score >= 65:
        return "MEDIUM-HIGH"
    elif score >= 50:
        return "MEDIUM"
    elif score >= 35:
        return "MEDIUM-LOW"
    else:
        return "LOW"



def calculate_option_probabilities(option, current_price, days_to_expiry, momentum_data, trade_type="day"):
    """
    Calculate realistic probability metrics for options
    Adjusts for trade type (day vs multi-day)
    """
    strike = option['strike']
    side = option['side']
    delta = abs(option['delta'])
    premium = option['ask']  # For BUY
    
    # Days to expiration factor
    dte_factor = min(days_to_expiry / 30, 1.0)
    
    # Calculate moneyness (how far ITM/OTM)
    if side == 'call':
        moneyness = current_price / strike
        # How far OTM as percentage
        if strike > current_price:
            otm_percent = (strike - current_price) / current_price * 100
            # Drastically reduce probability for OTM options
            if otm_percent > 10:  # More than 10% OTM
                prob_itm = max(1, 30 - otm_percent)
            elif otm_percent > 5:  # 5-10% OTM
                prob_itm = max(5, 40 - otm_percent * 2)
            else:  # 0-5% OTM
                prob_itm = max(10, 45 - otm_percent)
        else:
            # ITM call
            itm_percent = (current_price - strike) / current_price * 100
            prob_itm = min(90, 50 + itm_percent * 1.5)
    else:  # put
        moneyness = strike / current_price
        if strike < current_price:
            # OTM put
            otm_percent = (current_price - strike) / current_price * 100
            if otm_percent > 10:
                prob_itm = max(1, 30 - otm_percent)
            elif otm_percent > 5:
                prob_itm = max(5, 40 - otm_percent * 2)
            else:
                prob_itm = max(10, 45 - otm_percent)
        else:
            # ITM put
            itm_percent = (strike - current_price) / current_price * 100
            prob_itm = min(90, 50 + itm_percent * 1.5)
    
    # Adjust for days to expiration - less time means lower probability for OTM
    if trade_type == "day":
        # Day trading: time decay is critical
        if days_to_expiry <= 2:
            if (side == 'call' and strike > current_price) or (side == 'put' and strike < current_price):
                prob_itm = prob_itm * 0.3  # 70% reduction for OTM with 0-2 DTE
            else:
                prob_itm = prob_itm * 0.8
        elif days_to_expiry <= 5:
            if (side == 'call' and strike > current_price) or (side == 'put' and strike < current_price):
                prob_itm = prob_itm * 0.5  # 50% reduction for OTM with 3-5 DTE
    else:
        # Multi-day: more time, so better probability
        if days_to_expiry <= 7:
            if (side == 'call' and strike > current_price) or (side == 'put' and strike < current_price):
                prob_itm = prob_itm * 0.7
            else:
                prob_itm = prob_itm * 0.9
    
    # Calculate probability of hitting profit target
    target_multiplier = 1.10 if trade_type == "day" else 1.20  # 10% for day, 20% for multi-day
    target_price = premium * target_multiplier
    
    # Calculate required stock move for profit
    if side == 'call':
        required_stock_move = (target_price - premium) / delta if delta > 0 else 999
        required_move_pct = (required_stock_move / current_price) * 100
    else:  # put
        required_stock_move = (target_price - premium) / delta if delta > 0 else 999
        required_move_pct = (required_stock_move / current_price) * 100
    
    # Get average daily move based on volatility
    avg_daily_move = {
        'high': 2.5,
        'medium': 1.5,
        'low': 0.8
    }.get(momentum_data.get('volatility', 'medium'), 1.5)
    
    # Calculate probability of hitting required move
    if required_move_pct <= 0:
        prob_target = prob_itm * 0.9
    elif required_move_pct <= avg_daily_move * 0.5:
        prob_target = prob_itm * 0.7
    elif required_move_pct <= avg_daily_move:
        prob_target = prob_itm * 0.5
    elif required_move_pct <= avg_daily_move * 1.5:
        prob_target = prob_itm * 0.3
    elif required_move_pct <= avg_daily_move * 2:
        prob_target = prob_itm * 0.15
    else:
        prob_target = prob_itm * 0.05
    
    # For multi-day, probability is higher since we have more time
    if trade_type == "multi":
        prob_target = min(prob_target * 1.3, 95)
    
    # Risk of expiring worthless
    prob_worthless = 100 - prob_itm
    
    # Expected value calculation
    expected_payout = target_price * (prob_target / 100)
    expected_loss = premium * (1 - prob_target / 100)
    expected_value = expected_payout - expected_loss
    
    return {
        'prob_itm': max(0, min(100, prob_itm)),
        'prob_target': max(0, min(100, prob_target)),
        'prob_worthless': max(0, min(100, prob_worthless)),
        'expected_value': expected_value,
        'expected_value_ratio': expected_value / premium if premium > 0 else 0,
        'required_move_pct': required_move_pct,
        'otm_percent': otm_percent if 'otm_percent' in locals() else 0
    }


def find_trade_options(options_df, current_price, momentum_data, action="SELL", opt_type="AUTO", 
                       strategy_preference="trend_following", max_premium=5.0, trade_type="day"):
    """
    Select options for trading with 10-20% profit targets
    Supports both day trading and multi-day trading
    """
    if options_df.empty:
        return pd.DataFrame()
    
    filtered_options = []
    
    # Get days to expiry from momentum_data
    days_to_expiry = momentum_data.get('days_to_expiry', 4)
    min_probability = momentum_data.get('min_probability', 15)
    
    # Adjust filters based on trade type
    if trade_type == "day":
        min_bid_ask_spread = 0.30  # 30% spread max
        min_volume = 5
        min_oi = 20
        max_prob_worthless = 85
    else:
        # Multi-day: can be a bit more lenient
        min_bid_ask_spread = 0.40  # 40% spread max
        min_volume = 3
        min_oi = 10
        max_prob_worthless = 80  # Stricter on worthless probability
    
    # Probability thresholds
    min_prob_target = min_probability
    min_expected_value = -0.5
    
    # STRATEGY PREFERENCE FILTER
    strategy = strategy_preference.lower()
    trend = momentum_data['trend']
    momentum_score = momentum_data['score']
    
    logger.info(f"Strategy: {strategy}, Trend: {trend}, Momentum Score: {momentum_score}, Trade Type: {trade_type}")

    # ACTION / TYPE FILTERING
    action = (action or "SELL").upper()
    _type = (opt_type or "AUTO").upper()

    # Apply type filtering
    filtered_df = options_df.copy()
    if _type in ("CALL", "PUT"):
        filtered_df = filtered_df[filtered_df["side"] == _type.lower()].copy()
    elif action == "BUY" and _type == "AUTO":
        # For BUY, follow the trend
        if trend == "uptrend":
            filtered_df = filtered_df[filtered_df["side"] == "call"].copy()
            print(f"📈 Trend: UPTREND → Focusing on CALL options for BUY")
        elif trend == "downtrend":
            filtered_df = filtered_df[filtered_df["side"] == "put"].copy()
            print(f"📉 Trend: DOWNTREND → Focusing on PUT options for BUY")
    elif action == "SELL" and _type == "AUTO":
        # For SELL, sell against the trend
        if trend == "uptrend":
            filtered_df = filtered_df[filtered_df["side"] == "put"].copy()
        elif trend == "downtrend":
            filtered_df = filtered_df[filtered_df["side"] == "call"].copy()
    
    logger.info(f"After type filtering: {len(filtered_df)} contracts")
    
    if filtered_df.empty:
        logger.warning("No contracts after type filtering")
        return pd.DataFrame()
    
    # Collect potential options
    for _, option in filtered_df.iterrows():
        option_side = option['side']
        delta = abs(option['delta'])
        
        # Get premium based on action
        if action == "SELL":
            premium = option['bid']
        else:  # BUY
            premium = option['ask']
        
        # Skip if premium is 0 or extremely low
        if premium <= 0.05:
            continue
        
        # MONEYNESS FILTERS - adjusted for trade type
        if action == "BUY":
            if trade_type == "day":
                # Day trading: tighter moneyness range
                if option_side == "call":
                    if option['strike'] > current_price * 1.03:  # More than 3% OTM
                        continue
                    if option['strike'] < current_price * 0.90:  # More than 10% ITM
                        continue
                else:  # put
                    if option['strike'] < current_price * 0.97:  # More than 3% OTM
                        continue
                    if option['strike'] > current_price * 1.10:  # More than 10% ITM
                        continue
            else:
                # Multi-day: wider moneyness range
                if option_side == "call":
                    if option['strike'] > current_price * 1.10:  # More than 10% OTM
                        continue
                    if option['strike'] < current_price * 0.80:  # More than 20% ITM
                        continue
                else:  # put
                    if option['strike'] < current_price * 0.90:  # More than 10% OTM
                        continue
                    if option['strike'] > current_price * 1.20:  # More than 20% ITM
                        continue
            
            # Delta range - adjusted for trade type
            if trade_type == "day":
                if not (0.30 <= delta <= 0.70):
                    continue
            else:
                if not (0.25 <= delta <= 0.65):
                    continue
        
        # Calculate probability metrics with trade type
        prob_metrics = calculate_option_probabilities(option, current_price, days_to_expiry, momentum_data, trade_type)
        
        # Filter by probability
        if prob_metrics['prob_target'] < min_prob_target:
            continue
        if prob_metrics['prob_worthless'] > max_prob_worthless:
            continue
            
        # Basic liquidity check
        if option['bid'] <= 0.02 or option['ask'] <= 0.02:
            continue
            
        # Spread calculation
        if option['ask'] > 0 and option['bid'] > 0:
            spread = option['ask'] - option['bid']
            spread_pct = spread / option['ask'] if option['ask'] > 0 else 1.0
            
            if spread_pct > min_bid_ask_spread:
                continue
            
            volume = option.get('volume', 0)
            oi = option.get('oi', 0)
            if volume < min_volume and oi < min_oi:
                continue
            
            # Calculate stop loss with trade type
            try:
                if action == 'SELL':
                    stop_loss_info = calculate_stop_loss(option, current_price, momentum_data)
                else:
                    stop_loss_info = calculate_stop_loss_buy(option, momentum_data, trade_type)
            except:
                stop_loss_info = {
                    'stop_price': premium * 1.5 if action == 'SELL' else premium * 0.5,
                    'stop_multiplier': 1.5 if action == 'SELL' else 0.5,
                    'max_loss_per_contract': premium * 0.5,
                    'position_size': 1,
                    'total_risk': premium * 0.5
                }
            
            # Calculate confidence score with trade type
            try:
                if action == 'SELL':
                    confidence_score = calculate_confidence_score(option, momentum_data, stop_loss_info)
                else:
                    confidence_score = calculate_confidence_score_buy(option, momentum_data, trade_type)
            except:
                confidence_score = 50
            
            confidence_level = get_confidence_level(confidence_score)
            
            # Calculate trade score
            day_trade_score = 50
            
            # Premium score - adjusted for trade type
            if trade_type == "day":
                if 0.50 <= premium <= 3.00:
                    premium_score = 25
                elif 0.25 <= premium <= 4.00:
                    premium_score = 20
                elif premium < 0.25:
                    premium_score = 10
                else:
                    premium_score = 15
            else:
                if 1.00 <= premium <= 5.00:
                    premium_score = 25
                elif 0.50 <= premium <= 8.00:
                    premium_score = 20
                else:
                    premium_score = 15
            
            day_trade_score += premium_score
            
            # Probability score
            prob_score = prob_metrics['prob_target'] / 2
            day_trade_score += min(prob_score, 30)
            
            # Expected value score
            ev_score = max(0, prob_metrics['expected_value_ratio'] * 20)
            day_trade_score += min(ev_score, 15)
            
            # Delta score
            if action == "BUY":
                if trade_type == "day":
                    if 0.40 <= delta <= 0.60:
                        day_trade_score += 20
                    elif 0.30 <= delta <= 0.70:
                        day_trade_score += 15
                    else:
                        day_trade_score += 5
                else:
                    if 0.35 <= delta <= 0.55:
                        day_trade_score += 20
                    elif 0.25 <= delta <= 0.65:
                        day_trade_score += 15
                    else:
                        day_trade_score += 5
            
            # Spread score
            if spread_pct < 0.15:
                day_trade_score += 10
            elif spread_pct < 0.25:
                day_trade_score += 5
            
            filtered_options.append({
                'symbol': option['symbol'],
                'side': option['side'],
                'strike': option['strike'],
                'bid': option['bid'],
                'ask': option['ask'],
                'premium': premium,
                'delta': option['delta'],
                'iv': option['iv'],
                'oi': option['oi'],
                'volume': option.get('volume', 0),
                'trade_score': min(day_trade_score, 100),
                'confidence_score': confidence_score,
                'confidence_level': confidence_level,
                'stop_price': stop_loss_info['stop_price'],
                'stop_multiplier': stop_loss_info.get('stop_multiplier', 1.5),
                'max_loss_per_contract': stop_loss_info['max_loss_per_contract'],
                'position_size': stop_loss_info['position_size'],
                'total_risk': stop_loss_info.get('total_risk', stop_loss_info['max_loss_per_contract'] * stop_loss_info['position_size']),
                'spread_pct': spread_pct,
                'strike_pct': abs(option['strike'] - current_price) / current_price * 100,
                'prob_itm': prob_metrics['prob_itm'],
                'prob_target': prob_metrics['prob_target'],
                'prob_worthless': prob_metrics['prob_worthless'],
                'expected_value': prob_metrics['expected_value'],
                'required_move_pct': prob_metrics.get('required_move_pct', 0),
                'strategy': strategy,
                'trade_type': trade_type
            })
    
    if not filtered_options:
        logger.warning("No options passed basic filters")
        return pd.DataFrame()
    
    # Create DataFrame and sort by trade score
    result_df = pd.DataFrame(filtered_options)
    
    # Apply premium cap if specified
    if max_premium > 0:
        cheap_options = result_df[result_df['premium'] <= max_premium].copy()
        if not cheap_options.empty:
            result_df = cheap_options
            logger.info(f"Premium cap ${max_premium} applied: {len(cheap_options)} options within budget")
        else:
            logger.warning(f"No options found with premium ≤ ${max_premium}")
            return pd.DataFrame()
    
    # Sort by trade score
    result_df = result_df.sort_values('trade_score', ascending=False)
    
    if not result_df.empty:
        logger.info(f"Found {len(result_df)} potential trades")
        # Log top candidates by type
        calls = result_df[result_df['side'] == 'call']
        puts = result_df[result_df['side'] == 'put']
        if not calls.empty:
            logger.info(f"  - {len(calls)} CALL candidates (top: ${calls.iloc[0]['strike']:.2f}, score={calls.iloc[0]['trade_score']:.1f})")
        if not puts.empty:
            logger.info(f"  - {len(puts)} PUT candidates (top: ${puts.iloc[0]['strike']:.2f}, score={puts.iloc[0]['trade_score']:.1f})")
    
    return result_df



def calculate_trade_entry_exit(option, current_price, momentum, action="SELL", trade_type="day"):
    """
    Calculate specific entry and exit levels for trading
    Supports both day trading and multi-day trading
    """
    action = (action or "SELL").upper()
    entry_price = option['bid'] if action == 'SELL' else option.get('ask', option['bid'])
    
    # Profit targets - different for day vs multi-day
    if trade_type == "day":
        target_1 = round(entry_price * (0.90 if action == 'SELL' else 1.10), 2)  # 10% profit
        target_2 = round(entry_price * (0.80 if action == 'SELL' else 1.20), 2)  # 20% profit
        target_1_pct = 10
        target_2_pct = 20
    else:
        target_1 = round(entry_price * (0.80 if action == 'SELL' else 1.20), 2)  # 20% profit
        target_2 = round(entry_price * (0.60 if action == 'SELL' else 1.40), 2)  # 40% profit
        target_1_pct = 20
        target_2_pct = 40
    
    # Risk management
    risk = option.get('max_loss_per_contract', entry_price * 0.50)
    stop_loss = option.get('stop_price', entry_price * (1.50 if action == 'SELL' else 0.55))
    
    # Time-based exit
    if trade_type == "day":
        time_exit = "3:55 PM ET"  # Before market close
    else:
        # For multi-day, exit by Friday or 3 days before expiration
        now = _now_et()
        days_until_friday = (4 - now.weekday()) % 7
        if days_until_friday == 0:
            days_until_friday = 7
        
        # Get expiration from option if available
        expiration_date = option.get('expiration_date')
        if expiration_date:
            # Exit 3 days before expiration
            exp_date = datetime.strptime(expiration_date, "%Y-%m-%d").date() if isinstance(expiration_date, str) else expiration_date
            exit_date = exp_date - timedelta(days=3)
            if exit_date > now.date():
                time_exit = exit_date.strftime("%Y-%m-%d") + " (3 days before expiration)"
            else:
                time_exit = "This Friday at market close"
        else:
            # Default to Friday
            exit_date = now + timedelta(days=days_until_friday)
            time_exit = exit_date.strftime("%Y-%m-%d") + " (Friday)"
    
    # Exit signals
    if momentum['trend'] == "uptrend" and option['side'] == "call":
        exit_signals = [
            f"Underlying price > ${current_price * 1.01:.2f} (+1%)",
            f"Premium {'>' if action=='SELL' else '<'} ${stop_loss:.2f} (Stop Loss)",
            "15-min RSI > 70 (overbought)"
        ]
    elif momentum['trend'] == "downtrend" and option['side'] == "put":
        exit_signals = [
            f"Underlying price < ${current_price * 0.99:.2f} (-1%)",
            f"Premium {'>' if action=='SELL' else '<'} ${stop_loss:.2f} (Stop Loss)",
            "15-min RSI < 30 (oversold)"
        ]
    else:
        exit_signals = [
            f"Premium {'>' if action=='SELL' else '<'} ${stop_loss:.2f} (Stop Loss)",
            f"{abs(option['delta'])*100:.0f}% adverse price move",
            "Loss of key support/resistance"
        ]
    
    # Risk/Reward calculation
    reward_1 = (entry_price - target_1) if action == 'SELL' else (target_1 - entry_price)
    reward_2 = (entry_price - target_2) if action == 'SELL' else (target_2 - entry_price)
    
    risk_reward_1 = round(reward_1 / risk, 2) if risk > 0 else 0
    risk_reward_2 = round(reward_2 / risk, 2) if risk > 0 else 0
    
    return {
        'entry_price': entry_price,
        'target_1': target_1,
        'target_2': target_2,
        'target_1_pct': target_1_pct,
        'target_2_pct': target_2_pct,
        'stop_loss': stop_loss,
        'time_exit': time_exit,
        'exit_signals': exit_signals,
        'risk_reward_1': risk_reward_1,
        'risk_reward_2': risk_reward_2,
        'max_loss': risk,
        'potential_gain_1': reward_1,
        'potential_gain_2': reward_2
    }

# === MAIN EXECUTION =======================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Options Trading Guru - Day Trading & Multi-Day")
    parser.add_argument("symbol", nargs="?", default="SPY", help="Underlying symbol (e.g., TSLA, SPY)")
    parser.add_argument("--expiration", default="", help="Expiration date YYYY-MM-DD (optional)")
    parser.add_argument("--action", default="BUY", choices=["SELL", "BUY"], help="SELL (credit) or BUY (debit)")
    parser.add_argument("--type", dest="opt_type", default="AUTO", choices=["AUTO", "CALL", "PUT"], help="Option type filter")
    parser.add_argument("--trade-type", dest="trade_type", default="day", choices=["day", "multi"], 
                        help="day (exit by market close) or multi (hold multiple days)")
    parser.add_argument("--target-days", type=int, default=30, 
                        help="For multi-day, target days to expiration (e.g., 30, 45, 60)")
    parser.add_argument("--max-premium", type=float, default=None, help="Maximum premium for options (e.g., 1, 5, 10)")
    parser.add_argument("--no-premium-cap", action="store_true", help="Explicitly disable premium cap")
    args = parser.parse_args()

    SYMBOL = args.symbol.upper().strip()
    action = (args.action or "BUY").upper()
    opt_type = (args.opt_type or "AUTO").upper()
    trade_type = args.trade_type
    target_days = args.target_days if trade_type == "multi" else None
    
    # Handle premium cap logic
    if args.no_premium_cap:
        max_premium = None
        premium_display = "No cap"
    elif args.max_premium is not None:
        max_premium = args.max_premium
        premium_display = f"${max_premium}"
    else:
        max_premium = None
        premium_display = "No cap (use --max-premium to set limit)"

    print(f"\n{'='*60}")
    print(f"OPTIONS TRADING GURU for {SYMBOL}".center(60))
    print(f"{'='*60}")
    print(f"Mode: {action} | Type: {opt_type} | Trade Type: {trade_type.upper()}")
    print(f"Premium Limit: {premium_display}")
    if trade_type == "day":
        print("Focus: 10-20% daily profit targets | Exit by market close")
    else:
        print(f"Focus: 20-40% profit targets over multiple days | Target expiration: ~{target_days} DTE")
    print()

    # Step 1: Pick expiration
    expiration = None
    if args.expiration:
        try:
            expiration = datetime.strptime(args.expiration, "%Y-%m-%d").date()
        except Exception:
            print(f"❌ Invalid expiration format: {args.expiration}. Expected YYYY-MM-DD")
            sys.exit(1)

    if expiration is None:
        expiration = get_target_expiration(SYMBOL, trade_type=trade_type, target_days=target_days)

    if not expiration:
        print("❌ Could not find suitable expiration date. Exiting.")
        sys.exit(1)

    days_to_expiry = (expiration - datetime.now(pytz.timezone('US/Eastern')).date()).days
    print(f"📅 Selected expiration: {expiration.strftime('%Y-%m-%d')} ({days_to_expiry} days)")

    # AUTOMATIC PROBABILITY THRESHOLD based on days to expiry and trade type
    if trade_type == "day":
        if days_to_expiry <= 2:
            min_probability = 15
            print(f"⚠️  Only {days_to_expiry} days to expiry - focusing on ATM/ITM options only")
        elif days_to_expiry <= 5:
            min_probability = 15
        elif days_to_expiry <= 10:
            min_probability = 15
        elif days_to_expiry <= 20:
            min_probability = 10
        else:
            min_probability = 10
    else:
        # Multi-day: lower probability threshold since we have more time
        if days_to_expiry <= 7:
            min_probability = 15
        elif days_to_expiry <= 14:
            min_probability = 15
        elif days_to_expiry <= 30:
            min_probability = 15
        elif days_to_expiry <= 45:
            min_probability = 15
        else:
            min_probability = 15
    
    print(f"📊 Probability threshold: {min_probability}% (auto-set for {days_to_expiry} DTE, {trade_type} trade)")

    # Step 2: Fetch option chain and current price
    print(f"📊 Fetching option chain for {SYMBOL}...")
    options_df, price = fetch_full_option_chain(SYMBOL, expiration)

    if options_df.empty:
        print("❌ No options data retrieved. Exiting.")
        sys.exit(1)

    if not price:
        recent_data = get_schwab_todays_intraday_data(SYMBOL, frequency=5)
        if recent_data is not None and not recent_data.empty:
            price = recent_data['close'].iloc[-1]
        else:
            print("❌ Could not retrieve underlying price. Exiting.")
            sys.exit(1)

    print(f"💰 Current price: ${price:.2f}")
    print(f"📈 Options available: {len(options_df)} contracts")
    
    # Show sample of available options
    if not options_df.empty:
        print("\nSample of available options near the money:")
        atm_options = options_df[abs(options_df['strike'] - price) / price < 0.1].head(10)
        if not atm_options.empty:
            for _, opt in atm_options.iterrows():
                premium_display = opt['ask'] if action == 'BUY' else opt['bid']
                moneyness = "ITM" if (opt['side'] == 'call' and opt['strike'] < price) or (opt['side'] == 'put' and opt['strike'] > price) else "OTM" if (opt['side'] == 'call' and opt['strike'] > price) or (opt['side'] == 'put' and opt['strike'] < price) else "ATM"
                within_budget = "✓" if max_premium and premium_display <= max_premium else "✗" if max_premium else ""
                print(f"  {opt['side'].upper()} ${opt['strike']}: ${premium_display:.2f} {within_budget} ({moneyness}, delta={opt['delta']:.3f}, oi={opt['oi']})")

    # Step 3: Momentum analysis
    print(f"\n📉 Analyzing momentum for {SYMBOL}...")
    momentum_data = get_momentum_score(SYMBOL, price)

    # If AUTO type: choose the *directional* side that matches the action
    filtered_opt_type = opt_type
    if opt_type == "AUTO":
        if action == "BUY":
            if momentum_data.get("trend") == "uptrend":
                filtered_opt_type = "CALL"
                print(f"📈 Trend: UPTREND → Focusing on CALL options for BUY")
            elif momentum_data.get("trend") == "downtrend":
                filtered_opt_type = "PUT"
                print(f"📉 Trend: DOWNTREND → Focusing on PUT options for BUY")
            else:
                filtered_opt_type = "AUTO"
                print(f"📊 Trend: NEUTRAL → Evaluating both CALL and PUT")
        else:  # SELL
            if momentum_data.get("trend") == "uptrend":
                filtered_opt_type = "PUT"
                print(f"📈 Trend: UPTREND → Selling PUT options (bullish premium collection)")
            elif momentum_data.get("trend") == "downtrend":
                filtered_opt_type = "CALL"
                print(f"📉 Trend: DOWNTREND → Selling CALL options (bearish premium collection)")
            else:
                filtered_opt_type = "AUTO"
                print(f"📊 Trend: NEUTRAL → Evaluating both CALL and PUT for selling")

    print("\n🔍 Screening for trade opportunities...")
    
    # Pass parameters through momentum_data
    momentum_data['min_probability'] = min_probability
    momentum_data['days_to_expiry'] = days_to_expiry
    
    trade_options = find_trade_options(
        options_df,
        price,
        momentum_data,
        action=action,
        opt_type=filtered_opt_type,
        strategy_preference="trend_following",
        max_premium=max_premium if max_premium is not None else 0,
        trade_type=trade_type
    )

    if trade_options.empty:
        print("\n❌ No suitable trade options found.")
        
        print("\n🔍 DIAGNOSIS:")
        
        if options_df.empty:
            print("  - No options data available for this symbol/expiration")
        else:
            if max_premium:
                if action == "BUY":
                    premium_col = 'ask'
                    action_desc = "pay (ask)"
                else:
                    premium_col = 'bid'
                    action_desc = "receive (bid)"
                    
                cheap_count = len(options_df[options_df[premium_col] <= max_premium])
                    
                if cheap_count == 0:
                    print(f"  - No options found with premium ≤ ${max_premium} (what you'd {action_desc})")
                    min_premium = options_df[premium_col].min()
                    print(f"    Cheapest available: ${min_premium:.2f}")
                    print(f"    💡 Try increasing the premium limit with --max-premium {min_premium*1.2:.2f}")
            
            print(f"\n📋 Available options near the money:")
            
            temp_df = options_df.copy()
            if filtered_opt_type in ("CALL", "PUT"):
                temp_df = temp_df[temp_df["side"] == filtered_opt_type.lower()].copy()
            
            if not temp_df.empty:
                temp_df['strike_dist'] = abs(temp_df['strike'] - price)
                nearby = temp_df.nsmallest(5, 'strike_dist')
                
                for _, opt in nearby.iterrows():
                    premium = opt['ask'] if action == 'BUY' else opt['bid']
                    within_budget = "✓" if max_premium and premium <= max_premium else "✗" if max_premium else ""
                    print(f"  {opt['side'].upper()} ${opt['strike']}: ${premium:.2f} {within_budget} (delta={opt['delta']:.2f}, OI={opt['oi']})")
        
        print("\n❌ Exiting - no trades meet criteria.")
        sys.exit(1)

    print(f"✅ Found {len(trade_options)} potential trades")

    # Sort by trade score
    trade_options = trade_options.sort_values('trade_score', ascending=False)

    # Top candidates
    top_candidates = trade_options.head(8).copy()

    # Build trade plans
    print("📝 Calculating entry/exit plans...")
    trade_plans = []
    for _, opt in top_candidates.iterrows():
        plan = calculate_trade_entry_exit(opt, price, momentum_data, action=action, trade_type=trade_type)
        trade_plans.append({**opt.to_dict(), **plan})

    results_df = pd.DataFrame(trade_plans)

    # Print summary
    print("\n" + "="*60)
    print("MARKET ANALYSIS SUMMARY".center(60))
    print("="*60)
    print(f"Symbol: {SYMBOL}")
    print(f"Current Price: ${price:.2f}")
    print(f"Momentum Score: {momentum_data.get('score', 50)}/100")
    print(f"Primary Trend: {momentum_data.get('trend', 'neutral').upper()}")
    print(f"Volatility: {momentum_data.get('volatility', 'medium')}")
    print(f"Expiration: {expiration.strftime('%Y-%m-%d')} ({days_to_expiry} days)")
    print(f"Trade Type: {trade_type.upper()}")
    print(f"Probability Threshold: {min_probability}%")
    if max_premium:
        print(f"Premium Limit: ${max_premium}")
    else:
        print(f"Premium Limit: No cap")
    print("="*60)

    # Candidates table
    title = f"TOP {trade_type.upper()} TRADE CANDIDATES (Ranked)".center(90)
    print(f"\n{title}")
    print("="*90)
    print("Rank  Action     Strike   Premium  Delta   IV     Prob%  Score  Confidence              Stop    RR")
    print("-"*90)

    def _conf_label(s):
        if s >= 85: return "HIGH"
        if s >= 75: return "MEDIUM-HIGH"
        if s >= 60: return "MEDIUM"
        if s >= 45: return "MEDIUM-LOW"
        return "LOW"

    for i, row in results_df.iterrows():
        rank = int(i) + 1
        action_str = f"{action} {row['side'].upper()}"
        conf_label = _conf_label(float(row.get('confidence_score', row.get('trade_score', 50))))
        stop = row.get('stop_loss')
        rr = row.get('risk_reward_1', 0)
        
        if action == "BUY":
            display_premium = row.get('ask', row['bid'])
        else:
            display_premium = row.get('bid', row['ask'])
        
        prob_target = row.get('prob_target', 50)
        
        print(f"#{rank:<3} {action_str:<9} ${row['strike']:<7.2f} ${display_premium:<7.2f} {row['delta']:<6.2f} {row['iv']:<6.0f}% {prob_target:<5.0f}% {row['trade_score']:<6.1f} {conf_label:<22} ${stop:<6.2f} {rr:.2f}")

    print("="*90)

    # Premium summary
    if max_premium:
        premiums = results_df['premium'] if 'premium' in results_df.columns else results_df['bid']
        print(f"\nPremium Range: ${premiums.min():.2f} - ${premiums.max():.2f} (within ${max_premium} limit)")

    # Probability summary
    if 'prob_target' in results_df.columns:
        print(f"\nProbability Metrics:")
        print(f"  - Avg chance of hitting target: {results_df['prob_target'].mean():.1f}%")
        print(f"  - Best chance: {results_df['prob_target'].max():.1f}%")
        print(f"  - Min required: {min_probability}%")
        if 'prob_worthless' in results_df.columns:
            print(f"  - Avg chance of worthless: {results_df['prob_worthless'].mean():.1f}%")

    # Detailed analysis top 3
    print(f"\nDETAILED ANALYSIS - TOP 3 {trade_type.upper()} TRADE CANDIDATES")
    print("="*70)
    for j in range(min(3, len(results_df))):
        row = results_df.iloc[j]
        print("#"*50)
        print(f"#{j+1}: {action} {row['side'].upper()} ${row['strike']:.2f}")
        
        if action == "BUY":
            premium_display = row.get('ask', row['bid'])
        else:
            premium_display = row.get('bid', row['ask'])
            
        print(f"Premium: ${premium_display:.2f}")
        print(f"Trade Score: {row['trade_score']:.1f}/100")
        cscore = float(row.get('confidence_score', row.get('trade_score', 50)))
        print(f"Confidence: {_conf_label(cscore)} ({cscore:.1f}/100)")
        print(f"Delta: {row['delta']:.2f}")
        print(f"IV: {row['iv']:.1f}%")
        print(f"Open Interest: {int(row.get('oi', 0))}")
        print(f"Volume: {int(row.get('volume', 0))}")
        
        print(f"\n📊 PROBABILITY METRICS:")
        if 'prob_target' in row:
            print(f"  - Chance of hitting target: {row.get('prob_target', 0):.1f}%")
            meets_req = "✓ MEETS" if row.get('prob_target', 0) >= min_probability else "✗ BELOW"
            print(f"  - Requirement: {min_probability}% ({meets_req} threshold)")
        if 'prob_worthless' in row:
            print(f"  - Chance of expiring worthless: {row.get('prob_worthless', 0):.1f}%")
        if 'expected_value' in row:
            print(f"  - Expected value: ${row.get('expected_value', 0):.3f}")
        
        print(f"\n📈 TRADE LEVELS:")
        print(f"  Target 1 ({row['target_1_pct']:.0f}%): {'Buy at' if action=='SELL' else 'Sell at'} ${row['target_1']:.2f}")
        print(f"  Target 2 ({row['target_2_pct']:.0f}%): {'Buy at' if action=='SELL' else 'Sell at'} ${row['target_2']:.2f}")
        print(f"  Stop Loss: ${row['stop_loss']:.2f}")
        print(f"  Max Loss: ${row.get('max_loss', 0):.2f} per contract")
        print(f"  Position Size: {int(row.get('position_size', 1))} contracts")
        print(f"  Total Risk: ${float(row.get('total_risk', row.get('max_loss', 0) * int(row.get('position_size', 1)))):.2f}")
        print(f"  Risk/Reward (Target 1): {row.get('risk_reward_1', 0):.2f}")
    print("="*70)

    # Recommended
    best = results_df.iloc[0]
    print("\n" + "="*60)
    print(f"🎯 RECOMMENDED {trade_type.upper()} TRADE 🎯".center(60))
    print("="*60)
    print(f"Action: {action} {best['side'].upper()}")
    print(f"Strike: ${best['strike']:.2f}")
    print(f"Expiration: {expiration.strftime('%Y-%m-%d')} ({days_to_expiry} days)")
    print(f"Trade Type: {trade_type.upper()}")
    if max_premium:
        print(f"Premium Cap: ${max_premium}")
    print("="*60)

    print("\n📊 ENTRY & EXIT LEVELS:")
    
    if action == "BUY":
        entry_premium = best.get('ask', best['bid'])
    else:
        entry_premium = best.get('bid', best['ask'])
        
    print(f"Entry ({'Sell' if action=='SELL' else 'Buy'}): ${entry_premium:.2f}")
    print(f"Target 1 ({best['target_1_pct']:.0f}%): ${best['target_1']:.2f}")
    print(f"Target 2 ({best['target_2_pct']:.0f}%): ${best['target_2']:.2f}")
    print(f"Stop Loss: ${best['stop_loss']:.2f}")
    print(f"Max Loss/Contract: ${best.get('max_loss', 0):.2f}")
    print(f"Time Exit: {best.get('time_exit', 'N/A')}")

    # Confidence & risk block
    cscore = float(best.get('confidence_score', best.get('trade_score', 50)))
    print("\n📊 CONFIDENCE & RISK:")
    print(f"Confidence Score: {cscore:.1f}/100")
    print(f"Confidence Level: {_conf_label(cscore)}")
    print(f"Trade Score: {float(best.get('trade_score', 50)):.1f}/100")
    
    if 'prob_target' in best:
        print(f"Chance of hitting target: {best.get('prob_target', 0):.1f}%")
        print(f"Required threshold: {min_probability}%")
    if 'prob_worthless' in best:
        print(f"Chance of expiring worthless: {best.get('prob_worthless', 0):.1f}%")
    
    position_size = int(best.get('position_size', 1))
    max_loss = float(best.get('max_loss', 0))
    total_risk = float(best.get('total_risk', max_loss * position_size))
    print(f"Position Size: {position_size} contracts")
    print(f"Total Risk: ${total_risk:.2f}")

    print("\n📊 OPTION DETAILS:")
    print(f"Delta: {best['delta']:.2f}")
    print(f"IV: {best['iv']:.1f}%")
    print(f"Bid/Ask: ${best['bid']:.2f}/${best.get('ask', best['bid']):.2f}")
    if 'premium' in best:
        print(f"Premium: ${best['premium']:.2f}")
    
    if 'expected_value' in best:
        print(f"Expected Value: ${best['expected_value']:.3f}")
    
    print("="*60)
    print("\n⚠️  DISCLAIMER: Options trading involves significant risk. ")
    if trade_type == "day":
        print("   These are day trade recommendations - exit by market close.")
    else:
        print("   These are multi-day/swing trade recommendations - monitor positions daily.")
    print("   Past performance does not guarantee future results.")