# app/scripts/test_smart_picker.py

import sys
import os
import logging

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from app.utils.options.smart_option_picker import get_recommended_trade

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def test_picker():
    """Test the smart option picker with popular symbols"""
    symbols = ['SPY', 'QQQ', 'AAPL', 'MSFT', 'TSLA', 'NVDA']
    
    for symbol in symbols:
        print(f"\n{'='*50}")
        print(f"Analyzing {symbol}...")
        print(f"{'='*50}")
        
        trade = get_recommended_trade(symbol)
        
        if trade.get('recommendation') == 'NO_TRADE':
            print(f"No trade recommended for {symbol}")
            print(f"Reason: {trade['reason']}")
        else:
            print(f"RECOMMENDED TRADE: {trade['type']}")
            print(f"Symbol: {trade['symbol']}")
            print(f"Underlying Price: ${trade['underlying_price']:.2f}")
            print(f"Expiry: {trade['expiry']}")
            print(f"Probability of Profit: {trade.get('pop', 'N/A')}")
            
            if 'credit' in trade:
                print(f"Credit: ${trade['credit']:.2f}")
                print(f"Max Loss: ${trade['max_loss']:.2f}")
                print(f"ROI: {trade['roi']:.1%}")
            elif 'debit' in trade:
                print(f"Debit: ${trade['debit']:.2f}")
                print(f"Max Profit: ${trade['max_profit']:.2f}")
                print(f"ROI: {trade['roi']:.1%}")
            
            print(f"Market Bias: {trade['bias']}")
            print(f"IV Rank: {trade['iv_rank']:.1%}")

if __name__ == "__main__":
    test_picker()