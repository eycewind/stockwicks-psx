
#/var/www/stockwicks/app/scripts/analysis_engine.pyimport os
import sys
import asyncio
import aiohttp
import numpy as np
import pandas as pd
import logging
import re  # Added missing import
from datetime import datetime, timedelta
from dotenv import load_dotenv
import json
import pytz
from statistics import mean
from llm_engine import TradeLLM

# Load environment variables
load_dotenv()
os.environ['PYTHONIOENCODING'] = 'utf-8'

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Constants
DATA_DIR = os.getenv('DATA_DIR', '/var/www/stockwicks/data')
API_TOKEN = os.getenv('API_TOKEN')
if not API_TOKEN:
    logger.error("API_TOKEN is not set in .env file")
    sys.exit(1)

DURATION_MAPPING = {
      '1d': '1d'
}
DAYS = 30
INTERVAL_WEIGHTS = {'1d': 5}
INTERVAL_DURATIONS = {
     '1d': 'Multi-day to weekly position'
}

# Initialize LLM
llm = TradeLLM(model_path="/var/www/stockwicks/models/mistral/mistral-7b-instruct-v0.1.Q4_K_M.gguf")

try:
    symbol = sys.argv[1].upper()
    user_id = sys.argv[2]
except IndexError:
    logger.warning("No symbol or user_id provided; using defaults")
    symbol = 'SPY'
    user_id = 'default_user'

user_data_dir = os.path.join(DATA_DIR, str(user_id))
os.makedirs(user_data_dir, exist_ok=True)

async def fetch_stock_data(symbol, interval, session):
    days = 360 if interval == '1d' else DAYS
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=days)
    url = f"https://api.marketdata.app/v1/stocks/candles/{DURATION_MAPPING[interval]}/{symbol}?from={start_date.isoformat()}Z&to={end_date.isoformat()}Z&token={API_TOKEN}"
    try:
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.json()
                if data and data.get('s') == 'ok':
                    return interval, data
                logger.warning(f"No valid data for {symbol} ({interval}): {data}")
            else:
                logger.error(f"Fetch failed for {symbol} ({interval}): {response.status} - {await response.text()}")
    except Exception as e:
        logger.error(f"Exception fetching {symbol} ({interval}): {e}")
    return interval, None

def compute_indicators(df):
    if df.empty:
        return df
    df['SMA10'] = df['close'].rolling(10).mean().bfill()
    df['SMA50'] = df['close'].rolling(50).mean().bfill()
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = -delta.where(delta < 0, 0).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs.replace([np.inf, -np.inf], np.nan).fillna(50)))
    ema_short = df['close'].ewm(span=12).mean()
    ema_long = df['close'].ewm(span=26).mean()
    df['MACD'] = ema_short - ema_long
    df['MACD_Signal'] = df['MACD'].ewm(span=9).mean()
    df['Support'] = df['low'].rolling(20).min().bfill()
    df['Resistance'] = df['high'].rolling(20).max().bfill()
    df['BB_Middle'] = df['close'].rolling(20).mean()
    df['BB_Std'] = df['close'].rolling(20).std()
    df['BB_Upper'] = df['BB_Middle'] + 2 * df['BB_Std']
    df['BB_Lower'] = df['BB_Middle'] - 2 * df['BB_Std']
    df.bfill(inplace=True)
    return df

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

def analyze_trend(df):
    if df.empty or len(df) < 50:
        return 'Hold', 0.0, 0.0, 0.0, "Insufficient data", 0.0, 0.0

    last = df.iloc[-1]
    if not all(k in last for k in ['close', 'SMA10', 'SMA50', 'RSI', 'MACD', 'MACD_Signal', 'Support', 'Resistance', 'BB_Upper', 'BB_Lower', 'BB_Middle', 'volume']):
        logger.warning("Missing indicators in DataFrame")
        return 'Hold', 0.0, 0.0, 0.0, "Missing technical indicators", 0.0, 0.0

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

    # Price momentum over 50 periods
    price_50ago = df['close'].iloc[-50]
    price_change = (price - price_50ago) / price_50ago * 100 if price_50ago else 0

    # 52-week high/low check (approximated using available data)
    price_max = df['high'].max()
    price_min = df['low'].min()
    near_52w_high = price >= price_max * 0.98  # Within 2% of 52-week high
    near_52w_low = price <= price_min * 1.02  # Within 2% of 52-week low

    # MACD divergence
    macd_5ago = df['MACD'].iloc[-5]
    macd_divergence = (macd < macd_5ago) and (price_change > 0)

    # Check for stalling momentum near resistance
    last_range = last['high'] - last['low']
    avg_range = (df['high'] - df['low']).rolling(10).mean().iloc[-1]
    stalling = (last_range < avg_range * 0.5) and (price > resistance)

    sma_trend = last['SMA10'] - last['SMA50']
    rsi_trend = rsi - 50
    macd_trend = macd - macd_signal
    bb_position = (price - bb_middle) / (bb_upper - bb_lower) if (bb_upper - bb_lower) != 0 else 0

    buy_zone = max(support, price * 0.95)
    sell_zone = min(resistance, price * 1.10)
    breakout = resistance * 1.01
    breakdown = support * 0.99

    # Weighted scoring with interval consensus
    bullish_score = (
        (sma_trend > 0) +
        (rsi > 50) +
        (macd > macd_signal) +
        (bb_position < 0.5) +
        (price_change > 2) +
        (near_52w_high and price > breakout) * 2
    )
    bearish_score = (
        (sma_trend < 0) +
        (rsi < 60) +
        (macd < macd_signal) +
        (bb_position > 0.5) +
        (price_change < -2) +
        (near_52w_low and price < breakdown) * 2 +
        (rsi > 70 and near_52w_high) * 2 +
        (macd_divergence) * 1.5 +
        (stalling) * 1.5
    )

    trend = 'Hold'
    confidence = 40
    rationale = ""

    if bullish_score >= 2 and bearish_score < 2:
        confidence = min(95, int(bullish_score * 10 + price_change + (50 if near_52w_high and price > breakout else 0)))
        trend = 'Buy'
    elif bearish_score >= 2 or (price > resistance and (rsi > 70 or macd_divergence or stalling)):
        trend = 'Sell'
        buy_zone, sell_zone = sell_zone, buy_zone
        confidence = min(95, int(bearish_score * 10 + abs(price_change) + (50 if near_52w_low or rsi > 70 else 0)))
    else:
        confidence = 40

    rationale = readable_comment(trend, support, resistance, price, buy_zone, sell_zone, bb_upper, bb_lower, breakout, breakdown)
    if volume_spike and rsi > 70 and near_52w_high:
        trend = 'Sell'
        buy_zone, sell_zone = sell_zone, buy_zone
        confidence = min(95, confidence + 20)

    logger.info(f"Trend: {trend}, Buy Zone: {buy_zone:.2f}, Sell Zone: {sell_zone:.2f}, Confidence: {confidence}")
    return trend, buy_zone, sell_zone, confidence, rationale, breakout, breakdown

