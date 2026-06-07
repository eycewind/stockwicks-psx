#!/usr/bin/env python3
"""
CLI interface for testing the Smart Option Picker
"""

import sys
import os
import json
import logging
from datetime import datetime

# ---------------------------------------------------------
# Add project root (/var/www/stockwicks) to sys.path
# so "import app.XXX" works when running this file directly
# ---------------------------------------------------------
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app.utils.options.smart_option_picker import SmartOptionPicker, get_recommended_trade

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

def display_trade(trade: dict):
    """Pretty print trade recommendation"""
    print("\n" + "="*70)
    
    if trade.get('recommendation') == 'NO_TRADE':
        print(f"❌ NO TRADE for {trade['symbol']}")
        if trade.get('expirations_checked'):
            print(f"   Available Expiries: {', '.join(trade['expirations_checked'][:5])}")
        print(f"   Reason: {trade['reason']}")
        return
    
    print(f"✅ TRADE SIGNAL: {trade['type']}")
    print(f"   Symbol: {trade['symbol']}")
    print(f"   Underlying: ${trade.get('underlying_price', 'N/A'):.2f}")
    print(f"   Expiry: {trade.get('expiry', 'N/A')}")
    print(f"   DTE: {trade.get('days_to_expiry', 'N/A')} days")
    print(f"   Bias: {trade.get('bias', 'N/A')}")
    print(f"   IV Rank: {trade.get('iv_rank', 0):.1%}")
    
    if trade.get('pop'):
        print(f"   Probability of Profit: {trade['pop']:.1%}")
    
    if trade['type'] in ['BULL_PUT_SPREAD', 'BEAR_CALL_SPREAD', 'IRON_CONDOR']:
        print(f"   Credit: ${trade.get('credit', 0):.2f}")
        print(f"   Max Loss: ${trade.get('max_loss', 0):.2f}")
        if trade.get('width'):
            print(f"   Spread Width: {trade['width']} points")
        
        if trade['type'] == 'BULL_PUT_SPREAD':
            print(f"   Sell Put @ ${trade['short_strike']:.2f}")
            print(f"   Buy Put @ ${trade['long_strike']:.2f}")
        elif trade['type'] == 'BEAR_CALL_SPREAD':
            print(f"   Sell Call @ ${trade['short_strike']:.2f}")
            print(f"   Buy Call @ ${trade['long_strike']:.2f}")
        elif trade['type'] == 'IRON_CONDOR':
            print(f"   Put Spread: ${trade['put_short']:.2f} / ${trade['put_long']:.2f}")
            print(f"   Call Spread: ${trade['call_short']:.2f} / ${trade['call_long']:.2f}")
    
    elif trade['type'] in ['DEBIT_CALL_SPREAD', 'DEBIT_PUT_SPREAD']:
        print(f"   Debit: ${trade.get('debit', 0):.2f}")
        print(f"   Max Profit: ${trade.get('max_profit', 0):.2f}")
        
        if trade['type'] == 'DEBIT_CALL_SPREAD':
            print(f"   Buy Call @ ${trade['long_strike']:.2f}")
            print(f"   Sell Call @ ${trade['short_strike']:.2f}")
        elif trade['type'] == 'DEBIT_PUT_SPREAD':
            print(f"   Buy Put @ ${trade['long_strike']:.2f}")
            print(f"   Sell Put @ ${trade['short_strike']:.2f}")
    
    if trade.get('roi'):
        print(f"   Potential ROI: {trade['roi']:.1%}")
    
    print(f"   Generated: {trade.get('timestamp', 'N/A')}")
    print("="*70)

def analyze_single_symbol(symbol: str, expiry: str = None, min_dte: int = 7, max_dte: int = 45):
    """Analyze a single symbol with optional expiry"""
    print(f"\n🔍 Analyzing {symbol}...")
    if expiry:
        print(f"Target Expiry: {expiry}")
    else:
        print(f"DTE Range: {min_dte}-{max_dte} days")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    try:
        picker = SmartOptionPicker()
        
        # Show available expirations first
        print(f"\n📅 Available expirations:")
        expirations = picker.get_available_expirations(symbol, min_dte, max_dte)
        if expirations:
            for i, exp in enumerate(expirations[:10], 1):
                exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
                dte = (exp_date - date.today()).days
                print(f"   {i:2d}. {exp} ({dte} DTE)")
            if len(expirations) > 10:
                print(f"   ... and {len(expirations) - 10} more")
        else:
            print("   No expirations found in range")
        
        # Get trade recommendation
        trade = get_recommended_trade(symbol, expiry, (min_dte, max_dte))
        display_trade(trade)
        
        # Save to JSON file
        if trade.get('recommendation') != 'NO_TRADE':
            filename = f"trade_{symbol}_{expiry if expiry else 'auto'}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(filename, 'w') as f:
                json.dump(trade, f, indent=2)
            print(f"\n📁 Saved trade details to {filename}")
            
    except Exception as e:
        print(f"❌ Error analyzing {symbol}: {e}")
        logging.exception(f"Failed to analyze {symbol}")

