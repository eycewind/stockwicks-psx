#app.scripts.chart_analysis_daytrade.py
import sys
import os
#sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

import sys
import asyncio
import aiohttp
import numpy as np
import pandas as pd
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
import json
import pytz
from statistics import mean
import hashlib
import concurrent.futures
import requests
from app.utils.stock.schwab_token import get_valid_access_token

try:
    import psutil
except ImportError:
    psutil = None
    logging.warning("psutil not available; CPU affinity limiting skipped")

# Load environment variables
load_dotenv()
os.environ['PYTHONIOENCODING'] = 'utf-8'

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Debug Python environment
logger.info(f"Python executable: {sys.executable}")
logger.info(f"Python version: {sys.version}")
logger.info(f"sys.path: {sys.path}")

# Constants
DATA_DIR = os.getenv('DATA_DIR', '/var/www/stockwicks/data')
CACHE_DIR = os.path.join(DATA_DIR, 'cache')
API_TOKEN = os.getenv('API_TOKEN')
SKIP_LLM = os.getenv('SKIP_LLM', 'True').lower() == 'true'  # Default to True
if not API_TOKEN:
    logger.error("API_TOKEN is not set in .env file")
    sys.exit(1)
SCHWAB_INTERVALS = {
    '5min': ('day', 5, 'minute', 5),
    '15min': ('day', 10, 'minute', 15),
    '1d': ('year', 1, 'daily', 1)

}



INTERVAL_WEIGHTS = {
    '5min': 3,
    '15min': 3,
    '1d': 2
}

DAYS = 60  # Default days for 5min and 15min intervals


INTERVAL_DURATIONS = {
    '5min': 'Micro swing',
    '15min': 'Intra-day',
    '1d': 'Position trade'
}


# Ensure cache directory exists
os.makedirs(CACHE_DIR, exist_ok=True)

# Limit CPU usage if psutil is available
if psutil:
    try:
        process = psutil.Process()
        process.cpu_affinity([0, 1, 2, 3])  # Use 4 cores
        logger.info("Limited CPU affinity to 4 cores")
    except Exception as e:
        logger.warning(f"Failed to set CPU affinity: {e}")

# Initialize LLM (not used with SKIP_LLM=True)
llm = None
if not SKIP_LLM:
    try:
        from llm_enginer_daytrade import TradeLLM
        llm = TradeLLM(
            model_path="/var/www/stockwicks/models/mistral/mistral-7b-instruct-v0.1.Q4_K_M.gguf",
            n_ctx=512
        )
    except Exception as e:
        logger.warning(f"Failed to initialize TradeLLM: {e}")

try:
    symbol = sys.argv[1].upper()
    user_id = sys.argv[2]
except IndexError:
    logger.warning("No symbol or user_id provided; using defaults")
    symbol = 'SPY'
    user_id = 'default_user'

user_data_dir = os.path.join(DATA_DIR, str(user_id))
os.makedirs(user_data_dir, exist_ok=True)


