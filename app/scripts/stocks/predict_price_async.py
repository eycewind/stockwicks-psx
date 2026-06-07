# /var/www/stockwicks/app/scripts/stocks/predict_price_async.py
import sys
import os
import asyncio
import numpy as np
import pandas as pd
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import warnings
warnings.filterwarnings('ignore')

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from app.utils.stock.schwab_price_history import get_schwab_intraday_multi_day, get_schwab_history

# Enhanced logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DATA_DIR = os.getenv('DATA_DIR', '/var/www/stockwicks/data')

class PurePythonStockPredictor:
    def __init__(self):
        self.technical_indicators = []
        
    def calculate_sma(self, prices, period):
        """Simple Moving Average"""
        if len(prices) < period:
            return pd.Series([prices.mean()] * len(prices), index=prices.index)
        return prices.rolling(window=period, min_periods=1).mean()
    
    def calculate_ema(self, prices, period):
        """Exponential Moving Average"""
        if len(prices) < period:
            return pd.Series([prices.mean()] * len(prices), index=prices.index)
        return prices.ewm(span=period, adjust=False, min_periods=1).mean()
    
    def calculate_rsi(self, prices, period=14):
        """Relative Strength Index"""
        if len(prices) < period + 1:
            return pd.Series([50] * len(prices), index=prices.index)
            
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period, min_periods=1).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period, min_periods=1).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi.fillna(50)
    
    def calculate_macd(self, prices, fast=12, slow=26, signal=9):
        """MACD Indicator"""
        ema_fast = self.calculate_ema(prices, fast)
        ema_slow = self.calculate_ema(prices, slow)
        macd = ema_fast - ema_slow
        macd_signal = self.calculate_ema(macd, signal)
        macd_histogram = macd - macd_signal
        return macd, macd_signal, macd_histogram
    
    def calculate_bollinger_bands(self, prices, period=20, std_dev=2):
        """Bollinger Bands"""
        sma = self.calculate_sma(prices, period)
        std = prices.rolling(period, min_periods=1).std()
        upper_band = sma + (std * std_dev)
        lower_band = sma - (std * std_dev)
        return upper_band, sma, lower_band
    
    def calculate_atr(self, high, low, close, period=14):
        """Average True Range"""
        if len(high) < 2:
            return pd.Series([0] * len(high), index=high.index)
            
        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(period, min_periods=1).mean()
        return atr.fillna(0)
    
    def calculate_stochastic(self, high, low, close, k_period=14, d_period=3):
        """Stochastic Oscillator"""
        if len(high) < k_period:
            return pd.Series([50] * len(high), index=high.index), pd.Series([50] * len(high), index=high.index)
            
        lowest_low = low.rolling(k_period, min_periods=1).min()
        highest_high = high.rolling(k_period, min_periods=1).max()
        stoch_k = 100 * (close - lowest_low) / (highest_high - lowest_low)
        stoch_d = stoch_k.rolling(d_period, min_periods=1).mean()
        return stoch_k.fillna(50), stoch_d.fillna(50)
    
    def validate_price_data(self, df):
        """Validate and clean price data"""
        if df.empty:
            return df
            
        # Check for invalid prices
        price_columns = ['open', 'high', 'low', 'close']
        for col in price_columns:
            if col in df.columns:
                # Remove zero or negative prices
                mask = (df[col] > 0) & (df[col] < 1000000)  # Reasonable price range
                if not mask.all():
                    logger.warning(f"Found invalid prices in {col}: {df[col][~mask].values}")
                    df = df[mask]
        
        return df

    def calculate_advanced_indicators(self, df):
        """Calculate comprehensive technical indicators with validation"""
        try:
            # Validate data first
            df = self.validate_price_data(df)
            
            if df.empty or len(df) < 5:
                logger.warning("Insufficient data after validation")
                return pd.DataFrame()

            # Ensure we have required columns
            required_columns = ['open', 'high', 'low', 'close']
            if not all(col in df.columns for col in required_columns):
                logger.error(f"Missing required columns. Available: {df.columns.tolist()}")
                return pd.DataFrame()

            current_price = df['close'].iloc[-1]
            if current_price <= 0 or current_price > 1000000:
                logger.error(f"Invalid current price: {current_price}")
                return pd.DataFrame()

            # Price-based indicators
            df['SMA_20'] = self.calculate_sma(df['close'], 20)
            df['SMA_50'] = self.calculate_sma(df['close'], 50)
            df['EMA_12'] = self.calculate_ema(df['close'], 12)
            df['EMA_26'] = self.calculate_ema(df['close'], 26)
            
            # Bollinger Bands
            df['BB_upper'], df['BB_middle'], df['BB_lower'] = self.calculate_bollinger_bands(df['close'])
            
            # RSI with multiple timeframes
            df['RSI_14'] = self.calculate_rsi(df['close'], 14)
            df['RSI_7'] = self.calculate_rsi(df['close'], 7)
            
            # MACD
            df['MACD'], df['MACD_signal'], df['MACD_hist'] = self.calculate_macd(df['close'])
            
            # Stochastic
            df['STOCH_K'], df['STOCH_D'] = self.calculate_stochastic(df['high'], df['low'], df['close'])
            
            # Volume indicators
            if 'volume' in df.columns:
                df['Volume_SMA'] = self.calculate_sma(df['volume'], 20)
                df['Volume_Ratio'] = np.where(df['Volume_SMA'] > 0, 
                                            df['volume'] / df['Volume_SMA'], 
                                            1.0)
            else:
                df['Volume_Ratio'] = 1.0
            
            # Price rate of change
            df['ROC_10'] = df['close'].pct_change(periods=10) * 100
            df['ROC_21'] = df['close'].pct_change(periods=21) * 100
            
            # ATR for volatility
            df['ATR_14'] = self.calculate_atr(df['high'], df['low'], df['close'])
            
            # Support and Resistance levels (use recent price action)
            lookback = min(20, len(df))
            df['Support'] = df['low'].rolling(lookback, min_periods=1).min()
            df['Resistance'] = df['high'].rolling(lookback, min_periods=1).max()
            
            # Price position relative to ranges
            range_diff = df['Resistance'] - df['Support']
            df['Price_To_Support'] = np.where(range_diff > 0, 
                                            (df['close'] - df['Support']) / range_diff, 
                                            0.5)
            
            bb_range = df['BB_upper'] - df['BB_lower']
            df['BB_Position'] = np.where(bb_range > 0, 
                                       (df['close'] - df['BB_lower']) / bb_range, 
                                       0.5)
            
            # Trend strength
            df['Trend_Strength'] = self.calculate_trend_strength(df)
            
            # Momentum
            df['Momentum_10'] = df['close'] - df['close'].shift(10)
            
            # Fill NaN values with reasonable defaults
            numeric_columns = df.select_dtypes(include=[np.number]).columns
            df[numeric_columns] = df[numeric_columns].fillna(method='bfill').fillna(method='ffill')
            
            # Final validation
            if df['close'].isna().any() or (df['close'] <= 0).any():
                logger.error("Invalid prices found after indicator calculation")
                return pd.DataFrame()
                
            return df
            
        except Exception as e:
            logger.error(f"Error calculating indicators: {e}")
            return pd.DataFrame()

    def calculate_trend_strength(self, df, period=14):
        """Calculate trend strength with validation"""
        try:
            if len(df) < 2:
                return pd.Series([50] * len(df), index=df.index)
                
            up_days = (df['close'] > df['close'].shift(1)).rolling(period, min_periods=1).sum()
            down_days = (df['close'] < df['close'].shift(1)).rolling(period, min_periods=1).sum()
            trend_strength = abs(up_days - down_days) / period * 100
            return trend_strength.fillna(50)
        except Exception as e:
            logger.error(f"Error calculating trend strength: {e}")
            return pd.Series([50] * len(df), index=df.index)

    def calculate_comprehensive_score(self, df):
        """Calculate a comprehensive trading score (-10 to +10) with validation"""
        score = 0
        
        try:
            if df.empty or len(df) < 2:
                return 0
                
            current_price = df['close'].iloc[-1]
            if current_price <= 0:
                return 0

            # 1. Trend Analysis (30% weight)
            if 'SMA_20' in df.columns and 'SMA_50' in df.columns:
                sma20 = df['SMA_20'].iloc[-1]
                sma50 = df['SMA_50'].iloc[-1]
                if not pd.isna(sma20) and not pd.isna(sma50) and sma20 > 0 and sma50 > 0:
                    if sma20 > sma50:
                        score += 2
                    else:
                        score -= 1
                        
            if 'EMA_12' in df.columns and 'EMA_26' in df.columns:
                ema12 = df['EMA_12'].iloc[-1]
                ema26 = df['EMA_26'].iloc[-1]
                if not pd.isna(ema12) and not pd.isna(ema26) and ema12 > 0 and ema26 > 0:
                    if ema12 > ema26:
                        score += 1.5
                    else:
                        score -= 1
            
            # 2. Momentum (25% weight)
            if 'RSI_14' in df.columns:
                rsi = df['RSI_14'].iloc[-1]
                if not pd.isna(rsi):
                    if 30 <= rsi < 45:  # Oversold but not extreme
                        score += 2.0
                    elif 45 <= rsi < 60:  # Healthy bullish
                        score += 1.5
                    elif rsi >= 70:  # Overbought
                        score -= 2.5
                    elif rsi < 30:  # Extremely oversold
                        score += 1.0  # Potential bounce but risky
            
            # MACD momentum
            if 'MACD' in df.columns and 'MACD_signal' in df.columns:
                macd = df['MACD'].iloc[-1]
                signal = df['MACD_signal'].iloc[-1]
                if not pd.isna(macd) and not pd.isna(signal):
                    if macd > signal and macd > 0:
                        score += 1.5
                    elif macd > signal:
                        score += 0.5
                    elif macd < signal and macd < 0:
                        score -= 1.5
                    else:
                        score -= 0.5
            
            # 3. Mean Reversion (20% weight)
            if 'BB_Position' in df.columns:
                bb_position = df['BB_Position'].iloc[-1]
                if not pd.isna(bb_position):
                    if bb_position < 0.2:  # Near lower band - bullish
                        score += 2
                    elif bb_position > 0.8:  # Near upper band - bearish
                        score -= 2
                    elif 0.4 < bb_position < 0.6:  # Middle - neutral
                        score += 0.5
            
            # 4. Volume Confirmation (15% weight)
            if 'Volume_Ratio' in df.columns:
                volume_ratio = df['Volume_Ratio'].iloc[-1]
                if not pd.isna(volume_ratio):
                    if volume_ratio > 1.5:  # Very high volume
                        score += 2
                    elif volume_ratio > 1.2:  # High volume
                        score += 1
                    elif volume_ratio < 0.7:  # Low volume
                        score -= 1
            
            # 5. Support/Resistance (10% weight)
            if 'Price_To_Support' in df.columns:
                price_to_support = df['Price_To_Support'].iloc[-1]
                if not pd.isna(price_to_support):
                    if price_to_support < 0.3:  # Near support - bullish
                        score += 1.5
                    elif price_to_support > 0.7:  # Near resistance - bearish
                        score -= 1.5
            
            return max(min(score, 10), -10)
            
        except Exception as e:
            logger.error(f"Error calculating score: {e}")
            return 0

    def generate_clear_signal(self, score, df):
        """Generate clear BUY/SHORT/AVOID signals with STRICT validation"""
        try:
            if df.empty:
                return "AVOID", "No data available"
                
            current_price = df['close'].iloc[-1]
            if current_price <= 0:
                return "AVOID", "Invalid price data"
            
            rsi = df['RSI_14'].iloc[-1] if 'RSI_14' in df.columns and not pd.isna(df['RSI_14'].iloc[-1]) else 50
            volume_ratio = df['Volume_Ratio'].iloc[-1] if 'Volume_Ratio' in df.columns and not pd.isna(df['Volume_Ratio'].iloc[-1]) else 1.0
            
            # STRICT decision matrix - only generate BUY if conditions are strongly favorable
            if score >= 7 and 35 <= rsi <= 65 and volume_ratio > 1.1:
                return "BUY", "Strong bullish momentum with volume confirmation and healthy RSI"
            
            elif score >= 5 and 40 <= rsi <= 70 and volume_ratio > 0.9:
                return "BUY", "Moderate bullish setup with reasonable conditions"
            
            elif score <= -7 and rsi >= 40 and volume_ratio > 1.1:
                return "SHORT", "Strong bearish momentum with volume confirmation"
            
            elif score <= -5 and rsi >= 35 and volume_ratio > 0.9:
                return "SHORT", "Moderate bearish setup"
            
            else:
                # AVOID for any questionable conditions
                if rsi > 75 or rsi < 25:
                    return "AVOID", f"Extreme RSI levels ({rsi:.1f}) - too risky"
                elif volume_ratio < 0.8:
                    return "AVOID", f"Low volume (ratio: {volume_ratio:.2f}) - weak conviction"
                elif abs(score) < 3:
                    return "AVOID", "Weak directional momentum - unclear trend"
                else:
                    return "AVOID", "Mixed signals - wait for better setup"
                
        except Exception as e:
            logger.error(f"Error generating signal: {e}")
            return "AVOID", f"Error: {str(e)}"

    def calculate_confidence_score(self, df, signal, score):
        """Calculate meaningful confidence percentage with validation"""
        confidence_factors = []
        
        try:
            if df.empty:
                return 50
                
            # Trend alignment (30%)
            if 'SMA_20' in df.columns and 'SMA_50' in df.columns:
                sma20 = df['SMA_20'].iloc[-1]
                sma50 = df['SMA_50'].iloc[-1]
                if not pd.isna(sma20) and not pd.isna(sma50) and sma20 > 0 and sma50 > 0:
                    trend_aligned = (sma20 > sma50 and signal == "BUY") or \
                                  (sma20 < sma50 and signal == "SHORT")
                    confidence_factors.append(0.8 if trend_aligned else 0.4)
                else:
                    confidence_factors.append(0.5)
            else:
                confidence_factors.append(0.5)
            
            # RSI confirmation (25%)
            rsi = df['RSI_14'].iloc[-1] if 'RSI_14' in df.columns and not pd.isna(df['RSI_14'].iloc[-1]) else 50
            if signal == "BUY" and 40 <= rsi <= 65:
                confidence_factors.append(0.9)  # Optimal RSI for BUY
            elif signal == "SHORT" and 35 <= rsi <= 60:
                confidence_factors.append(0.9)  # Optimal RSI for SHORT
            elif signal == "AVOID" and (rsi > 70 or rsi < 30):
                confidence_factors.append(0.8)
            else:
                confidence_factors.append(0.4)  # RSI doesn't confirm signal
            
            # Volume confirmation (20%)
            volume_ratio = df['Volume_Ratio'].iloc[-1] if 'Volume_Ratio' in df.columns and not pd.isna(df['Volume_Ratio'].iloc[-1]) else 1.0
            if volume_ratio > 1.2:
                confidence_factors.append(0.8)
            elif volume_ratio > 0.9:
                confidence_factors.append(0.6)
            else:
                confidence_factors.append(0.3)
            
            # Score strength (25%)
            abs_score = abs(score)
            if abs_score >= 7:
                confidence_factors.append(0.9)
            elif abs_score >= 5:
                confidence_factors.append(0.7)
            elif abs_score >= 3:
                confidence_factors.append(0.5)
            else:
                confidence_factors.append(0.3)
            
            confidence = int(np.mean(confidence_factors) * 100)
            return max(10, min(90, confidence))  # Ensure within 10-90 range (never 100% certain)
            
        except Exception as e:
            logger.error(f"Error calculating confidence: {e}")
            return 50

    def generate_realistic_predictions(self, signal, current_price, atr, support, resistance):
        """Generate REALISTIC price predictions that make logical sense"""
        try:
            if current_price <= 0:
                return current_price, current_price, current_price
                
            # Use ATR for realistic prediction ranges
            if atr > 0:
                volatility_factor = atr / current_price
            else:
                volatility_factor = 0.02  # Default 2% volatility
            
            volatility_factor = min(volatility_factor, 0.1)  # Cap at 10%
            
            # ENSURE TARGET IS ALWAYS ABOVE ENTRY FOR BUY, BELOW FOR SHORT
            if signal == "BUY":
                # For BUY: target MUST be above entry, stop below entry
                min_upside = max(volatility_factor * 0.5, 0.01)  # At least 1% upside
                max_upside = min(volatility_factor * 2.0, 0.1)   # Max 10% upside
                
                pred_close = current_price * (1 + min_upside)
                pred_high = current_price * (1 + max_upside)
                pred_low = current_price * (1 - volatility_factor * 0.8)  # Reasonable stop
                
                # Ensure predictions make sense
                pred_close = max(pred_close, current_price * 1.005)  # At least 0.5% gain
                pred_high = max(pred_high, pred_close * 1.01)       # High above close
                pred_low = min(pred_low, current_price * 0.99)      # Low below current
                
            elif signal == "SHORT":
                # For SHORT: target MUST be below entry, stop above entry
                min_downside = max(volatility_factor * 0.5, 0.01)  # At least 1% downside
                max_downside = min(volatility_factor * 2.0, 0.1)   # Max 10% downside
                
                pred_close = current_price * (1 - min_downside)
                pred_high = current_price * (1 + volatility_factor * 0.8)  # Reasonable stop
                pred_low = current_price * (1 - max_downside)
                
                # Ensure predictions make sense
                pred_close = min(pred_close, current_price * 0.995)  # At least 0.5% drop
                pred_low = min(pred_low, pred_close * 0.99)          # Low below close
                pred_high = max(pred_high, current_price * 1.01)     # High above current
                
            else:  # AVOID - neutral predictions
                pred_close = current_price
                pred_high = current_price * (1 + volatility_factor)
                pred_low = current_price * (1 - volatility_factor)
            
            # Final sanity checks
            if signal == "BUY":
                if pred_close <= current_price:
                    pred_close = current_price * 1.01  # Force at least 1% gain
                if pred_high <= pred_close:
                    pred_high = pred_close * 1.005     # Force high above close
                    
            elif signal == "SHORT":
                if pred_close >= current_price:
                    pred_close = current_price * 0.99  # Force at least 1% drop
                if pred_low >= pred_close:
                    pred_low = pred_close * 0.995      # Force low below close
            
            return pred_close, pred_low, pred_high
            
        except Exception as e:
            logger.error(f"Error generating predictions: {e}")
            return current_price, current_price, current_price

    def generate_trading_recommendation(self, signal, df, score):
        """Generate LOGICAL trading recommendations with proper risk/reward"""
        try:
            if df.empty:
                return {
                    'action': 'AVOID',
                    'reason': 'No data available',
                    'suggestion': 'Check data connection and try again',
                    'next_check': '1 hour'
                }
                
            current_price = df['close'].iloc[-1]
            if current_price <= 0:
                return {
                    'action': 'AVOID',
                    'reason': 'Invalid price data',
                    'suggestion': 'Wait for valid price data',
                    'next_check': '1 hour'
                }

            atr = df['ATR_14'].iloc[-1] if 'ATR_14' in df.columns and not pd.isna(df['ATR_14'].iloc[-1]) else current_price * 0.02
            support = df['Support'].iloc[-1] if 'Support' in df.columns and not pd.isna(df['Support'].iloc[-1]) else current_price * 0.95
            resistance = df['Resistance'].iloc[-1] if 'Resistance' in df.columns and not pd.isna(df['Resistance'].iloc[-1]) else current_price * 1.05
        
            if signal == "BUY":
                entry = round(current_price, 2)
                
                # Calculate realistic target (ABOVE entry)
                min_target_distance = max(atr * 0.8, current_price * 0.01)  # At least 1% gain or 0.8 ATR
                target = round(min(resistance, current_price + min_target_distance * 3), 2)
                
                # Ensure target is meaningfully above entry
                if target <= entry * 1.005:  # If target is less than 0.5% above entry
                    target = round(entry * 1.015, 2)  # Force at least 1.5% gain
                
                # Calculate stop loss (BELOW entry)
                stop_loss = round(max(support, current_price - min_target_distance * 2), 2)
                
                # Ensure proper distance between entry and stop
                if stop_loss >= entry * 0.995:  # If stop is too close
                    stop_loss = round(entry * 0.98, 2)  # Set stop at 2% below
                
                # Calculate risk/reward ratio
                risk = entry - stop_loss
                reward = target - entry
                
                if risk > 0:
                    risk_reward = round(reward / risk, 2)
                    # If risk/reward is poor, consider avoiding
                    if risk_reward < 1.0:
                        return {
                            'action': 'AVOID',
                            'reason': f'Poor risk/reward ratio ({risk_reward}:1)',
                            'suggestion': 'Wait for better entry with improved risk/reward',
                            'next_check': '2-4 hours'
                        }
                else:
                    risk_reward = 1.0

                return {
                    'action': 'BUY',
                    'entry': entry,
                    'target': target,
                    'stop_loss': stop_loss,
                    'risk_reward': risk_reward,
                    'position_size': 'Standard' if risk_reward >= 1.5 else 'Reduced',
                    'timeframe': '1-3 days' if score > 6 else 'Intraday'
                }
        
            elif signal == "SHORT":
                entry = round(current_price, 2)
                
                # Calculate realistic target (BELOW entry)
                min_target_distance = max(atr * 0.8, current_price * 0.01)  # At least 1% drop or 0.8 ATR
                target = round(max(support, current_price - min_target_distance * 3), 2)
                
                # Ensure target is meaningfully below entry
                if target >= entry * 0.995:  # If target is less than 0.5% below entry
                    target = round(entry * 0.985, 2)  # Force at least 1.5% drop
                
                # Calculate stop loss (ABOVE entry)
                stop_loss = round(min(resistance, current_price + min_target_distance * 2), 2)
                
                # Ensure proper distance between entry and stop
                if stop_loss <= entry * 1.005:  # If stop is too close
                    stop_loss = round(entry * 1.02, 2)  # Set stop at 2% above
                
                # Calculate risk/reward ratio
                risk = stop_loss - entry
                reward = entry - target
                
                if risk > 0:
                    risk_reward = round(reward / risk, 2)
                    # If risk/reward is poor, consider avoiding
                    if risk_reward < 1.0:
                        return {
                            'action': 'AVOID',
                            'reason': f'Poor risk/reward ratio ({risk_reward}:1)',
                            'suggestion': 'Wait for better entry with improved risk/reward',
                            'next_check': '2-4 hours'
                        }
                else:
                    risk_reward = 1.0

                return {
                    'action': 'SHORT', 
                    'entry': entry,
                    'target': target,
                    'stop_loss': stop_loss,
                    'risk_reward': risk_reward,
                    'position_size': 'Standard' if risk_reward >= 1.5 else 'Reduced',
                    'timeframe': '1-3 days' if score < -6 else 'Intraday'
                }
        
            else:  # AVOID
                return {
                    'action': 'AVOID',
                    'reason': 'Market conditions not favorable',
                    'suggestion': 'Wait for clearer setup or consider alternative assets',
                    'next_check': '4-6 hours'
                }

        except Exception as e:
            logger.error(f"Error generating trading recommendation: {e}")
            return {
                'action': 'AVOID',
                'reason': f'Error: {str(e)}',
                'suggestion': 'System error - please try again',
                'next_check': '1 hour'
            }

    def generate_market_commentary(self, signal, trading_rec, df, score):
        """Generate clear market commentary with validation"""
        try:
            if df.empty:
                return "No data available for analysis"
                
            current_price = df['close'].iloc[-1]
            if current_price <= 0:
                return "Invalid price data - cannot generate commentary"
            
            rsi = df['RSI_14'].iloc[-1] if 'RSI_14' in df.columns and not pd.isna(df['RSI_14'].iloc[-1]) else 50
        
            if signal == "BUY":
                risk_reward = trading_rec.get('risk_reward', 0)
                if risk_reward < 1.0:
                    return f"CAUTION - BUY signal but poor risk/reward ({risk_reward}:1). Consider waiting for better entry."
                else:
                    return (
                        f"BUY SIGNAL - Price ${current_price:.2f} shows bullish momentum. "
                        f"RSI at {rsi:.1f} supports upward move. Target ${trading_rec['target']:.2f} "
                        f"with stop at ${trading_rec['stop_loss']:.2f}. Risk/Reward: {risk_reward}:1"
                    )
        
            elif signal == "SHORT":
                risk_reward = trading_rec.get('risk_reward', 0)
                if risk_reward < 1.0:
                    return f"CAUTION - SHORT signal but poor risk/reward ({risk_reward}:1). Consider waiting for better entry."
                else:
                    return (
                        f"SHORT SIGNAL - Price ${current_price:.2f} shows bearish momentum. "
                        f"RSI at {rsi:.1f} supports downward move. Target ${trading_rec['target']:.2f} "
                        f"with stop at ${trading_rec['stop_loss']:.2f}. Risk/Reward: {risk_reward}:1"
                    )
        
            else:  # AVOID
                if rsi > 75:
                    reason = f"RSI {rsi:.1f} indicates overbought conditions"
                elif rsi < 25:
                    reason = f"RSI {rsi:.1f} indicates oversold conditions"
                elif abs(score) < 3:
                    reason = "Weak directional momentum"
                else:
                    reason = "Mixed technical signals or poor risk/reward"
                    
                return (
                    f"AVOID - {reason}. "
                    f"Current price ${current_price:.2f}. Wait for stronger confirmation signals."
                )

        except Exception as e:
            logger.error(f"Error generating commentary: {e}")
            return f"Error generating market commentary: {str(e)}"