def analyze_with_date_range():
    """Interactive analysis with date range selection"""
    print("\n📊 Interactive Option Picker")
    print("="*40)
    
    symbol = input("Enter symbol (e.g., SPY): ").strip().upper()
    if not symbol:
        symbol = "SPY"
    
    print(f"\nSymbol: {symbol}")
    print("\nSelect expiry option:")
    print("1. Auto-select (7-45 DTE)")
    print("2. Specify exact expiry (YYYY-MM-DD)")
    print("3. Specify DTE range")
    
    choice = input("\nYour choice (1-3): ").strip()
    
    picker = SmartOptionPicker()
    trade = None
    
    if choice == "1":
        # Auto-select
        trade = get_recommended_trade(symbol)
        
    elif choice == "2":
        # Specific expiry
        expiry = input("Enter expiry (YYYY-MM-DD): ").strip()
        trade = get_recommended_trade(symbol, expiry)
        
    elif choice == "3":
        # DTE range
        min_dte = input("Min DTE (default 7): ").strip()
        max_dte = input("Max DTE (default 45): ").strip()
        
        min_dte = int(min_dte) if min_dte else 7
        max_dte = int(max_dte) if max_dte else 45
        
        trade = get_recommended_trade(symbol, None, (min_dte, max_dte))
    
    if trade:
        display_trade(trade)
    else:
        print(f"\n❌ No trade found for {symbol}")

def show_help():
    """Display help message"""
    print("""
🐍 Smart Option Picker CLI (with Date Control)

Usage:
  python smart_picker_cli.py [command] [options]

Commands:
  single <symbol> [expiry]     - Analyze with optional expiry
  interactive                  - Interactive mode with expiry selection
  list <symbol>                - List available expirations
  multi [min_dte] [max_dte]    - Analyze multiple symbols
  test                         - Quick test with SPY
  help                         - Show this help

Examples:
  python smart_picker_cli.py single SPY
  python smart_picker_cli.py single AAPL 2025-12-12
  python smart_picker_cli.py interactive
  python smart_picker_cli.py list SPY
  python smart_picker_cli.py multi 14 21
""")

def main():
    """Main CLI entry point"""
    if len(sys.argv) < 2:
        show_help()
        return
    
    command = sys.argv[1].lower()
    
    if command == 'single':
        if len(sys.argv) >= 3:
            symbol = sys.argv[2].upper()
            expiry = sys.argv[3] if len(sys.argv) >= 4 else None
            analyze_single_symbol(symbol, expiry)
        else:
            print("❌ Missing symbol. Usage: single <symbol> [expiry]")
            
    elif command == 'interactive':
        analyze_with_date_range()
        
    elif command == 'list' and len(sys.argv) >= 3:
        symbol = sys.argv[2].upper()
        picker = SmartOptionPicker()
        expirations = picker.get_available_expirations(symbol, 0, 365)
        
        print(f"\n📅 Available expirations for {symbol}:")
        if expirations:
            today = date.today()
            for exp in expirations:
                exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
                dte = (exp_date - today).days
                print(f"   • {exp} ({dte} DTE)")
        else:
            print("   No expirations found")
            
    elif command == 'multi':
        min_dte = int(sys.argv[2]) if len(sys.argv) >= 3 else 7
        max_dte = int(sys.argv[3]) if len(sys.argv) >= 4 else 45
        
        symbols = ['SPY', 'QQQ', 'IWM', 'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'TSLA']
        print(f"\n📊 Analyzing {len(symbols)} symbols (DTE: {min_dte}-{max_dte})")
        print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        results = []
        for symbol in symbols:
            try:
                trade = get_recommended_trade(symbol, None, (min_dte, max_dte))
                if trade and trade.get('recommendation') != 'NO_TRADE':
                    results.append(trade)
                    print(f"   ✅ {symbol}: {trade['type']} (PoP: {trade.get('pop', 0):.1%})")
                else:
                    print(f"   ❌ {symbol}: No trade")
            except Exception as e:
                print(f"   💥 {symbol}: Error - {str(e)[:30]}...")
        
        # Save results
        if results:
            filename = f"trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(filename, 'w') as f:
                json.dump(results, f, indent=2)
            print(f"\n📁 Saved {len(results)} trades to {filename}")
        
    elif command == 'test':
        analyze_single_symbol('SPY')
        
    elif command == 'help':
        show_help()
        
    else:
        print(f"❌ Unknown command: {command}")
        show_help()

if __name__ == "__main__":
    main()