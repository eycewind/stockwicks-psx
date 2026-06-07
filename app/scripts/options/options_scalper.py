#!/usr/bin/env python3
"""
options_scalper.py — Improved Options Scalping Script for Day Trading

This script is an enhanced version of the original guru_pick_4exp.py, optimized for intraday scalping.
Key improvements:
- Bias toward credit spreads for better theta decay and defined risk.
- Stricter filters: liquidity, IV, delta, intraday momentum.
- Dynamic sizing: user defines contracts or max allocation USD; algo caps based on risk.
- Asymmetric stops/targets for credit/debit.
- Cooldown per symbol to prevent over-trading.
- Focus on 0-7 DTE for scalping.
- Added intraday trend filter using 15-min/60-min data.

Supports both credit (sell premium) and debit (buy premium) modes.
For scalping, recommend hold_hours=1-4, style='credit'.

Usage (CLI):
  python options_scalper.py SPY --style credit --hold-hours 2 --max-allocation-usd 1000

Bot integration via run_options_scalper_bots().

Dependencies: pandas, pandas_ta, requests, pytz, dotenv.
Schwab API access token required via env or app.utils.stock.schwab_token.
"""

import os
import sys
import argparse
import logging
import json
import asyncio
from datetime import datetime, timedelta, date
from typing import List, Optional, Tuple, Dict, Any

import pandas as pd
import pandas_ta as ta
import requests
import pytz
from dotenv import load_dotenv

# --- CONFIG & LOGGING ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SCHWAB_API_URL = "https://api.schwabapi.com/marketdata/v1"
COOLDOWN_MINUTES = 30  # Prevent over-trading same symbol

# Project root setup
_THIS_FILE = os.path.abspath(__file__)
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(_THIS_FILE), "../../../../"))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# --- HELPERS ---

def get_schwab_headers() -> Dict[str, str]:
    try:
        from app.utils.stock.schwab_token import get_valid_access_token
        access_token = get_valid_access_token()
    except Exception:
        access_token = os.getenv("SCHWAB_ACCESS_TOKEN")
    if not access_token:
        raise ValueError("Could not get Schwab access token.")
    return {"Authorization": f"Bearer {access_token}"}

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
        resp.raise_for_status()
        data = resp.json()
        if "candles" not in data:
            return None
        df = pd.DataFrame(data["candles"])
        df["datetime"] = pd.to_datetime(df["datetime"], unit="ms")
        df.set_index("datetime", inplace=True)
        df.columns = [c.lower() for c in df.columns]
        return df
    except Exception as e:
        logger.error(f"Price history failed for {symbol}: {e}")
        return None

def get_expiration_dates(symbol: str) -> List[date]:
    url = f"{SCHWAB_API_URL}/expirationchain"
    params = {"symbol": symbol}
    headers = get_schwab_headers()
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        expirations = data.get("expirationList", [])
        out = [datetime.strptime(e["expirationDate"], "%Y-%m-%d").date() for e in expirations]
        today = _now_et().date()
        return sorted(d for d in out if d >= today)
    except Exception:
        return []

def choose_expirations(
    all_dates: List[date],
    expires: int,
    hold_hours: Optional[int],
    hold_days: Optional[int],
) -> List[date]:
    today = _now_et().date()
    if hold_hours is not None:
        # For scalping, prefer 0-3 DTE
        near = [d for d in all_dates if 0 <= (d - today).days <= 3]
        return near[:expires] or all_dates[:expires]
    if hold_days is not None:
        min_dte = max(1, hold_days)
        candidates = [d for d in all_dates if (d - today).days >= min_dte]
        preferred = [d for d in candidates if 3 <= (d - today).days <= 14]
        return preferred[:expires] or candidates[:expires] or all_dates[:expires]
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
        resp.raise_for_status()
        chain = resp.json()
        price = chain.get("underlying", {}).get("last") or chain.get("underlyingQuote", {}).get("lastPrice")
        options = []
        required = ["symbol", "strikePrice", "bid", "ask", "volatility", "delta", "openInterest", "totalVolume"]
        for side, key in [("call", "callExpDateMap"), ("put", "putExpDateMap")]:
            for _, strikes in chain.get(key, {}).items():
                for _, contracts in strikes.items():
                    for contract in contracts:
                        if all(k in contract for k in required):
                            options.append({
                                "symbol": contract["symbol"].replace(" ", ""),
                                "strike": float(contract["strikePrice"]),
                                "side": side,
                                "bid": float(contract["bid"]),
                                "ask": float(contract["ask"]),
                                "iv": float(contract["volatility"]),
                                "delta": float(contract["delta"]),
                                "oi": int(contract["openInterest"]),
                                "volume": int(contract.get("totalVolume", 0)),
                            })
        return pd.DataFrame(options), float(price) if price else None
    except Exception as e:
        logger.error(f"Option chain failed: {e}")
        return pd.DataFrame(), None