def detect_last_reversal_enhanced(df):
    """Enhanced reversal detection with validation"""
    try:
        if df.empty or len(df) < 10:
            return "No clear reversal", 0, "N/A"
            
        current_price = df['close'].iloc[-1]
        if current_price <= 0:
            return "Invalid price", 0, "N/A"
            
        # Price reversal detection
        recent_high = df['high'].iloc[-10:].max()
        recent_low = df['low'].iloc[-10:].min()
        
        # RSI reversal detection
        rsi = df['RSI_14'].iloc[-1] if 'RSI_14' in df.columns and not pd.isna(df['RSI_14'].iloc[-1]) else 50
        prev_rsi = df['RSI_14'].iloc[-2] if len(df) > 1 and 'RSI_14' in df.columns and not pd.isna(df['RSI_14'].iloc[-2]) else rsi
        
        # MACD reversal detection
        if 'MACD' in df.columns and 'MACD_signal' in df.columns:
            macd = df['MACD'].iloc[-1] if not pd.isna(df['MACD'].iloc[-1]) else 0
            prev_macd = df['MACD'].iloc[-2] if len(df) > 1 and not pd.isna(df['MACD'].iloc[-2]) else macd
            signal = df['MACD_signal'].iloc[-1] if not pd.isna(df['MACD_signal'].iloc[-1]) else 0
            prev_signal = df['MACD_signal'].iloc[-2] if len(df) > 1 and not pd.isna(df['MACD_signal'].iloc[-2]) else signal
        else:
            macd = prev_macd = signal = prev_signal = 0
        
        # Check for bullish reversals
        if (current_price <= recent_low * 1.01 and 
            ((prev_rsi < 30 and rsi > 30) or (prev_macd <= prev_signal and macd > signal))):
            return "Bullish Reversal", current_price, datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # Check for bearish reversals  
        elif (current_price >= recent_high * 0.99 and 
              ((prev_rsi > 70 and rsi < 70) or (prev_macd >= prev_signal and macd < signal))):
            return "Bearish Reversal", current_price, datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        return "No clear reversal", current_price, "N/A"
        
    except Exception as e:
        logger.error(f"Reversal detection error: {e}")
        return "Unknown", 0, "N/A"

