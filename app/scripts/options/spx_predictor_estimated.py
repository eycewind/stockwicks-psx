#!/usr/bin/env python3
"""
SPX Predictor with OI Estimation
Estimates open interest when API returns zeros
"""

import requests
import json
import sys
import os
from datetime import datetime
import random
from typing import Dict, List

# Add path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

class SPXPredictorWithEstimation:
    """SPX predictor that estimates OI when API returns zeros"""
    
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

    def get_spx_chain_with_estimated_oi(self, expiry: str) -> dict:
        """Get SPX chain and estimate OI when zeros are returned"""
        token = self.get_valid_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        
        print(f"🔍 Fetching SPX chain for {expiry}...")
        
        url = "https://api.schwabapi.com/marketdata/v1/chains"
        params = {
            "symbol": "$SPX",
            "contractType": "ALL",
            "strategy": "SINGLE", 
            "range": "ALL",
            "fromDate": expiry,
            "toDate": expiry,
            "includeQuotes": "TRUE"
        }
        
        response = requests.get(url, headers=headers, params=params, timeout=15)
        
        if response.status_code == 200:
            data = response.json()
            
            # Check if we need to estimate OI
            if self._needs_oi_estimation(data):
                print("⚠️ Zero OI detected, estimating based on volume and pricing...")
                data = self._estimate_open_interest(data)
            
            return data
        else:
            print(f"❌ API Error: {response.status_code}")
            return None

    def _needs_oi_estimation(self, data: dict) -> bool:
        """Check if we need to estimate OI (all zeros)"""
        total_oi = 0
        for option_type in ["callExpDateMap", "putExpDateMap"]:
            date_map = data.get(option_type, {})
            for expiry_key, strikes in date_map.items():
                for strike_price, contracts in strikes.items():
                    if contracts:
                        total_oi += contracts[0].get('openInterest', 0)
        
        return total_oi == 0

    def _estimate_open_interest(self, data: dict) -> dict:
        """Estimate realistic open interest based on available data"""
        current_price = data.get('underlyingPrice', 5675.50)
        
        for option_type in ["callExpDateMap", "putExpDateMap"]:
            date_map = data.get(option_type, {})
            for expiry_key, strikes in date_map.items():
                for strike_price, contracts in strikes.items():
                    if contracts:
                        contract = contracts[0]
                        strike = float(strike_price)
                        
                        # Estimate OI based on multiple factors
                        estimated_oi = self._calculate_oi_estimate(contract, strike, current_price)
                        
                        # Update the contract with estimated OI
                        contract['openInterest'] = estimated_oi
                        contract['estimatedOI'] = True  # Mark as estimated
        
        print("✅ Applied OI estimates based on market data")
        return data

    def _calculate_oi_estimate(self, contract: dict, strike: float, current_price: float) -> int:
        """Calculate realistic OI estimate for a contract"""
        
        # Base factors for OI estimation
        volume = contract.get('totalVolume', 0)
        bid = contract.get('bid', 0)
        ask = contract.get('ask', 0)
        in_the_money = contract.get('inTheMoney', False)
        is_call = contract.get('putCall') == 'CALL'
        
        # Base OI from volume (typical ratio)
        base_oi = max(volume * 3, 10)  # OI is typically 3x volume
        
        # Distance from current price effect
        distance_pct = abs(strike - current_price) / current_price
        distance_factor = 1.0 - min(distance_pct * 10, 0.8)  # Reduce OI for far OTM
        
        # Moneyness effect - higher OI for ATM
        moneyness_factor = 1.0
        if distance_pct < 0.02:  # Very close to current price
            moneyness_factor = 2.0
        elif distance_pct < 0.05:  # Near money
            moneyness_factor = 1.5
        
        # Bid-ask spread effect - tighter spreads usually mean higher liquidity/OI
        spread = ask - bid if bid > 0 and ask > 0 else 1.0
        spread_factor = 1.0 / max(spread, 0.1)  # Higher OI for tighter spreads
        
        # Calculate final OI estimate
        estimated_oi = int(base_oi * distance_factor * moneyness_factor * min(spread_factor, 3.0))
        
        # Add some random variation but keep it realistic
        variation = random.randint(-estimated_oi // 5, estimated_oi // 5)
        estimated_oi = max(10, estimated_oi + variation)  # Minimum 10 OI
        
        # Cap very high estimates
        estimated_oi = min(estimated_oi, 50000)
        
        return estimated_oi

    def analyze_and_predict(self, expiry: str):
        """Analyze SPX chain and make prediction"""
        data = self.get_spx_chain_with_estimated_oi(expiry)
        
        if not data:
            print("❌ Failed to get SPX data")
            return
        
        current_price = data.get('underlyingPrice', 0)
        print(f"\n💰 Current SPX: ${current_price:.2f}")
        
        # Extract and analyze OI data
        oi_data = self.extract_oi_data(data, expiry)
        
        if not oi_data:
            print("❌ No option data available")
            return
        
        # Calculate max pain
        max_pain = self.calculate_max_pain(oi_data)
        
        # Analyze market structure
        analysis = self.analyze_market_structure(oi_data, current_price)
        
        # Make prediction
        prediction = self.make_prediction(max_pain, analysis, current_price)
        
        # Print results
        self.print_analysis(current_price, max_pain, analysis, prediction, oi_data)

    def extract_oi_data(self, data: dict, expiry: str) -> List[dict]:
        """Extract OI data from chain"""
        result = []
        
        for option_type in ["callExpDateMap", "putExpDateMap"]:
            is_call = option_type == "callExpDateMap"
            date_map = data.get(option_type, {})
            
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
                            "open_interest": contract.get('openInterest', 0),
                            "volume": contract.get('totalVolume', 0),
                            "bid": contract.get('bid', 0),
                            "ask": contract.get('ask', 0),
                            "in_the_money": contract.get('inTheMoney', False),
                            "estimated_oi": contract.get('estimatedOI', False)
                        })
        
        print(f"✅ Analyzed {len(result)} SPX option contracts")
        return result

    def calculate_max_pain(self, oi_data: List[dict]) -> float:
        """Calculate max pain level"""
        strikes = sorted(list(set(item['strike'] for item in oi_data)))
        
        if not strikes:
            return 0
        
        pain_by_strike = []
        
        for target_strike in strikes:
            total_pain = 0
            for item in oi_data:
                if item["type"] == "call":
                    pain = max(0, target_strike - item["strike"]) * item["open_interest"]
                else:
                    pain = max(0, item["strike"] - target_strike) * item["open_interest"]
                total_pain += pain * 100  # SPX multiplier
            
            pain_by_strike.append((target_strike, total_pain))
        
        if pain_by_strike:
            max_pain_strike = min(pain_by_strike, key=lambda x: x[1])[0]
            return max_pain_strike
        
        return 0

    def analyze_market_structure(self, oi_data: List[dict], current_price: float) -> Dict:
        """Analyze market structure"""
        call_oi = sum(item['open_interest'] for item in oi_data if item['type'] == 'call')
        put_oi = sum(item['open_interest'] for item in oi_data if item['type'] == 'put')
        total_oi = call_oi + put_oi
        
        pcr = put_oi / call_oi if call_oi > 0 else 1.0
        
        if pcr > 1.2:
            bias = "BEARISH"
        elif pcr < 0.8:
            bias = "BULLISH"
        else:
            bias = "NEUTRAL"
        
        return {
            'total_call_oi': call_oi,
            'total_put_oi': put_oi,
            'put_call_ratio': round(pcr, 2),
            'market_bias': bias,
            'total_oi': total_oi
        }

    def make_prediction(self, max_pain: float, analysis: Dict, current_price: float) -> Dict:
        """Make SPX prediction"""
        move_pct = abs(max_pain - current_price) / current_price * 100
        direction = "UP" if max_pain > current_price else "DOWN" if max_pain < current_price else "FLAT"
        
        if move_pct < 0.3:
            move_type = "TIGHT PIN"
            confidence = 80
        elif move_pct < 0.8:
            move_type = "MODERATE MOVE"
            confidence = 70
        else:
            move_type = "SIGNIFICANT MOVE"
            confidence = 60
        
        return {
            'price': max_pain,
            'direction': direction,
            'move_pct': move_pct,
            'move_type': move_type,
            'confidence': confidence
        }

    def print_analysis(self, current_price: float, max_pain: float, analysis: Dict, prediction: Dict, oi_data: List[dict]):
        """Print analysis results"""
        print(f"\n🎯 SPX ANALYSIS RESULTS")
        print(f"{'='*50}")
        print(f"💰 Current Price: ${current_price:.2f}")
        print(f"🎯 Max Pain: ${max_pain:.2f}")
        print(f"📈 Predicted Move: {prediction['direction']} to ${prediction['price']:.2f}")
        print(f"📊 Move Type: {prediction['move_type']} ({prediction['move_pct']:.2f}%)")
        print(f"🎯 Confidence: {prediction['confidence']}%")
        
        print(f"\n📊 MARKET STRUCTURE:")
        print(f"   Total Call OI: {analysis['total_call_oi']:,}")
        print(f"   Total Put OI: {analysis['total_put_oi']:,}")
        print(f"   Put/Call Ratio: {analysis['put_call_ratio']:.2f}")
        print(f"   Market Bias: {analysis['market_bias']}")
        print(f"   Total OI: {analysis['total_oi']:,}")
        
        # Show top strikes by OI
        strike_oi = {}
        for item in oi_data:
            strike = item['strike']
            if strike not in strike_oi:
                strike_oi[strike] = 0
            strike_oi[strike] += item['open_interest']
        
        top_strikes = sorted(strike_oi.items(), key=lambda x: x[1], reverse=True)[:6]
        
        print(f"\n🏢 TOP STRIKES BY OI:")
        for strike, oi in top_strikes:
            distance_pct = (strike - current_price) / current_price * 100
            print(f"   ${strike:7.1f} - OI: {oi:6,} ({distance_pct:+.2f}%)")
        
        estimated_count = sum(1 for item in oi_data if item.get('estimated_oi', False))
        if estimated_count > 0:
            print(f"\n💡 Note: {estimated_count} contracts have estimated OI (API returned zeros)")

def main():
    """Main function"""
    if len(sys.argv) > 1:
        expiry_date = sys.argv[1]
    else:
        expiry_date = datetime.now().strftime("%Y-%m-%d")
    
    print(f"🎯 SPX PREDICTOR WITH OI ESTIMATION")
    print(f"📅 Date: {expiry_date}")
    print(f"{'='*60}")
    
    predictor = SPXPredictorWithEstimation()
    predictor.analyze_and_predict(expiry_date)

if __name__ == "__main__":
    main()