def fetch_schwab_stock_data_sync(symbol, interval):
    if interval not in SCHWAB_INTERVALS:
        raise ValueError(f"Unsupported interval. Supported intervals: {list(SCHWAB_INTERVALS.keys())}")
    periodType, period, frequencyType, frequency = SCHWAB_INTERVALS[interval]
    access_token = get_valid_access_token()
    if not access_token:
        logger.error("Failed to get Schwab API token")
        return None
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
    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)
    except Exception as e:
        logger.error(f"Schwab request exception: {e}")
        return None
    if response.status_code != 200:
        logger.error(f"Schwab API error: {response.status_code} {response.text}")
        return None
    data = response.json()
    if not data.get("candles"):
        logger.error("No candles returned from Schwab")
        return None
    candles = data["candles"]
    result = {
        "s": "ok",
        "t": [candle["datetime"] // 1000 for candle in candles],
        "o": [candle["open"] for candle in candles],
        "h": [candle["high"] for candle in candles],
        "l": [candle["low"] for candle in candles],
        "c": [candle["close"] for candle in candles],
        "v": [candle["volume"] for candle in candles],
    }
    return result

async def fetch_stock_data(symbol, interval, session=None):
    logger.info(f"Fetching Schwab data for {symbol} ({interval})")
    loop = asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        data = await loop.run_in_executor(pool, fetch_schwab_stock_data_sync, symbol, interval)
    if data and data.get('s') == 'ok' and len(data.get('t', [])) > 10:
        return interval, data
    else:
        logger.warning(f"No valid Schwab data for {symbol} ({interval})")
        return interval, None


def compute_indicators(df):
    logger.info("Computing indicators")
    if df.empty or len(df) < 50:
        return df
    df['SMA10'] = df['close'].rolling(10).mean().fillna(method='bfill')
    df['SMA50'] = df['close'].rolling(50).mean().fillna(method='bfill')
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = -delta.where(delta < 0, 0).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs.replace([np.inf, -np.inf], np.nan).fillna(50)))
    ema_short = df['close'].ewm(span=12).mean()
    ema_long = df['close'].ewm(span=26).mean()
    df['MACD'] = ema_short - ema_long
    df['MACD_Signal'] = df['MACD'].ewm(span=9).mean()
    df['Support'] = df['low'].rolling(20).min().shift(1).fillna(method='bfill')
    df['Resistance'] = df['high'].rolling(20).max().shift(1).fillna(method='bfill')
    df['BB_Middle'] = df['close'].rolling(20).mean()
    df['BB_Std'] = df['close'].rolling(20).std()
    df['BB_Upper'] = df['BB_Middle'] + 2 * df['BB_Std']
    df['BB_Lower'] = df['BB_Middle'] - 2 * df['BB_Std']
    df.fillna(method='bfill', inplace=True)
    return df

def analyze_trend(df):
    if df.empty or len(df) < 50:
        return 'Hold', 0.0, 0.0, 0.0, "Insufficient data", 0.0, 0.0

    last = df.iloc[-1]
    price = last['close']
    support = last['Support']
    resistance = last['Resistance']
    rsi = last['RSI']
    macd = last['MACD']
    macd_signal = last['MACD_Signal']
    bb_upper = last['BB_Upper']
    bb_lower = last['BB_Lower']
    bb_middle = last['BB_Middle']
    volume_spike = last['volume'] > df['volume'].rolling(20).mean().iloc[-1] * 1.5

    # Price momentum over 50 periods (~2-3 hours in 5min chart)
    price_50ago = df['close'].iloc[-50]
    price_change = (price - price_50ago) / price_50ago * 100

    # 52-week high/low check (approximated using 60-day data)
    price_max = df['high'].max()
    price_min = df['low'].min()
    near_52w_high = price >= price_max * 0.98  # Within 2% of 52-week high
    near_52w_low = price <= price_min * 1.02  # Within 2% of 52-week low

    # MACD divergence (check if MACD is decreasing while price is increasing)
    macd_5ago = df['MACD'].iloc[-5]
    macd_divergence = (macd < macd_5ago) and (price_change > 0)

    # Check for stalling momentum near resistance (small candlestick after breakout)
    last_range = last['high'] - last['low']
    avg_range = (df['high'] - df['low']).rolling(10).mean().iloc[-1]
    stalling = (last_range < avg_range * 0.5) and (price > resistance)

    sma_trend = last['SMA10'] - last['SMA50']
    rsi_trend = rsi - 50
    macd_trend = macd - macd_signal
    bb_position = (price - bb_middle) / (bb_upper - bb_lower) if (bb_upper - bb_lower) != 0 else 0

    buy_zone = max(support, price * 0.98)
    sell_zone = min(resistance, price * 1.02)
    breakout = resistance * 1.01
    breakdown = support * 0.99

    # Adjusted scoring with overbought/oversold conditions
    bullish_score = (
        (sma_trend > 0) +
        (rsi > 50) +
        (macd > macd_signal) +
        (bb_position < 0.5) +
        (price_change > 2) +
        (near_52w_high and price > breakout) * 2  # Strong weight only if breakout confirmed
    )
    bearish_score = (
        (sma_trend < 0) +
        (rsi < 60) +  # Adjusted to catch early reversals
        (macd < macd_signal) +
        (bb_position > 0.5) +
        (price_change < -2) +
        (near_52w_low and price < breakdown) * 2 +
        (rsi > 70 and near_52w_high) * 2 +  # Overbought near 52-week high
        (macd_divergence) * 1.5 +  # MACD divergence
        (stalling) * 1.5  # Stalling momentum near resistance
    )

    trend = 'Hold'
    confidence = 40
    rationale = ""

    # Enhanced decision logic
    if bullish_score >= 1 and bearish_score < 2:
        # Reduce confidence if price breaks resistance but not breakout level
        if price > resistance and price < breakout:
            confidence = min(60, int(bullish_score * 10 + price_change))
        else:
            confidence = min(95, int(bullish_score * 10 + price_change + (50 if near_52w_high and price > breakout else 0)))
        trend = 'Buy'
        rationale = readable_comment(trend, support, resistance, price, buy_zone, sell_zone, bb_upper, bb_lower, breakout, breakdown)
    elif bearish_score >= 1 or (price > resistance and (rsi > 70 or macd_divergence or stalling)):
        trend = 'Sell'
        confidence = min(95, int(bearish_score * 10 + abs(price_change) + (50 if near_52w_low or rsi > 70 else 0)))
        rationale = readable_comment(trend, support, resistance, price, sell_zone, buy_zone, bb_upper, bb_lower, breakout, breakdown)
    else:
        rationale = readable_comment('Hold', support, resistance, price, buy_zone, sell_zone, bb_upper, bb_lower, breakout, breakdown)

    # Adjust for volume spike in overbought conditions
    if volume_spike and rsi > 70 and near_52w_high:
        trend = 'Sell'
        confidence = min(95, confidence + 20)
        rationale = readable_comment(trend, support, resistance, price, sell_zone, buy_zone, bb_upper, bb_lower, breakout, breakdown)

    return trend, buy_zone, sell_zone, confidence, rationale, breakout, breakdown

