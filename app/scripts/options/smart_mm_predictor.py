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
from typing import Dict, List, Tuple, Any

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

    def analyze_mm_intent_simple(
        self,
        symbol: str,
        expiry: str = None,
        verbose: bool = True,   # <<< NEW: verbose flag for prints
    ) -> Dict[str, Any]:
        """
        Simple but effective MM intent analysis
        Focuses on: Highest OI strikes and PUT/CALL ratios

        When verbose=False, it does NOT print (for API use).
        """
        if expiry is None:
            expiry = datetime.now().strftime("%Y-%m-%d")
        
        if verbose:
            print(f"\n🎯 REAL-TIME MM INTENT ANALYSIS: {symbol}")
            print(f"📅 Expiry: {expiry}")
            print("=" * 60)
        
        # Get options data
        option_chain = self.get_options_chain(symbol, expiry)
        if not option_chain:
            return {"error": "Failed to fetch options data", "symbol": symbol, "expiry": expiry}
        
        current_price = option_chain.get('underlyingPrice', 0)
        oi_data = self.extract_oi_data(option_chain, expiry, verbose=verbose)  # <<< CHANGED
        
        if not oi_data:
            return {"error": "No options data available", "symbol": symbol, "expiry": expiry}
        
        if verbose:
            print(f"💰 Current Price: ${current_price:.2f}")
        
        # Simple but powerful analysis
        analysis = self._simple_oi_analysis(oi_data, current_price)
        analysis["contracts_analyzed"] = len(oi_data)  # <<< NEW
        
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
        
        if verbose:
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
        call_distance_pct = (highest_call - current_price) / current_price * 100 if current_price else 0.0
        put_distance_pct = (current_price - highest_put) / current_price * 100 if current_price else 0.0
        
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
        if highest_call > 0 and highest_put > 0 and current_price > 0:
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
            move_pct = (primary_target - current_price) / current_price * 100 if current_price else 0.0
        elif bias == 'BEARISH':
            direction = "DOWN" 
            move_pct = (current_price - primary_target) / current_price * 100 if current_price else 0.0
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

    def extract_oi_data(self, option_chain: dict, expiry: str, verbose: bool = True) -> List[dict]:
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
        
        if verbose:
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


# ===== NEW: service-friendly helper =====
def run_smart_mm_analysis(symbol: str, expiry: str | None = None) -> Dict[str, Any]:
    """
    Wrapper used by FastAPI route.
    - No printing
    - Adds human-friendly headline and explanation bullets
    """
    analyzer = RealTimeMMAnalyzer()
    data = analyzer.analyze_mm_intent_simple(symbol, expiry, verbose=False)

    if "error" in data:
        # Still return some metadata so UI can show something
        data.setdefault("symbol", symbol)
        data.setdefault("expiry", expiry or datetime.now().strftime("%Y-%m-%d"))
        data["headline"] = f"Unable to compute MM intent for {symbol} {data['expiry']}."
        data["explanation_points"] = [data["error"]]
        data["logic_points"] = []
        return data

    analysis = data["analysis"]
    intent = data["mm_intent"]
    rec = data["recommendation"]

    bias = intent.get("bias", "NEUTRAL")
    confidence = intent.get("confidence", 50)
    primary_target = intent.get("primary_target", analysis.get("current_price", 0.0))
    expiry = data["expiry"]
    symbol = data["symbol"].upper()

    bias_phrase = {
        "BULLISH": "an upward / bullish",
        "BEARISH": "a downward / bearish",
        "NEUTRAL": "a sideways / neutral"
    }.get(bias, "a neutral")

    headline = (
        f"{symbol} {expiry}: Market makers show {bias_phrase} bias "
        f"toward about ${primary_target:,.2f} into expiration "
        f"(confidence {confidence}%)."
    )

    explanation_points = [
        f"Analyzed {analysis.get('contracts_analyzed', 0)} option contracts for {symbol} expiring on {expiry}.",
        f"Current underlying price is around ${analysis.get('current_price', 0.0):,.2f}.",
        f"Highest CALL open interest sits at ${analysis['highest_call_strike']:.2f} "
        f"with OI ≈ {analysis['highest_call_oi']:,}.",
        f"Highest PUT open interest sits at ${analysis['highest_put_strike']:.2f} "
        f"with OI ≈ {analysis['highest_put_oi']:,}.",
        f"Total CALL OI: {analysis['total_call_oi']:,} | Total PUT OI: {analysis['total_put_oi']:,}.",
        f"Near-the-money PUT/CALL OI ratio is about {analysis['near_put_call_ratio']:.2f}, "
        f"giving a quick read on how aggressively traders are positioned.",
        f"Detected MM bias: {bias} with {confidence}% confidence. "
        f"Expected path: {intent.get('morning_sequence', 'no strong pattern detected')}."
    ]

    # Logic bullets (maps your console LOGIC section into readable lines)
    logic_points: List[str] = []
    if analysis['highest_call_oi'] > analysis['highest_put_oi']:
        logic_points.append(
            f"Open interest is more concentrated in CALLs at ${analysis['highest_call_strike']:.2f}, "
            f"which often acts like a ceiling where MMs prefer price not to run too far above."
        )
        if analysis['near_put_call_ratio'] > 1.5:
            logic_points.append(
                "However, there is relatively heavy PUT positioning near the current price, "
                "which can create an initial downward push before any potential rally."
            )
    else:
        logic_points.append(
            f"Open interest is more concentrated in PUTs at ${analysis['highest_put_strike']:.2f}, "
            f"which can act as a magnet / support zone into expiration."
        )
        if analysis['near_put_call_ratio'] < 0.7:
            logic_points.append(
                "There are relatively more CALLs than PUTs near the money, "
                "which can fuel an early rally before price drifts back toward the put wall."
            )

    data["headline"] = headline
    data["explanation_points"] = explanation_points
    data["logic_points"] = logic_points
    # keep existing "recommendation" as-is (your compact line)
    return data


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
        analyzer.analyze_mm_intent_simple(args.symbol, args.date, verbose=True)  # <<< CHANGED

if __name__ == "__main__":
    main()