# --- ANALYSIS ---

def get_momentum_score(symbol: str) -> Dict[str, Any]:
    df_daily = get_schwab_price_history(symbol, "day", 10, "minute", 15)
    if df_daily is None or len(df_daily) < 50:
        return {"score": 50, "trend": "neutral", "volatility": "medium", "atr": 0.015, "rsi": None, "adx": 15}

    # Add indicators
    df_daily.ta.rsi(length=14, append=True)
    df_daily.ta.atr(length=14, append=True)
    df_daily.ta.ema(length=8, append=True)
    df_daily.ta.ema(length=21, append=True)
    df_daily.ta.adx(length=14, append=True)  # Add ADX for trend strength

    score = 50
    trend = "neutral"
    volatility = "medium"
    atr_value = df_daily["ATRr_14"].iloc[-1] if "ATRr_14" in df_daily else 0.015
    rsi_val = df_daily["RSI_14"].iloc[-1] if "RSI_14" in df_daily else None
    adx_val = df_daily["ADX_14"].iloc[-1] if "ADX_14" in df_daily else 15

    if "EMA_8" in df_daily and "EMA_21" in df_daily:
        ema8 = df_daily["EMA_8"].iloc[-1]
        ema21 = df_daily["EMA_21"].iloc[-1]
        last = df_daily["close"].iloc[-1]
        if ema8 > ema21 and last > ema8:
            trend = "uptrend"
            score += 30 if adx_val > 25 else 20
        elif ema8 < ema21 and last < ema8:
            trend = "downtrend"
            score += 30 if adx_val > 25 else 20
        else:
            score += 10

    if rsi_val:
        if 45 < rsi_val < 55:
            score += 15
        elif rsi_val > 70 or rsi_val < 30:
            score += 10  # Extremes for potential reversal in scalping

    if atr_value > 0.025:
        volatility = "high"
        score += 15
    elif atr_value > 0.015:
        volatility = "medium"
        score += 10
    else:
        volatility = "low"

    return {
        "score": min(score, 100),
        "trend": trend,
        "volatility": volatility,
        "rsi": rsi_val,
        "atr": atr_value,
        "adx": adx_val,
    }

def get_intraday_trend(symbol: str) -> str:
    df_60 = get_schwab_price_history(symbol, "day", 1, "minute", 60)
    if df_60 is None or len(df_60) < 5:
        return "neutral"
    ema_fast = df_60['close'].ewm(span=5).mean().iloc[-1]
    ema_slow = df_60['close'].ewm(span=13).mean().iloc[-1]
    if ema_fast > ema_slow:
        return "uptrend"
    elif ema_fast < ema_slow:
        return "downtrend"
    return "neutral"

# --- SIZING ---

def calculate_position_size(max_loss_per_contract: float, max_risk_usd: float = 75.0) -> int:
    if max_loss_per_contract <= 0:
        return 1
    max_contracts = max(1, int(max_risk_usd / max_loss_per_contract))
    if max_loss_per_contract > 40:
        max_contracts = 1
    elif max_loss_per_contract > 20:
        max_contracts = min(max_contracts, 2)
    return max_contracts