def _create_fallback_result(symbol, interval, error_message):
    """Create a fallback result when processing fails"""
    return {
        'symbol': symbol,
        'interval': interval,
        'signal': 'AVOID',
        'signal_detail': error_message,
        'score': 0,
        'confidence': 50,
        'current_price': 0,
        'predicted_close': 0,
        'predicted_low': 0,
        'predicted_high': 0,
        'support': 0,
        'resistance': 0,
        'commentary': f"Error: {error_message}",
        'trading_recommendation': {
            'action': 'AVOID',
            'reason': error_message,
            'suggestion': 'Please try again later',
            'next_check': '1 hour'
        },
        'last_reversal_type': 'Unknown',
        'last_reversal_price': 0,
        'last_reversal_time': 'N/A',
        'rsi': 50,
        'volume_ratio': 1.0,
        'trend_strength': 50
    }

async def process_interval_enhanced(symbol, interval, df, user_id):
    """Enhanced processing with comprehensive validation"""
    try:
        predictor = PurePythonStockPredictor()
        
        # Validate input data
        if df is None or df.empty:
            logger.warning(f"No data provided for {symbol} ({interval})")
            return _create_fallback_result(symbol, interval, "No data available")
        
        # Calculate advanced indicators
        df = predictor.calculate_advanced_indicators(df)
        
        if df.empty:
            logger.warning(f"Insufficient data after indicator calculation for {symbol} ({interval})")
            return _create_fallback_result(symbol, interval, "Insufficient data")
            
        # Get current price and validate
        current_price = df['close'].iloc[-1]
        if current_price <= 0:
            logger.error(f"Invalid current price for {symbol} ({interval}): {current_price}")
            return _create_fallback_result(symbol, interval, "Invalid price data")
        
        # Calculate comprehensive score
        score = predictor.calculate_comprehensive_score(df)
        
        # Generate clear signal (BUY/SHORT/AVOID)
        signal, signal_detail = predictor.generate_clear_signal(score, df)
        
        # Calculate confidence
        confidence = predictor.calculate_confidence_score(df, signal, score)
        
        # Generate trading recommendation
        trading_rec = predictor.generate_trading_recommendation(signal, df, score)
        
        # Generate realistic price predictions
        atr = df['ATR_14'].iloc[-1] if 'ATR_14' in df.columns and not pd.isna(df['ATR_14'].iloc[-1]) else current_price * 0.02
        support = df['Support'].iloc[-1] if 'Support' in df.columns and not pd.isna(df['Support'].iloc[-1]) else current_price * 0.95
        resistance = df['Resistance'].iloc[-1] if 'Resistance' in df.columns and not pd.isna(df['Resistance'].iloc[-1]) else current_price * 1.05
        
        pred_close, pred_low, pred_high = predictor.generate_realistic_predictions(
            signal, current_price, atr, support, resistance
        )
        
        # Generate market commentary
        commentary = predictor.generate_market_commentary(signal, trading_rec, df, score)
        
        # Detect last reversal
        last_reversal_type, last_reversal_price, last_reversal_time = detect_last_reversal_enhanced(df)
        
        # Prepare results
        result = {
            'symbol': symbol,
            'interval': interval,
            'signal': signal,
            'signal_detail': signal_detail,
            'score': round(score, 2),
            'confidence': confidence,
            'current_price': round(current_price, 2),
            'predicted_close': round(pred_close, 2),
            'predicted_low': round(pred_low, 2),
            'predicted_high': round(pred_high, 2),
            'support': round(support, 2),
            'resistance': round(resistance, 2),
            'commentary': commentary,
            'trading_recommendation': trading_rec,
            'last_reversal_type': last_reversal_type,
            'last_reversal_price': round(last_reversal_price, 2) if last_reversal_price else round(current_price, 2),
            'last_reversal_time': last_reversal_time,
        }
        
        # Add technical indicators
        if 'RSI_14' in df.columns and not pd.isna(df['RSI_14'].iloc[-1]):
            result['rsi'] = round(float(df['RSI_14'].iloc[-1]), 2)
        else:
            result['rsi'] = 50.0
            
        if 'Volume_Ratio' in df.columns and not pd.isna(df['Volume_Ratio'].iloc[-1]):
            result['volume_ratio'] = round(float(df['Volume_Ratio'].iloc[-1]), 2)
        else:
            result['volume_ratio'] = 1.0
            
        if 'Trend_Strength' in df.columns and not pd.isna(df['Trend_Strength'].iloc[-1]):
            result['trend_strength'] = round(float(df['Trend_Strength'].iloc[-1]), 2)
        else:
            result['trend_strength'] = 50.0
        
        # Save results
        save_enhanced_results(symbol, interval, user_id, result)
        return result
        
    except Exception as e:
        logger.error(f"Error processing {symbol} ({interval}): {e}")
        return _create_fallback_result(symbol, interval, f"Processing error: {str(e)}")

