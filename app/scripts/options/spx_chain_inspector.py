#!/usr/bin/env python3
"""
Simple SPX Chain Inspector
Pulls SPX options chains and shows exactly what data we're getting
"""

import requests
import json
import sys
import os
from datetime import datetime

# Add path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

def get_spx_chain_info(expiry_date: str):
    """Get SPX chain data and inspect what we receive"""
    
    # Read token
    token_path = "/var/www/stockwicks/data/schwab_token.json"
    try:
        with open(token_path, 'r') as f:
            token_data = json.load(f)
        token = token_data.get('access_token')
        print(f"✅ Token loaded successfully")
    except Exception as e:
        print(f"❌ Error reading token: {e}")
        return
    
    headers = {"Authorization": f"Bearer {token}"}
    
    # Test different symbol formats
    symbols_to_test = ["$SPX", "SPX", "$SPX.X", "SPXW", ".SPX"]
    
    for symbol in symbols_to_test:
        print(f"\n{'='*60}")
        print(f"🔍 Testing symbol: '{symbol}'")
        print(f"{'='*60}")
        
        try:
            url = "https://api.schwabapi.com/marketdata/v1/chains"
            params = {
                "symbol": symbol,
                "contractType": "ALL",
                "strategy": "SINGLE", 
                "range": "ALL",
                "fromDate": expiry_date,
                "toDate": expiry_date,
                "includeQuotes": "TRUE"
            }
            
            response = requests.get(url, headers=headers, params=params, timeout=15)
            print(f"📡 Response Status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                analyze_chain_data(data, symbol)
            else:
                print(f"❌ API Error: {response.status_code}")
                print(f"Response text: {response.text[:200]}...")
                
        except Exception as e:
            print(f"❌ Request failed: {e}")

def analyze_chain_data(data: dict, symbol: str):
    """Analyze what data we actually received"""
    
    print(f"\n📊 ANALYSIS FOR '{symbol}':")
    print(f"{'-'*40}")
    
    # Basic info
    underlying_price = data.get('underlyingPrice', 'N/A')
    print(f"Underlying Price: {underlying_price}")
    
    # Check call data
    call_map = data.get('callExpDateMap', {})
    print(f"Call expiration dates: {len(call_map)}")
    
    call_contracts = 0
    call_total_oi = 0
    call_strikes_with_oi = 0
    
    for expiry_key, strikes in call_map.items():
        print(f"  Expiry: {expiry_key} - {len(strikes)} strikes")
        for strike, contracts in strikes.items():
            if contracts:
                call_contracts += 1
                oi = contracts[0].get('openInterest', 0)
                call_total_oi += oi
                if oi > 0:
                    call_strikes_with_oi += 1
                    
                # Print first few contracts for inspection
                if call_contracts <= 3:
                    print(f"    Strike {strike}: OI={oi}, Bid={contracts[0].get('bid', 'N/A')}, Ask={contracts[0].get('ask', 'N/A')}")
    
    print(f"Total call contracts: {call_contracts}")
    print(f"Total call OI: {call_total_oi}")
    print(f"Call strikes with OI > 0: {call_strikes_with_oi}")
    
    # Check put data
    put_map = data.get('putExpDateMap', {})
    print(f"\nPut expiration dates: {len(put_map)}")
    
    put_contracts = 0
    put_total_oi = 0
    put_strikes_with_oi = 0
    
    for expiry_key, strikes in put_map.items():
        print(f"  Expiry: {expiry_key} - {len(strikes)} strikes")
        for strike, contracts in strikes.items():
            if contracts:
                put_contracts += 1
                oi = contracts[0].get('openInterest', 0)
                put_total_oi += oi
                if oi > 0:
                    put_strikes_with_oi += 1
                    
                # Print first few contracts for inspection
                if put_contracts <= 3:
                    print(f"    Strike {strike}: OI={oi}, Bid={contracts[0].get('bid', 'N/A')}, Ask={contracts[0].get('ask', 'N/A')}")
    
    print(f"Total put contracts: {put_contracts}")
    print(f"Total put OI: {put_total_oi}")
    print(f"Put strikes with OI > 0: {put_strikes_with_oi}")
    
    # Summary
    print(f"\n📈 SUMMARY for '{symbol}':")
    print(f"Total contracts: {call_contracts + put_contracts}")
    print(f"Total OI: {call_total_oi + put_total_oi}")
    print(f"Strikes with OI > 0: {call_strikes_with_oi + put_strikes_with_oi}")
    
    # Show sample of actual data structure
    if call_map or put_map:
        print(f"\n🔍 SAMPLE DATA STRUCTURE:")
        sample_key = next(iter(call_map)) if call_map else next(iter(put_map))
        sample_strikes = call_map.get(sample_key, put_map.get(sample_key, {}))
        if sample_strikes:
            sample_strike = next(iter(sample_strikes))
            sample_contract = sample_strikes[sample_strike][0]
            print("First contract keys:", list(sample_contract.keys()))
            print("First contract values:", {k: v for k, v in sample_contract.items() if k != 'symbol'})

def main():
    """Main function"""
    if len(sys.argv) > 1:
        expiry_date = sys.argv[1]
    else:
        # Default to today
        expiry_date = datetime.now().strftime("%Y-%m-%d")
    
    print(f"🎯 SPX CHAIN INSPECTOR")
    print(f"📅 Date: {expiry_date}")
    print(f"{'='*60}")
    
    get_spx_chain_info(expiry_date)

if __name__ == "__main__":
    main()