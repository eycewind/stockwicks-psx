#!/usr/bin/env python3
"""
COMPLETE OPTIONS TRADING ENGINE - ALL-IN-ONE
==============================================

One complete script that:
1. Scans 1-4 weeks of puts/calls
2. Analyzes and decides what to buy/sell
3. Calculates contract size
4. Calculates allocation
5. Calculates profit targets
6. Calculates stop loss

Usage:
    python complete_trading_engine.py AAPL credit 2 10000
    python complete_trading_engine.py TSLA debit 3 5000
    python complete_trading_engine.py SPY credit 4 25000
"""

import asyncio
import os
import json
import logging
from datetime import datetime, date, timedelta
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any
import pytz

import aiohttp
import pandas as pd
import numpy as np
from cachetools import TTLCache

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ============================================================================
# DATA MODELS
# ============================================================================

@dataclass
class Trade:
    """Complete trade recommendation"""
    symbol: str
    action: str  # "BUY CALL", "SELL PUT", etc
    strike: float
    expiration: date
    dte: int
    entry_price: float
    target_1: float
    target_2: float
    stop_loss: float
    contract_size: int
    max_loss: float
    max_gain: float
    confidence_score: float
    capital_required: float


# ============================================================================
# OPTIONS SCRAPER - Complete implementation
# ============================================================================

class OptionsScraper:
    """Fetch options data from Schwab API"""
    
    BASE_URL = "https://api.schwabapi.com/marketdata/v1"
    
    def __init__(self, access_token: str):
        self.access_token = access_token
        self.session = None
        self.cache = TTLCache(maxsize=100, ttl=60)
        self.requests_made = 0
        self.requests_cached = 0
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
    
    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }
    
    async def get_expirations(self, symbol: str) -> List[date]:
        """Get available expiration dates"""
        logger.info(f"Fetching expirations for {symbol}...")
        
        url = f"{self.BASE_URL}/expirationchain?symbol={symbol.upper()}"
        
        try:
            async with self.session.get(url, headers=self._get_headers(), timeout=aiohttp.ClientTimeout(total=10)) as resp:
                self.requests_made += 1
                if resp.status != 200:
                    logger.error(f"Failed to get expirations: {resp.status}")
                    return []
                
                data = await resp.json()
                expirations = []
                
                for exp in data.get("expirationList", []):
                    try:
                        exp_date = datetime.strptime(exp["expirationDate"], "%Y-%m-%d").date()
                        if exp_date >= date.today():
                            expirations.append(exp_date)
                    except:
                        continue
                
                return sorted(expirations)
        except Exception as e:
            logger.error(f"Error fetching expirations: {e}")
            return []
    
    async def get_chain(self, symbol: str, expiration: date) -> Tuple[Dict, float]:
        """Get option chain for specific expiration
        
        Returns: (options_dict, underlying_price)
        """
        
        # Check cache
        cache_key = f"{symbol}:{expiration}"
        if cache_key in self.cache:
            self.requests_cached += 1
            return self.cache[cache_key]
        
        logger.info(f"Fetching chain for {symbol} {expiration}...")
        
        url = (
            f"{self.BASE_URL}/chains"
            f"?symbol={symbol.upper()}"
            f"&fromDate={expiration.isoformat()}"
            f"&toDate={expiration.isoformat()}"
            f"&includeUnderlyingQuote=true"
            f"&strategy=SINGLE"
            f"&range=ALL"
        )
        
        try:
            async with self.session.get(url, headers=self._get_headers(), timeout=aiohttp.ClientTimeout(total=15)) as resp:
                self.requests_made += 1
                if resp.status != 200:
                    logger.error(f"Failed to get chain: {resp.status}")
                    return {}, 0.0
                
                data = await resp.json()
                
                # Get underlying price
                underlying_price = data.get("underlying", {}).get("last")
                if not underlying_price:
                    quote = data.get("underlyingQuote", {})
                    underlying_price = quote.get("lastPrice") or 0
                
                # Parse options
                options = {}
                
                # Calls
                for _, strikes in data.get("callExpDateMap", {}).items():
                    for _, contracts in strikes.items():
                        for c in contracts:
                            try:
                                strike = float(c["strikePrice"])
                                if strike not in options:
                                    options[strike] = {"calls": {}, "puts": {}}
                                
                                options[strike]["calls"] = {
                                    "bid": float(c.get("bid", 0)),
                                    "ask": float(c.get("ask", 0)),
                                    "delta": float(c.get("delta", 0)),
                                    "gamma": float(c.get("gamma", 0)),
                                    "theta": float(c.get("theta", 0)),
                                    "vega": float(c.get("vega", 0)),
                                    "iv": float(c.get("volatility", 0)),
                                    "volume": int(c.get("totalVolume", 0)),
                                    "oi": int(c.get("openInterest", 0)),
                                }
                            except:
                                continue
                
                # Puts
                for _, strikes in data.get("putExpDateMap", {}).items():
                    for _, contracts in strikes.items():
                        for p in contracts:
                            try:
                                strike = float(p["strikePrice"])
                                if strike not in options:
                                    options[strike] = {"calls": {}, "puts": {}}
                                
                                options[strike]["puts"] = {
                                    "bid": float(p.get("bid", 0)),
                                    "ask": float(p.get("ask", 0)),
                                    "delta": float(p.get("delta", 0)),
                                    "gamma": float(p.get("gamma", 0)),
                                    "theta": float(p.get("theta", 0)),
                                    "vega": float(p.get("vega", 0)),
                                    "iv": float(p.get("volatility", 0)),
                                    "volume": int(p.get("totalVolume", 0)),
                                    "oi": int(p.get("openInterest", 0)),
                                }
                            except:
                                continue
                
                result = (options, float(underlying_price) if underlying_price else 0.0)
                self.cache[cache_key] = result
                return result
        
        except Exception as e:
            logger.error(f"Error fetching chain: {e}")
            return {}, 0.0


