#!/usr/bin/env python3
#/var/www/stockwicks/app/scripts/options/algos/guru_pick_4exp.py
"""
guru_pick_4exp.py — Options "Guru" (Schwab) | Scan next N expirations

This is the main algorithm file that integrates with the option_bots_runner system.
It provides both CLI functionality and bot runner integration.
"""

import argparse
import logging
import sys
import json
import asyncio
from datetime import datetime, timedelta, date
from typing import List, Optional, Tuple, Dict, Any, Union

import pandas as pd
import pandas_ta as ta
import requests
import pytz
from dotenv import load_dotenv

# === CONFIG & LOGGING ===================================
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SCHWAB_API_URL = "https://api.schwabapi.com/marketdata/v1"


# === HELPERS ============================================

def get_schwab_headers() -> Dict[str, str]:
    # StockWicks token helper
    try:
        from app.utils.stock.schwab_token import get_valid_access_token
        access_token = get_valid_access_token()
        if not access_token:
            raise ValueError("Could not get Schwab access token.")
        return {"Authorization": f"Bearer {access_token}"}
    except ImportError:
        import os
        access_token = os.getenv("SCHWAB_ACCESS_TOKEN")
        if access_token:
            return {"Authorization": f"Bearer {access_token}"}
        raise ValueError("Could not get Schwab access token.")


def _now_et() -> datetime:
    return datetime.now(pytz.timezone("US/Eastern"))