def save_enhanced_results(symbol, interval, user_id, result):
    """Save enhanced results to file with comprehensive validation"""
    try:
        user_dir = os.path.join(DATA_DIR, str(user_id))
        os.makedirs(user_dir, exist_ok=True)
        
        filename = f"{user_id}_{symbol}_{interval}_recommendation.txt"
        filepath = os.path.join(user_dir, filename)
        
        trading_rec = result.get('trading_recommendation', {})
        
        with open(filepath, 'w') as f:
            f.write(f"Symbol: {result['symbol']}\n")
            f.write(f"Action: {result['signal']}\n")
            f.write(f"Confidence: {result['confidence']}%\n")
            f.write(f"Score: {result['score']}\n")
            f.write(f"Current Price: {result['current_price']:.2f}\n")
            
            if result['signal'] in ['BUY', 'SHORT']:
                f.write(f"Entry Price: {trading_rec.get('entry', result['current_price']):.2f}\n")
                f.write(f"Target Price: {trading_rec.get('target', 0):.2f}\n")
                f.write(f"Stop Loss: {trading_rec.get('stop_loss', 0):.2f}\n")
                f.write(f"Risk/Reward: {trading_rec.get('risk_reward', 0):.1f}:1\n")
                f.write(f"Timeframe: {trading_rec.get('timeframe', 'N/A')}\n")
                f.write(f"Position Size: {trading_rec.get('position_size', 'Standard')}\n")
            else:
                f.write(f"Reason: {trading_rec.get('reason', 'Market conditions unclear')}\n")
                f.write(f"Suggestion: {trading_rec.get('suggestion', 'Wait for better setup')}\n")
                f.write(f"Next Check: {trading_rec.get('next_check', '4-6 hours')}\n")
            
            f.write(f"Predicted High: {result['predicted_high']:.2f}\n")
            f.write(f"Predicted Low: {result['predicted_low']:.2f}\n")
            f.write(f"Predicted Close: {result['predicted_close']:.2f}\n")
            f.write(f"Support: {result['support']:.2f}\n")
            f.write(f"Resistance: {result['resistance']:.2f}\n")
            f.write(f"RSI: {result.get('rsi', 50.0):.2f}\n")
            f.write(f"Volume Ratio: {result.get('volume_ratio', 1.0):.2f}\n")
            f.write(f"Market Commentary: {result['commentary']}\n")
            
        logger.info(f"✅ Enhanced trading recommendation saved for {symbol} ({interval}): {result['signal']}")
        
    except Exception as e:
        logger.error(f"Error saving enhanced results: {e}")

