# app/utils/options/smart_option_picker.py

import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import requests

from app.utils.stock.schwab_token import get_valid_access_token
from app.scripts.options.option_market_data import get_option_chain, get_nearest_expiry


class SmartOptionPicker:
    """
    High-probability option picker focusing on:
    1. High IV Rank for premium selling
    2. Technical analysis for direction
    3. Liquidity filters for execution
    4. Risk-defined spreads for capital protection
    """
    
    def __init__(self):
        self.schwab_base = "https://api.schwabapi.com/marketdata/v1"
        
    def get_underlying_price(self, symbol: str) -> float:
        """Get current underlying price"""
        access_token = get_valid_access_token()
        if not access_token:
            return 0.0
            
        url = f"{self.schwab_base}/quotes"
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {"symbols": symbol.upper(), "fields": "quote"}
        
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            data = resp.json()
            return float(data[symbol.upper()]["quote"]["lastPrice"])
        except Exception as e:
            logging.error(f"Price fetch failed for {symbol}: {e}")
            return 0.0

    def calculate_iv_rank(self, symbol: str, lookback_days: int = 30) -> float:
        """Calculate IV Rank for premium selling opportunities"""
        # Simplified IV rank - in production, use historical IV data
        try:
            expiry = get_nearest_expiry(symbol, 1)
            if not expiry:
                return 0.0
                
            chain = get_option_chain(symbol, expiry)
            if chain.empty:
                return 0.0
                
            # Use ATM options for IV
            atm_calls = chain[(chain['side'] == 'call') & 
                            (abs(chain['delta'] - 0.5) < 0.1)]
            atm_puts = chain[(chain['side'] == 'put') & 
                           (abs(chain['delta'] + 0.5) < 0.1)]
            
            if not atm_calls.empty:
                current_iv = atm_calls['iv'].iloc[0]
            elif not atm_puts.empty:
                current_iv = atm_puts['iv'].iloc[0]
            else:
                return 0.0
                
            # Simplified IV rank (in production, use historical data)
            # High IV (>40%) is good for selling, low IV (<20%) for buying
            if current_iv > 40:
                return 0.8  # High IV rank
            elif current_iv > 30:
                return 0.5  # Medium IV rank
            else:
                return 0.2  # Low IV rank
                
        except Exception as e:
            logging.error(f"IV rank calculation failed: {e}")
            return 0.0

    def get_technical_bias(self, symbol: str) -> str:
        """Simple technical analysis for direction bias"""
        try:
            price = self.get_underlying_price(symbol)
            if not price:
                return "neutral"
                
            # Simple moving average analysis
            # In production, use proper technical analysis
            # For now, use a simplified approach
            expiry = get_nearest_expiry(symbol, 1)
            chain = get_option_chain(symbol, expiry)
            
            if chain.empty:
                return "neutral"
                
            # Analyze put/call ratio and OI for sentiment
            calls = chain[chain['side'] == 'call']
            puts = chain[chain['side'] == 'put']
            
            if calls.empty or puts.empty:
                return "neutral"
                
            total_call_oi = calls['openInterest'].sum()
            total_put_oi = puts['openInterest'].sum()
            
            put_call_ratio = total_put_oi / total_call_oi if total_call_oi > 0 else 1
            
            if put_call_ratio > 1.2:
                return "bearish"  # More puts than calls
            elif put_call_ratio < 0.8:
                return "bullish"  # More calls than puts
            else:
                return "neutral"
                
        except Exception as e:
            logging.error(f"Technical bias failed: {e}")
            return "neutral"

    def find_credit_spread(self, symbol: str, bias: str) -> Optional[Dict]:
        """Find high-probability credit spreads"""
        try:
            price = self.get_underlying_price(symbol)
            expiry = get_nearest_expiry(symbol, 2)  # 2 weeks out for better theta
            chain = get_option_chain(symbol, expiry)
            
            if chain.empty or not price:
                return None
                
            iv_rank = self.calculate_iv_rank(symbol)
            
            # Only sell premium in high IV environments
            if iv_rank < 0.4:
                logging.info(f"Low IV rank {iv_rank} for {symbol}, skipping credit spread")
                return None
            
            if bias == "bullish":
                # Bull Put Spread: Sell higher delta, buy lower delta put
                puts = chain[chain['side'] == 'put']
                short_puts = puts[(puts['delta'] < -0.25) & (puts['delta'] > -0.40)]
                long_puts = puts[(puts['delta'] < -0.10) & (puts['delta'] > -0.20)]
                
            elif bias == "bearish":
                # Bear Call Spread: Sell lower delta, buy higher delta call
                calls = chain[chain['side'] == 'call']
                short_calls = calls[(calls['delta'] > 0.25) & (calls['delta'] < 0.40)]
                long_calls = calls[(calls['delta'] > 0.10) & (calls['delta'] < 0.20)]
                
            else:
                # Iron Condor for neutral bias
                return self.find_iron_condor(symbol, price, expiry, chain)
            
            if (bias == "bullish" and (short_puts.empty or long_puts.empty)) or \
               (bias == "bearish" and (short_calls.empty or long_calls.empty)):
                return None
            
            # Select strikes with best liquidity
            if bias == "bullish":
                short_put = short_puts.loc[short_puts['openInterest'].idxmax()]
                long_put = long_puts.loc[long_puts['openInterest'].idxmax()]
                
                credit = short_put['mid'] - long_put['mid']
                width = long_put['strike'] - short_put['strike']
                pop = 1 - abs(short_put['delta'])  # Probability of profit
                
                if credit > 0 and width > 0 and pop > 0.65:
                    return {
                        'type': 'BULL_PUT_SPREAD',
                        'short_strike': short_put['strike'],
                        'long_strike': long_put['strike'],
                        'credit': round(credit, 2),
                        'width': width,
                        'pop': round(pop, 3),
                        'expiry': expiry,
                        'max_loss': width - credit,
                        'roi': credit / (width - credit)
                    }
                    
            elif bias == "bearish":
                short_call = short_calls.loc[short_calls['openInterest'].idxmax()]
                long_call = long_calls.loc[long_calls['openInterest'].idxmax()]
                
                credit = short_call['mid'] - long_call['mid']
                width = long_call['strike'] - short_call['strike']
                pop = 1 - abs(short_call['delta'])
                
                if credit > 0 and width > 0 and pop > 0.65:
                    return {
                        'type': 'BEAR_CALL_SPREAD',
                        'short_strike': short_call['strike'],
                        'long_strike': long_call['strike'],
                        'credit': round(credit, 2),
                        'width': width,
                        'pop': round(pop, 3),
                        'expiry': expiry,
                        'max_loss': width - credit,
                        'roi': credit / (width - credit)
                    }
                    
            return None
            
        except Exception as e:
            logging.error(f"Credit spread search failed: {e}")
            return None

    def find_iron_condor(self, symbol: str, price: float, expiry: str, chain: pd.DataFrame) -> Optional[Dict]:
        """Find Iron Condor for neutral markets"""
        try:
            # Sell OTM put and call, buy further OTM for protection
            puts = chain[chain['side'] == 'put']
            calls = chain[chain['side'] == 'call']
            
            # 30 delta short strikes, 15 delta long strikes
            short_put = puts[(puts['delta'] < -0.25) & (puts['delta'] > -0.35)]
            long_put = puts[(puts['delta'] < -0.10) & (puts['delta'] > -0.20)]
            short_call = calls[(calls['delta'] > 0.25) & (calls['delta'] < 0.35)]
            long_call = calls[(calls['delta'] > 0.10) & (calls['delta'] < 0.20)]
            
            if short_put.empty or long_put.empty or short_call.empty or long_call.empty:
                return None
            
            # Pick most liquid
            short_put = short_put.loc[short_put['openInterest'].idxmax()]
            long_put = long_put.loc[long_put['openInterest'].idxmax()]
            short_call = short_call.loc[short_call['openInterest'].idxmax()]
            long_call = long_call.loc[long_call['openInterest'].idxmax()]
            
            put_credit = short_put['mid'] - long_put['mid']
            call_credit = short_call['mid'] - long_call['mid']
            total_credit = put_credit + call_credit
            
            put_width = long_put['strike'] - short_put['strike']
            call_width = long_call['strike'] - short_call['strike']
            
            if total_credit > 0 and min(put_width, call_width) > 0:
                max_loss = max(put_width, call_width) - total_credit
                pop = 0.7  # Rough estimate for iron condor
                
                return {
                    'type': 'IRON_CONDOR',
                    'put_short': short_put['strike'],
                    'put_long': long_put['strike'],
                    'call_short': short_call['strike'],
                    'call_long': long_call['strike'],
                    'credit': round(total_credit, 2),
                    'pop': pop,
                    'expiry': expiry,
                    'max_loss': max_loss,
                    'roi': total_credit / max_loss
                }
                
            return None
            
        except Exception as e:
            logging.error(f"Iron condor search failed: {e}")
            return None

    def find_debit_spread(self, symbol: str, bias: str) -> Optional[Dict]:
        """Find debit spreads for directional plays in low IV"""
        try:
            price = self.get_underlying_price(symbol)
            expiry = get_nearest_expiry(symbol, 1)
            chain = get_option_chain(symbol, expiry)
            
            if chain.empty or not price:
                return None
                
            iv_rank = self.calculate_iv_rank(symbol)
            
            # Only buy in low IV environments
            if iv_rank > 0.6:
                logging.info(f"High IV rank {iv_rank} for {symbol}, avoid debit spreads")
                return None
            
            if bias == "bullish":
                # Debit Call Spread
                calls = chain[chain['side'] == 'call']
                long_calls = calls[(calls['delta'] > 0.65) & (calls['delta'] < 0.80)]
                short_calls = calls[(calls['delta'] > 0.45) & (calls['delta'] < 0.60)]
                
            elif bias == "bearish":
                # Debit Put Spread
                puts = chain[chain['side'] == 'put']
                long_puts = puts[(puts['delta'] < -0.65) & (puts['delta'] > -0.80)]
                short_puts = puts[(puts['delta'] < -0.45) & (puts['delta'] > -0.60)]
            else:
                return None
            
            if (bias == "bullish" and (long_calls.empty or short_calls.empty)) or \
               (bias == "bearish" and (long_puts.empty or short_puts.empty)):
                return None
            
            if bias == "bullish":
                long_call = long_calls.loc[long_calls['openInterest'].idxmax()]
                short_call = short_calls.loc[short_calls['openInterest'].idxmax()]
                
                debit = long_call['mid'] - short_call['mid']
                width = short_call['strike'] - long_call['strike']
                
                if debit > 0 and width > debit:  # Positive expectancy
                    return {
                        'type': 'DEBIT_CALL_SPREAD',
                        'long_strike': long_call['strike'],
                        'short_strike': short_call['strike'],
                        'debit': round(debit, 2),
                        'width': width,
                        'max_profit': width - debit,
                        'expiry': expiry,
                        'roi': (width - debit) / debit
                    }
                    
            elif bias == "bearish":
                long_put = long_puts.loc[long_puts['openInterest'].idxmax()]
                short_put = short_puts.loc[short_puts['openInterest'].idxmax()]
                
                debit = long_put['mid'] - short_put['mid']
                width = long_put['strike'] - short_put['strike']
                
                if debit > 0 and width > debit:
                    return {
                        'type': 'DEBIT_PUT_SPREAD',
                        'long_strike': long_put['strike'],
                        'short_strike': short_put['strike'],
                        'debit': round(debit, 2),
                        'width': width,
                        'max_profit': width - debit,
                        'expiry': expiry,
                        'roi': (width - debit) / debit
                    }
                    
            return None
            
        except Exception as e:
            logging.error(f"Debit spread search failed: {e}")
            return None

    def pick_best_trade(self, symbol: str) -> Optional[Dict]:
        """Main function to pick the best trade for a symbol"""
        try:
            logging.info(f"Analyzing {symbol} for best trade...")
            
            # Get market context
            bias = self.get_technical_bias(symbol)
            iv_rank = self.calculate_iv_rank(symbol)
            
            logging.info(f"{symbol}: Bias={bias}, IV Rank={iv_rank}")
            
            # Strategy selection based on IV and bias
            trades = []
            
            # Prefer credit spreads in high IV
            if iv_rank > 0.4:
                credit_trade = self.find_credit_spread(symbol, bias)
                if credit_trade:
                    trades.append(credit_trade)
            
            # Consider debit spreads in low IV with strong bias
            if iv_rank < 0.6 and bias != "neutral":
                debit_trade = self.find_debit_spread(symbol, bias)
                if debit_trade:
                    trades.append(debit_trade)
            
            # Iron condor for high IV neutral markets
            if iv_rank > 0.4 and bias == "neutral":
                condor_trade = self.find_iron_condor(symbol, self.get_underlying_price(symbol), 
                                                   get_nearest_expiry(symbol, 2), 
                                                   get_option_chain(symbol, get_nearest_expiry(symbol, 2)))
                if condor_trade:
                    trades.append(condor_trade)
            
            if not trades:
                logging.info(f"No suitable trades found for {symbol}")
                return None
            
            # Rank trades by probability of profit first, then ROI
            best_trade = max(trades, key=lambda x: (x.get('pop', 0), x.get('roi', 0)))
            
            # Add metadata
            best_trade['symbol'] = symbol
            best_trade['underlying_price'] = self.get_underlying_price(symbol)
            best_trade['timestamp'] = datetime.now().isoformat()
            best_trade['iv_rank'] = iv_rank
            best_trade['bias'] = bias
            
            logging.info(f"Selected trade for {symbol}: {best_trade['type']}")
            return best_trade
            
        except Exception as e:
            logging.error(f"Trade selection failed for {symbol}: {e}")
            return None

# Simple usage function
def get_recommended_trade(symbol: str) -> Dict:
    """Simple interface for the option picker"""
    picker = SmartOptionPicker()
    trade = picker.pick_best_trade(symbol)
    
    if trade:
        return trade
    else:
        return {
            'symbol': symbol,
            'recommendation': 'NO_TRADE',
            'reason': 'No high-probability setup found',
            'timestamp': datetime.now().isoformat()
        }