def get_schwab_price_history(
    symbol: str,
    periodType: str = "day",
    period: int = 1,
    frequencyType: str = "minute",
    frequency: int = 1,
) -> Optional[pd.DataFrame]:
    url = f"{SCHWAB_API_URL}/pricehistory"
    params = {
        "symbol": symbol,
        "periodType": periodType,
        "period": period,
        "frequencyType": frequencyType,
        "frequency": frequency,
    }
    headers = get_schwab_headers()
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        logger.info(f"Historical PriceHistory API [{symbol} - {frequencyType}:{frequency}] status: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        if not data or "candles" not in data:
            return None
        df = pd.DataFrame(data["candles"])
        df["datetime"] = pd.to_datetime(df["datetime"], unit="ms")
        df.set_index("datetime", inplace=True)
        df.columns = [c.lower() for c in df.columns]
        if not all(c in df.columns for c in ["high", "low", "close", "volume"]):
            return None
        return df
    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed for {symbol}: {e}")
        return None


def get_expiration_dates(symbol: str) -> List[date]:
    url = f"{SCHWAB_API_URL}/expirationchain"
    params = {"symbol": symbol}
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params, timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to get expiration chain: status={resp.status_code}")
    data = resp.json()
    expirations = data.get("expirationList", [])
    out: List[date] = []
    for e in expirations:
        try:
            out.append(datetime.strptime(e["expirationDate"], "%Y-%m-%d").date())
        except Exception:
            continue
    today = _now_et().date()
    return sorted({d for d in out if d >= today})


def choose_expirations(
    all_dates: List[date],
    expires: int,
    hold_hours: Optional[int],
    hold_days: Optional[int],
) -> List[date]:
    today = _now_et().date()

    if hold_hours is not None:
        near = [d for d in all_dates if 0 <= (d - today).days <= 14]
        return (near if near else all_dates)[:expires]

    if hold_days is not None:
        min_dte = max(3, hold_days + 2)
        candidates = [d for d in all_dates if (d - today).days >= min_dte]
        preferred = [d for d in candidates if 7 <= (d - today).days <= 35]
        base = preferred if preferred else candidates
        return (base if base else all_dates)[:expires]

    return all_dates[:expires]


def fetch_full_option_chain(symbol: str, expiration_date: date) -> Tuple[pd.DataFrame, Optional[float]]:
    url = f"{SCHWAB_API_URL}/chains"
    params = {
        "symbol": symbol,
        "fromDate": expiration_date.strftime("%Y-%m-%d"),
        "toDate": expiration_date.strftime("%Y-%m-%d"),
        "includeUnderlyingQuote": "true",
        "strategy": "SINGLE",
        "range": "ALL",
    }
    headers = get_schwab_headers()
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code != 200:
            logger.error(f"Option chain API failed: {resp.status_code}")
            return pd.DataFrame(), None
        chain = resp.json()

        price = chain.get("underlying", {}).get("last")
        if not price:
            quote = chain.get("underlyingQuote", {})
            price = quote.get("lastPrice") or quote.get("askPrice") or quote.get("bidPrice")

        options: List[Dict[str, Any]] = []
        required = ["symbol", "strikePrice", "bid", "ask", "volatility", "delta", "openInterest", "totalVolume"]

        for side, key in [("call", "callExpDateMap"), ("put", "putExpDateMap")]:
            for _, strikes in chain.get(key, {}).items():
                for _, contracts in strikes.items():
                    for contract in contracts:
                        if all(k in contract and contract[k] is not None for k in required):
                            options.append(
                                {
                                    "symbol": contract["symbol"].replace(" ", ""),
                                    "strike": float(contract["strikePrice"]),
                                    "side": side,
                                    "bid": float(contract["bid"]),
                                    "ask": float(contract["ask"]),
                                    "iv": float(contract["volatility"]),
                                    "delta": float(contract["delta"]),
                                    "oi": int(contract["openInterest"]),
                                    "volume": int(contract.get("totalVolume", 0)),
                                }
                            )
        return pd.DataFrame(options), (float(price) if price is not None else None)
    except Exception as e:
        logger.error(f"Error fetching option chain: {e}")
        return pd.DataFrame(), None


# === ANALYSIS ===========================================

def get_momentum_score(symbol: str) -> Dict[str, Any]:
    df = get_schwab_price_history(symbol, periodType="day", period=5, frequencyType="minute", frequency=15)
    if df is None or len(df) < 20:
        logger.warning(f"Insufficient data for momentum analysis on {symbol}")
        return {"score": 50, "trend": "neutral", "volatility": "medium", "atr": 0.015, "rsi": None}

    df.ta.rsi(length=14, append=True)
    df.ta.atr(length=14, append=True)
    df.ta.ema(length=9, append=True)
    df.ta.ema(length=20, append=True)

    score = 50
    trend = "neutral"
    volatility = "medium"
    atr_value = 0.015
    rsi_val = None

    if "RSI_14" in df.columns:
        rsi = df["RSI_14"].iloc[-1]
        if not pd.isna(rsi):
            rsi_val = float(rsi)
            if 40 < rsi < 60:
                score += 15
            elif 30 < rsi < 70:
                score += 10

    if "EMA_9" in df.columns and "EMA_20" in df.columns:
        ema9 = df["EMA_9"].iloc[-1]
        ema20 = df["EMA_20"].iloc[-1]
        last = df["close"].iloc[-1]
        if not pd.isna(ema9) and not pd.isna(ema20):
            if ema9 > ema20 and last > ema9:
                score += 25
                trend = "uptrend"
            elif ema9 < ema20 and last < ema9:
                score += 25
                trend = "downtrend"
            else:
                score += 10
                trend = "neutral"

    if "volume" in df.columns:
        recent_volume = df["volume"].tail(10).mean()
        avg_volume = df["volume"].mean()
        if recent_volume > avg_volume * 1.5:
            score += 15

    if "ATRr_14" in df.columns:
        atr = df["ATRr_14"].iloc[-1]
        if not pd.isna(atr):
            atr_value = float(atr)
            if atr > 0.02:
                score += 10
                volatility = "high"
            elif atr > 0.015:
                score += 5
                volatility = "medium"
            else:
                volatility = "low"

    returns = df["close"].pct_change()
    if len(returns) >= 5:
        recent_momentum = returns.tail(5).mean()
        if not pd.isna(recent_momentum) and abs(recent_momentum) > 0.005:
            score += 10

    return {"score": min(score, 100), "trend": trend, "volatility": volatility, "rsi": rsi_val, "atr": atr_value}


def get_confidence_level(score: float) -> str:
    if score >= 85:
        return "HIGH"
    if score >= 70:
        return "MEDIUM"
    return "LOW"


def calculate_position_size(max_loss_per_contract: float) -> int:
    """
    Keeps your current convention: risk cap ~$100 using option price units (not *100 contract multiplier).
    """
    max_risk_per_trade = 100.0
    if max_loss_per_contract <= 0:
        return 1
    max_contracts = max(1, int(max_risk_per_trade / max_loss_per_contract))

    if max_loss_per_contract > 50:
        max_contracts = 1
    elif max_loss_per_contract > 25:
        max_contracts = min(max_contracts, 2)
    elif max_loss_per_contract > 10:
        max_contracts = min(max_contracts, 3)

    return max_contracts


# --- STOP MODELS ----------------------------------------

def stop_for_credit(option: pd.Series, momentum: Dict[str, Any]) -> Dict[str, Any]:
    """
    Credit (SELL): stop is a multiple of entry, like v2.
    """
    entry = float(option["bid"])
    delta = abs(float(option["delta"]))
    iv = float(option["iv"])

    base = 1.50
    iv_factor = 1.3 if iv > 70 else (1.2 if iv > 50 else (1.0 if iv > 30 else 0.9))
    delta_factor = 1.3 if delta > 0.40 else (1.1 if delta > 0.30 else (1.0 if delta > 0.20 else 0.8))
    vol_factor = {"high": 1.3, "medium": 1.0, "low": 0.8}.get(momentum["volatility"], 1.0)

    stop_mult = base * iv_factor * delta_factor * vol_factor
    stop_mult = max(1.25, min(stop_mult, 2.0))

    stop_price = round(entry * stop_mult, 2)
    max_loss = stop_price - entry
    return {
        "stop_price": stop_price,
        "stop_multiplier": stop_mult,
        "max_loss_per_contract": max_loss,
        "max_loss_per_contract_real": max_loss * 100.0,
        "position_size": calculate_position_size(max_loss),
    }


def stop_for_debit(option: pd.Series, momentum: Dict[str, Any]) -> Dict[str, Any]:
    """
    Debit (BUY): stop is a % drawdown from entry (ask).
    - default: 35% stop for medium vol, 45% for high vol, 25% for low vol
    """
    entry = float(option["ask"])
    vol = momentum["volatility"]
    stop_pct = 0.45 if vol == "high" else (0.25 if vol == "low" else 0.35)

    stop_price = round(entry * (1.0 - stop_pct), 2)
    max_loss = entry - stop_price
    # If ask is tiny, max_loss can become tiny -> cap contracts
    return {
        "stop_price": stop_price,
        "stop_multiplier": 1.0 - stop_pct,
        "max_loss_per_contract": max_loss,
        "max_loss_per_contract_real": max_loss * 100.0,
        "position_size": calculate_position_size(max_loss),
    }


# --- SCORING ---------------------------------------------

def _auto_strategy(momentum_data: Dict[str, Any]) -> str:
    score = float(momentum_data.get("score", 50))
    trend = momentum_data.get("trend", "neutral")
    if trend in ("uptrend", "downtrend") and score >= 65:
        return "momentum"
    return "premium_collection"


def _side_allowed(trend: str, strategy: str, style: str) -> Tuple[bool, bool]:
    """
    Returns (allow_calls, allow_puts) for a given style/strategy/trend.

    CREDIT:
      momentum:   uptrend->sell PUT, downtrend->sell CALL
      contrarian: uptrend->sell CALL, downtrend->sell PUT
      premium_collection: both

    DEBIT:
      momentum:   uptrend->buy CALL, downtrend->buy PUT
      contrarian: uptrend->buy PUT, downtrend->buy CALL
      premium_collection: both (but score will sort)
    """
    strategy = strategy.lower()
    style = style.lower()

    if strategy == "premium_collection" or trend == "neutral":
        return True, True

    if style == "credit":
        if strategy == "momentum":
            return (False, True) if trend == "uptrend" else (True, False)
        if strategy == "contrarian":
            return (True, False) if trend == "uptrend" else (False, True)
        return True, True

    # debit
    if strategy == "momentum":
        return (True, False) if trend == "uptrend" else (False, True)
    if strategy == "contrarian":
        return (False, True) if trend == "uptrend" else (True, False)
    return True, True


def find_trade_options(
    options_df: pd.DataFrame,
    current_price: float,
    momentum_data: Dict[str, Any],
    strategy_preference: str,
    style: str,
    min_premium: Optional[float] = None,
    max_premium: Optional[float] = None,
) -> pd.DataFrame:
    """
    style:
      - credit: SELL options (entry at bid)
      - debit:  BUY options (entry at ask)
    """
    if options_df.empty:
        return pd.DataFrame()

    strategy = strategy_preference.lower().strip()
    if strategy == "auto":
        strategy = _auto_strategy(momentum_data)

    trend = momentum_data["trend"]
    mom_score = float(momentum_data["score"])
    allow_calls, allow_puts = _side_allowed(trend, strategy, style)

    logger.info(f"Strategy: {strategy}, Style: {style}, Trend: {trend}, Momentum Score: {mom_score:.0f}")

    rows: List[Dict[str, Any]] = []

    # shared liquidity filters
    max_spread_pct = 0.10
    min_volume = 50

    for _, opt in options_df.iterrows():
        side = opt["side"]
        if side == "call" and not allow_calls:
            continue
        if side == "put" and not allow_puts:
            continue

        bid = float(opt["bid"])
        ask = float(opt["ask"])
        if bid <= 0 or ask <= 0 or ask < bid:
            continue

        spread = ask - bid
        spread_pct = spread / ask if ask > 0 else 1.0
        if spread_pct > max_spread_pct:
            continue
        if float(opt.get("volume", 0)) < min_volume and int(opt.get("oi", 0)) < 100:
            continue

        delta_abs = abs(float(opt["delta"]))
        iv = float(opt["iv"])

        # Style-specific entry price + filters
        if style == "credit":
            entry = bid
            if min_premium is not None and entry < min_premium:
                continue
            if max_premium is not None and entry > max_premium:
                continue
            if not (0.20 <= delta_abs <= 0.50):
                continue
            if iv < 20 or iv > 120:
                continue
            if entry < 0.30 or entry > 15.00:
                continue
            stop = stop_for_credit(opt, momentum_data)
        else:
            entry = ask
            if min_premium is not None and entry < min_premium:
                continue
            if max_premium is not None and entry > max_premium:
                continue
            # Long options: allow slightly higher delta for directional exposure
            if not (0.25 <= delta_abs <= 0.65):
                continue
            # For debit, prefer not-crazy IV (you pay for it)
            if iv < 15 or iv > 100:
                continue
            if entry < 0.25 or entry > 12.00:
                continue
            stop = stop_for_debit(opt, momentum_data)

        # Alignment score
        if trend == "uptrend":
            aligned = (side == "put") if style == "credit" else (side == "call")
        elif trend == "downtrend":
            aligned = (side == "call") if style == "credit" else (side == "put")
        else:
            aligned = True

        momentum_alignment = 1.0 if aligned else (0.6 if trend == "neutral" else 0.4)

        # Liquidity score
        volume_score = min(float(opt.get("volume", 0)) / 500, 1.0)
        oi_score = min(float(opt.get("oi", 0)) / 1000, 1.0)
        liquidity = (0.7 * volume_score + 0.3 * oi_score)

        # IV score: credit likes higher IV; debit prefers moderate/lower
        if style == "credit":
            iv_score = min(max((iv - 20) / 60, 0), 1.0)
        else:
            # peak around 35–55; penalize very high IV
            if iv <= 20:
                iv_score = 0.3
            elif iv <= 55:
                iv_score = 1.0
            elif iv <= 75:
                iv_score = 0.7
            else:
                iv_score = 0.4

        # "Gamma-ish" (closer to 0.5 delta is more responsive)
        gamma_score = 1.0 - abs(delta_abs - 0.5) * 2
        gamma_score = max(0.0, min(gamma_score, 1.0))

        # Confidence score (reuse structure, tuned a bit)
        conf = 0.0
        # trend alignment weight
        conf += 35.0 * momentum_alignment
        # delta sweet spot
        if style == "credit":
            conf += 20.0 if 0.25 <= delta_abs <= 0.45 else (14.0 if 0.20 <= delta_abs <= 0.50 else 6.0)
        else:
            conf += 20.0 if 0.35 <= delta_abs <= 0.60 else (14.0 if 0.25 <= delta_abs <= 0.65 else 6.0)
        # liquidity
        conf += 20.0 * liquidity
        # IV appropriateness
        conf += 15.0 * iv_score
        # spread quality
        conf += 10.0 * (1.0 - min(spread_pct, 1.0))
        conf = float(min(conf, 100.0))

        # Scoring objective:
        # CREDIT: higher entry premium is good (to a point)
        # DEBIT: lower entry premium is good (cheaper) but still want responsiveness
        if style == "credit":
            premium_score = min(entry / 10.0, 1.0)  # 0..1
            score = (
                gamma_score * 22
                + momentum_alignment * 26
                + (conf / 100.0) * 22
                + iv_score * 15
                + (1.0 - spread_pct) * 10
                + liquidity * 5
                + premium_score * 5
            )
        else:
            # cheaper gets a boost; cap at ask<=6 gives full boost
            cheap_score = min(6.0 / max(entry, 0.25), 1.0)
            score = (
                gamma_score * 24
                + momentum_alignment * 30
                + (conf / 100.0) * 22
                + iv_score * 12
                + (1.0 - spread_pct) * 7
                + liquidity * 5
                + cheap_score * 0  # keep neutral; you can increase later if you want cheaper bias
            )

        confidence_level = get_confidence_level(conf)

        rows.append(
            {
                "option_symbol": opt["symbol"],
                "side": side,
                "strike": float(opt["strike"]),
                "bid": bid,
                "ask": ask,
                "delta": float(opt["delta"]),
                "iv": iv,
                "oi": int(opt["oi"]),
                "volume": int(opt.get("volume", 0)),
                "trade_style": style,
                "strategy": strategy,
                "entry_price": float(entry),
                "day_trade_score": float(score),
                "confidence_score": float(conf),
                "confidence_level": confidence_level,
                "stop_price": float(stop["stop_price"]),
                "stop_multiplier": float(stop["stop_multiplier"]),
                "max_loss_per_contract": float(stop["max_loss_per_contract"]),
                "max_loss_per_contract_real": float(stop.get("max_loss_per_contract_real", stop["max_loss_per_contract"]*100.0)),
                "position_size": int(stop["position_size"]),
                "spread_pct": float(spread_pct),
            }
        )

    out = pd.DataFrame(rows)
    if out.empty:
        logger.warning("No candidates after filtering")
        return out

    logger.info(f"Found {len(out)} potential trades")
    calls = out[out["side"] == "call"]
    puts = out[out["side"] == "put"]
    if not calls.empty:
        top = calls.sort_values("day_trade_score", ascending=False).iloc[0]
        logger.info(f"  - {len(calls)} CALL candidates (top: ${top['strike']:.2f}, score={top['day_trade_score']:.1f})")
    if not puts.empty:
        top = puts.sort_values("day_trade_score", ascending=False).iloc[0]
        logger.info(f"  - {len(puts)} PUT candidates (top: ${top['strike']:.2f}, score={top['day_trade_score']:.1f})")
    return out


def build_trade_plan(row: pd.Series, hold_hours: Optional[int], hold_days: Optional[int]) -> Dict[str, Any]:
    style = str(row["trade_style"]).lower()
    entry = float(row["entry_price"])

    if style == "credit":
        # targets are buy-back prices
        t10 = round(entry * 0.90, 2)
        t20 = round(entry * 0.80, 2)
        action = f"SELL {str(row['side']).upper()}"
        target_label = "Buy back"
    else:
        # debit: targets are sell prices (profit)
        t25 = round(entry * 1.25, 2)
        t50 = round(entry * 1.50, 2)
        t10 = t25
        t20 = t50
        action = f"BUY {str(row['side']).upper()}"
        target_label = "Sell"

    if hold_hours is not None:
        time_exit = f"{hold_hours}h hold (or by 3:55 PM ET if same-day)"
    elif hold_days is not None:
        time_exit = f"{hold_days}d hold (exit on/around that day or earlier if stop/targets hit)"
    else:
        time_exit = "3:55 PM ET (day trade only)"

    risk = float(row["max_loss_per_contract"])
    reward1 = abs(entry - t10) if style == "credit" else (t10 - entry)
    rr = round((reward1 / risk), 2) if risk > 0 else 0.0

    return {
        "action": action,
        "target_1": float(t10),
        "target_2": float(t20),
        "target_label": target_label,
        "time_exit": time_exit,
        "risk_reward_1": rr,
    }


# === BOT RUNNER INTEGRATION =============================

async def run_guru_pick_4exp_bots(bot_params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Main function called by the option_bots_runner system.
    
    Args:
        bot_params: Dictionary containing bot parameters from the database
            - symbol: Underlying symbol
            - strategy: momentum/contrarian/premium_collection/auto
            - style: credit/debit
            - expires: Number of expirations to scan
            - top: Top N candidates per expiration
            - min_premium: Minimum option premium
            - max_premium: Maximum option premium
            - hold_hours: Hold duration in hours
            - hold_days: Hold duration in days
            - contracts: Force number of contracts
    
    Returns:
        Dictionary with scan results
    """
    try:
        # Extract parameters with defaults
        symbol = str(bot_params.get("symbol", "SPY")).upper()
        strategy = str(bot_params.get("strategy", "auto")).lower()
        style = str(bot_params.get("style", "credit")).lower()
        expires = int(bot_params.get("expires", 4))
        top = int(bot_params.get("top", 8))
        min_premium = float(bot_params["min_premium"]) if bot_params.get("min_premium") is not None else None
        max_premium = float(bot_params["max_premium"]) if bot_params.get("max_premium") is not None else None
        hold_hours = int(bot_params["hold_hours"]) if bot_params.get("hold_hours") is not None else None
        hold_days = int(bot_params["hold_days"]) if bot_params.get("hold_days") is not None else None
        forced_contracts = int(bot_params["contracts"]) if bot_params.get("contracts") is not None else None
        
        # Validate parameters
        if min_premium is not None and max_premium is not None and min_premium > max_premium:
            return {
                "success": False,
                "error": "min_premium cannot be greater than max_premium"
            }
        
        if hold_hours is not None and hold_days is not None:
            return {
                "success": False,
                "error": "Please specify only one: hold_hours OR hold_days"
            }
        
        logger.info(f"Running guru_pick_4exp bot for {symbol} with strategy={strategy}, style={style}")
        
        # Get expiration dates
        all_exp = get_expiration_dates(symbol)
        if not all_exp:
            return {
                "success": False,
                "error": "No expiration dates available"
            }
        
        # Choose which expirations to scan
        scan_dates = choose_expirations(all_exp, expires=expires, hold_hours=hold_hours, hold_days=hold_days)
        
        # Get momentum analysis
        momentum = get_momentum_score(symbol)
        today = _now_et().date()
        
        # Get underlying price from first chain
        first_df, first_price = fetch_full_option_chain(symbol, scan_dates[0])
        if first_price is None:
            return {
                "success": False,
                "error": "Could not retrieve underlying price"
            }
        
        # Store results
        results_by_expiration = {}
        best_overall = None
        best_overall_score = -1
        best_overall_exp = None
        
        # Scan each expiration
        for exp_date in scan_dates:
            dte = (exp_date - today).days
            logger.info(f"Scanning expiration {exp_date} ({dte} DTE)")
            
            # Fetch option chain
            opt_df, price = fetch_full_option_chain(symbol, exp_date)
            if opt_df.empty:
                logger.warning(f"No options data for expiration {exp_date}")
                continue
                
            if price is None:
                price = first_price
            
            # Find trade options
            candidates = find_trade_options(
                opt_df,
                current_price=float(price),
                momentum_data=momentum,
                strategy_preference=strategy,
                style=style,
                min_premium=min_premium,
                max_premium=max_premium,
            )
            
            if candidates.empty:
                logger.info(f"No candidates found for expiration {exp_date}")
                continue
            
            # Get top candidates
            top_candidates = candidates.sort_values("day_trade_score", ascending=False).head(top).copy()
            
            # Add trade plans
            plans = []
            for _, row in top_candidates.iterrows():
                plan = build_trade_plan(row, hold_hours=hold_hours, hold_days=hold_days)
                if forced_contracts is not None:
                    row["position_size"] = forced_contracts
                plan.update(row.to_dict())
                plans.append(plan)
            
            results_by_expiration[exp_date.strftime("%Y-%m-%d")] = {
                "dte": dte,
                "underlying_price": float(price),
                "candidates": plans[:10]  # Limit to 10 candidates
            }
            
            # Update best overall
            if not plans:
                continue
                
            best_in_exp = max(plans, key=lambda x: float(x["day_trade_score"]))
            best_score = float(best_in_exp["day_trade_score"])
            
            if best_score > best_overall_score:
                best_overall = best_in_exp
                best_overall_score = best_score
                best_overall_exp = exp_date
        
        # Prepare response
        response = {
            "success": True,
            "symbol": symbol,
            "strategy": strategy,
            "style": style,
            "timestamp": _now_et().isoformat(),
            "market_analysis": {
                "underlying_price": float(first_price),
                "momentum_score": momentum["score"],
                "trend": momentum["trend"],
                "volatility": momentum["volatility"],
                "rsi": momentum.get("rsi")
            },
            "expirations_scanned": [d.strftime("%Y-%m-%d") for d in scan_dates],
            "results_by_expiration": results_by_expiration
        }
        
        # Add best overall if found
        # Add best overall if found
        if best_overall:
            response["best_overall_trade"] = {
                "expiration": best_overall_exp.strftime("%Y-%m-%d"),
                "action": best_overall["action"],

                # ✅ Real Schwab contract symbol (THIS fixes fallback)
                "symbol": best_overall.get("option_symbol", ""),
                "option_symbol": best_overall.get("option_symbol", ""),

                "strike": float(best_overall["strike"]),
                "entry_price": float(best_overall["entry_price"]),
                "score": float(best_overall["day_trade_score"]),
                "confidence": best_overall["confidence_level"],
                "delta": abs(float(best_overall["delta"])),
                "iv": float(best_overall["iv"]),
                "stop_price": float(best_overall["stop_price"]),
                "target_1": float(best_overall["target_1"]),
                "target_2": float(best_overall["target_2"]),
                "position_size": int(best_overall["position_size"]),
                "max_loss_per_contract_real": float(best_overall["max_loss_per_contract_real"]),
            }

        return response
        
    except Exception as e:
        logger.error(f"Error running guru_pick_4exp bot: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "timestamp": _now_et().isoformat()
        }


# === CLI / MAIN =========================================

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Options Guru (Schwab) — scan next expirations")
    p.add_argument("symbol", nargs="?", default="SPY", help="Underlying symbol (default: SPY)")
    p.add_argument("--expires", type=int, default=4, help="How many upcoming expirations to scan (default: 4)")
    p.add_argument("--top", type=int, default=8, help="Show top K candidates per expiration (default: 8)")
    p.add_argument("--strategy", choices=["auto", "momentum", "contrarian", "premium_collection"], default="auto",
                   help="Directional logic (default: auto)")
    p.add_argument("--style", choices=["credit", "debit"], default="credit",
                   help="CREDIT = sell premium, DEBIT = buy premium (default: credit)")
    p.add_argument("--min-premium", type=float, default=None,
                   help="Minimum option premium (entry price). Applies to both styles (bid for credit, ask for debit).")
    p.add_argument("--max-premium", type=float, default=None,
                   help="Maximum option premium (entry price). Applies to both styles (bid for credit, ask for debit).")
    p.add_argument("--hold-hours", type=int, default=None, help="Hold duration in hours (intraday)")
    p.add_argument("--hold-days", type=int, default=None, help="Hold duration in days (swing)")
    p.add_argument("--contracts", type=int, default=None,
                   help="Force number of contracts (overrides auto position sizing).")
    p.add_argument("--json", action="store_true", help="Output results as JSON")
    return p


def main() -> int:
    args = build_arg_parser().parse_args()

    symbol = str(args.symbol).upper()
    expires = max(1, int(args.expires))
    topk = max(1, int(args.top))
    style = str(args.style).lower()

    min_premium = float(args.min_premium) if args.min_premium is not None else None
    max_premium = float(args.max_premium) if args.max_premium is not None else None
    if min_premium is not None and max_premium is not None and min_premium > max_premium:
        print("❌ --min-premium cannot be greater than --max-premium")
        return 2

    forced_contracts = int(args.contracts) if args.contracts is not None else None
    if forced_contracts is not None and forced_contracts < 1:
        print("❌ --contracts must be >= 1")
        return 2

    hold_hours = int(args.hold_hours) if args.hold_hours is not None else None
    hold_days = int(args.hold_days) if args.hold_days is not None else None
    if hold_hours is not None and hold_days is not None:
        print("❌ Please specify only one: --hold-hours OR --hold-days")
        return 2

    # Run the scan
    bot_params = {
        "symbol": symbol,
        "strategy": args.strategy,
        "style": style,
        "expires": expires,
        "top": topk,
        "min_premium": min_premium,
        "max_premium": max_premium,
        "hold_hours": hold_hours,
        "hold_days": hold_days,
        "contracts": forced_contracts
    }
    
    # Run synchronously for CLI
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(run_guru_pick_4exp_bots(bot_params))
    finally:
        loop.close()
    
    if not result.get("success", False):
        print(f"❌ Error: {result.get('error', 'Unknown error')}")
        return 1
    
    # Output results
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    
    # Pretty print for CLI
    print(f"\n{'='*60}")
    print(f"DAY TRADE OPTIONS DASHBOARD for {symbol}".center(60))
    print(f"{'='*60}")
    
    market = result["market_analysis"]
    print(f"\n📊 MARKET ANALYSIS:")
    print(f"   Underlying Price: ${market['underlying_price']:.2f}")
    print(f"   Momentum Score:   {market['momentum_score']}/100")
    print(f"   Trend:            {market['trend'].upper()}")
    print(f"   Volatility:       {market['volatility'].upper()}")
    if market.get("rsi") is not None:
        print(f"   RSI:              {market['rsi']:.2f}")
    
    print(f"\n🎯 CONFIGURATION:")
    print(f"   Strategy:         {result['strategy'].upper()}")
    print(f"   Style:            {result['style'].upper()} ({'SELL premium' if style=='credit' else 'BUY premium'})")
    print(f"   Expirations:      {', '.join(result['expirations_scanned'])}")
    
    # Best overall trade
    if "best_overall_trade" in result:
        best = result["best_overall_trade"]
        print(f"\n{'='*60}")
        print("🎯 RECOMMENDED TRADE (BEST OVERALL) 🎯".center(60))
        print(f"{'='*60}")
        print(f"SYMBOL:      {symbol}")
        print(f"ACTION:      {best['action']}")
        print(f"STRIKE:      ${best['strike']:.2f}")
        print(f"EXPIRATION:  {best['expiration']}")
        print(f"{'='*60}")
        
        print(f"\n📊 ENTRY & EXIT LEVELS:")
        print(f"Entry:            ${best['entry_price']:.2f} ({'bid' if style=='credit' else 'ask'})")
        print(f"Target 1:         ${best['target_1']:.2f}")
        print(f"Target 2:         ${best['target_2']:.2f}")
        print(f"Stop Loss:        ${best['stop_price']:.2f}")
        
        print(f"\n📈 CONFIDENCE & RISK:")
        print(f"Score:            {best['score']:.1f}/100")
        print(f"Confidence:       {best['confidence']}")
        print(f"Delta:            {best['delta']:.3f}")
        print(f"IV:               {best['iv']:.1f}%")
        print(f"Position Size:    {best['position_size']} contracts")
        total_risk = best['max_loss_per_contract_real'] * best['position_size']
        print(f"Total Risk:       ${total_risk:.2f}")
    
    # Show candidates by expiration
    print(f"\n{'='*60}")
    print("CANDIDATES BY EXPIRATION".center(60))
    print(f"{'='*60}")
    
    for exp_date, exp_data in result["results_by_expiration"].items():
        print(f"\n📅 {exp_date} ({exp_data['dte']} DTE):")
        if not exp_data["candidates"]:
            print("   ⚠️  No candidates")
            continue
        
        print(f"   {'Rank':<4} {'Action':<12} {'Strike':<8} {'Entry':<7} {'Δ':<5} {'IV%':<5} {'Score':<6} {'Conf':<6} {'Pos':<4}")
        print(f"   {'-'*65}")
        
        for i, candidate in enumerate(exp_data["candidates"][:5], 1):  # Show top 5
            entry = candidate['entry_price']
            action = candidate['action']
            print(
                f"   {i:<4} {action:<12} ${candidate['strike']:<7.2f} ${entry:<6.2f} "
                f"{abs(candidate['delta']):<4.2f} {candidate['iv']:<4.0f} "
                f"{candidate['day_trade_score']:<5.1f} {candidate['confidence_level']:<6} "
                f"{candidate['position_size']:<4}"
            )
    
    print(f"\n{'='*60}")
    print(f"Analysis complete at {_now_et().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"{'='*60}")
    
    return 0


if __name__ == "__main__":
    # Allow running both as CLI and as module
    if len(sys.argv) > 1 and not sys.argv[1].startswith('--'):
        raise SystemExit(main())
    # If imported as module, just define the functions
    # The bot runner will call run_guru_pick_4exp_bots directly