def validate_llm_commentary(commentary, buy_zone, sell_zone):
    prices = re.findall(r'\$\d+\.\d{2}', commentary)
    for price in prices:
        price_val = float(price[1:])
        if not (buy_zone * 0.95 <= price_val <= sell_zone * 1.05):
            logger.warning(f"LLM suggested unrealistic price {price}; falling back to rationale")
            return False
    return True

def generate_trade_strategy(df, trend, buy_zone, sell_zone, confidence, rationale, interval, last_close, breakout, breakdown):
    is_short = trend == 'Sell' and sell_zone < buy_zone
    stop_loss = round(buy_zone * 0.97, 2) if trend == 'Buy' else round(sell_zone * 1.03, 2) if trend == 'Sell' else round(buy_zone * 0.95, 2)

    last = df.iloc[-1]
    volume_spike = last['volume'] > df['volume'].rolling(20).mean().iloc[-1] * 1.5
    try:
        llm_commentary = llm.explain_trade(
            symbol=symbol,
            interval=interval,
            last_close=last_close,
            entry=round(buy_zone, 2),
            exit=round(sell_zone, 2),
            stop=stop_loss,
            trend=trend,
            rsi=last['RSI'],
            macd=last['MACD'],
            support=last['Support'],
            resistance=last['Resistance'],
            breakout=round(breakout, 2),
            breakdown=round(breakdown, 2),
            bb_upper=round(last['BB_Upper'], 2),
            bb_lower=round(last['BB_Lower'], 2),
            volume_spike=volume_spike
        )

        if not validate_llm_commentary(llm_commentary, buy_zone, sell_zone):
            logger.warning(f"Using fallback rationale for {symbol} ({interval}) due to invalid LLM prices")
            llm_commentary = rationale

    except Exception as e:
        logger.warning(f"⚠️ LLM failed to generate explanation: {e}")
        llm_commentary = rationale

    expected_gain = round((sell_zone - buy_zone) / buy_zone * 100, 2) if trend == 'Buy' else round((buy_zone - sell_zone) / sell_zone * 100, 2) if trend == 'Sell' else 0.0

    return {
        'entry_price': round(buy_zone, 2),
        'exit_price': round(sell_zone, 2),
        'stop_loss': stop_loss,
        'duration': INTERVAL_DURATIONS.get(interval, '1-3 days') if trend != 'Hold' else 'Watch until breakout',
        'action': trend,
        'expected_gain': expected_gain,
        'confidence_level': confidence,
        'commentary': llm_commentary,
        'is_short': is_short,
        'breakout': round(breakout, 2),
        'breakdown': round(breakdown, 2)
    }

def summarize_analysis(results):
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
            }
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
    confidence_avg = round(mean(confidence_scores) if confidence_scores else 50, 2)
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
            f"{crossing_text}Consensus is '{consensus_action}' with {confidence_avg}% confidence based on 60min and 1d intervals. "
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
        }
    }

async def process_interval(symbol, interval, data):
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
        'strategy': strategy
    }

async def run_analysis(symbol):
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_stock_data(symbol, interval, session) for interval in DURATION_MAPPING.keys()]
        results = await asyncio.gather(*tasks)

    analysis_results = []
    for interval, data in results:
        if data:
            result = await process_interval(symbol, interval, data)
            if result:
                analysis_results.append(result)
        else:
            logger.warning(f"No data fetched for {symbol} ({interval}), skipping analysis")

    summary = summarize_analysis(analysis_results)
    output = {
        'summary': summary,
        'details': analysis_results
    }

    output_file = os.path.join(user_data_dir, f'{symbol}_daily_analysis.json')
    try:
        with open(output_file, 'w') as f:
            json.dump(output, f, indent=2, default=str)
        logger.info(f"✅ Saved analysis for {symbol} to {output_file}")
    except Exception as e:
        logger.error(f"Failed to save analysis for {symbol}: {e}")

    return output

if __name__ == "__main__":
    try:
        asyncio.run(run_analysis(symbol))
    except Exception as e:
        logger.error(f"❌ Script failed: {e}")
        sys.exit(1)