async def fetch_and_process_enhanced(symbol, interval, user_id, num_days, loop, executor):
    """Fetch data and process with enhanced predictor and fallback"""
    try:
        logger.info(f"📊 Fetching data for {symbol} ({interval})")
        
        if interval == '1d':
            def fetch_daily():
                try:
                    data = get_schwab_history(
                        symbol,
                        periodType="year",
                        period=1,
                        frequencyType="daily",
                        frequency=1,
                    )
                    if not data or "candles" not in data or not data["candles"]:
                        logger.warning(f"No daily data found for {symbol}")
                        return None
                    df = pd.DataFrame(data["candles"])
                    if df.empty:
                        return None
                    df["timestamp"] = pd.to_datetime(df["datetime"], unit="ms")
                    df = df[["timestamp", "open", "high", "low", "close", "volume"]]
                    logger.info(f"✅ Daily data fetched: {len(df)} rows")
                    return df
                except Exception as e:
                    logger.error(f"Error fetching daily data: {e}")
                    return None
            df = await loop.run_in_executor(executor, fetch_daily)
        else:
            def fetch_intraday():
                try:
                    data = get_schwab_intraday_multi_day(symbol, interval, num_days, user_id, False)
                    if data is None or data.empty:
                        logger.warning(f"No intraday data found for {symbol} ({interval})")
                        return None
                    logger.info(f"✅ Intraday data fetched: {len(data)} rows")
                    return data
                except Exception as e:
                    logger.error(f"Error fetching intraday data: {e}")
                    return None
            df = await loop.run_in_executor(executor, fetch_intraday)
        
        if df is None or df.empty:
            logger.error(f"No data retrieved for {symbol} ({interval})")
            return None
            
        # Validate we have reasonable data
        if 'close' not in df.columns or df['close'].isna().all() or (df['close'] <= 0).all():
            logger.error(f"Invalid close prices for {symbol} ({interval})")
            return None
            
        # Process with enhanced predictor
        result = await process_interval_enhanced(symbol, interval, df, user_id)
        return result
        
    except Exception as e:
        logger.error(f"Error in fetch_and_process_enhanced for {symbol} ({interval}): {e}")
        return None

