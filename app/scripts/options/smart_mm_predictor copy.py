#!/usr/bin/env python3
#/var/www/stockwicks/app/scripts/options/smart_mm_predictor.py
"""
Real-time MM Intent Analyzer
Focuses on PUT/CALL OI ratios and morning price action patterns
"""

import logging
import requests
import sys
import os
import json
from datetime import datetime, time, timedelta
from typing import Dict, List, Tuple

# Add path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

class RealTimeMMAnalyzer:
    """Real-time MM intent analyzer focusing on PUT/CALL OI patterns"""
    
    def __init__(self):
        self.token_path = "/var/www/stockwicks/data/schwab_token.json"
        
    def get_valid_access_token(self):
        """Get valid access token from file"""
        try:
            with open(self.token_path, 'r') as f:
                token_data = json.load(f)
            return token_data.get('access_token')
        except Exception as e:
            print(f"❌ Error reading token: {e}")
            raise

    def get_options_chain(self, symbol: str, expiry: str) -> dict:
        """Fetch options chain"""
        try:
            token = self.get_valid_access_token()
            headers = {"Authorization": f"Bearer {token}"}
            
            url = "https://api.schwabapi.com/marketdata/v1/chains"
            params = {
                "symbol": symbol.upper(),
                "contractType": "ALL",
                "strategy": "SINGLE",
                "range": "ALL",
                "fromDate": expiry,
                "toDate": expiry,
                "includeQuotes": "TRUE"
            }
            
            response = requests.get(url, headers=headers, params=params, timeout=15)
            if response.status_code == 200:
                return response.json()
            else:
                print(f"❌ API Error: {response.status_code}")
                return None
                
        except Exception as e:
            print(f"❌ API call failed: {e}")
            return None

    def analyze_mm_intent_simple(self, symbol: str, expiry: str = None) -> Dict:
        """
        Simple but effective MM intent analysis
        Focuses on: Highest OI strikes and PUT/CALL ratios
        """
        if expiry is None:
            expiry = datetime.now().strftime("%Y-%m-%d")
        
        print(f"\n🎯 REAL-TIME MM INTENT ANALYSIS: {symbol}")
        print(f"📅 Expiry: {expiry}")
        print("=" * 60)
        
        # Get options data
        option_chain = self.get_options_chain(symbol, expiry)
        if not option_chain:
            return {"error": "Failed to fetch options data"}
        
        current_price = option_chain.get('underlyingPrice', 0)
        oi_data = self.extract_oi_data(option_chain, expiry)
        
        if not oi_data:
            return {"error": "No options data available"}
        
        print(f"💰 Current Price: ${current_price:.2f}")
        
        # Simple but powerful analysis
        analysis = self._simple_oi_analysis(oi_data, current_price)
        mm_intent = self._determine_mm_intent(analysis, current_price)
        
        result = {
            "symbol": symbol,
            "expiry": expiry,
            "current_price": current_price,
            "mm_intent": mm_intent,
            "analysis": analysis,
            "recommendation": self._generate_simple_recommendation(mm_intent, analysis, current_price),
            "timestamp": datetime.now().isoformat()
        }
        
        self._print_simple_analysis(result)
        return result

    def _simple_oi_analysis(self, oi_data: List[dict], current_price: float) -> Dict:
        """
        Simple OI analysis focusing on what matters:
        1. Highest OI CALL strike
        2. Highest OI PUT strike  
        3. PUT/CALL ratio near current price
        4. Key resistance/support levels
        """
        # Find highest OI calls and puts
        call_oi = {}
        put_oi = {}
        
        for item in oi_data:
            strike = item['strike']
            if item['type'] == 'call':
                if strike not in call_oi:
                    call_oi[strike] = 0
                call_oi[strike] += item['open_interest']
            else:
                if strike not in put_oi:
                    put_oi[strike] = 0
                put_oi[strike] += item['open_interest']
        
        # Find highest OI strikes
        highest_call = max(call_oi.items(), key=lambda x: x[1]) if call_oi else (0, 0)
        highest_put = max(put_oi.items(), key=lambda x: x[1]) if put_oi else (0, 0)
        
        # Calculate PUT/CALL ratios in different zones
        call_oi_near = sum(oi for strike, oi in call_oi.items() if strike > current_price and strike <= current_price * 1.02)
        put_oi_near = sum(oi for strike, oi in put_oi.items() if strike < current_price and strike >= current_price * 0.98)
        
        call_oi_above = sum(oi for strike, oi in call_oi.items() if strike > current_price * 1.02)
        put_oi_below = sum(oi for strike, oi in put_oi.items() if strike < current_price * 0.98)
        
        near_cp_ratio = put_oi_near / call_oi_near if call_oi_near > 0 else float('inf')
        overall_cp_ratio = (put_oi_near + put_oi_below) / (call_oi_near + call_oi_above) if (call_oi_near + call_oi_above) > 0 else float('inf')
        
        return {
            'current_price': current_price,
            'highest_call_strike': highest_call[0],
            'highest_call_oi': highest_call[1],
            'highest_put_strike': highest_put[0],
            'highest_put_oi': highest_put[1],
            'near_put_call_ratio': near_cp_ratio,
            'overall_put_call_ratio': overall_cp_ratio,
            'call_oi_near': call_oi_near,
            'put_oi_near': put_oi_near,
            'call_oi_above': call_oi_above,
            'put_oi_below': put_oi_below,
            'total_call_oi': sum(call_oi.values()),
            'total_put_oi': sum(put_oi.values())
        }

    def _determine_mm_intent(self, analysis: Dict, current_price: float) -> Dict:
        """
        Determine MM intent based on simple but effective rules
        """
        highest_call = analysis['highest_call_strike']
        highest_put = analysis['highest_put_strike']
        near_cp_ratio = analysis['near_put_call_ratio']
        
        # Rule 1: If highest OI CALL is close to current price, MMs want to defend below it
        call_distance_pct = (highest_call - current_price) / current_price * 100
        put_distance_pct = (current_price - highest_put) / current_price * 100
        
        # Rule 2: High PUT/CALL ratio near current price suggests downward pressure
        # Rule 3: Morning pattern: They usually drop first, then rally
        
        intent = {
            'primary_target': current_price,
            'secondary_target': current_price,
            'defense_level': highest_put,
            'attack_level': highest_call,
            'bias': 'NEUTRAL',
            'confidence': 50,
            'morning_pattern': 'DROP_THEN_RALLY'  # This is their typical pattern
        }
        
        # Analyze based on highest OI strikes
        if highest_call > 0 and highest_put > 0:
            # If highest CALL is closer than highest PUT, expect upward move to call wall
            if call_distance_pct < put_distance_pct and call_distance_pct < 2.0:
                intent['primary_target'] = highest_call
                intent['bias'] = 'BULLISH'
                intent['confidence'] = 70
            # If highest PUT is closer, expect downward move to put wall
            elif put_distance_pct < call_distance_pct and put_distance_pct < 2.0:
                intent['primary_target'] = highest_put
                intent['bias'] = 'BEARISH' 
                intent['confidence'] = 70
        
        # Adjust based on PUT/CALL ratio
        if near_cp_ratio > 2.0:  # Heavy put positioning near current price
            intent['bias'] = 'BEARISH'
            intent['confidence'] = min(85, intent['confidence'] + 15)
        elif near_cp_ratio < 0.5:  # Heavy call positioning near current price
            intent['bias'] = 'BULLISH'
            intent['confidence'] = min(85, intent['confidence'] + 15)
        
        # Factor in the morning pattern
        if intent['bias'] == 'BEARISH':
            # Morning drop expected, but then rally toward call wall
            intent['secondary_target'] = analysis['highest_call_strike']
            intent['morning_sequence'] = f"Drop to ${intent['primary_target']:.2f}, then rally to ${intent['secondary_target']:.2f}"
        else:
            intent['morning_sequence'] = f"Rally to ${intent['primary_target']:.2f}"
        
        return intent

    def _generate_simple_recommendation(self, mm_intent: Dict, analysis: Dict, current_price: float) -> str:
        """Generate simple, actionable recommendation"""
        bias = mm_intent['bias']
        confidence = mm_intent['confidence']
        primary_target = mm_intent['primary_target']
        secondary_target = mm_intent['secondary_target']
        
        if bias == 'BULLISH':
            direction = "UP"
            move_pct = (primary_target - current_price) / current_price * 100
        elif bias == 'BEARISH':
            direction = "DOWN" 
            move_pct = (current_price - primary_target) / current_price * 100
        else:
            direction = "SIDEWAYS"
            move_pct = 0
        
        recommendation = f"MMs likely pushing {direction} to ${primary_target:.2f}"
        
        if mm_intent.get('morning_sequence'):
            recommendation += f" | Pattern: {mm_intent['morning_sequence']}"
        
        recommendation += f" | Confidence: {confidence}%"
        
        # Add OI context
        if analysis['highest_call_oi'] > analysis['highest_put_oi']:
            recommendation += f" | Highest OI: CALL ${analysis['highest_call_strike']:.2f}"
        else:
            recommendation += f" | Highest OI: PUT ${analysis['highest_put_strike']:.2f}"
        
        return recommendation

    def extract_oi_data(self, option_chain: dict, expiry: str) -> List[dict]:
        """Extract OI data from options chain"""
        result = []
        
        for option_type in ["callExpDateMap", "putExpDateMap"]:
            is_call = option_type == "callExpDateMap"
            date_map = option_chain.get(option_type, {})
            
            for expiry_key, strikes in date_map.items():
                expiry_date = expiry_key.split(':')[0]
                if expiry_date != expiry:
                    continue
                    
                for strike_price, contracts in strikes.items():
                    if contracts:
                        contract = contracts[0]
                        result.append({
                            "strike": float(strike_price),
                            "type": "call" if is_call else "put",
                            "open_interest": contract.get("openInterest", 0),
                            "volume": contract.get("totalVolume", 0),
                            "bid": contract.get("bid", 0),
                            "ask": contract.get("ask", 0),
                            "delta": contract.get("delta", 0),
                            "gamma": contract.get("gamma", 0),
                            "in_the_money": contract.get("inTheMoney", False)
                        })
        
        print(f"✅ Analyzed {len(result)} option contracts")
        return result

    def _print_simple_analysis(self, result: Dict):
        """Print simple but insightful analysis"""
        analysis = result['analysis']
        intent = result['mm_intent']
        
        print(f"\n🎯 KEY FINDINGS:")
        print(f"   Highest OI CALL: ${analysis['highest_call_strike']:.2f} (OI: {analysis['highest_call_oi']:,})")
        print(f"   Highest OI PUT:  ${analysis['highest_put_strike']:.2f} (OI: {analysis['highest_put_oi']:,})")
        print(f"   Near PUT/CALL Ratio: {analysis['near_put_call_ratio']:.2f}")
        print(f"   Total CALL OI: {analysis['total_call_oi']:,}")
        print(f"   Total PUT OI:  {analysis['total_put_oi']:,}")
        
        print(f"\n🤖 MM INTENT ANALYSIS:")
        print(f"   Primary Bias: {intent['bias']}")
        print(f"   Confidence: {intent['confidence']}%")
        print(f"   Primary Target: ${intent['primary_target']:.2f}")
        if intent.get('secondary_target') and intent['secondary_target'] != intent['primary_target']:
            print(f"   Secondary Target: ${intent['secondary_target']:.2f}")
        if intent.get('morning_sequence'):
            print(f"   Expected Pattern: {intent['morning_sequence']}")
        
        print(f"\n💡 RECOMMENDATION: {result['recommendation']}")
        
        # Simple logic explanation
        print(f"\n🔍 LOGIC:")
        if analysis['highest_call_oi'] > analysis['highest_put_oi']:
            print(f"   • Highest OI is CALLs at ${analysis['highest_call_strike']:.2f}")
            print(f"   • MMs likely want price to approach this level")
            if analysis['near_put_call_ratio'] > 1.5:
                print(f"   • But heavy PUTs nearby may cause initial drop")
        else:
            print(f"   • Highest OI is PUTs at ${analysis['highest_put_strike']:.2f}")
            print(f"   • MMs likely want price to approach this level")
            if analysis['near_put_call_ratio'] < 0.7:
                print(f"   • But heavy CALLs nearby may cause initial rally")

    def analyze_multiple_expiries(self, symbol: str) -> Dict:
        """Analyze multiple expiry dates to see MM positioning across time"""
        today = datetime.now().strftime("%Y-%m-%d")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        
        # Calculate next Friday
        days_ahead = 4 - datetime.now().weekday()
        if days_ahead <= 0:
            days_ahead += 7
        next_friday = (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        
        expiries = [today, tomorrow, next_friday]
        results = {}
        
        print(f"\n📊 MULTI-EXPIRY ANALYSIS: {symbol}")
        print("=" * 60)
        
        for expiry in expiries:
            print(f"\n🔍 Analyzing {expiry}...")
            try:
                result = self.analyze_mm_intent_simple(symbol, expiry)
                results[expiry] = result
            except Exception as e:
                print(f"❌ Failed for {expiry}: {e}")
                results[expiry] = {"error": str(e)}
        
        return results

# Command line interface
def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Real-time MM Intent Analyzer')
    parser.add_argument('--symbol', '-s', type=str, default='SPY', help='Symbol to analyze')
    parser.add_argument('--date', '-d', type=str, help='Date (default: today)')
    parser.add_argument('--multi', '-m', action='store_true', help='Analyze multiple expiries')
    
    args = parser.parse_args()
    
    analyzer = RealTimeMMAnalyzer()
    
    if args.multi:
        analyzer.analyze_multiple_expiries(args.symbol)
    else:
        analyzer.analyze_mm_intent_simple(args.symbol, args.date)

if __name__ == "__main__":
    main()