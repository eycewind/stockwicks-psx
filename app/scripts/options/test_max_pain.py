#!/usr/bin/env python3
"""
CLI Test Script for 0DTE MM Pin Prediction with Date Support
"""

import sys
import os
import argparse
from datetime import datetime

# Add the parent directory to Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    from scripts.options.mm_max_pain_calculator import ZeroDTEMaxPainCalculator, calculate_mm_max_pain
except ImportError:
    from mm_max_pain_calculator import ZeroDTEMaxPainCalculator, calculate_mm_max_pain

def test_mm_pin_prediction(symbol: str, date_input: str = None):
    """Test MM pin prediction for a single symbol with date selection"""
    print(f"\n🎯 Testing MM Pin Prediction for {symbol}")
    print("=" * 60)
    
    try:
        calculator = ZeroDTEMaxPainCalculator()
        result = calculator.calculate_mm_pin_prediction(symbol, date_input)
        
        if "error" in result:
            print(f"❌ Error: {result['error']}")
            return False
        
        # Display results
        print(f"📈 Symbol: {result['symbol']}")
        print(f"📅 Expiry: {result.get('expiry', 'N/A')}")
        print(f"💰 Current Price: ${result.get('current_price', 'N/A'):.2f}")
        print(f"🎯 MM Pin Prediction: ${result['mm_pin_prediction']:.2f}")
        print(f"⏰ Market Hours: {result.get('market_hours', 'N/A')}")
        print(f"📊 Total Options: {result.get('total_options', 'N/A')}")
        
        # Analysis details
        analysis = result.get('analysis', {})
        if analysis:
            print(f"\n📊 PIN ANALYSIS:")
            print(f"   OI Concentration: {analysis.get('oi_concentration_ratio', 0):.1%}")
            print(f"   Call/Put Ratio: {analysis.get('call_put_ratio_nearby', 0):.2f}")
            print(f"   Distance: {analysis.get('distance_from_current', 0):.2f} points")
            print(f"   Distance %: {analysis.get('distance_percent', 0):.2f}%")
            print(f"   Total OI: {analysis.get('total_oi', 0):,}")
            print(f"   Nearby OI: {analysis.get('nearby_oi', 0):,}")
        
        # Prediction methods
        methods = result.get('prediction_methods', {})
        if methods:
            print(f"\n🔧 PREDICTION METHODS:")
            for method, price in methods.items():
                diff = price - result['current_price']
                print(f"   {method}: ${price:.2f} ({diff:+.2f})")
        
        # Calculate difference and direction
        current_price = result.get('current_price')
        if current_price and current_price != 'N/A':
            pin_price = result['mm_pin_prediction']
            diff = pin_price - current_price
            percent = (diff / current_price) * 100
            
            direction = "📈 BULLISH" if diff > 0 else "📉 BEARISH" if diff < 0 else "➡️ NEUTRAL"
            print(f"\n🎯 DIRECTION: {direction}")
            print(f"   Target: ${pin_price:.2f}")
            print(f"   Difference: {diff:+.2f} points ({percent:+.2f}%)")
            
            if abs(percent) < 0.1:
                print("   💡 Expected: Minimal movement (pinning)")
            elif abs(percent) < 0.5:
                print("   💡 Expected: Small directional move")
            else:
                print("   💡 Expected: Significant directional move")
        
        return True
        
    except Exception as e:
        print(f"❌ Test failed for {symbol}: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_batch_predictions(symbols: list, date_input: str = None):
    """Test MM pin predictions for multiple symbols"""
    print(f"\n🚀 Batch Testing {len(symbols)} Symbols")
    if date_input:
        print(f"📅 Date: {date_input}")
    print("=" * 60)
    
    calculator = ZeroDTEMaxPainCalculator()
    results = {}
    
    for symbol in symbols:
        try:
            results[symbol] = calculator.calculate_mm_pin_prediction(symbol, date_input)
        except Exception as e:
            results[symbol] = {"error": str(e)}
    
    success_count = 0
    predictions = []
    
    for symbol, result in results.items():
        if "error" not in result:
            current = result.get('current_price', 0)
            prediction = result['mm_pin_prediction']
            diff = prediction - current
            percent = (diff / current) * 100 if current > 0 else 0
            
            direction = "↑" if diff > 0 else "↓" if diff < 0 else "→"
            print(f"✅ {symbol}: ${prediction:.2f} (Current: ${current:.2f}) {direction} {diff:+.2f} ({percent:+.2f}%)")
            
            predictions.append({
                'symbol': symbol,
                'prediction': prediction,
                'current': current,
                'direction': direction,
                'diff': diff
            })
            success_count += 1
        else:
            print(f"❌ {symbol}: {result['error']}")
    
    # Summary
    print(f"\n📊 BATCH SUMMARY:")
    print(f"   Successful: {success_count}/{len(symbols)}")
    
    if predictions:
        bullish = sum(1 for p in predictions if p['diff'] > 0)
        bearish = sum(1 for p in predictions if p['diff'] < 0)
        neutral = sum(1 for p in predictions if p['diff'] == 0)
        
        print(f"   Bullish: {bullish}, Bearish: {bearish}, Neutral: {neutral}")
        
        if predictions:
            most_bullish = max(predictions, key=lambda x: x['diff'])
            most_bearish = min(predictions, key=lambda x: x['diff'])
            print(f"   Most Bullish: {most_bullish['symbol']} (+{most_bullish['diff']:.2f})")
            print(f"   Most Bearish: {most_bearish['symbol']} ({most_bearish['diff']:.2f})")
    
    return success_count

def main():
    parser = argparse.ArgumentParser(description='MM Pin Prediction Calculator with Date Support')
    parser.add_argument('--symbol', '-s', type=str, help='Single symbol to test (e.g., SPY)')
    parser.add_argument('--batch', '-b', action='store_true', help='Test batch of major symbols')
    parser.add_argument('--date', '-d', type=str, help='Date for options (today, tomorrow, YYYY-MM-DD)')
    parser.add_argument('--all', '-a', action='store_true', help='Run comprehensive analysis')
    
    args = parser.parse_args()
    
    major_symbols = ['SPY', 'QQQ', 'SPX', 'IWM', 'DIA']
    
    if args.all:
        print("🏃 Running Comprehensive Analysis")
        test_batch_predictions(major_symbols, args.date)
        return
    
    if args.symbol:
        test_mm_pin_prediction(args.symbol, args.date)
    
    elif args.batch:
        test_batch_predictions(major_symbols, args.date)
    
    else:
        # Interactive mode
        symbol = input("Enter symbol (e.g., SPY): ").strip().upper()
        if symbol:
            test_mm_pin_prediction(symbol, args.date)

if __name__ == "__main__":
    main()