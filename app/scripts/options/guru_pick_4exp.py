#!/usr/bin/env python3
"""
FIXED guru_pick_4exp.py — Picks best options from next 4 expirations
Auto-selects best option based on scoring with max premium $20
"""

from __future__ import annotations

import os
import sys
import json
import asyncio
import logging
import importlib
import importlib.util
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import List, Optional, Tuple, Dict, Any
from collections import deque

import pandas as pd
import pandas_ta as ta
import requests
import pytz
from dotenv import load_dotenv

# -----------------------------------------------------------------------------
# Ensure StockWicks project root is on sys.path
# -----------------------------------------------------------------------------
_THIS_FILE = os.path.abspath(__file__)
_THIS_DIR = os.path.dirname(_THIS_FILE)
PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "../../../"))

if not (os.path.isdir(PROJECT_ROOT) and os.path.isdir(os.path.join(PROJECT_ROOT, "app"))):
    cur = _THIS_DIR
    for _ in range(8):
        if os.path.isdir(os.path.join(cur, "app")):
            PROJECT_ROOT = cur
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent

if os.path.isdir(PROJECT_ROOT) and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# === CONFIG & LOGGING ===================================
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(f"/tmp/guru_pick_{datetime.now().strftime('%Y%m%d')}.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

SCHWAB_API_URL = "https://api.schwabapi.com/marketdata/v1"
MAX_PREMIUM = 20.0  # Hard cap at $20

# === PERFORMANCE TRACKING ===============================
class PerformanceTracker:
    """Track real-time performance for adaptive adjustments"""

    def __init__(self, max_trades: int = 50):
        self.wins = 0
        self.losses = 0
        self.pnl_history = deque(maxlen=max_trades)
        self.win_streak = 0
        self.loss_streak = 0
        self.avg_win = 0.0
        self.avg_loss = 0.0
        self.max_loss = 0.0
        self.max_win = 0.0

    def add_trade(self, pnl: float) -> None:
        """Add trade result and update statistics"""
        self.pnl_history.append(pnl)

        if pnl > 0:
            self.wins += 1
            self.win_streak += 1
            self.loss_streak = 0
            if self.wins == 1:
                self.avg_win = pnl
            else:
                self.avg_win = (self.avg_win * (self.wins - 1) + pnl) / self.wins
            self.max_win = max(self.max_win, pnl)
        else:
            self.losses += 1
            self.loss_streak += 1
            self.win_streak = 0
            if self.losses == 1:
                self.avg_loss = abs(pnl)
            else:
                self.avg_loss = (self.avg_loss * (self.losses - 1) + abs(pnl)) / self.losses
            self.max_loss = max(self.max_loss, abs(pnl))

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0.0

    @property
    def profit_factor(self) -> float:
        total_wins = sum(p for p in self.pnl_history if p > 0)
        total_losses = abs(sum(p for p in self.pnl_history if p < 0))
        return total_wins / total_losses if total_losses > 0 else 1.0

    @property
    def expectancy(self) -> float:
        if self.wins + self.losses == 0:
            return 0.0
        return (self.win_rate * self.avg_win) - ((1 - self.win_rate) * self.avg_loss)

    def get_risk_multiplier(self) -> float:
        """Adjust risk based on recent performance"""
        if len(self.pnl_history) < 5:
            return 1.0

        if self.loss_streak >= 3:
            return 0.5
        if self.loss_streak == 2:
            return 0.7

        if self.win_streak >= 3 and self.profit_factor > 1.5:
            return 1.2
        if self.win_streak >= 2 and self.profit_factor > 1.2:
            return 1.1

        return 1.0


performance_tracker = PerformanceTracker()

# === HELPERS ============================================

_TOKEN_GETTER = None  # cached callable


def _load_token_getter_from_repo(project_root: str):
    """Fallback loader for token getter"""
    app_dir = Path(project_root) / "app"
    if not app_dir.exists():
        return None

    candidates: List[Path] = []
    for name in ["schwab_token.py", "schwab_tokens.py", "schwab_auth.py"]:
        p = app_dir / "utils" / "stock" / name
        if p.exists():
            candidates.append(p)
        p = app_dir / "utils" / name
        if p.exists():
            candidates.append(p)
        p = app_dir / name
        if p.exists():
            candidates.append(p)

    if not candidates:
        for p in app_dir.rglob("schwab_token*.py"):
            candidates.append(p)

    for path in candidates[:30]:
        try:
            spec = importlib.util.spec_from_file_location(f"_sw_token_{path.stem}", str(path))
            if not spec or not spec.loader:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            fn = getattr(mod, "get_valid_access_token", None)
            if callable(fn):
                logger.info(f"Loaded token getter from: {path}")
                return fn
        except Exception:
            continue

    return None


def _read_token_from_json_file(path: str) -> str | None:
    try:
        p = Path(path)
        if not p.exists():
            return None
        data = json.loads(p.read_text())
        token = (
            data.get("access_token")
            or data.get("token", {}).get("access_token")
            or data.get("schwab", {}).get("access_token")
        )
        if token and isinstance(token, str) and token.strip():
            return token.strip()
    except Exception:
        return None
    return None


def get_schwab_headers() -> Dict[str, str]:
    """Get Schwab API headers with robust token discovery"""
    global _TOKEN_GETTER

    if _TOKEN_GETTER is None:
        import_attempts = [
            "app.utils.stock.schwab_token",
            "app.utils.schwab_token",
            "app.schwab_token",
            "app.schwab_auth",
        ]
        for modpath in import_attempts:
            try:
                mod = importlib.import_module(modpath)
                fn = getattr(mod, "get_valid_access_token", None)
                if callable(fn):
                    _TOKEN_GETTER = fn
                    logger.info(f"Using token getter from import: {modpath}")
                    break
            except Exception:
                continue

        if _TOKEN_GETTER is None:
            _TOKEN_GETTER = _load_token_getter_from_repo(PROJECT_ROOT)

    if callable(_TOKEN_GETTER):
        try:
            access_token = _TOKEN_GETTER()
            if access_token and isinstance(access_token, str) and access_token.strip():
                return {"Authorization": f"Bearer {access_token.strip()}"}
        except Exception as e:
            logger.warning(f"Token getter failed: {e}")

    token_file = os.getenv("SCHWAB_TOKEN_FILE")
    if token_file:
        tok = _read_token_from_json_file(token_file)
        if tok:
            return {"Authorization": f"Bearer {tok}"}

    for guess in [
        f"{PROJECT_ROOT}/app/data/schwab_token.json",
        f"{PROJECT_ROOT}/app/data/schwab_tokens.json",
        f"{PROJECT_ROOT}/schwab_token.json",
        "/var/www/stockwicks/app/data/schwab_token.json",
    ]:
        tok = _read_token_from_json_file(guess)
        if tok:
            return {"Authorization": f"Bearer {tok}"}

    access_token = os.getenv("SCHWAB_ACCESS_TOKEN")
    if access_token and access_token.strip():
        return {"Authorization": f"Bearer {access_token.strip()}"}

    raise ValueError("Could not get Schwab access token.")


def _now_et() -> datetime:
    return datetime.now(pytz.timezone("US/Eastern"))


def get_schwab_price_history(
    symbol: str,
    periodType: str = "day",
    period: int = 5,
    frequencyType: str = "minute",
    frequency: int = 15,
) -> Optional[pd.DataFrame]:
    """Get price history from Schwab"""
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
        logger.info(f"PriceHistory API [{symbol}] status: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        if not data or "candles" not in data:
            return None
        df = pd.DataFrame(data["candles"])
        df["datetime"] = pd.to_datetime(df["datetime"], unit="ms")
        df.set_index("datetime", inplace=True)
        df.columns = [c.lower() for c in df.columns]
        return df
    except Exception as e:
        logger.error(f"API request failed for {symbol}: {e}")
        return None


# === MARKET CONTEXT =====================================
def get_market_context(symbol: str) -> Dict[str, Any]:
    """Get market context for the symbol"""
    df = get_schwab_price_history(symbol, periodType="day", period=5, frequencyType="minute", frequency=15)
    if df is None or len(df) < 20:
        return {
            "score": 50,
            "trend": "neutral",
            "volatility": "medium",
            "confidence": "low",
            "price": 0.0,
            "rsi": 50.0,
            "atr": 0.015,
        }

    try:
        df.ta.rsi(length=14, append=True)
        df.ta.atr(length=14, append=True)
        df.ta.ema(length=9, append=True)
        df.ta.ema(length=20, append=True)
        df.ta.ema(length=50, append=True)
    except Exception as e:
        logger.error(f"Error calculating indicators: {e}")

    price = float(df["close"].iloc[-1])
    rsi = float(df["RSI_14"].iloc[-1]) if "RSI_14" in df.columns and not pd.isna(df["RSI_14"].iloc[-1]) else 50.0
    atr = float(df["ATRr_14"].iloc[-1]) if "ATRr_14" in df.columns and not pd.isna(df["ATRr_14"].iloc[-1]) else 0.015
    volatility = "high" if atr > 0.025 else ("medium" if atr > 0.015 else "low")

    trend = "neutral"
    trend_score = 50

    if "EMA_9" in df.columns and "EMA_20" in df.columns and "EMA_50" in df.columns:
        ema9 = df["EMA_9"].iloc[-1]
        ema20 = df["EMA_20"].iloc[-1]
        ema50 = df["EMA_50"].iloc[-1]

        if not pd.isna(ema9) and not pd.isna(ema20) and not pd.isna(ema50):
            if ema9 > ema20 > ema50 and price > ema9:
                trend, trend_score = "strong_uptrend", 85
            elif ema9 > ema20 and price > ema9:
                trend, trend_score = "uptrend", 70
            elif ema9 < ema20 < ema50 and price < ema9:
                trend, trend_score = "strong_downtrend", 85
            elif ema9 < ema20 and price < ema9:
                trend, trend_score = "downtrend", 70

    confidence_score = trend_score
    if 40 < rsi < 60:
        confidence_score += 10

    if "volume" in df.columns:
        recent_volume = df["volume"].tail(5).mean()
        avg_volume = df["volume"].mean()
        if recent_volume > avg_volume * 1.5:
            confidence_score += 10

    confidence_score = min(100, max(0, confidence_score))
    confidence = "HIGH" if confidence_score >= 70 else ("MEDIUM" if confidence_score >= 50 else "LOW")

    return {
        "trend": trend,
        "trend_score": trend_score,
        "volatility": volatility,
        "atr": atr,
        "rsi": rsi,
        "confidence_score": confidence_score,
        "confidence": confidence,
        "price": price,
    }


# === EXPIRATION SELECTION ===============================
def get_expiration_dates(symbol: str) -> List[date]:
    """Get all expiration dates for a symbol"""
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


def get_next_expirations(all_dates: List[date], count: int = 4) -> List[date]:
    """Get the next 'count' expiration dates"""
    today = _now_et().date()
    future_dates = [d for d in all_dates if d >= today]
    return future_dates[:count]


# === OPTION CHAIN FETCHING ==============================
def fetch_full_option_chain(symbol: str, expiration_date: date) -> Tuple[pd.DataFrame, Optional[float]]:
    """Fetch option chain for a specific expiration"""
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


# === TRADE ANALYSIS =====================================
def calculate_stop_and_targets_debit(option: pd.Series, entry_price: float, market_context: Dict[str, Any]) -> Dict[str, Any]:
    """Calculate stop loss and targets for debit (BUY) trades"""
    delta = abs(float(option["delta"]))
    iv = float(option["iv"])
    volatility = market_context.get("volatility", "medium")
    
    # Stop loss - tighter for debit trades
    base_stop_pct = 0.25  # 25% loss
    
    if volatility == "high":
        stop_pct = base_stop_pct * 1.2  # Wider stop for high vol
    elif volatility == "low":
        stop_pct = base_stop_pct * 0.8  # Tighter stop for low vol
    else:
        stop_pct = base_stop_pct
    
    # Adjust for delta
    if delta > 0.60:
        stop_pct *= 0.9  # Tighter stop for high delta
    elif delta < 0.30:
        stop_pct *= 1.1  # Wider stop for low delta
    
    stop_price = round(max(0.05, entry_price * (1.0 - stop_pct)), 2)
    max_loss = entry_price - stop_price
    
    # Targets - 20% and 40% profit
    target_1 = round(entry_price * 1.20, 2)
    target_2 = round(entry_price * 1.40, 2)
    
    return {
        "stop_price": stop_price,
        "max_loss_per_contract": max_loss,
        "target_1": target_1,
        "target_2": target_2,
        "target_1_pct": 20.0,
        "target_2_pct": 40.0,
    }


def calculate_stop_and_targets_credit(option: pd.Series, entry_price: float, market_context: Dict[str, Any]) -> Dict[str, Any]:
    """Calculate stop loss and targets for credit (SELL) trades"""
    delta = abs(float(option["delta"]))
    iv = float(option["iv"])
    volatility = market_context.get("volatility", "medium")
    
    # Stop loss - when premium doubles (100% loss)
    base_stop_mult = 2.0
    
    if volatility == "high":
        stop_mult = base_stop_mult * 1.2  # Wider stop for high vol
    elif volatility == "low":
        stop_mult = base_stop_mult * 0.8  # Tighter stop for low vol
    else:
        stop_mult = base_stop_mult
    
    # Adjust for delta
    if delta > 0.40:
        stop_mult *= 0.9  # Tighter stop for high delta
    elif delta < 0.20:
        stop_mult *= 1.1  # Wider stop for low delta
    
    stop_price = round(entry_price * stop_mult, 2)
    max_loss = stop_price - entry_price
    
    # Targets - 50% and 75% profit
    target_1 = round(entry_price * 0.50, 2)
    target_2 = round(entry_price * 0.25, 2)
    
    return {
        "stop_price": stop_price,
        "max_loss_per_contract": max_loss,
        "target_1": target_1,
        "target_2": target_2,
        "target_1_pct": 50.0,
        "target_2_pct": 75.0,
    }


def calculate_position_size(max_loss_per_contract: float, market_context: Dict[str, Any]) -> int:
    """Calculate position size based on risk management"""
    max_risk_per_trade = 100.0 * performance_tracker.get_risk_multiplier()
    
    if max_loss_per_contract <= 0:
        return 1
    
    volatility_factor = {"high": 0.7, "medium": 1.0, "low": 1.3}.get(market_context.get("volatility", "medium"), 1.0)
    confidence_factor = market_context.get("confidence_score", 50) / 100.0
    confidence_factor = max(0.5, min(1.5, confidence_factor))
    
    adjusted_max_risk = max_risk_per_trade * volatility_factor * confidence_factor
    max_contracts = max(1, int(adjusted_max_risk / max_loss_per_contract))
    
    if max_loss_per_contract > 50:
        max_contracts = 1
    elif max_loss_per_contract > 25:
        max_contracts = min(max_contracts, 2)
    elif max_loss_per_contract > 15:
        max_contracts = min(max_contracts, 3)
    
    return max(1, max_contracts)


def calculate_option_score(
    option: pd.Series,
    style: str,
    market_context: Dict[str, Any],
) -> Tuple[float, str]:
    """Calculate a quality score for an option (0-100)"""
    delta_abs = abs(float(option["delta"]))
    iv = float(option["iv"])
    bid = float(option["bid"])
    ask = float(option["ask"])
    volume = int(option.get("volume", 0))
    oi = int(option.get("oi", 0))
    side = str(option["side"])
    trend = market_context.get("trend", "neutral")
    
    # Normalize trend
    if trend in ("strong_uptrend",):
        trend_norm = "uptrend"
    elif trend in ("strong_downtrend",):
        trend_norm = "downtrend"
    else:
        trend_norm = trend
    
    # Entry price
    entry_price = bid if style == "credit" else ask
    
    # Premium score (cheaper is better, but not too cheap)
    if entry_price <= 1.0:
        premium_score = 20
    elif entry_price <= 2.0:
        premium_score = 25
    elif entry_price <= 3.0:
        premium_score = 20
    elif entry_price <= 5.0:
        premium_score = 15
    elif entry_price <= 10.0:
        premium_score = 10
    else:
        premium_score = 5
    
    # Spread quality
    spread = ask - bid
    spread_pct = spread / ((bid + ask) / 2) if bid > 0 and ask > 0 else 0
    if spread_pct <= 0.05:
        spread_score = 20
    elif spread_pct <= 0.10:
        spread_score = 15
    elif spread_pct <= 0.15:
        spread_score = 10
    elif spread_pct <= 0.20:
        spread_score = 5
    else:
        spread_score = 0
    
    # Liquidity score
    volume_score = min(volume / 500.0, 1.0) * 10
    oi_score = min(oi / 1000.0, 1.0) * 10
    liquidity_score = volume_score + oi_score
    
    # Delta score
    if style == "credit":
        if 0.20 <= delta_abs <= 0.35:
            delta_score = 20
        elif 0.15 <= delta_abs <= 0.40:
            delta_score = 15
        elif 0.10 <= delta_abs <= 0.45:
            delta_score = 10
        else:
            delta_score = 5
    else:  # debit
        if 0.35 <= delta_abs <= 0.65:
            delta_score = 20
        elif 0.30 <= delta_abs <= 0.70:
            delta_score = 15
        elif 0.25 <= delta_abs <= 0.75:
            delta_score = 10
        else:
            delta_score = 5
    
    # IV score
    if style == "credit":
        if iv >= 50:
            iv_score = 15
        elif iv >= 35:
            iv_score = 12
        elif iv >= 25:
            iv_score = 8
        else:
            iv_score = 5
    else:  # debit
        if 25 <= iv <= 60:
            iv_score = 15
        elif 20 <= iv <= 80:
            iv_score = 12
        elif 15 <= iv <= 100:
            iv_score = 8
        else:
            iv_score = 5
    
    # Trend alignment
    if style == "credit":
        if (trend_norm == "uptrend" and side == "put") or (trend_norm == "downtrend" and side == "call"):
            alignment_score = 15
        elif trend_norm == "neutral":
            alignment_score = 10
        else:
            alignment_score = 5
    else:  # debit
        if (trend_norm == "uptrend" and side == "call") or (trend_norm == "downtrend" and side == "put"):
            alignment_score = 15
        elif trend_norm == "neutral":
            alignment_score = 10
        else:
            alignment_score = 5
    
    total_score = premium_score + spread_score + liquidity_score + delta_score + iv_score + alignment_score
    
    if total_score >= 80:
        confidence = "HIGH"
    elif total_score >= 65:
        confidence = "MEDIUM-HIGH"
    elif total_score >= 50:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"
    
    return min(100, total_score), confidence


# === MAIN FILTERING FUNCTION ============================
def find_best_options(
    options_df: pd.DataFrame,
    market_context: Dict[str, Any],
    style: str,
    max_premium: float = 20.0,
) -> pd.DataFrame:
    """Find the best options based on scoring with premium cap"""
    if options_df.empty:
        return pd.DataFrame()
    
    rows: List[Dict[str, Any]] = []
    
    for _, opt in options_df.iterrows():
        bid = float(opt["bid"])
        ask = float(opt["ask"])
        
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        
        entry_price = bid if style == "credit" else ask
        
        # Apply premium cap
        if entry_price > max_premium:
            continue
        
        # Basic liquidity check
        volume = int(opt.get("volume", 0))
        oi = int(opt.get("oi", 0))
        if volume < 10 and oi < 50:
            continue
        
        # Calculate score
        score, confidence = calculate_option_score(opt, style, market_context)
        
        # Skip low scoring options
        if score < 50:
            continue
        
        # Calculate stop and targets based on style
        if style == "credit":
            stop_targets = calculate_stop_and_targets_credit(opt, entry_price, market_context)
        else:
            stop_targets = calculate_stop_and_targets_debit(opt, entry_price, market_context)
        
        # Calculate position size
        position_size = calculate_position_size(stop_targets["max_loss_per_contract"], market_context)
        
        # Calculate risk/reward
        if style == "credit":
            reward = entry_price - stop_targets["target_1"]
        else:
            reward = stop_targets["target_1"] - entry_price
        
        rr_ratio = reward / stop_targets["max_loss_per_contract"] if stop_targets["max_loss_per_contract"] > 0 else 0
        
        rows.append({
            "side": str(opt["side"]),
            "strike": float(opt["strike"]),
            "bid": bid,
            "ask": ask,
            "entry_price": entry_price,
            "delta": float(opt["delta"]),
            "iv": float(opt["iv"]),
            "oi": oi,
            "volume": volume,
            "quality_score": score,
            "confidence": confidence,
            "stop_price": stop_targets["stop_price"],
            "target_1": stop_targets["target_1"],
            "target_2": stop_targets["target_2"],
            "target_1_pct": stop_targets["target_1_pct"],
            "target_2_pct": stop_targets["target_2_pct"],
            "max_loss_per_contract": stop_targets["max_loss_per_contract"],
            "position_size": position_size,
            "rr_ratio": rr_ratio,
        })
    
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    
    return out.sort_values("quality_score", ascending=False)


# === MAIN BOT RUNNER ====================================
async def run_guru_pick_4exp_bots(bot_params: Dict[str, Any]) -> Dict[str, Any]:
    """Main function called by the bot runner"""
    try:
        symbol = str(bot_params.get("symbol", "SPY")).upper()
        style = str(bot_params.get("style", "debit")).lower()  # Default to debit (BUY)
        expires = int(bot_params.get("expires", 4))
        
        # Use provided max_premium or default to $20
        max_premium = float(bot_params.get("max_premium", 20.0))
        
        logger.info(f"Running guru_pick_4exp for {symbol} (style: {style}, max_premium: ${max_premium})")
        
        # Get market context
        market_context = get_market_context(symbol)
        logger.info(f"Market context: {market_context.get('trend')}, vol: {market_context.get('volatility')}")
        
        # Get expiration dates
        all_exp = get_expiration_dates(symbol)
        if not all_exp:
            return {"success": False, "error": "No expiration dates available"}
        
        # Get next 'expires' expirations
        scan_dates = get_next_expirations(all_exp, expires)
        today = _now_et().date()
        
        logger.info(f"Scanning {len(scan_dates)} expirations: {[d.strftime('%Y-%m-%d') for d in scan_dates]}")
        
        results_by_expiration: Dict[str, Any] = {}
        all_candidates = []
        
        # Scan each expiration
        for exp_date in scan_dates:
            dte = (exp_date - today).days
            logger.info(f"Scanning {exp_date} ({dte} DTE)")
            
            opt_df, price = fetch_full_option_chain(symbol, exp_date)
            if opt_df.empty:
                logger.warning(f"No options data for {exp_date}")
                continue
            
            # Find best options
            candidates = find_best_options(opt_df, market_context, style, max_premium)
            
            if candidates.empty:
                logger.info(f"No quality candidates for {exp_date}")
                continue
            
            # Add expiration info
            candidates['expiration'] = exp_date.strftime('%Y-%m-%d')
            candidates['dte'] = dte
            
            # Store top 5 per expiration
            top_candidates = candidates.head(5).to_dict('records')
            results_by_expiration[exp_date.strftime('%Y-%m-%d')] = {
                "dte": dte,
                "underlying_price": float(price) if price else 0,
                "candidates": top_candidates
            }
            
            # Add to all candidates for overall ranking
            all_candidates.extend(top_candidates)
        
        if not all_candidates:
            return {
                "success": False,
                "error": f"No quality options found within ${max_premium} premium limit",
                "market_analysis": market_context
            }
        
        # Sort all candidates by quality score
        all_candidates.sort(key=lambda x: float(x["quality_score"]), reverse=True)
        
        # Prepare response
        resp: Dict[str, Any] = {
            "success": True,
            "symbol": symbol,
            "style": style,
            "max_premium": max_premium,
            "timestamp": _now_et().isoformat(),
            "market_analysis": market_context,
            "expirations_scanned": [d.strftime("%Y-%m-%d") for d in scan_dates],
            "results_by_expiration": results_by_expiration,
            "top_picks": all_candidates[:8],  # Top 8 overall
            "best_overall": all_candidates[0] if all_candidates else None,
        }
        
        # Log summary
        logger.info(f"Found {len(all_candidates)} total candidates")
        if all_candidates:
            best = all_candidates[0]
            logger.info(f"Best option: {best['side'].upper()} ${best['strike']:.2f} @ ${best['entry_price']:.2f} (score: {best['quality_score']:.1f})")
        
        return resp
        
    except Exception as e:
        logger.error(f"Error running guru_pick_4exp bot: {e}", exc_info=True)
        return {"success": False, "error": str(e), "timestamp": _now_et().isoformat()}


# === CLI ================================================
def _run_cli() -> int:
    """CLI entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Options Guru - Picks best from next 4 expirations")
    parser.add_argument("symbol", nargs="?", default="SPY", help="Underlying symbol")
    parser.add_argument("--style", choices=["credit", "debit"], default="debit", help="debit=BUY, credit=SELL")
    parser.add_argument("--expires", type=int, default=4, help="Number of expirations to scan")
    parser.add_argument("--max-premium", type=float, default=20.0, help="Maximum premium (default: $20)")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    
    args = parser.parse_args()
    
    bot_params = {
        "symbol": args.symbol.upper(),
        "style": args.style,
        "expires": args.expires,
        "max_premium": args.max_premium,
    }
    
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(run_guru_pick_4exp_bots(bot_params))
    finally:
        loop.close()
    
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result.get("success") else 1
    
    # Pretty print results
    if not result.get("success"):
        print(f"❌ Error: {result.get('error', 'Unknown error')}")
        return 1
    
    print("\n" + "="*80)
    print(f"OPTIONS GURU - {result['symbol']} (max premium: ${result['max_premium']})".center(80))
    print("="*80)
    
    market = result['market_analysis']
    print(f"\n📊 MARKET ANALYSIS:")
    print(f"   Trend: {market.get('trend', 'N/A').upper()}")
    print(f"   Volatility: {market.get('volatility', 'N/A').upper()}")
    print(f"   Confidence: {market.get('confidence', 'N/A')}")
    print(f"   RSI: {market.get('rsi', 0):.1f}")
    
    print(f"\n🎯 TOP PICKS (across all expirations):")
    print("-"*80)
    print(f"{'#':<3} {'Action':<10} {'Strike':<8} {'Premium':<8} {'Delta':<6} {'DTE':<4} {'Score':<6} {'Confidence':<12} {'Stop':<6} {'Target1':<7}")
    print("-"*80)
    
    for i, pick in enumerate(result.get('top_picks', [])[:8]):
        action = f"{'BUY' if args.style == 'debit' else 'SELL'} {pick['side'].upper()}"
        print(f"{i+1:<3} {action:<10} ${pick['strike']:<7.2f} ${pick['entry_price']:<7.2f} {abs(pick['delta']):<6.2f} {pick.get('dte', 0):<4} {pick['quality_score']:<6.1f} {pick['confidence']:<12} ${pick['stop_price']:<6.2f} ${pick['target_1']:<7.2f}")
    
    if result.get('best_overall'):
        best = result['best_overall']
        print("\n" + "="*80)
        print("🏆 BEST OVERALL TRADE 🏆".center(80))
        print("="*80)
        print(f"Action: {'BUY' if args.style == 'debit' else 'SELL'} {best['side'].upper()} ${best['strike']:.2f}")
        print(f"Expiration: {best.get('expiration', 'N/A')} ({best.get('dte', 0)} DTE)")
        print(f"Entry: ${best['entry_price']:.2f}")
        print(f"Stop Loss: ${best['stop_price']:.2f}")
        print(f"Target 1 ({best['target_1_pct']:.0f}%): ${best['target_1']:.2f}")
        print(f"Target 2 ({best['target_2_pct']:.0f}%): ${best['target_2']:.2f}")
        print(f"Max Loss: ${best['max_loss_per_contract']:.2f}/contract")
        print(f"Position Size: {best['position_size']} contracts")
        print(f"Risk/Reward: {best['rr_ratio']:.2f}")
        print(f"Quality Score: {best['quality_score']:.1f}/100 ({best['confidence']})")
    
    print("\n" + "="*80)
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_cli())