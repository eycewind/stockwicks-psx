#!/usr/bin/env python3
"""
IMPROVED guru_pick_4exp.py — Optimized Options "Guru" Strategy (Scanner/Planner)

FIXED VERSION - Combines working logic with proper scoring + robust Schwab token discovery.

What was fixed vs your version:
1) Token loading:
   - Tries multiple import paths for get_valid_access_token()
   - If imports fail, scans the repo for schwab_token*.py and loads dynamically
   - Optional SCHWAB_TOKEN_FILE JSON support
   - Env var fallback: SCHWAB_ACCESS_TOKEN

2) Trend alignment scoring:
   - Treats strong_uptrend/strong_downtrend as uptrend/downtrend for scoring alignment.

Script path expectation:
  /var/www/stockwicks/app/scripts/options/guru_pick_4exp.py
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
from datetime import datetime, date
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

# Script lives at: <repo>/app/scripts/options/guru_pick_4exp.py
# We need <repo> on sys.path so `import app...` works.
PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "../../../"))

# Fallback: walk upwards a few levels and pick the first dir that contains an `app/` folder.
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

        # Reduce risk during losing streaks
        if self.loss_streak >= 3:
            return 0.5
        if self.loss_streak == 2:
            return 0.7

        # Increase risk modestly during winning streaks
        if self.win_streak >= 3 and self.profit_factor > 1.5:
            return 1.2
        if self.win_streak >= 2 and self.profit_factor > 1.2:
            return 1.1

        return 1.0


performance_tracker = PerformanceTracker()

# === HELPERS ============================================

_TOKEN_GETTER = None  # cached callable


def _load_token_getter_from_repo(project_root: str):
    """
    Fallback loader:
    - searches <repo>/app for a schwab_token*.py file
    - loads it via importlib even if packages/__init__.py are missing
    - returns get_valid_access_token if present
    """
    app_dir = Path(project_root) / "app"
    if not app_dir.exists():
        return None

    # common filenames first, then any schwab_token*.py
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

    # broader search (bounded)
    if not candidates:
        for p in app_dir.rglob("schwab_token*.py"):
            candidates.append(p)

    for path in candidates[:30]:
        try:
            spec = importlib.util.spec_from_file_location(f"_sw_token_{path.stem}", str(path))
            if not spec or not spec.loader:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[attr-defined]
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
        # allow a few common shapes
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
    """
    Get Schwab API headers with robust token discovery.

    Priority:
      1) token helper import (multiple known paths)
      2) repo scan + dynamic load of schwab_token*.py
      3) SCHWAB_TOKEN_FILE JSON
      4) common token JSON paths
      5) SCHWAB_ACCESS_TOKEN env var
    """
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

    # 1/2) Use token getter if available
    if callable(_TOKEN_GETTER):
        try:
            access_token = _TOKEN_GETTER()
            if access_token and isinstance(access_token, str) and access_token.strip():
                return {"Authorization": f"Bearer {access_token.strip()}"}
        except Exception as e:
            logger.warning(f"Token getter failed: {e}")

    # 3) SCHWAB_TOKEN_FILE JSON
    token_file = os.getenv("SCHWAB_TOKEN_FILE")
    if token_file:
        tok = _read_token_from_json_file(token_file)
        if tok:
            return {"Authorization": f"Bearer {tok}"}

    # 4) common token paths (safe optional)
    for guess in [
        f"{PROJECT_ROOT}/app/data/schwab_token.json",
        f"{PROJECT_ROOT}/app/data/schwab_tokens.json",
        f"{PROJECT_ROOT}/schwab_token.json",
        "/var/www/stockwicks/app/data/schwab_token.json",
        "/var/www/stockwicks/app/data/schwab_tokens.json",
    ]:
        tok = _read_token_from_json_file(guess)
        if tok:
            return {"Authorization": f"Bearer {tok}"}

    # 5) env var fallback
    access_token = os.getenv("SCHWAB_ACCESS_TOKEN")
    if access_token and access_token.strip():
        return {"Authorization": f"Bearer {access_token.strip()}"}

    raise ValueError(
        "Could not get Schwab access token. Fix options:\n"
        "1) Ensure your token helper exists and is importable (recommended: add __init__.py to app/utils and app/utils/stock)\n"
        "2) Set SCHWAB_ACCESS_TOKEN in your environment\n"
        "3) Set SCHWAB_TOKEN_FILE to a JSON file containing {\"access_token\": \"...\"}\n"
    )


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
        logger.info(
            f"Historical PriceHistory API [{symbol} - {frequencyType}:{frequency}] status: {resp.status_code}"
        )
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
        return {
            "score": 50,
            "trend": "neutral",
            "volatility": "medium",
            "confidence": "low",
            "price": float(df["close"].iloc[-1]) if len(df) > 0 else 0.0,
            "rsi": 50.0,
            "atr": 0.015,
        }

    price = float(df["close"].iloc[-1])

    # Calculate RSI
    rsi = float(df["RSI_14"].iloc[-1]) if "RSI_14" in df.columns and not pd.isna(df["RSI_14"].iloc[-1]) else 50.0

    # Calculate ATR and volatility
    atr = float(df["ATRr_14"].iloc[-1]) if "ATRr_14" in df.columns and not pd.isna(df["ATRr_14"].iloc[-1]) else 0.015
    volatility = "high" if atr > 0.025 else ("medium" if atr > 0.015 else "low")

    # Determine trend
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

    # Calculate confidence score
    confidence_score = trend_score

    # Adjust based on RSI
    if 40 < rsi < 60:
        confidence_score += 10

    # Volume analysis
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


def choose_expirations(
    all_dates: List[date],
    expires: int,
    hold_hours: Optional[int],
    hold_days: Optional[int],
    symbol: str,
) -> List[date]:
    """Choose which expirations to scan"""
    today = _now_et().date()
    now = _now_et()

    if hold_hours is not None:
        # Intraday: use near-term expirations
        if now.hour < 12:
            near = [d for d in all_dates if 0 <= (d - today).days <= 2]
        else:
            near = [d for d in all_dates if 1 <= (d - today).days <= 7]
        return (near if near else all_dates)[:expires]

    if hold_days is not None:
        # Swing trade: match expiration to hold time
        min_dte = max(3, hold_days + 2)
        candidates = [d for d in all_dates if (d - today).days >= min_dte]
        preferred = [d for d in candidates if 7 <= (d - today).days <= 21]
        if preferred:
            return preferred[:expires]
        fallback = [d for d in candidates if 5 <= (d - today).days <= 35]
        return (fallback if fallback else candidates)[:expires]

    # Default: mix based on volatility
    df = get_schwab_price_history(symbol, periodType="day", period=5, frequencyType="minute", frequency=15)
    if df is not None and len(df) > 20:
        returns = df["close"].pct_change().dropna()
        volatility = returns.std() * (252 ** 0.5)
        if volatility > 0.4:
            selected = [d for d in all_dates if 3 <= (d - today).days <= 14]
        else:
            selected = [d for d in all_dates if 0 <= (d - today).days <= 7]
    else:
        selected = [d for d in all_dates if 0 <= (d - today).days <= 7]

    return (selected if selected else all_dates)[:expires]


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

        # Get underlying price
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
def calculate_position_size(entry_price: float, max_loss_per_contract: float, market_context: Dict[str, Any], style: str) -> int:
    """Calculate position size based on risk management"""
    max_risk_per_trade = 100.0 * performance_tracker.get_risk_multiplier()

    if max_loss_per_contract <= 0:
        return 1

    # Adjust based on volatility
    volatility_factor = {"high": 0.7, "medium": 1.0, "low": 1.3}.get(market_context.get("volatility", "medium"), 1.0)

    # Adjust based on confidence
    confidence_factor = market_context.get("confidence_score", 50) / 100.0
    confidence_factor = max(0.5, min(1.5, confidence_factor))

    adjusted_max_risk = max_risk_per_trade * volatility_factor * confidence_factor
    max_contracts = max(1, int(adjusted_max_risk / max_loss_per_contract))

    # Additional constraints
    if max_loss_per_contract > 50:
        max_contracts = 1
    elif max_loss_per_contract > 25:
        max_contracts = min(max_contracts, 2)
    elif max_loss_per_contract > 15:
        max_contracts = min(max_contracts, 3)

    if style == "debit":
        max_contracts = max(1, int(max_contracts * 0.8))

    return max(1, max_contracts)


def calculate_stop_and_targets(
    option: pd.Series,
    style: str,
    entry_price: float,
    market_context: Dict[str, Any],
) -> Dict[str, Any]:
    """Calculate stop loss and targets"""
    delta = abs(float(option["delta"]))
    iv = float(option["iv"])
    volatility = market_context.get("volatility", "medium")
    trend_score = market_context.get("trend_score", 50)

    if style == "credit":
        # Credit: stop is above entry (since we're selling)
        base_mult = 1.50
        iv_factor = 1.4 if iv > 70 else (1.3 if iv > 50 else (1.1 if iv > 30 else 1.0))
        delta_factor = 1.4 if delta > 0.40 else (1.2 if delta > 0.30 else (1.0 if delta > 0.20 else 0.9))
        vol_factor = {"high": 1.4, "medium": 1.1, "low": 0.9}.get(volatility, 1.1)
        trend_factor = 0.9 if trend_score > 70 else (1.2 if trend_score < 40 else 1.0)

        stop_mult = max(1.25, min(base_mult * iv_factor * delta_factor * vol_factor * trend_factor, 2.5))
        stop_price = round(entry_price * stop_mult, 2)
        max_loss = stop_price - entry_price

        # Targets (buy back prices)
        target_1 = round(entry_price * 0.90, 2)
        target_2 = round(entry_price * 0.80, 2)
        target_1_pct = round((entry_price - target_1) / entry_price * 100, 1)
        target_2_pct = round((entry_price - target_2) / entry_price * 100, 1)
    else:
        # Debit: stop is below entry (since we're buying)
        base_pct = 0.22 if volatility == "high" else (0.14 if volatility == "low" else 0.17)
        delta_factor = 1.2 if delta > 0.50 else (1.1 if delta > 0.40 else (1.0 if delta > 0.30 else 0.9))
        confidence_factor = max(0.8, min(1.2, market_context.get("confidence_score", 50) / 50.0))

        stop_pct = max(0.10, min(base_pct * delta_factor * confidence_factor, 0.30))
        stop_price = round(max(0.05, entry_price * (1.0 - stop_pct)), 2)
        max_loss = entry_price - stop_price

        # Targets (sell prices)
        target_1 = round(entry_price * 1.25, 2)
        target_2 = round(entry_price * 1.50, 2)
        target_1_pct = round((target_1 - entry_price) / entry_price * 100, 1)
        target_2_pct = round((target_2 - entry_price) / entry_price * 100, 1)

    # Ensure minimum stop distance
    min_stop_distance = 0.10 if entry_price > 1.0 else 0.05
    if style == "credit" and (stop_price - entry_price) < min_stop_distance:
        stop_price = round(entry_price + min_stop_distance, 2)
        max_loss = stop_price - entry_price
    elif style == "debit" and (entry_price - stop_price) < min_stop_distance:
        stop_price = round(entry_price - min_stop_distance, 2)
        max_loss = entry_price - stop_price

    return {
        "stop_price": stop_price,
        "max_loss_per_contract": max_loss,
        "max_loss_per_contract_real": max_loss * 100.0,
        "target_1": target_1,
        "target_2": target_2,
        "target_1_pct": target_1_pct,
        "target_2_pct": target_2_pct,
        "stop_type": "adaptive",
    }


def calculate_option_score(
    option: pd.Series,
    style: str,
    market_context: Dict[str, Any],
    strategy: str,
    trend: str,
) -> Tuple[float, str]:
    """Calculate a quality score for an option (0-100)"""
    delta_abs = abs(float(option["delta"]))
    iv = float(option["iv"])
    bid = float(option["bid"])
    ask = float(option["ask"])
    entry_price = bid if style == "credit" else ask
    volume = int(option.get("volume", 0))
    oi = int(option.get("oi", 0))

    # Spread quality
    spread = ask - bid
    spread_pct = spread / ((bid + ask) / 2) if bid > 0 and ask > 0 else 0
    spread_score = max(0, 1.0 - min(spread_pct / 0.10, 1.0)) * 20

    # Liquidity score
    volume_score = min(volume / 500.0, 1.0) * 15
    oi_score = min(oi / 1000.0, 1.0) * 10
    liquidity_score = volume_score + oi_score

    # Greeks score
    if style == "credit":
        # Credit: prefer delta 0.20-0.45
        delta_ideal = 0.30
        delta_score = max(0, 1.0 - abs(delta_abs - delta_ideal) / 0.20) * 20

        # Credit: prefer higher IV
        iv_score = min(max((iv - 25) / 50, 0), 1.0) * 15
    else:
        # Debit: prefer delta 0.35-0.65
        delta_ideal = 0.50
        delta_score = max(0, 1.0 - abs(delta_abs - delta_ideal) / 0.20) * 20

        # Debit: moderate IV is best
        if iv < 20:
            iv_score = 0.3 * 15
        elif iv <= 50:
            iv_score = 1.0 * 15
        elif iv <= 75:
            iv_score = 0.7 * 15
        else:
            iv_score = 0.4 * 15

    # --- FIX: normalize strong_* trend values ---
    trend_norm = trend
    if trend in ("strong_uptrend",):
        trend_norm = "uptrend"
    elif trend in ("strong_downtrend",):
        trend_norm = "downtrend"

    # Trend alignment
    side = str(option["side"])
    if trend_norm == "uptrend":
        if style == "credit":
            alignment = 1.0 if side == "put" else 0.4  # Sell puts in uptrend
        else:
            alignment = 1.0 if side == "call" else 0.4  # Buy calls in uptrend
    elif trend_norm == "downtrend":
        if style == "credit":
            alignment = 1.0 if side == "call" else 0.4  # Sell calls in downtrend
        else:
            alignment = 1.0 if side == "put" else 0.4  # Buy puts in downtrend
    else:
        alignment = 0.7  # Neutral trend

    # Strategy adjustment
    if strategy == "momentum":
        alignment *= 1.2  # Boost for momentum
    elif strategy == "contrarian":
        alignment *= 0.8  # Reduce for contrarian

    alignment_score = alignment * 20

    # Calculate total score
    total_score = spread_score + liquidity_score + delta_score + iv_score + alignment_score

    # Determine confidence level
    if total_score >= 75:
        confidence = "HIGH"
    elif total_score >= 60:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    return min(100, total_score), confidence


# === MAIN FILTERING FUNCTION ============================
def find_quality_options(
    options_df: pd.DataFrame,
    market_context: Dict[str, Any],
    strategy: str,
    style: str,
    min_premium: Optional[float] = None,
    max_premium: Optional[float] = None,
) -> pd.DataFrame:
    """Find quality options based on filters and scoring"""
    if options_df.empty:
        return pd.DataFrame()

    trend = market_context.get("trend", "neutral")
    volatility = market_context.get("volatility", "medium")

    logger.info(f"Filtering options: strategy={strategy}, style={style}, trend={trend}, volatility={volatility}")

    rows: List[Dict[str, Any]] = []
    candidates_considered = 0

    # Filter settings
    max_spread_pct = float(os.getenv("GURU_MAX_SPREAD_PCT", "0.10"))
    min_volume = int(os.getenv("GURU_MIN_VOLUME", "100"))
    min_oi = int(os.getenv("GURU_MIN_OI", "100"))

    if style == "debit":
        delta_min, delta_max = 0.30, 0.65
        iv_min, iv_max = 15, 120
        max_entry_price = float(os.getenv("GURU_DEBIT_MAX_ASK", "8.00"))
    else:
        delta_min, delta_max = 0.15, 0.45
        iv_min, iv_max = 20, 150
        max_entry_price = float(os.getenv("GURU_CREDIT_MAX_BID", "12.00"))

    for _, opt in options_df.iterrows():
        candidates_considered += 1

        side = str(opt["side"])
        bid = float(opt["bid"])
        ask = float(opt["ask"])

        # Basic validity checks
        if bid <= 0 or ask <= 0 or ask < bid:
            continue

        # Calculate entry price based on style
        entry_price = bid if style == "credit" else ask

        # Premium filters
        if min_premium is not None and entry_price < min_premium:
            continue
        if max_premium is not None and entry_price > max_premium:
            continue
        if entry_price > max_entry_price:
            continue

        # Spread filter
        spread = ask - bid
        spread_pct = spread / ((bid + ask) / 2) if bid > 0 and ask > 0 else 1.0
        if spread_pct > max_spread_pct:
            continue

        # Liquidity filters
        volume = int(opt.get("volume", 0))
        oi = int(opt.get("oi", 0))
        if volume < min_volume and oi < min_oi:
            continue

        # Delta filter
        delta_abs = abs(float(opt["delta"]))
        if not (delta_min <= delta_abs <= delta_max):
            continue

        # IV filter
        iv = float(opt["iv"])
        if not (iv_min <= iv <= iv_max):
            continue

        # Calculate score
        score, confidence = calculate_option_score(opt, style, market_context, strategy, trend)

        # Skip low scoring options
        if score < 40:
            continue

        # Calculate stop and targets
        stop_targets = calculate_stop_and_targets(opt, style, entry_price, market_context)

        # Calculate position size
        position_size = calculate_position_size(
            entry_price,
            stop_targets["max_loss_per_contract"],
            market_context,
            style,
        )

        # Calculate risk/reward ratio
        risk = stop_targets["max_loss_per_contract"]
        if style == "credit":
            reward = entry_price - stop_targets["target_1"]
        else:
            reward = stop_targets["target_1"] - entry_price

        rr_ratio = reward / risk if risk > 0 else 0

        rows.append(
            {
                "symbol": str(opt["symbol"]),
                "side": side,
                "strike": float(opt["strike"]),
                "bid": bid,
                "ask": ask,
                "delta": float(opt["delta"]),
                "iv": iv,
                "oi": oi,
                "volume": volume,
                "trade_style": style,
                "strategy": strategy,
                "entry_price": entry_price,
                "quality_score": score,
                "confidence_level": confidence,
                "stop_price": stop_targets["stop_price"],
                "stop_type": stop_targets["stop_type"],
                "target_1": stop_targets["target_1"],
                "target_2": stop_targets["target_2"],
                "target_1_pct": stop_targets["target_1_pct"],
                "target_2_pct": stop_targets["target_2_pct"],
                "max_loss_per_contract": stop_targets["max_loss_per_contract"],
                "max_loss_per_contract_real": stop_targets["max_loss_per_contract_real"],
                "position_size": position_size,
                "spread_pct": spread_pct,
                "rr_ratio": rr_ratio,
                "market_volatility": volatility,
                "market_trend": trend,
            }
        )

    out = pd.DataFrame(rows)
    logger.info(f"Candidates considered: {candidates_considered}, passed filters: {len(out)}")

    # If nothing passed, `out` may have zero columns and sorting would raise KeyError.
    if out.empty or "quality_score" not in out.columns:
        return out

    logger.info(
        f"Top score: {out['quality_score'].max():.1f}, "
        f"Average score: {out['quality_score'].mean():.1f}"
    )
    for _, row in out.nlargest(3, "quality_score").iterrows():
        logger.info(
            f"  - {row['side'].upper()} ${row['strike']:.2f}: score={row['quality_score']:.1f}, "
            f"entry=${row['entry_price']:.2f}, delta={abs(row['delta']):.3f}, iv={row['iv']:.1f}%"
        )

    return out.sort_values("quality_score", ascending=False)


# === ALLOCATION CAP =====================================
def _cap_contracts_by_allocation(
    entry_price: float,
    desired_contracts: int,
    max_allocation_usd: Optional[float],
) -> Tuple[int, Optional[int], Optional[float]]:
    """Cap contracts based on allocation limit"""
    if desired_contracts < 1:
        desired_contracts = 1

    if entry_price <= 0:
        return max(1, desired_contracts), None, None

    if max_allocation_usd is None or max_allocation_usd <= 0:
        est_alloc = round(entry_price * 100.0 * float(desired_contracts), 2)
        return max(1, desired_contracts), None, est_alloc

    alloc_cap_qty = int(max_allocation_usd // (entry_price * 100.0))
    alloc_cap_qty = max(1, alloc_cap_qty)
    final_qty = min(desired_contracts, alloc_cap_qty)
    est_alloc = round(entry_price * 100.0 * float(final_qty), 2)

    return final_qty, alloc_cap_qty, est_alloc


# === MAIN BOT RUNNER ====================================
async def run_guru_pick_4exp_bots(bot_params: Dict[str, Any]) -> Dict[str, Any]:
    """Main function called by the bot runner"""
    try:
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

        max_allocation_usd = (
            float(bot_params.get("max_allocation_usd"))
            if bot_params.get("max_allocation_usd") is not None
            else (float(os.getenv("GURU_MAX_ALLOCATION_USD")) if os.getenv("GURU_MAX_ALLOCATION_USD") else None)
        )

        # Validate parameters
        if min_premium is not None and max_premium is not None and min_premium > max_premium:
            return {"success": False, "error": "min_premium cannot be greater than max_premium"}

        if hold_hours is not None and hold_days is not None:
            return {"success": False, "error": "Please specify only one: hold_hours OR hold_days"}

        if style not in ("credit", "debit"):
            style = "credit"

        logger.info(f"Running IMPROVED guru_pick_4exp bot for {symbol}")
        logger.info(f"Strategy: {strategy}, Style: {style}, Performance: {performance_tracker.win_rate:.1%} WR")

        # Get market context
        market_context = get_market_context(symbol)

        # Auto strategy selection
        if strategy == "auto":
            if market_context.get("trend") in ["strong_uptrend", "strong_downtrend"]:
                strategy = "momentum"
            elif market_context.get("volatility") == "high":
                strategy = "premium_collection"
            else:
                strategy = "momentum" if performance_tracker.profit_factor > 1.2 else "premium_collection"

        # Get expiration dates
        all_exp = get_expiration_dates(symbol)
        if not all_exp:
            return {"success": False, "error": "No expiration dates available"}

        # Choose expirations to scan
        scan_dates = choose_expirations(all_exp, expires=expires, hold_hours=hold_hours, hold_days=hold_days, symbol=symbol)
        today = _now_et().date()

        # Get first chain for price reference
        _, first_price = fetch_full_option_chain(symbol, scan_dates[0])
        if first_price is None:
            return {"success": False, "error": "Could not retrieve underlying price"}

        results_by_expiration: Dict[str, Any] = {}
        best_overall: Optional[Dict[str, Any]] = None
        best_overall_score = -1.0

        # Scan each expiration
        for exp_date in scan_dates:
            dte = (exp_date - today).days
            logger.info(f"Scanning expiration {exp_date} ({dte} DTE)")

            opt_df, price = fetch_full_option_chain(symbol, exp_date)
            if opt_df.empty:
                logger.warning(f"No options data for expiration {exp_date}")
                continue

            if price is None:
                price = first_price

            # Find quality options
            candidates = find_quality_options(
                opt_df,
                market_context=market_context,
                strategy=strategy,
                style=style,
                min_premium=min_premium,
                max_premium=max_premium,
            )

            if candidates.empty:
                logger.info(f"No quality candidates for expiration {exp_date}")
                continue

            # Process top candidates
            top_candidates = candidates.head(top).copy()
            plans: List[Dict[str, Any]] = []

            for _, row in top_candidates.iterrows():
                base_qty = int(row.get("position_size", 1))
                desired_qty = int(forced_contracts) if forced_contracts is not None else base_qty

                final_qty, _, est_alloc_used = _cap_contracts_by_allocation(
                    entry_price=float(row["entry_price"]),
                    desired_contracts=desired_qty,
                    max_allocation_usd=max_allocation_usd,
                )

                plan = {
                    "expiration": exp_date.strftime("%Y-%m-%d"),
                    "dte": dte,
                    "symbol": str(row["symbol"]),
                    "side": str(row["side"]),
                    "strike": float(row["strike"]),
                    "action": f"{'SELL' if style == 'credit' else 'BUY'} {row['side'].upper()}",
                    "entry_price": float(row["entry_price"]),
                    "quality_score": float(row["quality_score"]),
                    "confidence_level": str(row["confidence_level"]),
                    "delta": abs(float(row["delta"])),
                    "iv": float(row["iv"]),
                    "stop_price": float(row["stop_price"]),
                    "stop_type": str(row.get("stop_type", "adaptive")),
                    "target_1": float(row["target_1"]),
                    "target_2": float(row["target_2"]),
                    "position_size_final": int(final_qty),
                    "estimated_allocation_used": float(est_alloc_used) if est_alloc_used is not None else None,
                    "max_loss_per_contract_real": float(row["max_loss_per_contract_real"]),
                }
                plans.append(plan)

            results_by_expiration[exp_date.strftime("%Y-%m-%d")] = {
                "dte": dte,
                "underlying_price": float(price),
                "candidates": plans[:10],  # Limit to 10
            }

            # Update best overall
            if plans:
                best_in_exp = max(plans, key=lambda x: float(x["quality_score"]))
                if float(best_in_exp["quality_score"]) > best_overall_score:
                    best_overall = best_in_exp
                    best_overall_score = float(best_in_exp["quality_score"])

        # Prepare response
        resp: Dict[str, Any] = {
            "success": True,
            "symbol": symbol,
            "strategy": strategy,
            "style": style,
            "timestamp": _now_et().isoformat(),
            "market_analysis": market_context,
            "performance_stats": {
                "win_rate": performance_tracker.win_rate,
                "profit_factor": performance_tracker.profit_factor,
                "expectancy": performance_tracker.expectancy,
                "current_streak": performance_tracker.win_streak if performance_tracker.win_streak > 0 else -performance_tracker.loss_streak,
                "risk_multiplier": performance_tracker.get_risk_multiplier(),
            },
            "expirations_scanned": [d.strftime("%Y-%m-%d") for d in scan_dates],
            "results_by_expiration": results_by_expiration,
        }

        if best_overall:
            resp["best_overall_trade"] = best_overall

        return resp

    except Exception as e:
        logger.error(f"Error running improved guru_pick_4exp bot: {e}", exc_info=True)
        return {"success": False, "error": str(e), "timestamp": _now_et().isoformat()}


# === CLI ================================================
def _run_cli() -> int:
    """CLI entry point"""
    import argparse

    parser = argparse.ArgumentParser(description="Improved Options Guru Scanner")
    parser.add_argument("symbol", nargs="?", default="SPY", help="Underlying symbol")
    parser.add_argument("--style", choices=["credit", "debit"], default="credit")
    parser.add_argument("--strategy", choices=["auto", "momentum", "contrarian", "premium_collection"], default="auto")
    parser.add_argument("--expires", type=int, default=4)
    parser.add_argument("--top", type=int, default=8)
    parser.add_argument("--min-premium", type=float, default=None)
    parser.add_argument("--max-premium", type=float, default=None)
    parser.add_argument("--contracts", type=int, default=None)
    parser.add_argument("--max-allocation-usd", type=float, default=None)
    parser.add_argument("--hold-hours", type=int, default=None)
    parser.add_argument("--hold-days", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="Output JSON")

    args = parser.parse_args()

    bot_params = {
        "symbol": args.symbol.upper(),
        "style": args.style,
        "strategy": args.strategy,
        "expires": args.expires,
        "top": args.top,
        "min_premium": args.min_premium,
        "max_premium": args.max_premium,
        "contracts": args.contracts,
        "max_allocation_usd": args.max_allocation_usd,
        "hold_hours": args.hold_hours,
        "hold_days": args.hold_days,
    }

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(run_guru_pick_4exp_bots(bot_params))
    finally:
        loop.close()

    if args.json or result.get("success"):
        print(json.dumps(result, indent=2))
        return 0 if result.get("success") else 1

    print(f"❌ Error: {result.get('error', 'Unknown error')}")
    return 1


if __name__ == "__main__":
    raise SystemExit(_run_cli())