def readable_comment(trend, support, resistance, last_close, entry_price, exit_price, bb_upper, bb_lower, breakout, breakdown):
    support = round(support, 2)
    resistance = round(resistance, 2)
    last_close = round(last_close, 2)
    entry_price = round(entry_price, 2)
    exit_price = round(exit_price, 2)
    bb_upper = round(bb_upper, 2)
    bb_lower = round(bb_lower, 2)
    breakout = round(breakout, 2)
    breakdown = round(breakdown, 2)

    crossing_status = []
    if last_close > resistance:
        crossing_status.append(f"Price has broken above resistance (${resistance})")
    elif last_close < support:
        crossing_status.append(f"Price has broken below support (${support})")
    if last_close > breakout:
        crossing_status.append(f"Price has confirmed breakout above ${breakout}")
    elif last_close < breakdown:
        crossing_status.append(f"Price has confirmed breakdown below ${breakdown}")

    crossing_text = "; ".join(crossing_status) + ". " if crossing_status else ""

    if trend == 'Buy':
        if last_close < entry_price:
            return f"{crossing_text}Price ${last_close} is below entry (${entry_price}). Wait for confirmation above ${entry_price} or near support (${support}). Breakout above ${breakout}, breakdown below ${breakdown}. Bollinger Upper: ${bb_upper}."
        elif entry_price <= last_close <= exit_price:
            return f"{crossing_text}Price ${last_close} is in the buy range. Consider entering now, targeting ${exit_price}. Breakout above ${breakout}, breakdown below ${breakdown}. Bollinger Lower: ${bb_lower}."
        else:
            return f"{crossing_text}Price ${last_close} exceeds entry (${entry_price}). Wait for a pullback to ${entry_price} or breakout above ${breakout}. Resistance: ${resistance}, breakdown below ${breakdown}. Bollinger Upper: ${bb_upper}."
    elif trend == 'Sell':
        if last_close > entry_price:
            return f"{crossing_text}Price ${last_close} is above short entry (${entry_price}). Wait for a drop to ${entry_price} or breakdown below ${breakdown}. Support: ${support}, breakout above ${breakout}. Bollinger Lower: ${bb_lower}."
        elif exit_price <= last_close <= entry_price:
            return f"{crossing_text}Price ${last_close} is in the short-sell range. Consider shorting now, targeting ${exit_price}. Breakout above ${breakout}, breakdown below ${breakdown}. Bollinger Upper: ${bb_upper}."
        else:
            return f"{crossing_text}Price ${last_close} is below target. Avoid chasing; wait for retracement to ${entry_price}. Breakout above ${breakout}, support: ${support}. Bollinger Lower: ${bb_lower}."
    else:
        return f"{crossing_text}Price ${last_close} is range-bound. Monitor for breakout above ${breakout} or breakdown below ${breakdown}. Support: ${support}, Resistance: ${resistance}. Bollinger Bands: ${bb_lower} - ${bb_upper}."

