#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/options/algos/guru_pick_4exp.py
"""
guru_pick_4exp.py — Options "Guru" (Schwab) | Scan next N expirations

Designed for:
- CLI usage (for debugging + JSON output)
- Bot usage via option_bots_runner.py

Key behaviors:
- style:
    credit -> SELL premium (entry=bid, TP=buy-back lower, SL=higher)
    debit  -> BUY premium  (entry=ask, TP=sell higher, SL=lower)
- Supports user overrides for profit_target + stop_loss:
    * If provided, those override the script's recommended TP/SL and all P/L math uses the override.
- All monetary calculations are in "option price points" (e.g., 11.75) and then "real" = points * 100 * contracts.
- JSON output is rounded to 2 decimals everywhere.

Defaults (when user leaves fields blank in the web UI):
    style=credit
    contracts=1
    min_premium=0.20
    max_premium=100.00
    profit_target=None (use script recommendation)
    stop_loss=None (use script recommendation)
"""
# --- Project bootstrap (so `import app...` works when run as a standalone script) ---
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


import argparse
import json
import logging
from datetime import datetime, date
from typing import List, Optional, Tuple, Dict, Any

import pandas as pd
import pandas_ta as ta
import requests
import pytz
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SCHWAB_API_URL = "https://api.schwabapi.com/marketdata/v1"


# =========================
# Schwab / time helpers
# =========================
def get_schwab_headers() -> Dict[str, str]:
    try:
        from app.utils.stock.schwab_token import get_valid_access_token
        access_token = get_valid_access_token()
        if not access_token:
            raise ValueError("Could not get Schwab access token.")
        return {"Authorization": f"Bearer {access_token}"}
    except Exception:
        access_token = os.getenv("SCHWAB_ACCESS_TOKEN")
        if access_token:
            return {"Authorization": f"Bearer {access_token}"}
        raise ValueError("Could not get Schwab access token.")



def _now_et() -> datetime:
    return datetime.now(pytz.timezone("US/Eastern"))


