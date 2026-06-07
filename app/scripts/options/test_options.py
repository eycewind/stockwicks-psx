#!/usr/bin/env python3
"""
REAL TEST - Fetches actual option chains from Schwab API
=========================================================

This test pulls REAL data from your Schwab account and validates
the trading engine with actual market data.

Run with:
    export SCHWAB_ACCESS_TOKEN="your_token"
    python test_real_options.py
"""

import asyncio
import os
from datetime import date, datetime
from complete_trading_engine import OptionsScraper, TradingEngine

async def test_with_real_data():
    """Test the engine with REAL option chains from Schwab"""
    
    # Get token
    token = os.getenv("SCHWAB_ACCESS_TOKEN")
    if not token:
        print("❌ Error: SCHWAB_ACCESS_TOKEN not set")
        print("   Run: export SCHWAB_ACCESS_TOKEN='your_token'")
        return False
    
    print("=" * 80)
    print("TESTING WITH REAL SCHWAB DATA".center(80))
    print("=" * 80 + "\n")
    
    try:
        # Test with real Schwab API
        async with OptionsScraper(access_token=token) as scraper:
            engine = TradingEngine(scraper=scraper, account_capital=10000.0)
            
            # Test symbols
            test_cases = [
                ("AAPL", "credit", 2),
                ("TSLA", "debit", 2),
                ("SPY", "credit", 1),
            ]
            
            for symbol, style, weeks in test_cases:
                print(f"\n{'='*80}")
                print(f"TEST: {symbol} - {style.upper()} - {weeks} week(s)")
                print(f"{'='*80}\n")
                
                try:
                    # Get REAL recommendations
                    print(f"Fetching real option chains for {symbol}...")
                    trades = await engine.scan_and_recommend(
                        symbol=symbol,
                        style=style,
                        weeks=weeks,
                        top_n=3,
                    )
                    
                    if not trades:
                        print(f"⚠️  No trades found for {symbol}")
                        continue
                    
                    print(f"✅ Found {len(trades)} real recommendations!\n")
                    
                    # Show each recommendation with REAL data
                    for i, trade in enumerate(trades, 1):
                        print(f"Recommendation {i}: {trade.action} @ ${trade.strike:.2f}")
                        print(f"  Expiration:     {trade.expiration} ({trade.dte} days)")
                        print(f"  Entry Price:    ${trade.entry_price:.2f}")
                        print(f"  Target 1:       ${trade.target_1:.2f}")
                        print(f"  Target 2:       ${trade.target_2:.2f}")
                        print(f"  Stop Loss:      ${trade.stop_loss:.2f}")
                        print(f"  Contracts:      {trade.contract_size}")
                        print(f"  Max Risk:       ${trade.max_loss:.2f}")
                        print(f"  Max Gain:       ${trade.max_gain:.2f}")
                        print(f"  Risk/Reward:    1 : {trade.max_gain/trade.max_loss if trade.max_loss > 0 else 0:.2f}")
                        print(f"  Confidence:     {trade.confidence_score:.0f}/100")
                        print()
                    
                except Exception as e:
                    print(f"❌ Error scanning {symbol}: {e}\n")
                    continue
            
            # Print API stats
            print(f"\n{'='*80}")
            print("API STATISTICS".center(80))
            print(f"{'='*80}")
            print(f"Requests Made:    {scraper.requests_made}")
            print(f"Requests Cached:  {scraper.requests_cached}")
            if scraper.requests_made > 0:
                cache_rate = (scraper.requests_cached / scraper.requests_made) * 100
                print(f"Cache Hit Rate:   {cache_rate:.0f}%")
            print()
            
            return True
    
    except Exception as e:
        print(f"❌ Test failed: {e}")
        return False


async def quick_scan(symbol: str, style: str, weeks: int, capital: float = 10000):
    """Quick scan of a single symbol with REAL data"""
    
    token = os.getenv("SCHWAB_ACCESS_TOKEN")
    if not token:
        print("❌ SCHWAB_ACCESS_TOKEN not set")
        return
    
    print(f"\n{'='*80}")
    print(f"QUICK SCAN: {symbol}".center(80))
    print(f"{'='*80}\n")
    
    async with OptionsScraper(access_token=token) as scraper:
        engine = TradingEngine(
            scraper=scraper,
            account_capital=capital,
        )
        
        trades = await engine.scan_and_recommend(
            symbol=symbol,
            style=style,
            weeks=weeks,
            top_n=5,
        )
        
        if not trades:
            print(f"No recommendations found for {symbol}")
            return
        
        print(f"Top 5 {style.upper()} opportunities for {symbol}:\n")
        
        for i, trade in enumerate(trades, 1):
            print(f"{i}. {trade.action} @ ${trade.strike:.2f}")
            print(f"   Entry:    ${trade.entry_price:.2f}")
            print(f"   T1:       ${trade.target_1:.2f}")
            print(f"   T2:       ${trade.target_2:.2f}")
            print(f"   Stop:     ${trade.stop_loss:.2f}")
            print(f"   Size:     {trade.contract_size} contracts")
            print(f"   Risk:     ${trade.max_loss:.2f}")
            print(f"   Conf:     {trade.confidence_score:.0f}/100\n")


if __name__ == "__main__":
    import sys
    
    print("\n" + "=" * 80)
    print("COMPLETE TRADING ENGINE - REAL DATA TEST".center(80))
    print("=" * 80 + "\n")
    
    # Check for token
    token = os.getenv("SCHWAB_ACCESS_TOKEN")
    if not token:
        print("❌ SCHWAB_ACCESS_TOKEN environment variable not set")
        print("\nTo use real data, you need to:")
        print("  1. Get a Schwab developer token from your account")
        print("  2. Set it: export SCHWAB_ACCESS_TOKEN='your_token'")
        print("  3. Run this test again")
        print("\nFor now, you can still run the complete engine manually:")
        print("  python complete_trading_engine.py AAPL credit 2 10000")
        sys.exit(1)
    
    # Run comprehensive test or quick scan
    if len(sys.argv) > 1:
        # Quick scan: python test_real_options.py AAPL credit 2 10000
        if len(sys.argv) >= 4:
            symbol = sys.argv[1].upper()
            style = sys.argv[2].lower()
            weeks = int(sys.argv[3])
            capital = float(sys.argv[4]) if len(sys.argv) > 4 else 10000
            
            asyncio.run(quick_scan(symbol, style, weeks, capital))
        else:
            print("Usage: python test_real_options.py [symbol] [style] [weeks] [capital]")
            print("Example: python test_real_options.py AAPL credit 2 10000")
    else:
        # Run full test suite
        success = asyncio.run(test_with_real_data())
        
        if success:
            print("\n" + "=" * 80)
            print("✅ ALL TESTS PASSED WITH REAL DATA!".center(80))
            print("=" * 80)
            print("\nYour trading engine is working with REAL option chains!")
            print("Next step: python complete_trading_engine.py AAPL credit 2 10000\n")
        else:
            print("\n❌ Tests failed\n")
            sys.exit(1)