def generate_trade_strategy(df, trend, buy_zone, sell_zone, confidence, rationale, interval, last_close, breakout, breakdown):
    logger.info("Generating trade strategy")
    is_short = trend == 'Sell' and sell_zone < buy_zone
    stop_loss = (
        round(buy_zone * 0.97, 2) if trend == 'Buy'
        else round(sell_zone * 1.03, 2) if trend == 'Sell'
        else round(buy_zone * 0.95, 2)
    )

    commentary = rationale

    return {
        'entry_price': round(buy_zone, 2),
        'exit_price': round(sell_zone, 2),
        'stop_loss': stop_loss,
        'duration': INTERVAL_DURATIONS.get(interval, '1-3 days') if trend != 'Hold' else 'Watch until breakout',
        'action': trend,
        'expected_gain': round((sell_zone - buy_zone) / buy_zone * 100, 2) if trend == 'Buy' else round((buy_zone - sell_zone) / sell_zone * 100, 2),
        'confidence_level': confidence,
        'commentary': commentary,
        'is_short': is_short,
        'breakout': round(breakout, 2),
        'breakdown': round(breakdown, 2)
    }

def summarize_analysis(results):
    logger.info("Summarizing analysis")
    if not results:
        return {
            'symbol': symbol,
            'consensus_action': 'Hold',
            'confidence_avg': 0,
            'summary_commentary': 'No data available to generate analysis.',
            'suggested_strategy': {
                'entry_price': 0.0,
                'exit_price': 0.0,
                'stop_loss': 0.0,
                'duration': 'N/A',
                'action': 'Hold',
                'expected_gain': 0.0,
                'confidence_level': 0,
                'commentary': 'No market data or valid indicators to provide actionable insight.',
                'breakout': 0.0,
                'breakdown': 0.0
            },
            'analysis_start_time': '',
            'analysis_end_time': '',
            'analysis_duration_seconds': 0.0,
            'support': 0.0,
            'resistance': 0.0,
            'breakout': 0.0,
            'breakdown': 0.0
        }
    votes = {'Buy': 0, 'Sell': 0, 'Hold': 0}
    confidence_scores = []
    weighted_votes = {}
    summary_text = ""

    for r in results:
        action = r['strategy']['action']
        interval = r['interval']
        confidence = r['strategy']['confidence_level']
        weight = INTERVAL_WEIGHTS.get(interval, 1)
        votes[action] += 1
        weighted_votes[action] = weighted_votes.get(action, 0) + weight
        if action != 'Hold':
            confidence_scores.append(confidence)

    consensus_action = max(weighted_votes.items(), key=lambda x: x[1])[0] if weighted_votes else 'Hold'
    confidence_avg = round(mean(confidence_scores) if confidence_scores else 40, 2)
    top_strategy = max((r for r in results if r['strategy']['action'] == consensus_action), key=lambda x: x['strategy']['confidence_level'], default=None)

    if top_strategy:
        last = results[0]['last_close']
        support = top_strategy['support']
        resistance = top_strategy['resistance']
        breakout = top_strategy['strategy']['breakout']
        breakdown = top_strategy['strategy']['breakdown']
        crossing_status = []
        if last > resistance:
            crossing_status.append(f"Price has broken above resistance (${resistance})")
        elif last < support:
            crossing_status.append(f"Price has broken below support (${support})")
        if last > breakout:
            crossing_status.append(f"Price has confirmed breakout above ${breakout}")
        elif last < breakdown:
            crossing_status.append(f"Price has confirmed breakdown below ${breakdown}")
        crossing_text = "; ".join(crossing_status) + ". " if crossing_status else ""

        llm_commentary = (
            f"{crossing_text}Consensus is '{consensus_action}' with {confidence_avg}% confidence based on 5min and 15min intervals. "
            f"Monitor breakout above ${breakout} or breakdown below ${breakdown}. Support: ${support}, Resistance: ${resistance}."
        )
    else:
        llm_commentary = "No valid strategy for summary."

    summary_text = (
        f"Consensus is '{consensus_action}' based on {votes['Buy']} Buy, {votes['Sell']} Sell, and {votes['Hold']} Hold votes. "
        f"Confidence level is {confidence_avg}%. {llm_commentary}"
    )

    return {
        'symbol': results[0]['symbol'] if results else '',
        'consensus_action': consensus_action,
        'confidence_avg': confidence_avg,
        'summary_commentary': summary_text,
        'suggested_strategy': top_strategy['strategy'] if top_strategy else {
            'entry_price': 0.0,
            'exit_price': 0.0,
            'stop_loss': 0.0,
            'duration': 'N/A',
            'action': 'Hold',
            'expected_gain': 0.0,
            'confidence_level': 0,
            'commentary': 'No valid strategy.',
            'is_short': False,
            'breakout': 0.0,
            'breakdown': 0.0
        },
        'support': support if top_strategy else 0.0,
        'resistance': resistance if top_strategy else 0.0,
        'breakout': breakout if top_strategy else 0.0,
        'breakdown': breakdown if top_strategy else 0.0,
        'analysis_start_time': '',
        'analysis_end_time': '',
        'analysis_duration_seconds': 0.0
    }