# ============================================================================
# TRADING DECISION ENGINE
# ============================================================================

class TradingEngine:
    """Main trading decision engine"""
    
    def __init__(self, scraper: OptionsScraper, account_capital: float):
        self.scraper = scraper
        self.account_capital = account_capital
        self.risk_per_trade = 0.02  # 2% max risk
        self.max_allocation_pct = 0.10  # 10% max allocation
    
    async def scan_and_recommend(
        self,
        symbol: str,
        style: str,  # "credit" or "debit"
        weeks: int = 2,
        top_n: int = 5,
    ) -> List[Trade]:
        """Scan and return top N trade recommendations"""
        
        symbol = symbol.upper()
        style = style.lower()
        
        if style not in ("credit", "debit"):
            raise ValueError("Style must be 'credit' or 'debit'")
        
        # Get expirations
        all_expirations = await self.scraper.get_expirations(symbol)
        if not all_expirations:
            logger.error(f"No expirations found for {symbol}")
            return []
        
        # Filter by weeks
        today = date.today()
        max_dte = weeks * 7
        expirations = [
            e for e in all_expirations
            if 0 < (e - today).days <= max_dte
        ]
        
        if not expirations:
            logger.error(f"No expirations within {weeks} weeks")
            return []
        
        logger.info(f"Scanning {len(expirations)} expirations for {symbol}...")
        
        # Fetch all chains
        trades: List[Trade] = []
        
        for expiration in expirations:
            options, underlying_price = await self.scraper.get_chain(symbol, expiration)
            
            if not options or underlying_price == 0:
                continue
            
            dte = (expiration - today).days
            
            # Generate trades for each strike
            for strike, option_data in sorted(options.items()):
                
                # CREDIT STRATEGY - Sell puts or calls
                if style == "credit":
                    # Sell puts (bullish - collect credit if stock stays above strike)
                    if option_data["puts"] and option_data["puts"]["bid"] > 0.05:
                        trade = self._build_trade(
                            symbol=symbol,
                            action="SELL PUT",
                            strike=strike,
                            expiration=expiration,
                            dte=dte,
                            underlying_price=underlying_price,
                            entry_price=option_data["puts"]["bid"],
                            contract_data=option_data["puts"],
                            style="credit",
                        )
                        if trade:
                            trades.append(trade)
                    
                    # Sell calls (bearish - collect credit if stock stays below strike)
                    if option_data["calls"] and option_data["calls"]["bid"] > 0.05:
                        trade = self._build_trade(
                            symbol=symbol,
                            action="SELL CALL",
                            strike=strike,
                            expiration=expiration,
                            dte=dte,
                            underlying_price=underlying_price,
                            entry_price=option_data["calls"]["bid"],
                            contract_data=option_data["calls"],
                            style="credit",
                        )
                        if trade:
                            trades.append(trade)
                
                # DEBIT STRATEGY - Buy calls or puts
                else:  # debit
                    # Buy calls (bullish - profit if stock goes up)
                    if option_data["calls"] and option_data["calls"]["ask"] > 0.05:
                        trade = self._build_trade(
                            symbol=symbol,
                            action="BUY CALL",
                            strike=strike,
                            expiration=expiration,
                            dte=dte,
                            underlying_price=underlying_price,
                            entry_price=option_data["calls"]["ask"],
                            contract_data=option_data["calls"],
                            style="debit",
                        )
                        if trade:
                            trades.append(trade)
                    
                    # Buy puts (bearish - profit if stock goes down)
                    if option_data["puts"] and option_data["puts"]["ask"] > 0.05:
                        trade = self._build_trade(
                            symbol=symbol,
                            action="BUY PUT",
                            strike=strike,
                            expiration=expiration,
                            dte=dte,
                            underlying_price=underlying_price,
                            entry_price=option_data["puts"]["ask"],
                            contract_data=option_data["puts"],
                            style="debit",
                        )
                        if trade:
                            trades.append(trade)
        
        # Sort by confidence
        trades.sort(key=lambda t: t.confidence_score, reverse=True)
        
        logger.info(f"Found {len(trades)} potential trades, returning top {top_n}")
        
        return trades[:top_n]
    
    def _build_trade(
        self,
        symbol: str,
        action: str,
        strike: float,
        expiration: date,
        dte: int,
        underlying_price: float,
        entry_price: float,
        contract_data: Dict,
        style: str,
    ) -> Optional[Trade]:
        """Build a single trade recommendation"""
        
        # Skip low liquidity
        if contract_data["volume"] < 20 and contract_data["oi"] < 100:
            return None
        
        # Calculate targets and stop loss
        if style == "credit":
            # Selling: want price to go DOWN
            # Target 1: 50% of credit
            # Target 2: 75% of credit
            # Stop: 200% of credit
            target_1 = max(0.01, entry_price * 0.50)
            target_2 = max(0.01, entry_price * 0.25)
            stop_loss = entry_price * 2.0
            max_gain = entry_price * 100
            max_loss = (stop_loss - entry_price) * 100
        else:  # debit
            # Buying: want price to go UP
            # Target 1: +25%
            # Target 2: +50%
            # Stop: -33%
            target_1 = entry_price * 1.25
            target_2 = entry_price * 1.50
            stop_loss = entry_price * 0.67
            max_gain = (target_2 - entry_price) * 100
            max_loss = (entry_price - stop_loss) * 100
        
        # Calculate contract size
        max_loss_per_contract = max_loss
        risk_amount = self.account_capital * self.risk_per_trade
        max_contracts = int(risk_amount / max_loss_per_contract) if max_loss_per_contract > 0 else 1
        contract_size = max(1, min(max_contracts, 10))
        
        # Check allocation
        capital_required = entry_price * 100 * contract_size
        max_allocation = self.account_capital * self.max_allocation_pct
        
        if capital_required > max_allocation:
            contract_size = max(1, int(max_allocation / (entry_price * 100)))
            capital_required = entry_price * 100 * contract_size
        
        # Calculate confidence score
        confidence_score = self._calculate_confidence(
            contract_data=contract_data,
            strike=strike,
            underlying_price=underlying_price,
            style=style,
        )
        
        return Trade(
            symbol=symbol,
            action=action,
            strike=strike,
            expiration=expiration,
            dte=dte,
            entry_price=entry_price,
            target_1=target_1,
            target_2=target_2,
            stop_loss=stop_loss,
            contract_size=contract_size,
            max_loss=max_loss,
            max_gain=max_gain,
            confidence_score=confidence_score,
            capital_required=capital_required,
        )
    
    def _calculate_confidence(
        self,
        contract_data: Dict,
        strike: float,
        underlying_price: float,
        style: str,
    ) -> float:
        """Calculate confidence score (0-100)"""
        
        score = 50.0
        
        # Volume
        if contract_data["volume"] > 500:
            score += 20
        elif contract_data["volume"] > 200:
            score += 10
        
        # Open interest
        if contract_data["oi"] > 2000:
            score += 20
        elif contract_data["oi"] > 1000:
            score += 10
        
        # Delta (measure of certainty)
        delta_abs = abs(contract_data["delta"])
        if 0.3 < delta_abs < 0.7:
            score += 15
        
        # IV (high IV is good for sellers, low for buyers)
        iv = contract_data["iv"]
        if style == "credit" and iv > 50:
            score += 10
        elif style == "debit" and iv < 35:
            score += 10
        
        # Moneyness
        moneyness = strike / underlying_price
        if 0.95 < moneyness < 1.05:  # Near ATM is best
            score += 10
        
        return min(100.0, max(0.0, score))