async def process_all_intervals_enhanced(symbol, user_id, num_days=7):
    """Enhanced main processing function with comprehensive error handling"""
    intervals = ['5min', '15min', '30min', '1d']
    
    logger.info(f"🚀 Starting enhanced prediction for {symbol} (User: {user_id})")
    
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=4) as executor:
        tasks = []
        for interval in intervals:
            task = fetch_and_process_enhanced(symbol, interval, user_id, num_days, loop, executor)
            tasks.append(task)
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Log results and handle errors
        successful = 0
        for i, result in enumerate(results):
            interval = intervals[i]
            if isinstance(result, Exception):
                logger.error(f"❌ Error processing {interval}: {result}")
            elif result and result.get('current_price', 0) > 0:
                signal = result.get('signal', 'UNKNOWN')
                confidence = result.get('confidence', 0)
                price = result.get('current_price', 0)
                logger.info(f"✅ {interval}: {signal} (Confidence: {confidence}%, Price: ${price:.2f})")
                successful += 1
            else:
                logger.warning(f"⚠️  No valid result for {interval}")
                
        logger.info(f"🎯 Enhanced prediction completed: {successful}/{len(intervals)} intervals successful")
        return successful > 0

# Main execution
if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python predict_price_enhanced.py <symbol> <user_id>")
        sys.exit(1)
        
    symbol = sys.argv[1].upper()
    user_id = sys.argv[2]
    
    logger.info(f"🚀 Starting enhanced prediction for {symbol} (User: {user_id})")
    
    try:
        success = asyncio.run(process_all_intervals_enhanced(symbol, user_id))
        if success:
            logger.info(f"✅ Enhanced prediction completed successfully for {symbol}")
            sys.exit(0)
        else:
            logger.error(f"❌ Enhanced prediction failed for {symbol}")
            sys.exit(1)
    except Exception as e:
        logger.error(f"❌ Critical error in enhanced prediction: {e}")
        sys.exit(1)
