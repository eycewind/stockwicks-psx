# test_with_dates.py
#!/usr/bin/env python3

import sys
import os
sys.path.insert(0, os.path.abspath('.'))

from app.utils.options.smart_option_picker import SmartOptionPicker

def test_dates():
    picker = SmartOptionPicker()
    
    # Test SPY
    print("Testing SPY...")
    
    # Get available expirations
    expirations = picker.get_available_expirations('SPY', 0, 60)
    print(f"Available expirations (next 60 days):")
    for exp in expirations[:5]:
        print(f"  - {exp}")
    
    # Test with auto expiry
    print("\n1. Auto expiry (7-45 DTE):")
    trade = picker.pick_best_trade('SPY')
    if trade:
        print(f"   Trade: {trade['type']}")
        print(f"   Expiry: {trade['expiry']}")
    else:
        print("   No trade found")
    
    # Test with specific expiry
    if expirations:
        print(f"\n2. Specific expiry ({expirations[0]}):")
        trade = picker.pick_best_trade('SPY', expirations[0])
        if trade:
            print(f"   Trade: {trade['type']}")
            print(f"   Credit/Debit: ${trade.get('credit', trade.get('debit', 0)):.2f}")
        else:
            print("   No trade found for this expiry")

if __name__ == "__main__":
    test_dates()