# ============================================================================
# MAIN PROGRAM
# ============================================================================

async def main():
    """Main entry point"""
    import sys
    
    print("\n" + "="*80)
    print("COMPLETE OPTIONS TRADING ENGINE".center(80))
    print("="*80 + "\n")
    
    # Get parameters
    if len(sys.argv) < 4:
        print("Usage: python complete_trading_engine.py <symbol> <style> <weeks> [capital]")
        print("Example: python complete_trading_engine.py AAPL credit 2 10000")
        print("Example: python complete_trading_engine.py TSLA debit 3 5000")
        print("\nParameters:")
        print("  symbol: Stock symbol (AAPL, TSLA, SPY, etc)")
        print("  style:  'credit' (sell premium) or 'debit' (buy premium)")
        print("  weeks:  1-4 (how many weeks to scan)")
        print("  capital: Account capital (default 10000)")
        return
    
    symbol = sys.argv[1]
    style = sys.argv[2]
    weeks = int(sys.argv[3])
    capital = float(sys.argv[4]) if len(sys.argv) > 4 else 10000.0
    
    # Get token
    token = os.getenv("SCHWAB_ACCESS_TOKEN")
    if not token:
        print("❌ Set SCHWAB_ACCESS_TOKEN environment variable")
        print("   export SCHWAB_ACCESS_TOKEN='your_token'")
        return
    
    # Run engine
    async with OptionsScraper(access_token=token) as scraper:
        engine = TradingEngine(scraper=scraper, account_capital=capital)
        
        try:
            print(f"📊 SCANNING {symbol}")
            print(f"   Style:      {style.upper()} ({'SELL' if style=='credit' else 'BUY'} premium)")
            print(f"   Weeks:      {weeks}")
            print(f"   Capital:    ${capital:,.2f}")
            print(f"   Max Risk:   ${capital * 0.02:,.2f} (2% per trade)")
            print(f"   Max Alloc:  ${capital * 0.10:,.2f} (10% per trade)\n")
            
            trades = await engine.scan_and_recommend(
                symbol=symbol,
                style=style,
                weeks=weeks,
                top_n=5,
            )
            
            if not trades:
                print("❌ No trades found")
                return
            
            print(f"✅ Found {len(trades)} recommendations\n")
            
            # Display results
            for i, trade in enumerate(trades, 1):
                print(f"\n{'='*80}")
                print(f"RECOMMENDATION {i}: {trade.action} @ ${trade.strike:.2f}")
                print(f"{'='*80}")
                print(f"  Symbol:           {trade.symbol}")
                print(f"  Expiration:       {trade.expiration} ({trade.dte} days)")
                print(f"\n  ENTRY & TARGETS:")
                print(f"    Entry Price:    ${trade.entry_price:.2f}")
                print(f"    Target 1:       ${trade.target_1:.2f} ({((trade.target_1/trade.entry_price)-1)*100:+.1f}%)")
                print(f"    Target 2:       ${trade.target_2:.2f} ({((trade.target_2/trade.entry_price)-1)*100:+.1f}%)")
                print(f"    Stop Loss:      ${trade.stop_loss:.2f}")
                print(f"\n  SIZING & RISK:")
                print(f"    Contracts:      {trade.contract_size}")
                print(f"    Max Risk:       ${trade.max_loss:.2f}")
                print(f"    Max Gain:       ${trade.max_gain:.2f}")
                print(f"    Risk/Reward:    1 : {trade.max_gain/trade.max_loss if trade.max_loss > 0 else 0:.2f}")
                print(f"    Capital Used:   ${trade.capital_required:,.2f}")
                print(f"\n  QUALITY:")
                print(f"    Confidence:     {trade.confidence_score:.0f}/100 {'⭐⭐⭐' if trade.confidence_score >= 75 else '⭐⭐' if trade.confidence_score >= 60 else '⭐'}")
            
            # Summary
            print(f"\n{'='*80}")
            print("SUMMARY".center(80))
            print(f"{'='*80}")
            total_capital = sum(t.capital_required for t in trades)
            total_max_loss = sum(t.max_loss for t in trades)
            print(f"Total Top 5 Recommendations")
            print(f"  Capital Required: ${total_capital:,.2f}")
            print(f"  Total Max Loss:   ${total_max_loss:,.2f}")
            print(f"  % of Account:     {(total_capital/capital)*100:.1f}%")
            print(f"\nAPI Stats:")
            print(f"  Requests Made:    {scraper.requests_made}")
            print(f"  Requests Cached:  {scraper.requests_cached}")
            if scraper.requests_made > 0:
                cache_rate = (scraper.requests_cached / scraper.requests_made) * 100
                print(f"  Cache Hit Rate:   {cache_rate:.0f}%")
            print(f"{'='*80}\n")
        
        except Exception as e:
            logger.error(f"Error: {e}")
            print(f"❌ Error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