def fetch_schwab_realtime_quote(symbol):
    access_token = get_valid_access_token()
    if not access_token:
        logger.error("Failed to get Schwab API token")
        return None

    url = f"https://api.schwabapi.com/marketdata/v1/quotes"
    params = {
        "symbols": symbol.upper(),
        "fields": "quote"
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
    except Exception as e:
        logger.error(f"Quote API exception: {e}")
        return None
    if response.status_code != 200:
        logger.error(f"Quote API error: {response.status_code} {response.text}")
        return None
    data = response.json()
    q = data.get(symbol.upper(), {}).get('quote', {})
    if not q or 'lastPrice' not in q:
        logger.warning(f"No real-time quote data found for {symbol}")
        return None

    ts = pd.to_datetime(q['quoteTime'], unit='ms', utc=True)
    candle = {
        'timestamp': ts,
        'open': q.get('openPrice', q['lastPrice']),
        'high': q.get('highPrice', q['lastPrice']),
        'low': q.get('lowPrice', q['lastPrice']),
        'close': q['lastPrice'],
        'volume': q.get('totalVolume', 0),
    }
    return candle

async def process_interval(symbol, interval, session):
    logger.info(f"Processing interval {interval} for {symbol}")
    interval, data = await fetch_stock_data(symbol, interval, session)
    if not data or 't' not in data:
        logger.warning(f"No data available for {symbol} ({interval})")
        return None

    df = pd.DataFrame({
        'timestamp': pd.to_datetime(data['t'], unit='s', utc=True),
        'open': data['o'],
        'high': data['h'],
        'low': data['l'],
        'close': data['c'],
        'volume': data['v']
    })
    df['timestamp'] = df['timestamp'].dt.tz_convert('US/Eastern')
    df.set_index('timestamp', inplace=True)

    if interval != '1d':
        original_length = len(df)
        df = df.between_time('09:30', '16:00')
        if len(df) < original_length * 0.1:
            logger.warning(f"Insufficient data after filtering for {symbol} ({interval}): {len(df)} rows")
            return None

    if df.empty:
        logger.warning(f"Empty DataFrame for {symbol} ({interval}) after processing")
        return None

    # ------- NEW: Append real-time candle if it's newer -------
    try:
        realtime_candle = fetch_schwab_realtime_quote(symbol)
        if realtime_candle:
            realtime_ts = realtime_candle['timestamp'].tz_convert('US/Eastern')
            if realtime_ts > df.index[-1]:
                realtime_candle['timestamp'] = realtime_ts
                df = pd.concat([df, pd.DataFrame([realtime_candle]).set_index('timestamp')])
                logger.info(f"Appended real-time quote for {symbol} to DataFrame.")
            else:
                logger.info(f"Real-time quote is not newer than last candle for {symbol}. Not appending.")
    except Exception as e:
        logger.error(f"Error while adding real-time quote: {e}")
    # ---------------------------------------------------------

    ohlc_file = os.path.join(user_data_dir, f"{symbol}_{interval}.json")
    df_reset = df.reset_index()
    ohlc_payload = df_reset[['timestamp', 'open', 'high', 'low', 'close']].copy()
    ohlc_payload['timestamp'] = ohlc_payload['timestamp'].dt.strftime('%Y-%m-%dT%H:%M:%S')
    try:
        with open(ohlc_file, 'w') as f:
            json.dump(ohlc_payload.to_dict(orient='records'), f, indent=2)
        logger.info(f"✅ Saved OHLC data for {symbol} ({interval}) to {ohlc_file}")
    except Exception as e:
        logger.error(f"Failed to save OHLC data for {symbol} ({interval}): {e}")
        return None

    df = compute_indicators(df)
    try:
        trend, buy_zone, sell_zone, confidence, rationale, breakout, breakdown = analyze_trend(df)
    except Exception as e:
        logger.error(f"Failed to analyze trend for {symbol} ({interval}): {e}")
        return None

    last_close = round(df['close'].iloc[-1], 2)
    strategy = generate_trade_strategy(df, trend, buy_zone, sell_zone, confidence, rationale, interval, last_close, breakout, breakdown)

    return {
        'symbol': symbol,
        'interval': interval,
        'last_close': last_close,
        'last_updated': df.index[-1].strftime('%Y-%m-%d %H:%M:%S %Z'),
        'trend': trend,
        'support': round(df['Support'].iloc[-1], 2) if not df['Support'].empty else 0.0,
        'resistance': round(df['Resistance'].iloc[-1], 2) if not df['Resistance'].empty else 0.0,
        'buy_zone': round(buy_zone, 2),
        'sell_zone': round(sell_zone, 2),
        'rsi': round(df['RSI'].iloc[-1], 2) if not df['RSI'].empty else 0.0,
        'macd': round(df['MACD'].iloc[-1], 2) if not df['MACD'].empty else 0.0,
        'bb_upper': round(df['BB_Upper'].iloc[-1], 2) if not df['BB_Upper'].empty else 0.0,
        'bb_lower': round(df['BB_Lower'].iloc[-1], 2) if not df['BB_Lower'].empty else 0.0,
        'breakout': round(breakout, 2),
        'breakdown': round(breakdown, 2),
        'strategy': strategy
    }


async def run_analysis(symbol):
    start_time = datetime.now(pytz.timezone('US/Eastern'))
    logger.info(f"Analysis started at {start_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    async with aiohttp.ClientSession() as session:
        analysis_results = []
        for interval in SCHWAB_INTERVALS.keys():
            result = await process_interval(symbol, interval, session)
            if result:
                analysis_results.append(result)
            else:
                logger.warning(f"No data fetched for {symbol} ({interval}), skipping analysis")

    summary = summarize_analysis(analysis_results)
    end_time = datetime.now(pytz.timezone('US/Eastern'))
    duration = (end_time - start_time).total_seconds()

    summary['analysis_start_time'] = start_time.strftime('%Y-%m-%d %H:%M:%S %Z')
    summary['analysis_end_time'] = end_time.strftime('%Y-%m-%d %H:%M:%S %Z')
    summary['analysis_duration_seconds'] = round(duration, 2)

    output = {
        'summary': summary,
        'details': analysis_results
    }

    output_file = os.path.join(user_data_dir, f'{symbol}_daytrade_analysis.json')
    try:
        with open(output_file, 'w') as f:
            json.dump(output, f, indent=2, default=str)
        logger.info(f"✅ Saved analysis for {symbol} to {output_file}")
    except Exception as e:
        logger.error(f"Failed to save analysis for {symbol}: {e}")

    logger.info(
        f"Analysis completed. Started: {start_time.strftime('%Y-%m-%d %H:%M:%S %Z')}, "
        f"Ended: {end_time.strftime('%Y-%m-%d %H:%M:%S %Z')}, "
        f"Duration: {duration:.2f} seconds"
    )

    return output

if __name__ == "__main__":
    try:
        import time
        asyncio.run(run_analysis(symbol))
    except Exception as e:
        logger.error(f"❌ Script failed: {e}")
        sys.exit(1)