# =========================
# Data fetchers
# =========================
def get_schwab_price_history(
    symbol: str,
    periodType: str = "day",
    period: int = 5,
    frequencyType: str = "minute",
    frequency: int = 15,
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
        if "datetime" not in df.columns:
            return None
        df["datetime"] = pd.to_datetime(df["datetime"], unit="ms")
        df.set_index("datetime", inplace=True)
        df.columns = [c.lower() for c in df.columns]
        for c in ["high", "low", "close", "volume"]:
            if c not in df.columns:
                return None
        return df
    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed for {symbol}: {e}")
        return None

def get_expiration_dates(symbol: str) -> List[date]:
    url = f"{SCHWAB_API_URL}/expirationchain"
    headers = get_schwab_headers()

    def _try(sym: str):
        resp = requests.get(url, headers=headers, params={"symbol": sym}, timeout=10)
        if resp.status_code != 200:
            body = (resp.text or "").strip().replace("\n", " ")[:400]
            logger.error(f"[EXPCHAIN] symbol={sym} status={resp.status_code} body={body}")
            return None
        return resp.json()

    sym = symbol.strip().upper()
    data = _try(sym)

    if data is None and sym == "SPX":
        data = _try("$SPX")
    elif data is None and sym == "$SPX":
        data = _try("SPX")

    if not data:
        raise RuntimeError(f"Failed to get expiration chain for {symbol}")

    expirations = data.get("expirationList", [])
    out: List[date] = []
    for e in expirations:
        try:
            out.append(datetime.strptime(e["expirationDate"], "%Y-%m-%d").date())
        except Exception:
            continue

    today = _now_et().date()
    return sorted({d for d in out if d >= today})


def choose_expirations(all_dates: List[date], expires: int) -> List[date]:
    today = _now_et().date()
    future = [d for d in all_dates if d >= today]
    return (future if future else all_dates)[:expires]

def fetch_full_option_chain(symbol: str, expiration_date: date) -> Tuple[pd.DataFrame, Optional[float]]:
    """
    Schwab chains endpoint sometimes requires $SPX for SPX index options.
    This function:
      - logs status + response body when non-200
      - retries symbol variants for SPX: SPX <-> $SPX
    """
    url = f"{SCHWAB_API_URL}/chains"

    base_params = {
        "fromDate": expiration_date.strftime("%Y-%m-%d"),
        "toDate": expiration_date.strftime("%Y-%m-%d"),
        "includeUnderlyingQuote": "true",
        "strategy": "SINGLE",
        "range": "ALL",
    }

    headers = get_schwab_headers()

    def _try(sym: str) -> Tuple[pd.DataFrame, Optional[float]]:
        params = dict(base_params)
        params["symbol"] = sym

        try:
            resp = requests.get(url, headers=headers, params=params, timeout=20)
            if resp.status_code != 200:
                body = (resp.text or "").strip().replace("\n", " ")[:600]
                logger.error(f"[CHAIN] symbol={sym} exp={expiration_date} status={resp.status_code} body={body}")
                return pd.DataFrame(), None

            chain = resp.json()

            # Underlying price
            price = chain.get("underlying", {}).get("last")
            if not price:
                quote = chain.get("underlyingQuote", {}) or {}
                price = quote.get("lastPrice") or quote.get("askPrice") or quote.get("bidPrice")

            options: List[Dict[str, Any]] = []
            required = ["symbol", "strikePrice", "bid", "ask", "volatility", "delta", "openInterest"]

            for side, key in [("call", "callExpDateMap"), ("put", "putExpDateMap")]:
                exp_map = chain.get(key, {}) or {}
                for _, strikes in exp_map.items():
                    for _, contracts in (strikes or {}).items():
                        for contract in (contracts or []):
                            if all(k in contract and contract[k] is not None for k in required):
                                options.append(
                                    {
                                        "symbol": str(contract["symbol"]).replace(" ", ""),
                                        "strike": float(contract["strikePrice"]),
                                        "side": side,
                                        "bid": float(contract["bid"]),
                                        "ask": float(contract["ask"]),
                                        "iv": float(contract["volatility"]),
                                        "delta": float(contract["delta"]),
                                        "oi": int(contract["openInterest"]),
                                        "volume": int(contract.get("totalVolume", 0) or 0),
                                    }
                                )

            df = pd.DataFrame(options)
            return df, (float(price) if price is not None else None)

        except Exception as e:
            logger.exception(f"[CHAIN] symbol={sym} exp={expiration_date} error={e}")
            return pd.DataFrame(), None

    symbol_clean = symbol.strip().upper()

    # 1) first try exactly what caller asked for
    df, price = _try(symbol_clean)
    if price is not None:
        return df, price

    # 2) SPX fallback rules
    if symbol_clean == "SPX":
        df2, price2 = _try("$SPX")
        if price2 is not None:
            return df2, price2

    if symbol_clean == "$SPX":
        df2, price2 = _try("SPX")
        if price2 is not None:
            return df2, price2

    return pd.DataFrame(), None


# =========================
# Analysis helpers
# =========================
def get_momentum_score(symbol: str) -> Dict[str, Any]:
    df = get_schwab_price_history(symbol, periodType="day", period=5, frequencyType="minute", frequency=15)
    if df is None or len(df) < 20:
        logger.warning(f"Insufficient data for momentum analysis on {symbol}")
        return {"score": 50.0, "trend": "neutral", "volatility": "medium", "atr": 0.015, "rsi": None}

    df.ta.rsi(length=14, append=True)
    df.ta.atr(length=14, append=True)
    df.ta.ema(length=9, append=True)
    df.ta.ema(length=20, append=True)

    score = 50.0
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
        recent_volume = float(df["volume"].tail(10).mean())
        avg_volume = float(df["volume"].mean())
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
        if not pd.isna(recent_momentum) and abs(float(recent_momentum)) > 0.005:
            score += 10

    return {"score": float(min(score, 100.0)), "trend": trend, "volatility": volatility, "rsi": rsi_val, "atr": float(atr_value)}


def get_confidence_level(score: float) -> str:
    if score >= 85:
        return "HIGH"
    if score >= 70:
        return "MEDIUM"
    return "LOW"


def calculate_position_size(max_loss_per_contract: float) -> int:
    # legacy: cap ~ $100 in option-price points (not *100 multiplier)
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


def stop_for_credit(entry_bid: float, delta_abs: float, iv: float, volatility: str) -> Dict[str, Any]:
    base = 1.50
    iv_factor = 1.3 if iv > 70 else (1.2 if iv > 50 else (1.0 if iv > 30 else 0.9))
    delta_factor = 1.3 if delta_abs > 0.40 else (1.1 if delta_abs > 0.30 else (1.0 if delta_abs > 0.20 else 0.8))
    vol_factor = {"high": 1.3, "medium": 1.0, "low": 0.8}.get(volatility, 1.0)

    stop_mult = base * iv_factor * delta_factor * vol_factor
    stop_mult = max(1.25, min(stop_mult, 2.0))

    stop_price = round(entry_bid * stop_mult, 2)
    max_loss = stop_price - entry_bid
    return {
        "stop_price": float(stop_price),
        "stop_multiplier": float(stop_mult),
        "max_loss_per_contract": float(max_loss),
        "max_loss_per_contract_real": float(max_loss * 100.0),
        "position_size": int(calculate_position_size(max_loss)),
    }


def stop_for_debit(entry_ask: float, volatility: str) -> Dict[str, Any]:
    stop_pct = 0.45 if volatility == "high" else (0.25 if volatility == "low" else 0.35)
    stop_price = round(entry_ask * (1.0 - stop_pct), 2)
    max_loss = entry_ask - stop_price
    return {
        "stop_price": float(stop_price),
        "stop_multiplier": float(1.0 - stop_pct),
        "max_loss_per_contract": float(max_loss),
        "max_loss_per_contract_real": float(max_loss * 100.0),
        "position_size": int(calculate_position_size(max_loss)),
    }


def _auto_strategy(momentum_data: Dict[str, Any]) -> str:
    score = float(momentum_data.get("score", 50.0))
    trend = str(momentum_data.get("trend", "neutral"))
    if trend in ("uptrend", "downtrend") and score >= 65:
        return "momentum"
    return "premium_collection"


def _side_allowed(trend: str, strategy: str, style: str) -> Tuple[bool, bool]:
    """
    Returns (allow_calls, allow_puts)

    CREDIT:
      momentum:   uptrend->sell PUT, downtrend->sell CALL
      contrarian: uptrend->sell CALL, downtrend->sell PUT
      premium_collection: both

    DEBIT:
      momentum:   uptrend->buy CALL, downtrend->buy PUT
      contrarian: uptrend->buy PUT, downtrend->buy CALL
      premium_collection: both
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
    momentum_data: Dict[str, Any],
    strategy_preference: str,
    style: str,
    min_premium: Optional[float],
    max_premium: Optional[float],
) -> pd.DataFrame:
    if options_df.empty:
        return pd.DataFrame()

    strategy = (strategy_preference or "auto").lower().strip()
    if strategy == "auto":
        strategy = _auto_strategy(momentum_data)

    trend = str(momentum_data.get("trend", "neutral"))
    mom_score = float(momentum_data.get("score", 50.0))
    allow_calls, allow_puts = _side_allowed(trend, strategy, style)

    logger.info(f"Strategy: {strategy}, Style: {style}, Trend: {trend}, Momentum Score: {mom_score:.0f}")

    rows: List[Dict[str, Any]] = []

    max_spread_pct = 0.10
    min_volume = 50

    for _, opt in options_df.iterrows():
        side = str(opt["side"])
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

        volume = float(opt.get("volume", 0) or 0)
        oi = float(opt.get("oi", 0) or 0)
        if volume < min_volume and oi < 100:
            continue

        delta_abs = abs(float(opt["delta"]))
        iv = float(opt["iv"])
        volatility = str(momentum_data.get("volatility", "medium"))

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
            if entry < 0.20 or entry > 100.00:
                continue
            stop = stop_for_credit(entry, delta_abs, iv, volatility)
        else:
            entry = ask
            if min_premium is not None and entry < min_premium:
                continue
            if max_premium is not None and entry > max_premium:
                continue
            if not (0.25 <= delta_abs <= 0.65):
                continue
            if iv < 15 or iv > 100:
                continue
            if entry < 0.20 or entry > 100.00:
                continue
            stop = stop_for_debit(entry, volatility)

        # Alignment vs trend
        if trend == "uptrend":
            aligned = (side == "put") if style == "credit" else (side == "call")
        elif trend == "downtrend":
            aligned = (side == "call") if style == "credit" else (side == "put")
        else:
            aligned = True
        momentum_alignment = 1.0 if aligned else (0.6 if trend == "neutral" else 0.4)

        # Liquidity score
        volume_score = min(volume / 500.0, 1.0)
        oi_score = min(oi / 1000.0, 1.0)
        liquidity = (0.7 * volume_score + 0.3 * oi_score)

        # IV preference
        if style == "credit":
            iv_score = min(max((iv - 20.0) / 60.0, 0.0), 1.0)
        else:
            if iv <= 20:
                iv_score = 0.3
            elif iv <= 55:
                iv_score = 1.0
            elif iv <= 75:
                iv_score = 0.7
            else:
                iv_score = 0.4

        gamma_score = 1.0 - abs(delta_abs - 0.5) * 2.0
        gamma_score = max(0.0, min(gamma_score, 1.0))

        conf = 0.0
        conf += 35.0 * momentum_alignment
        if style == "credit":
            conf += 20.0 if 0.25 <= delta_abs <= 0.45 else (14.0 if 0.20 <= delta_abs <= 0.50 else 6.0)
        else:
            conf += 20.0 if 0.35 <= delta_abs <= 0.60 else (14.0 if 0.25 <= delta_abs <= 0.65 else 6.0)
        conf += 20.0 * liquidity
        conf += 15.0 * iv_score
        conf += 10.0 * (1.0 - min(spread_pct, 1.0))
        conf = float(min(conf, 100.0))

        if style == "credit":
            premium_score = min(entry / 10.0, 1.0)
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
            score = (
                gamma_score * 24
                + momentum_alignment * 30
                + (conf / 100.0) * 22
                + iv_score * 12
                + (1.0 - spread_pct) * 7
                + liquidity * 5
            )

        rows.append(
            {
                "occ": str(opt["symbol"]),
                "side": side,
                "strike": float(opt["strike"]),
                "bid": float(bid),
                "ask": float(ask),
                "delta": float(opt["delta"]),
                "iv": float(iv),
                "oi": int(oi),
                "volume": int(volume),
                "trade_style": style,
                "strategy": strategy,
                "entry_price": float(entry),
                "day_trade_score": float(score),
                "confidence_score": float(conf),
                "confidence_level": get_confidence_level(conf),
                "stop_price": float(stop["stop_price"]),
                "max_loss_per_contract": float(stop["max_loss_per_contract"]),
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


def build_trade_plan(style: str, side: str, entry: float) -> Dict[str, Any]:
    style = style.lower()
    side = side.lower()
    if style == "credit":
        # TP are buy-back prices
        t1 = round(entry * 0.90, 2)
        t2 = round(entry * 0.80, 2)
        action = f"SELL {side.upper()}"
        target_label = "Buy back"
    else:
        t1 = round(entry * 1.25, 2)
        t2 = round(entry * 1.50, 2)
        action = f"BUY {side.upper()}"
        target_label = "Sell"
    return {"action": action, "target_1": float(t1), "target_2": float(t2), "target_label": target_label, "time_exit": "3:55 PM ET (day trade only)"}


def _round_floats(obj: Any, nd: int = 2) -> Any:
    if isinstance(obj, float):
        return round(obj, nd)
    if isinstance(obj, dict):
        return {k: _round_floats(v, nd) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, nd) for v in obj]
    return obj


def analyze_symbol(
    symbol: str,
    style: str = "credit",
    contracts: int = 1,
    min_premium: float = 0.20,
    max_premium: float = 100.00,
    strategy: str = "auto",
    expires: int = 4,
    top: int = 8,
    profit_target: Optional[float] = None,
    stop_loss: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Returns a JSON-serializable payload with the "recommended" trade.
    All floats are rounded to 2 decimals in the returned payload.
    """
    symbol = symbol.upper().strip()
    style = (style or "credit").lower().strip()
    if style not in ("credit", "debit"):
        style = "credit"
    contracts = int(contracts or 1)
    contracts = max(1, contracts)

    # Scan expirations
    all_exp = get_expiration_dates(symbol)
    scan_dates = choose_expirations(all_exp, expires=max(1, int(expires)))

    # Momentum
    momentum = get_momentum_score(symbol)

    # Underlying price from first chain
    first_df, first_price = fetch_full_option_chain(symbol, scan_dates[0])
    if first_price is None:
        raise RuntimeError("Could not retrieve underlying price from chain.")

    best_overall: Optional[pd.Series] = None
    best_overall_exp: Optional[date] = None

    today = _now_et().date()
    for exp in scan_dates:
        opt_df, price = fetch_full_option_chain(symbol, exp)
        if opt_df.empty:
            continue

        candidates = find_trade_options(
            opt_df,
            momentum_data=momentum,
            strategy_preference=strategy,
            style=style,
            min_premium=float(min_premium) if min_premium is not None else None,
            max_premium=float(max_premium) if max_premium is not None else None,
        )
        if candidates.empty:
            continue

        best = candidates.sort_values("day_trade_score", ascending=False).head(max(1, int(top))).iloc[0]
        if best_overall is None or float(best["day_trade_score"]) > float(best_overall["day_trade_score"]):
            best_overall = best
            best_overall_exp = exp

    if best_overall is None or best_overall_exp is None:
        raise RuntimeError("No trades found across scanned expirations.")

    entry = float(best_overall["entry_price"])
    side = str(best_overall["side"]).lower()
    plan = build_trade_plan(style, side, entry)

    # Stop recommendation (from scoring) is best_overall["stop_price"].
    recommended_sl = float(best_overall["stop_price"])
    recommended_t1 = float(plan["target_1"])
    recommended_t2 = float(plan["target_2"])

    # Apply overrides (these are in option price points)
    tp_used = float(profit_target) if profit_target is not None else recommended_t1
    sl_used = float(stop_loss) if stop_loss is not None else recommended_sl

    # Compute P/L totals (points + real)
    # BUY: profit = (tp - entry), loss = (entry - sl)
    # SELL: profit = (entry - tp), loss = (sl - entry)
    if style == "debit":
        profit_t1_per_contract = max(tp_used - entry, 0.0)
        profit_t2_per_contract = max(recommended_t2 - entry, 0.0) if profit_target is None else max(tp_used - entry, 0.0)
        risk_points_per_contract = max(entry - sl_used, 0.0)
    else:
        profit_t1_per_contract = max(entry - tp_used, 0.0)
        profit_t2_per_contract = max(entry - recommended_t2, 0.0) if profit_target is None else max(entry - tp_used, 0.0)
        risk_points_per_contract = max(sl_used - entry, 0.0)

    payload = {
        "symbol": symbol,
        "style": style,
        "contracts": int(contracts),
        "underlying_price": float(first_price),
        "momentum": momentum,
        "recommended": {
            "action": str(plan["action"]),
            "occ": str(best_overall["occ"]),
            "side": side,
            "strike": float(best_overall["strike"]),
            "expiration": best_overall_exp.strftime("%Y-%m-%d"),
            "entry": float(entry),
            "entry_label": "bid" if style == "credit" else "ask",
            "target_1": float(tp_used),                   # TP used (override or recommended)
            "target_2": float(recommended_t2),            # still show script's second target for reference
            "target_label": str(plan["target_label"]),
            "stop_loss": float(sl_used),                  # SL used (override or recommended)
            "time_exit": str(plan["time_exit"]),
            "confidence_score": float(best_overall["confidence_score"]),
            "confidence_level": str(best_overall["confidence_level"]),
            "score": float(best_overall["day_trade_score"]),
            "delta": float(best_overall["delta"]),
            "iv": float(best_overall["iv"]),
            "bid": float(best_overall["bid"]),
            "ask": float(best_overall["ask"]),
            "risk": {
                "risk_points_per_contract": float(risk_points_per_contract),
                "risk_points_total": float(risk_points_per_contract * contracts),
                "risk_real_total": float(risk_points_per_contract * contracts * 100.0),
                "profit_points_t1_total": float(profit_t1_per_contract * contracts),
                "profit_real_t1_total": float(profit_t1_per_contract * contracts * 100.0),
                "profit_points_t2_total": float(profit_t2_per_contract * contracts),
                "profit_real_t2_total": float(profit_t2_per_contract * contracts * 100.0),
            },
            "overrides": {
                "profit_target": (float(profit_target) if profit_target is not None else None),
                "stop_loss": (float(stop_loss) if stop_loss is not None else None),
            },
        },
    }
    return _round_floats(payload, 2)


# =========================
# Bot entrypoint
# =========================
def run_guru_pick_4exp_bots(db, bots: List[Any], logger: Any) -> None:
    """
    Bot runner handler used by option_bots_runner.py
    - Creates one SINGLE-leg trade per bot if there is no OPEN trade.
    - Stores TP/SL as option-price points (NOT percent), so manage_open_trades can close on price.
    """
    from sqlalchemy.orm import Session
    from app.models.paper_option_trading_bot import PaperOptionBotOpenTrade

    if bots is None:
        return

    for bot in bots:
        try:
            # Skip if already has an open trade
            existing = (
                db.query(PaperOptionBotOpenTrade)
                .filter(PaperOptionBotOpenTrade.bot_id == bot.id)
                .filter(PaperOptionBotOpenTrade.status == "OPEN")
                .first()
            )
            if existing:
                continue

            params = getattr(bot, "algo_params", None) or {}
            style = str(params.get("style") or "credit").lower()
            min_premium = float(params.get("min_premium", 0.20))
            max_premium = float(params.get("max_premium", 100.00))
            contracts = int(params.get("contracts") or getattr(bot, "trade_size", 1) or 1)

            # Optional overrides:
            profit_target = params.get("profit_target", None)
            stop_loss = params.get("stop_loss", None)
            profit_target = float(profit_target) if profit_target is not None and str(profit_target) != "" else None
            stop_loss = float(stop_loss) if stop_loss is not None and str(stop_loss) != "" else None

            result = analyze_symbol(
                symbol=str(bot.symbol),
                style=style,
                contracts=contracts,
                min_premium=min_premium,
                max_premium=max_premium,
                strategy="auto",
                expires=4,
                top=8,
                profit_target=profit_target,
                stop_loss=stop_loss,
            )
            rec = result["recommended"]

            position_side = "sell" if style == "credit" else "buy"

            trade = PaperOptionBotOpenTrade(
                bot_id=bot.id,
                user_id=bot.user_id,
                underlying_symbol=str(bot.symbol).upper(),
                trade_type="SINGLE",
                position_side=position_side,
                quantity=int(contracts),
                entry_price=float(rec["entry"]),
                option_symbol=str(rec["occ"]),
                side=str(rec["side"]),
                strike_price=float(rec["strike"]),
                expiry_date=datetime.strptime(str(rec["expiration"]), "%Y-%m-%d"),
                planned_take_profit=float(rec["target_1"]),
                planned_stop_loss=float(rec["stop_loss"]),
                algo_name=getattr(bot, "algo_name", "guru_pick_4exp"),
                interval=getattr(bot, "interval", None),
                status="OPEN",
            )

            db.add(trade)
            bot.status = f"TRADE OPEN: {rec['action']} {rec['strike']} exp {rec['expiration']}"
            db.commit()

        except Exception as e:
            try:
                bot.status = f"ERROR: {e}"
                db.commit()
            except Exception:
                db.rollback()
            logger.exception(f"[guru_pick_4exp] Bot {getattr(bot,'id',None)} failed: {e}")


# =========================
# CLI
# =========================
def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Options Guru (Schwab) — scan next expirations")
    p.add_argument("symbol", nargs="?", default="SPY", help="Underlying symbol (default: SPY)")
    p.add_argument("--style", choices=["credit", "debit"], default="credit", help="credit=SELL premium, debit=BUY premium")
    p.add_argument("--contracts", type=int, default=1, help="Number of contracts (default: 1)")
    p.add_argument("--min-premium", type=float, default=0.20, help="Min premium (entry) (default: 0.20)")
    p.add_argument("--max-premium", type=float, default=100.00, help="Max premium (entry) (default: 100.00)")
    p.add_argument("--profit-target", type=float, default=None, help="Override TP (option price points).")
    p.add_argument("--stop-loss", type=float, default=None, help="Override SL (option price points).")
    p.add_argument("--json", action="store_true", help="Print JSON payload only (rounded to 2 decimals).")
    return p


def main() -> int:
    parser = build_arg_parser()
    args, unknown = parser.parse_known_args()
    if unknown:
        logger.warning(f"Ignoring unknown CLI args: {unknown}")

    try:
        payload = analyze_symbol(
            symbol=str(args.symbol),
            style=str(args.style),
            contracts=int(args.contracts),
            min_premium=float(args.min_premium),
            max_premium=float(args.max_premium),
            profit_target=(float(args.profit_target) if args.profit_target is not None else None),
            stop_loss=(float(args.stop_loss) if args.stop_loss is not None else None),
        )
    except Exception as e:
        print(f"❌ {e}")
        return 1

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    # Human-friendly summary (2 decimals)
    rec = payload["recommended"]
    print("\n" + "=" * 60)
    print(f"DAY TRADE OPTIONS DASHBOARD for {payload['symbol']}".center(60))
    print("=" * 60)
    print(f"Symbol:            {payload['symbol']}")
    print(f"Underlying Price:  ${payload['underlying_price']:.2f}")
    print(f"Trade Style:       {payload['style'].upper()}")
    print(f"Contracts:         {payload['contracts']}")
    print("\n" + "=" * 60)
    print("🎯 RECOMMENDED TRADE (BEST OVERALL) 🎯".center(60))
    print("=" * 60)
    print(f"ACTION:      {rec['action']}")
    print(f"STRIKE:      ${rec['strike']:.2f}")
    print(f"EXPIRATION:  {rec['expiration']}")
    print("=" * 60)

    print("\n📊 ENTRY & EXIT LEVELS:")
    print(f"Entry:            ${rec['entry']:.2f} ({rec['entry_label']})")
    print(f"Target 1:         ${rec['target_1']:.2f} ({rec['target_label']})")
    print(f"Target 2:         ${rec['target_2']:.2f} ({rec['target_label']})")
    print(f"Stop Loss:        ${rec['stop_loss']:.2f}")
    print(f"Time Exit:        {rec['time_exit']}")

    print("\n📈 CONFIDENCE & RISK:")
    print(f"Confidence Score: {rec['confidence_score']:.2f}/100")
    print(f"Confidence Level: {rec['confidence_level']}")
    print(f"Score:            {rec['score']:.2f}/100")
    print(f"Total Risk (real): ${rec['risk']['risk_real_total']:.2f}")

    print("\n📊 OPTION DETAILS:")
    print(f"Delta:            {abs(rec['delta']):.2f}")
    print(f"IV:               {rec['iv']:.2f}%")
    print(f"Bid/Ask:          ${rec['bid']:.2f}/${rec['ask']:.2f}")

    print("\n" + "=" * 60)
    print("Analysis complete at", _now_et().strftime("%Y-%m-%d %H:%M:%S %Z"))
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
