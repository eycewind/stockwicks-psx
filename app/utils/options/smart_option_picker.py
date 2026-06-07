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
    High-probability option picker with date parameter support.
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

    def get_available_expirations(self, symbol: str, min_days: int = 0, max_days: int = 60) -> List[str]:
        """Get available expiration dates within range"""
        try:
            expiries = get_expiration_chain(symbol)
            if not expiries:
                return []
            
            today = date.today()
            filtered = []
            for exp_str in expiries:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                days_to_exp = (exp_date - today).days
                if min_days <= days_to_exp <= max_days:
                    filtered.append(exp_str)
            
            return sorted(filtered)
        except Exception as e:
            logging.error(f"Failed to get expirations: {e}")
            return []

    def calculate_iv_rank(self, symbol: str, expiry: str = None) -> float:
        """Better IV Rank calculation using current vs historical"""
        try:
            # Get available expiries
            expiries = self.get_available_expirations(symbol, 0, 90)
            if not expiries:
                return 0.0
            
            # Get current IV from ATM options
            current_ivs = []
            for exp in expiries[:3]:  # Check next 3 expiries
                try:
                    chain = get_option_chain(symbol, exp)
                    if not chain.empty:
                        # Get ATM options (delta between 0.4 and 0.6)
                        atm_options = chain[
                            ((chain['side'] == 'call') & (abs(chain['delta'] - 0.5) < 0.1)) |
                            ((chain['side'] == 'put') & (abs(chain['delta'] + 0.5) < 0.1))
                        ]
                        if not atm_options.empty:
                            current_ivs.append(atm_options['iv'].mean())
                except:
                    continue
            
            if not current_ivs:
                return 0.0
            
            current_iv = np.mean(current_ivs)
            
            # Simple IV rank based on absolute IV values
            if current_iv > 40:
                return 0.8  # High IV
            elif current_iv > 30:
                return 0.6  # Medium-High IV
            elif current_iv > 20:
                return 0.4  # Medium IV
            elif current_iv > 10:
                return 0.2  # Low IV
            else:
                return 0.1  # Very Low IV
                
        except Exception as e:
            logging.error(f"IV rank calculation failed: {e}")
            return 0.0

    def get_technical_bias(self, symbol: str) -> str:
        """Improved technical bias using price action"""
        try:
            price = self.get_underlying_price(symbol)
            if not price:
                return "neutral"
            
            # Try to get some price history for basic analysis
            access_token = get_valid_access_token()
            if not access_token:
                return "neutral"
            
            # Get recent price data
            url = f"{self.schwab_base}/pricehistory"
            headers = {"Authorization": f"Bearer {access_token}"}
            params = {
                "symbol": symbol.upper(),
                "periodType": "day",
                "period": 5,
                "frequencyType": "minute",
                "frequency": 30,
            }
            
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=10)
                data = resp.json()
                
                if 'candles' in data and data['candles']:
                    closes = [c['close'] for c in data['candles']]
                    
                    if len(closes) >= 2:
                        # Simple trend detection
                        sma_short = np.mean(closes[-5:]) if len(closes) >= 5 else closes[-1]
                        sma_long = np.mean(closes[-10:]) if len(closes) >= 10 else closes[-1]
                        
                        if sma_short > sma_long * 1.02:
                            return "bullish"
                        elif sma_short < sma_long * 0.98:
                            return "bearish"
                        
            except Exception as e:
                logging.warning(f"Could not get price history for {symbol}: {e}")
            
            # Fallback to put/call ratio
            expiries = self.get_available_expirations(symbol, 0, 30)
            if expiries:
                chain = get_option_chain(symbol, expiries[0])
                if not chain.empty:
                    calls = chain[chain['side'] == 'call']
                    puts = chain[chain['side'] == 'put']
                    
                    if not calls.empty and not puts.empty:
                        put_call_ratio = puts['openInterest'].sum() / calls['openInterest'].sum()
                        
                        if put_call_ratio > 1.3:
                            return "bearish"
                        elif put_call_ratio < 0.7:
                            return "bullish"
            
            return "neutral"
                
        except Exception as e:
            logging.error(f"Technical bias failed: {e}")
            return "neutral"

    def find_credit_spread(self, symbol: str, bias: str, expiry: str = None) -> Optional[Dict]:
        """Find high-probability credit spreads with specified expiry"""
        try:
            price = self.get_underlying_price(symbol)
            
            # Get expiry if not provided
            if not expiry:
                expiries = self.get_available_expirations(symbol, 7, 30)
                if not expiries:
                    return None
                expiry = expiries[0]  # 1-4 weeks out
            
            chain = get_option_chain(symbol, expiry)
            
            if chain.empty or not price:
                return None
                
            iv_rank = self.calculate_iv_rank(symbol)
            
            # Only sell premium in medium-high IV environments
            if iv_rank < 0.3:
                logging.info(f"Low IV rank {iv_rank:.1%} for {symbol}, skipping credit spread")
                return None
            
            if bias == "bullish":
                # Bull Put Spread: Sell 30 delta, buy 15 delta put
                puts = chain[chain['side'] == 'put']
                short_puts = puts[(puts['delta'] < -0.25) & (puts['delta'] > -0.35)]
                long_puts = puts[(puts['delta'] < -0.10) & (puts['delta'] > -0.20)]
                
            elif bias == "bearish":
                # Bear Call Spread: Sell 30 delta, buy 15 delta call
                calls = chain[chain['side'] == 'call']
                short_calls = calls[(calls['delta'] > 0.25) & (calls['delta'] < 0.35)]
                long_calls = calls[(calls['delta'] > 0.10) & (calls['delta'] < 0.20)]
                
            else:
                # Iron Condor for neutral bias
                return self.find_iron_condor(symbol, price, expiry, chain)
            
            # Filter for liquidity
            if bias == "bullish":
                short_puts = short_puts[short_puts['openInterest'] >= 100]
                long_puts = long_puts[long_puts['openInterest'] >= 50]
                if short_puts.empty or long_puts.empty:
                    return None
                    
                short_put = short_puts.loc[short_puts['openInterest'].idxmax()]
                long_put = long_puts.loc[long_puts['openInterest'].idxmax()]
                
            else:  # bearish
                short_calls = short_calls[short_calls['openInterest'] >= 100]
                long_calls = long_calls[long_calls['openInterest'] >= 50]
                if short_calls.empty or long_calls.empty:
                    return None
                    
                short_call = short_calls.loc[short_calls['openInterest'].idxmax()]
                long_call = long_calls.loc[long_calls['openInterest'].idxmax()]
            
            # Validate strikes
            if bias == "bullish":
                if not (long_put['strike'] < short_put['strike'] < price * 1.05):
                    return None
                credit = short_put['mid'] - long_put['mid']
                width = long_put['strike'] - short_put['strike']
                pop = 1 - abs(short_put['delta'])
                
                if credit > 0 and width > 0 and pop > 0.65 and credit < width * 0.5:
                    return {
                        'type': 'BULL_PUT_SPREAD',
                        'short_strike': float(short_put['strike']),
                        'long_strike': float(long_put['strike']),
                        'credit': round(credit, 2),
                        'width': width,
                        'pop': round(pop, 3),
                        'expiry': expiry,
                        'max_loss': width - credit,
                        'roi': credit / (width - credit) if (width - credit) > 0 else 0
                    }
                    
            elif bias == "bearish":
                if not (price * 0.95 < short_call['strike'] < long_call['strike']):
                    return None
                credit = short_call['mid'] - long_call['mid']
                width = long_call['strike'] - short_call['strike']
                pop = 1 - abs(short_call['delta'])
                
                if credit > 0 and width > 0 and pop > 0.65 and credit < width * 0.5:
                    return {
                        'type': 'BEAR_CALL_SPREAD',
                        'short_strike': float(short_call['strike']),
                        'long_strike': float(long_call['strike']),
                        'credit': round(credit, 2),
                        'width': width,
                        'pop': round(pop, 3),
                        'expiry': expiry,
                        'max_loss': width - credit,
                        'roi': credit / (width - credit) if (width - credit) > 0 else 0
                    }
                    
            return None
            
        except Exception as e:
            logging.error(f"Credit spread search failed: {e}")
            return None

    # ... (keep other methods similar but add expiry parameter to find_iron_condor, find_debit_spread)

    def pick_best_trade(self, symbol: str, target_expiry: str = None, dte_range: Tuple[int, int] = (7, 45)) -> Optional[Dict]:
        """Main function to pick the best trade with date parameter"""
        try:
            logging.info(f"Analyzing {symbol} for best trade...")
            
            # Get market context
            bias = self.get_technical_bias(symbol)
            
            # Get available expirations
            expirations = self.get_available_expirations(symbol, dte_range[0], dte_range[1])
            if not expirations:
                logging.info(f"No expirations found for {symbol} in range {dte_range}")
                return None
            
            # Use target expiry if provided, otherwise use first available
            if target_expiry:
                if target_expiry in expirations:
                    expiry = target_expiry
                else:
                    logging.warning(f"Target expiry {target_expiry} not available, using {expirations[0]}")
                    expiry = expirations[0]
            else:
                expiry = expirations[0]
            
            iv_rank = self.calculate_iv_rank(symbol)
            
            logging.info(f"{symbol}: Bias={bias}, IV Rank={iv_rank:.1%}, Expiry={expiry}")
            
            # Try different strategies
            trades = []
            
            # 1. Credit spreads (high probability)
            if iv_rank > 0.3:  # Medium to high IV
                credit_trade = self.find_credit_spread(symbol, bias, expiry)
                if credit_trade:
                    credit_trade['strategy_score'] = 8  # High score for credit spreads
                    trades.append(credit_trade)
            
            # 2. Debit spreads (directional, lower IV)
            if iv_rank < 0.5 and bias != "neutral":  # Low to medium IV with clear bias
                debit_trade = self.find_debit_spread(symbol, bias, expiry)
                if debit_trade:
                    debit_trade['strategy_score'] = 6
                    trades.append(debit_trade)
            
            # 3. Iron condor (neutral, high IV)
            if iv_rank > 0.4 and bias == "neutral":
                condor_trade = self.find_iron_condor(symbol, self.get_underlying_price(symbol), expiry)
                if condor_trade:
                    condor_trade['strategy_score'] = 7
                    trades.append(condor_trade)
            
            if not trades:
                logging.info(f"No suitable trades found for {symbol}")
                return None
            
            # Rank trades by strategy score, then probability, then ROI
            best_trade = max(trades, key=lambda x: (
                x.get('strategy_score', 0),
                x.get('pop', 0),
                x.get('roi', 0)
            ))
            
            # Add metadata
            best_trade['symbol'] = symbol
            best_trade['underlying_price'] = self.get_underlying_price(symbol)
            best_trade['timestamp'] = datetime.now().isoformat()
            best_trade['iv_rank'] = iv_rank
            best_trade['bias'] = bias
            best_trade['days_to_expiry'] = (datetime.strptime(expiry, "%Y-%m-%d").date() - date.today()).days
            
            logging.info(f"Selected trade for {symbol}: {best_trade['type']}")
            return best_trade
            
        except Exception as e:
            logging.error(f"Trade selection failed for {symbol}: {e}")
            return None

# Updated interface function with date parameter
def get_recommended_trade(symbol: str, target_expiry: str = None, dte_range: Tuple[int, int] = (7, 45)) -> Dict:
    """Simple interface for the option picker with date parameter"""
    picker = SmartOptionPicker()
    trade = picker.pick_best_trade(symbol, target_expiry, dte_range)
    
    if trade:
        return trade
    else:
        return {
            'symbol': symbol,
            'recommendation': 'NO_TRADE',
            'reason': 'No high-probability setup found',
            'timestamp': datetime.now().isoformat(),
            'expirations_checked': picker.get_available_expirations(symbol, dte_range[0], dte_range[1])
        }