def cap_contracts_by_allocation(
    entry_price: float,
    desired_contracts: int,
    max_allocation_usd: Optional[float],
) -> Tuple[int, Optional[float]]:
    if entry_price <= 0:
        return max(1, desired_contracts), None
    if max_allocation_usd is None or max_allocation_usd <= 0:
        return desired_contracts, None
    alloc_cap_qty = int(max_allocation_usd // (entry_price * 100.0))
    final_qty = max(1, min(desired_contracts, alloc_cap_qty))
    est_alloc = round(entry_price * 100.0 * final_qty, 2)
    return final_qty, est_alloc

# --- STOP & TARGET MODELS (ASYMMETRIC) ---

def stop_for_credit(option: pd.Series, momentum: Dict[str, Any]) -> Dict[str, Any]:
    entry = float(option["bid"])
    vol = momentum["volatility"]
    mult = 1.35 if vol == "high" else 1.20 if vol == "medium" else 1.10
    stop_price = round(entry * mult, 2)
    max_loss = stop_price - entry
    return {
        "stop_price": stop_price,
        "max_loss_per_contract": max_loss,
        "position_size": calculate_position_size(max_loss),
    }

def target_for_credit(entry: float) -> Tuple[float, float]:
    t1 = round(entry * 0.85, 2)  # 15% profit
    t2 = round(entry * 0.70, 2)  # 30% profit
    return t1, t2

def stop_for_debit(option: pd.Series, momentum: Dict[str, Any]) -> Dict[str, Any]:
    entry = float(option["ask"])
    vol = momentum["volatility"]
    pct = 0.30 if vol == "high" else 0.20 if vol == "medium" else 0.15
    stop_price = round(entry * (1.0 - pct), 2)
    max_loss = entry - stop_price
    return {
        "stop_price": stop_price,
        "max_loss_per_contract": max_loss,
        "position_size": calculate_position_size(max_loss, max_risk_usd=50.0),  # Tighter for debit
    }

def target_for_debit(entry: float) -> Tuple[float, float]:
    t1 = round(entry * 1.20, 2)  # 20% profit
    t2 = round(entry * 1.40, 2)  # 40% profit
    return t1, t2

# --- SCORING & FILTERING ---

def find_trade_options(
    options_df: pd.DataFrame,
    current_price: float,
    momentum_data: Dict[str, Any],
    style: str,
    min_premium: float = 0.5,
    max_premium: float = 4.0,
) -> pd.DataFrame:
    if options_df.empty:
        return pd.DataFrame()

    style = style.lower()
    trend = momentum_data["trend"]
    intraday_trend = get_intraday_trend(options_df.iloc[0]["symbol"].split("_")[0]) if not options_df.empty else "neutral"
    if intraday_trend != trend:
        logger.info(f"Intraday trend mismatch: {intraday_trend} vs daily {trend} - skipping")
        return pd.DataFrame()

    # Stricter filters
    min_oi = 300
    min_volume = 150
    max_spread_pct = 0.06

    rows = []
    for _, opt in options_df.iterrows():
        bid = float(opt["bid"])
        ask = float(opt["ask"])
        spread = ask - bid
        spread_pct = spread / ask if ask > 0 else 1.0
        if spread_pct > max_spread_pct or opt["oi"] < min_oi or opt["volume"] < min_volume:
            continue

        delta_abs = abs(float(opt["delta"]))
        iv = float(opt["iv"])

        if style == "credit":
            entry = bid
            if not (min_premium <= entry <= max_premium) or not (0.15 <= delta_abs <= 0.38) or not (35 <= iv <= 90):
                continue
            stop = stop_for_credit(opt, momentum_data)
            t1, t2 = target_for_credit(entry)
            position = "short"
        else:  # debit
            entry = ask
            if not (min_premium <= entry <= 2.5) or not (0.40 <= delta_abs <= 0.70) or not (30 <= iv <= 65):
                continue
            stop = stop_for_debit(opt, momentum_data)
            t1, t2 = target_for_debit(entry)
            position = "long"

        # Alignment
        aligned = (trend == "uptrend" and opt["side"] == ("put" if style == "credit" else "call")) or \
                  (trend == "downtrend" and opt["side"] == ("call" if style == "credit" else "put")) or \
                  trend == "neutral"
        alignment_score = 1.0 if aligned else 0.5

        # Score
        liquidity = min(opt["volume"] / 500, 1.0) * 0.6 + min(opt["oi"] / 2000, 1.0) * 0.4
        iv_score = min((iv - 30) / 50, 1.0) if style == "credit" else max(1.0 - (iv - 50) / 50, 0.5)
        score = alignment_score * 40 + liquidity * 30 + iv_score * 20 + (1 - spread_pct) * 10

        rows.append({
            "symbol": opt["symbol"],
            "side": opt["side"],
            "strike": opt["strike"],
            "entry_price": entry,
            "delta": delta_abs,
            "iv": iv,
            "oi": opt["oi"],
            "volume": opt["volume"],
            "position": position,
            "stop_price": stop["stop_price"],
            "target_1": t1,
            "target_2": t2,
            "max_loss_per_contract": stop["max_loss_per_contract"],
            "position_size": stop["position_size"],
            "score": score,
        })

    out = pd.DataFrame(rows).sort_values("score", ascending=False)
    return out

# --- BOT RUNNER ---

last_trade_time: Dict[str, datetime] = {}

async def run_options_scalper_bots(bot_params: Dict[str, Any]) -> Dict[str, Any]:
    symbol = bot_params.get("symbol", "SPY").upper()
    style = bot_params.get("style", "credit").lower()
    expires = int(bot_params.get("expires", 2))  # Fewer for scalping
    max_allocation_usd = bot_params.get("max_allocation_usd")
    forced_contracts = bot_params.get("contracts")
    hold_hours = bot_params.get("hold_hours")
    hold_days = bot_params.get("hold_days")

    if symbol in last_trade_time and (_now_et() - last_trade_time[symbol]) < timedelta(minutes=COOLDOWN_MINUTES):
        return {"success": False, "error": f"Cooldown active for {symbol}"}

    all_exp = get_expiration_dates(symbol)
    if not all_exp:
        return {"success": False, "error": "No expirations"}

    scan_dates = choose_expirations(all_exp, expires, hold_hours, hold_days)
    momentum = get_momentum_score(symbol)

    results = {}
    best_overall = None
    best_score = -1

    for exp_date in scan_dates:
        opt_df, price = fetch_full_option_chain(symbol, exp_date)
        if opt_df.empty or price is None:
            continue

        candidates = find_trade_options(opt_df, price, momentum, style)
        if candidates.empty:
            continue

        for _, row in candidates.iterrows():
            base_qty = row["position_size"]
            desired_qty = forced_contracts or base_qty
            final_qty, est_alloc = cap_contracts_by_allocation(row["entry_price"], desired_qty, max_allocation_usd)
            row["position_size"] = final_qty
            row["est_allocation"] = est_alloc

        results[exp_date.strftime("%Y-%m-%d")] = candidates.to_dict(orient="records")

        top = candidates.iloc[0]
        if top["score"] > best_score:
            best_overall = top.to_dict()
            best_score = top["score"]

    last_trade_time[symbol] = _now_et()

    return {
        "success": True,
        "symbol": symbol,
        "style": style,
        "results": results,
        "best_trade": best_overall,
    }

# --- CLI ---

def main():
    p = argparse.ArgumentParser(description="Options Scalper — Day Trade Script")
    p.add_argument("symbol", default="SPY")
    p.add_argument("--style", choices=["credit", "debit"], default="credit")
    p.add_argument("--expires", type=int, default=2)
    p.add_argument("--hold-hours", type=int)
    p.add_argument("--hold-days", type=int)
    p.add_argument("--contracts", type=int)
    p.add_argument("--max-allocation-usd", type=float)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    bot_params = vars(args)
    loop = asyncio.get_event_loop()
    result = loop.run_until_complete(run_options_scalper_bots(bot_params))

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result)  # Pretty print as needed

if __name__ == "__main__":
    main()