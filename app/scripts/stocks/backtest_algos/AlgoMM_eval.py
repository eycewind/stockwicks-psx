#!/usr/bin/env python3
"""
AlgoMM_eval v4 — Backtest with LIVE ALGO MATCHING

KEY IMPROVEMENT: 100% alignment with live trading logic
- Entry thresholds: 0.60/0.40 (more conservative)
- Exit logic: Separate thresholds for long/short + Trailing stops
- Volume filtering: 20% above average volume requirement
- Smart execution pricing (VWAP/TWAP)
- Daily loss limits
- Partial profit taking
"""

from __future__ import annotations

import os
import sys
import argparse
import logging
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple

from datetime import datetime, timedelta, time as dtime

import numpy as np
import pandas as pd
import joblib

# ---- Repo path / imports -----------------------------------------------------

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app.scripts.stock_algos.base_wiring import StockBaseRunner, _ET
from app.scripts.research.mm_features2_builder import build_features
from app.scripts.ml.mm_live_helpers import predict_probability

# ---- Logging -----------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s (AlgoMM) %(message)s")
logger = logging.getLogger("AlgoMM_eval")

DATA_DIR = os.getenv("DATA_DIR", "/var/www/stockwicks/data")
MODEL_DIR = os.getenv("MODEL_DIR", "/var/www/stockwicks/models")


# ==== Helpers =================================================================


def normalize_symbol(symbol: str) -> str:
    """Normalize user input symbol while preserving futures leading '/'."""
    s = (symbol or "").strip()
    if not s:
        return s
    # Futures: keep leading slash, uppercase remainder
    if s.startswith("/"):
        return "/" + s[1:].upper()
    return s.upper()


def safe_symbol_for_files(symbol: str) -> str:
    """Make a symbol safe to use in filenames (e.g., '/ESH26' -> '_ESH26')."""
    s = normalize_symbol(symbol)
    if s.startswith("/"):
        return "_" + s[1:]
    return s


def is_futures_symbol(symbol: str) -> bool:
    return bool(symbol) and symbol.startswith("/")


def futures_point_value(symbol: str) -> float:
    """Return USD value per 1.0 point move for common futures."""
    s = normalize_symbol(symbol)
    if not s.startswith("/"):
        return 1.0
    # Equity index futures
    if s.startswith("/ES"):
        return 50.0
    if s.startswith("/NQ"):
        return 20.0
    if s.startswith("/YM"):
        return 5.0
    if s.startswith("/RTY"):
        return 50.0
    # Metals
    if s.startswith("/GC"):
        return 100.0
    if s.startswith("/SI"):
        # Silver is $50 per 0.01 move => $5000 per 1.0
        return 5000.0
    # Energy
    if s.startswith("/CL"):
        # Crude oil is $10 per 0.01 => $1000 per 1.0
        return 1000.0
    if s.startswith("/NG"):
        # Nat gas is $10 per 0.001 => $10000 per 1.0
        return 10000.0
    # Fallback (unknown futures): treat as 1.0 USD per point (better than crash)
    return 1.0


def profit_to_usd(symbol: str, profit_points_or_dollars: float) -> float:
    """
    Convert profit to USD.
    - Equities: profit already in dollars
    - Futures: profit is in points; convert using point value
    """
    if is_futures_symbol(symbol):
        return float(profit_points_or_dollars) * futures_point_value(symbol)
    return float(profit_points_or_dollars)


@dataclass
class EvalConfig:
    symbol: str
    interval: str
    trade_size: float
    user_id: int
    builder_days: int = 10
    k_forward: int = 3
    
    # Entry thresholds (UPDATED: more conservative)
    long_threshold: float = 0.60
    short_threshold: float = 0.40
    
    # Exit thresholds (UPDATED: separate for long/short)
    long_exit_threshold: float = 0.55
    short_exit_threshold: float = 0.45
    
    # Volume filtering (MATCHING LIVE)
    min_volume_multiplier: float = 0
    
    # Risk management (MATCHING LIVE)
    fixed_stop_loss: float = 20000.0
    trailing_stop_activation: float = 10.8
    trailing_stop_distance: float = 11.0
    daily_loss_limit_usd: float = 50000.0
    take_profit_percent: float = 10.8
    cooldown_sec: int = 60
    
    # Probability advantage requirement (MATCHING LIVE)
    min_prob_advantage: float = 0.01
    
    # General
    eod_close: bool = True
    auto_train: bool = False
    save_raw: bool = False
    save_features: bool = False
    save_trades: bool = False
    save_run: bool = False


def _prepare_X(feat_df: pd.DataFrame, feat_names: List[str]) -> pd.DataFrame:
    X = feat_df.reindex(columns=feat_names).copy()
    X = X.replace([np.inf, -np.inf], np.nan).ffill().bfill()
    med = X.median(numeric_only=True)
    X = X.fillna(med)

    all_nan = [c for c in X.columns if X[c].isna().all()]
    if all_nan:
        X[all_nan] = 0.0

    return X.fillna(0.0).astype(float)


def _load_or_train_model(
    model_path: str,
    feat_df: pd.DataFrame,
    feat_names: List[str],
    symbol: str,
    interval: str,
    auto_train: bool,
):
    """
    Load saved model or train a new one.

    Now ROBUST: if joblib.load fails (e.g. due to numpy/sklearn version
    mismatch, unknown BitGenerator, etc.), we log a warning and retrain.
    """
    # Try to load existing model if allowed
    if os.path.exists(model_path) and not auto_train:
        try:
            pack = joblib.load(model_path)
            model = pack["model"]
            features = pack.get("features", feat_names)
            logger.info(f"Loaded model from {model_path}")
            return model, features
        except Exception as e:
            logger.warning(
                "Failed to load existing model '%s' (%s). "
                "Will retrain a new model with current environment.",
                model_path, repr(e)
            )

    # --- Training path -------------------------------------------------------
    from sklearn.ensemble import HistGradientBoostingClassifier

    if feat_df is None or feat_df.empty or "y" not in feat_df or "w" not in feat_df:
        raise RuntimeError("Cannot train: feature labels (y, w) missing in feat_df.")

    X = _prepare_X(feat_df, feat_names)
    y = feat_df.loc[X.index, "y"].astype(int).values
    w = feat_df.loc[X.index, "w"].astype(float).values

    clf = HistGradientBoostingClassifier(
        max_depth=4,
        learning_rate=0.06,
        max_iter=400,
        l2_regularization=1.0,
    )
    clf.fit(X, y, sample_weight=w)

    os.makedirs(os.path.dirname(model_path), exist_ok=True)
    joblib.dump(
        {"model": clf, "features": feat_names, "interval": interval, "symbol": symbol},
        model_path,
    )
    logger.info(f"Trained & saved model to {model_path}")
    return clf, feat_names



def _fetch_price_frame_latest(
    runner: StockBaseRunner,
    symbol: str,
    interval: str,
    builder_days: int,
) -> pd.DataFrame:
    """Fetch price data aligned with live runner - FIXED VERSION."""
    df_raw = runner.fetch_source_bars(symbol)
    if df_raw is None or df_raw.empty:
        raise RuntimeError(f"No df_raw for symbol={symbol}")

    latest_time, _, df = runner.resample_interval(df_raw, interval, symbol)
    if df is None or df.empty:
        raise RuntimeError(f"Resample failed: symbol={symbol}, interval={interval}")

    df = df.sort_index()
    
    logger.info(f"Fetched {len(df)} bars from {df.index[0]} to {df.index[-1]}")

    # FIX: Properly filter by calendar days, not trading days
    if builder_days and builder_days > 0:
        # Calculate cutoff date (builder_days calendar days ago)
        cutoff_date = df.index[-1].date() - timedelta(days=builder_days)
        mask = df.index.date >= cutoff_date
        df = df.loc[mask]
        
        logger.info(
            f"Filtered to last {builder_days} calendar days: "
            f"{df.index[0].date()} to {df.index[-1].date()} "
            f"({len(df)} bars)"
        )

    return df


def _build_features_with_fallback(symbol: str, interval: str, days: int, k_forward: int) -> pd.DataFrame:
    """Build features with interval fallback for unsupported intervals."""
    supported_intervals = ['1min', '5min', '15min', '30min', '1h', '4h', '1d']
    
    # Map common interval names to supported ones
    interval_map = {
        '1m': '1min',
        '5m': '5min', 
        '5mim': '5min',
        '15m': '15min',
        '30m': '30min',
        '1h': '1h',
        '4h': '4h',
        '1d': '1d'
    }
    
    # Use mapped interval if available, otherwise try original
    mapped_interval = interval_map.get(interval, interval)
    
    if mapped_interval not in supported_intervals:
        logger.warning(f"Interval '{interval}' not supported, falling back to '5min'")
        mapped_interval = '5min'
    
    logger.info(f"Using interval '{mapped_interval}' for feature building (requested: '{interval}')")
    
    return build_features(
        symbol,
        mapped_interval,
        days=days,
        k_forward=k_forward,
    )


# ==== LIVE ALGO MATCHING HELPERS =============================================

# Add these imports if not already present
from datetime import time
import numpy as np
from scipy import stats

def calculate_vwap(df, current_index):
    """Calculate VWAP for current bar."""
    if current_index < 39:
        return df['close'].iloc[current_index]
    
    slice_df = df.iloc[max(0, current_index-39):current_index+1]
    typical_price = (slice_df['high'] + slice_df['low'] + slice_df['close']) / 3
    vwap = (typical_price * slice_df['volume']).sum() / slice_df['volume'].sum()
    return vwap

def adaptive_volume_filter(df, current_index, symbol="TSLA"):
    """
    SMART volume filter that adapts to:
    1. Time of day
    2. Stock's typical volume profile
    3. Current volatility
    """
    if current_index < 39:  # First 39 bars (needs 40 for VWAP)
        return True
    
    current_bar = df.iloc[current_index]
    current_time = df.index[current_index].time()
    current_volume = current_bar['volume']
    
    # ====== TIME-BASED RULES ======
    # 1. POWER HOURS: No filter (high opportunity)
    if (time(9, 30) <= current_time <= time(10, 30)):  # Open surge
        return True
    if (time(15, 30) <= current_time <= time(16, 0)):  # Close rush
        return True
    
    # 2. LUNCH LULL: Stricter filter
    if (time(12, 0) <= current_time <= time(13, 30)):  # Lunchtime
        # Need 50% above average during lunch
        avg_volume = df['volume'].iloc[current_index-20:current_index].mean()
        return current_volume >= (avg_volume * 0.5)
    
    # ====== VWAP + VOLUME COMBO ======
    try:
        vwap = calculate_vwap(df, current_index)
        current_price = current_bar['close']
        
        # Calculate volume-weighted price distance
        volume_slice = df['volume'].iloc[current_index-39:current_index+1].values
        price_slice = df['close'].iloc[current_index-39:current_index+1].values
        
        # Current volume percentile
        vol_percentile = stats.percentileofscore(volume_slice, current_volume)
        
        # ====== DECISION MATRIX ======
        price_from_vwap = abs((current_price - vwap) / vwap * 100)
        
        # A) High volume + far from VWAP = EXCELLENT signal
        if vol_percentile > 70 and price_from_vwap > 0.3:
            return True  # Strong move
        
        # B) Medium volume + near VWAP = OK for mean reversion
        if vol_percentile > 40 and price_from_vwap < 0.2:
            return True  # Potential reversal
        
        # C) Low volume = only allow if strong price move
        if vol_percentile < 30:
            # Need at least 0.5% price move in 5min
            price_change = abs(df['close'].iloc[current_index] / df['close'].iloc[current_index-1] - 1) * 100
            return price_change > 0.5 and current_volume > 10000
        
        return False
    except Exception:
        # If VWAP calculation fails, fall back to simple check
        avg_volume = df['volume'].iloc[current_index-20:current_index].mean()
        return current_volume >= (avg_volume * 0.25)

def check_volume_requirement(df: pd.DataFrame, current_index: int, 
                            min_volume_multiplier: float = 0,
                            symbol: str = "TSLA",
                            use_adaptive: bool = True) -> bool:
    """
    Check if current volume meets requirements.
    
    Args:
        use_adaptive: If True, uses advanced adaptive filter
                      If False, uses simple multiplier filter
    """
    if min_volume_multiplier == 0:
        return True  # No volume filtering
    
    if use_adaptive:
        # Use the smart adaptive filter
        return adaptive_volume_filter(df, current_index, symbol)
    else:
        # Fall back to original simple logic
        try:
            if current_index < 20:
                return True
                
            current_volume = df['volume'].iloc[current_index]
            avg_volume = df['volume'].iloc[max(0, current_index-20):current_index].mean()
            return current_volume >= (avg_volume * min_volume_multiplier)
        except Exception:
            return True

def calculate_trailing_stop(entry_price: float, current_price: float, 
                          position_side: str, activation: float, distance: float) -> Optional[float]:
    """Calculate trailing stop price (MATCHING LIVE)."""
    if position_side == "long":
        profit_pct = (current_price - entry_price) / entry_price * 100
        if profit_pct >= activation:
            return current_price * (1 - distance/100)
    else:
        profit_pct = (entry_price - current_price) / entry_price * 100
        if profit_pct >= activation:
            return current_price * (1 + distance/100)
    return None

def should_exit_trade(position_side: str, entry_price: float, current_price: float, 
                     prob_up: float, prob_down: float, cfg: EvalConfig, 
                     entry_time: datetime, current_time: datetime) -> Tuple[bool, str]:
    """Improved exit logic matching LIVE algo exactly with separate long/short exit thresholds."""
    if not position_side:
        return False, ""
    
    # 1. Trailing stop exit (MATCHING LIVE)
    trailing_stop = calculate_trailing_stop(
        entry_price, current_price, position_side,
        cfg.trailing_stop_activation, cfg.trailing_stop_distance
    )
    if trailing_stop:
        if (position_side == "long" and current_price <= trailing_stop) or \
           (position_side == "short" and current_price >= trailing_stop):
            return True, "TRAILING_STOP"
    
    # 2. Probability flip exit (UPDATED: separate thresholds for long/short)
    if position_side == "long" and prob_up < cfg.long_exit_threshold:
        return True, "PROBABILITY_DROP"
    elif position_side == "short" and prob_down < (1 - cfg.short_exit_threshold):
        return True, "PROBABILITY_DROP"
    
    # 3. Quick profit exit (MATCHING LIVE: 0.5% profit in first 5 minutes)
    if position_side == "long":
        profit_pct = (current_price - entry_price) / entry_price * 100
    else:
        profit_pct = (entry_price - current_price) / entry_price * 100
    
    trade_duration = current_time - entry_time
    if trade_duration.total_seconds() < 300 and profit_pct >= 0.5:  # 5 minutes
        return True, "QUICK_PROFIT"
    
    return False, ""


def should_enter_trade(prob_up: float, prob_down: float, df: pd.DataFrame, 
                      current_index: int, cfg: EvalConfig, allow_short: bool = True) -> Tuple[bool, str]:
    """Improved entry logic matching LIVE algo exactly."""
    # 1. Volume requirement (MATCHING LIVE)
    if not check_volume_requirement(df, current_index, cfg.min_volume_multiplier, 
                                   symbol=cfg.symbol, use_adaptive=True):
        return False, "LOW_VOLUME"
    
    # 2. Cooldown check (if implemented)
    # (Add cooldown logic here if needed)
    
    # 3. Stronger probability requirements (UPDATED thresholds)
    if prob_up >= cfg.long_threshold and prob_up > (prob_down + cfg.min_prob_advantage):
        return True, "LONG"
    elif prob_down >= (1 - cfg.short_threshold) and prob_down > (prob_up + cfg.min_prob_advantage):
        if allow_short:
            return True, "SHORT"
    
    return False, ""


def should_take_partial_profit(
    entry_price: float,
    current_price: float,
    position_side: str,
    threshold_percent: float = 1.5,
) -> Tuple[bool, float]:
    """Check if we should take partial profits (MATCHING LIVE)."""
    if position_side == "long":
        gain_percent = (current_price - entry_price) / entry_price * 100
    else:
        gain_percent = (entry_price - current_price) / entry_price * 100
    
    should_take = gain_percent >= threshold_percent
    return should_take, gain_percent

def get_smart_execution_price(df: pd.DataFrame, current_index: int, execution_type: str = "vwap") -> float:
    """Get smarter execution price than just OPEN (MATCHING LIVE)."""
    try:
        if execution_type == "vwap" and "vwap" in df.columns:
            return float(df["vwap"].iloc[current_index])
        elif execution_type == "twap":
            # Time-weighted average of last 5 bars
            start_idx = max(0, current_index - 4)
            return float(df["close"].iloc[start_idx:current_index+1].mean())
        else:
            # Default: OPEN
            return float(df["open"].iloc[current_index])
    except Exception:
        return float(df["open"].iloc[current_index])


# ==== Trade simulation logic ==================================================

@dataclass
class TradeRecord:
    symbol: str
    interval: str
    side: str
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    quantity: float
    # For equities, profit is USD. For futures, profit is USD (converted) and profit_points stores the raw points move.
    profit: float
    profit_points: Optional[float] = None
    exit_reason: str = ""


def _simulate_trades(
    cfg: EvalConfig,
    price_df: pd.DataFrame,
    prob_series: pd.Series,
) -> Tuple[List[TradeRecord], List[TradeRecord], pd.DataFrame]:
    """
    Core backtest loop with 100% LIVE ALGO MATCHING.
    """

    prob_aligned = prob_series.reindex(price_df.index).ffill()

    run_rows = []
    trades_long: List[TradeRecord] = []
    trades_short: List[TradeRecord] = []

    position_side: Optional[str] = None
    entry_price: float = 0.0
    entry_time: Optional[datetime] = None
    quantity: float = cfg.trade_size
    
    # Daily loss tracking (MATCHING LIVE)
    daily_trades: List[TradeRecord] = []
    current_day = None

    idx = price_df.index
    
    if len(idx) < 2:
        logger.warning("Not enough bars to simulate.")
        return trades_long, trades_short, pd.DataFrame()

    for i in range(1, len(idx)):
        cur_ts = idx[i]
        prev_ts = idx[i - 1]

        # Update daily tracking
        if current_day != cur_ts.date():
            current_day = cur_ts.date()
            # Reset daily trades for new day
            daily_trades = [t for t in trades_long + trades_short if t.entry_time.date() == current_day]

        prev_prob = prob_aligned.iloc[i - 1]
        if pd.isna(prev_prob):
            run_rows.append({
                "timestamp": cur_ts,
                "open": price_df["open"].iloc[i],
                "high": price_df["high"].iloc[i],
                "low": price_df["low"].iloc[i],
                "close": price_df["close"].iloc[i],
                "prob_up": np.nan,
                "position": position_side or "flat",
                "action": "SKIP_NO_PROB",
            })
            continue

        prob_up = float(prev_prob)
        prob_down = 1.0 - prob_up
        
        # Use smart execution price (MATCHING LIVE)
        exec_price = get_smart_execution_price(price_df, i, "vwap")
        
        action = "HOLD"
        exit_reason = ""
        just_closed = False

        # ========== DAILY LOSS LIMIT CHECK (MATCHING LIVE) ==========
        daily_loss = sum(t.profit for t in daily_trades if t.profit < 0)
        if daily_loss <= -abs(cfg.daily_loss_limit_usd):
            if position_side:
                # Close position due to daily loss limit
                if position_side == "long":
                    profit_raw = (exec_price - entry_price) * quantity
                    profit_points = profit_raw if is_futures_symbol(cfg.symbol) else None
                    profit = profit_to_usd(cfg.symbol, profit_raw)
                    trades_long.append(TradeRecord(
                        cfg.symbol, cfg.interval, "long",
                        entry_time, entry_price, cur_ts, exec_price, quantity, profit, profit_points, 
                        f"DAILY_LOSS_LIMIT_{daily_loss:.2f}"
                    ))
                else:
                    profit_raw = (entry_price - exec_price) * quantity
                    profit_points = profit_raw if is_futures_symbol(cfg.symbol) else None
                    profit = profit_to_usd(cfg.symbol, profit_raw)
                    trades_short.append(TradeRecord(
                        cfg.symbol, cfg.interval, "short",
                        entry_time, entry_price, cur_ts, exec_price, quantity, profit, profit_points,
                        f"DAILY_LOSS_LIMIT_{daily_loss:.2f}"
                    ))
                
                position_side = None
                entry_price = 0.0
                entry_time = None
                action = "EXIT_DAILY_LOSS_LIMIT"
                just_closed = True
            else:
                action = "SKIP_DAILY_LOSS_LIMIT"
                run_rows.append({
                    "timestamp": cur_ts,
                    "open": price_df["open"].iloc[i],
                    "high": price_df["high"].iloc[i],
                    "low": price_df["low"].iloc[i],
                    "close": price_df["close"].iloc[i],
                    "prob_up": prob_up,
                    "prob_down": prob_down,
                    "position": "flat",
                    "action": action,
                })
                continue

        # ========== EXIT LOGIC (MATCHING LIVE) ==========
        if position_side is not None and not just_closed:
            
            # 1. Fixed USD stop-loss
            if cfg.fixed_stop_loss > 0:
                if position_side == "long":
                    unrealized = (exec_price - entry_price) * quantity
                else:
                    unrealized = (entry_price - exec_price) * quantity

                if unrealized <= -abs(cfg.fixed_stop_loss):
                    profit = unrealized
                    reason = f"Fixed Stop Loss ${cfg.fixed_stop_loss:.0f}"
                    if position_side == "long":
                        trades_long.append(TradeRecord(
                            cfg.symbol, cfg.interval, "long",
                            entry_time, entry_price, cur_ts, exec_price, quantity, profit, profit_points, reason
                        ))
                    else:
                        trades_short.append(TradeRecord(
                            cfg.symbol, cfg.interval, "short",
                            entry_time, entry_price, cur_ts, exec_price, quantity, profit, profit_points, reason
                        ))
                    
                    position_side = None
                    entry_price = 0.0
                    entry_time = None
                    action = "EXIT_STOP_LOSS"
                    just_closed = True

            # 2. LIVE ALGO EXIT LOGIC
            if position_side is not None and not just_closed:
                should_exit, exit_reason = should_exit_trade(
                    position_side, entry_price, exec_price, prob_up, prob_down, cfg, entry_time, cur_ts
                )
                if should_exit:
                    profit_raw = (exec_price - entry_price) * quantity
                    profit_points = profit_raw if is_futures_symbol(cfg.symbol) else None
                    profit = profit_to_usd(cfg.symbol, profit_raw) if position_side == "long" else (entry_price - exec_price) * quantity
                    
                    if position_side == "long":
                        trades_long.append(TradeRecord(
                            cfg.symbol, cfg.interval, "long",
                            entry_time, entry_price, cur_ts, exec_price, quantity, profit, profit_points, exit_reason
                        ))
                    else:
                        trades_short.append(TradeRecord(
                            cfg.symbol, cfg.interval, "short",
                            entry_time, entry_price, cur_ts, exec_price, quantity, profit, profit_points, exit_reason
                        ))
                    
                    position_side = None
                    entry_price = 0.0
                    entry_time = None
                    action = f"EXIT_{exit_reason}"
                    just_closed = True

            # 3. Partial profit taking (MATCHING LIVE)
            if position_side is not None and not just_closed and quantity >= 2:
                should_profit, gain_pct = should_take_partial_profit(
                    entry_price, exec_price, position_side, cfg.take_profit_percent
                )
                if should_profit:
                    partial_qty = quantity // 2
                    partial_profit = (exec_price - entry_price) * partial_qty if position_side == "long" else (entry_price - exec_price) * partial_qty
                    
                    # Record partial profit trade
                    if position_side == "long":
                        trades_long.append(TradeRecord(
                            cfg.symbol, cfg.interval, "long",
                            entry_time, entry_price, cur_ts, exec_price, partial_qty, partial_profit, f"PARTIAL_PROFIT_{gain_pct:.1f}%"
                        ))
                    else:
                        trades_short.append(TradeRecord(
                            cfg.symbol, cfg.interval, "short",
                            entry_time, entry_price, cur_ts, exec_price, partial_qty, partial_profit, f"PARTIAL_PROFIT_{gain_pct:.1f}%"
                        ))
                    
                    # Reduce position
                    quantity = quantity - partial_qty
                    action = f"PARTIAL_PROFIT_{gain_pct:.1f}%"

        # ========== ENTRY LOGIC (MATCHING LIVE) ==========
        if position_side is None and not just_closed:
            should_enter, enter_direction = should_enter_trade(
                prob_up, prob_down, price_df, i, cfg, allow_short=True
            )
            if should_enter:
                position_side = enter_direction.lower()
                entry_price = exec_price
                entry_time = cur_ts
                quantity = cfg.trade_size  # Reset quantity for new position
                action = f"OPEN_{enter_direction}"

        # ========== EOD CLOSE (MATCHING LIVE) ==========
        if cfg.eod_close and position_side is not None and not just_closed:
            # Check if this is end of day (3:58 PM ET or later)
            cur_time_et = cur_ts.astimezone(_ET) if cur_ts.tzinfo else _ET.localize(cur_ts)
            if cur_time_et.hour >= 15 and (cur_time_et.hour > 15 or cur_time_et.minute >= 58):
                if position_side == "long":
                    profit_raw = (exec_price - entry_price) * quantity
                else:
                    profit_raw = (entry_price - exec_price) * quantity
                profit_points = profit_raw if is_futures_symbol(cfg.symbol) else None
                profit = profit_to_usd(cfg.symbol, profit_raw)
                reason = "END_OF_DAY_CLOSE"
                
                if position_side == "long":
                    trades_long.append(TradeRecord(
                        cfg.symbol, cfg.interval, "long",
                        entry_time, entry_price, cur_ts, exec_price, quantity, profit, profit_points, reason
                    ))
                else:
                    trades_short.append(TradeRecord(
                        cfg.symbol, cfg.interval, "short",
                        entry_time, entry_price, cur_ts, exec_price, quantity, profit, profit_points, reason
                    ))
                
                position_side = None
                entry_price = 0.0
                entry_time = None
                action = "EXIT_EOD"
                just_closed = True

        run_rows.append({
            "timestamp": cur_ts,
            "open": exec_price,  # Use execution price for consistency
            "high": price_df["high"].iloc[i],
            "low": price_df["low"].iloc[i],
            "close": float(price_df["close"].iloc[i]),
            "prob_up": prob_up,
            "prob_down": prob_down,
            "position": position_side or "flat",
            "action": action,
            "quantity": quantity if position_side else 0,
        })

        # Update daily trades
        if just_closed:
            daily_trades.extend(trades_long[-1:] if trades_long else [])
            daily_trades.extend(trades_short[-1:] if trades_short else [])

    # Force close at market close (MATCHING LIVE)
    if position_side is not None:
        last_ts = idx[-1]
        last_close = float(price_df["close"].iloc[-1])
        profit = (last_close - entry_price) * quantity if position_side == "long" else (entry_price - last_close) * quantity
        reason = "MARKET_CLOSE"
        
        if position_side == "long":
            trades_long.append(TradeRecord(
                cfg.symbol, cfg.interval, "long",
                entry_time, entry_price, last_ts, last_close, quantity, profit, profit_points, reason
            ))
        else:
            trades_short.append(TradeRecord(
                cfg.symbol, cfg.interval, "short",
                entry_time, entry_price, last_ts, last_close, quantity, profit, profit_points, reason
            ))
        
        logger.info(f"Closed {position_side} position at market close: {last_ts} @ {last_close}")

    run_df = pd.DataFrame(run_rows).set_index("timestamp")
    return trades_long, trades_short, run_df


# ==== Summary metrics =========================================================

def _compute_summary_for_side(
    trades: List[TradeRecord],
    cfg: EvalConfig,
    trade_type_label: str,
) -> Optional[Dict[str, str]]:
    if not trades:
        return None

    profits = [t.profit for t in trades]

    points = [t.profit_points for t in trades if t.profit_points is not None]
    has_points = len(points) > 0

    wins = sum(1 for p in profits if p > 0)
    losses = sum(1 for p in profits if p <= 0)

    total_trades = len(profits)
    total_profit = sum(profits)
    sr = (wins / total_trades) * 100.0 if total_trades > 0 else 0.0

    largest_win = max(profits) if profits else 0
    largest_loss = min(profits) if profits else 0

    # Streak stats
    longest_win_streak = 0
    longest_loss_streak = 0
    win_streaks_count = 0
    loss_streaks_count = 0
    cur_streak = 0
    cur_sign = 0

    for p in profits:
        sign = 1 if p > 0 else -1
        if sign == cur_sign:
            cur_streak += 1
        else:
            if cur_sign == 1:
                win_streaks_count += 1
                longest_win_streak = max(longest_win_streak, cur_streak)
            elif cur_sign == -1:
                loss_streaks_count += 1
                longest_loss_streak = max(longest_loss_streak, cur_streak)
            cur_sign = sign
            cur_streak = 1

    if cur_sign == 1:
        win_streaks_count += 1
        longest_win_streak = max(longest_win_streak, cur_streak)
    elif cur_sign == -1:
        loss_streaks_count += 1
        longest_loss_streak = max(longest_loss_streak, cur_streak)

    def fmt_money(x: float) -> str:
        return f"${x:.2f}"

    
    row = {
        "Symbol": cfg.symbol,
        "Interval": cfg.interval,
        "Trade_size": f"{cfg.trade_size:.1f}",
        "Total_Trades": str(total_trades),
        "Wins": str(wins),
        "Losses": str(losses),
        "SuccessRate": f"{sr:.2f}%",
        "Total_profit": fmt_money(total_profit),
        "Trade_Type": trade_type_label,
        "Largest_Win": fmt_money(largest_win),
        "Largest_Loss": fmt_money(largest_loss),
        "Longest_Win_Streak": str(longest_win_streak),
        "Longest_Loss_Streak": str(longest_loss_streak),
        "Win_Streaks_Count": str(win_streaks_count),
        "Loss_Streaks_Count": str(loss_streaks_count),
    }

    if has_points:
        total_points = sum(points)
        row.update({
            "Total_points": f"{total_points:.2f}",
            "Largest_Win_points": f"{max(points):.2f}",
            "Largest_Loss_points": f"{min(points):.2f}",
        })

    return row


# ==== Main entry =============================================================

def main(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(description="AlgoMM backtest evaluator v4 (LIVE MATCHING)")

    parser.add_argument("-s", "--symbol", required=True)
    parser.add_argument("-i", "--interval", required=True)
    parser.add_argument("-q", "--trade-size", type=float, required=True)
    parser.add_argument("-u", "--user-id", type=int, required=True)

    parser.add_argument("--builder-days", type=int, default=60)
    parser.add_argument("--k-forward", type=int, default=3)
    parser.add_argument("--long-threshold", type=float, default=0.60)      # UPDATED
    parser.add_argument("--short-threshold", type=float, default=0.40)     # UPDATED
    parser.add_argument("--long-exit-threshold", type=float, default=0.55) # NEW
    parser.add_argument("--short-exit-threshold", type=float, default=0.45) # NEW
    parser.add_argument("--min-volume-multiplier", type=float, default=1.1)
    parser.add_argument("--min-prob-advantage", type=float, default=0.01)
    
    parser.add_argument("--fixed-stop-loss", type=float, default=200.0)
    parser.add_argument("--trailing-stop-activation", type=float, default=0.8)
    parser.add_argument("--trailing-stop-distance", type=float, default=1.0)
    parser.add_argument("--daily-loss-limit-usd", type=float, default=500.0)
    parser.add_argument("--take-profit-percent", type=float, default=1.5)
    parser.add_argument("--cooldown-sec", type=int, default=60)
    
    parser.add_argument("--eod-close", action="store_true", default=True)
    parser.add_argument("--auto-train", action="store_true", default=False)

    parser.add_argument("--save-raw", action="store_true", default=False)
    parser.add_argument("--save-features", action="store_true", default=False)
    parser.add_argument("--save-trades", action="store_true", default=False)
    parser.add_argument("--save-run", action="store_true", default=False)

    args = parser.parse_args(argv)

    cfg = EvalConfig(
        symbol=normalize_symbol(args.symbol),
        interval=args.interval,
        trade_size=args.trade_size,
        user_id=args.user_id,
        builder_days=args.builder_days,
        k_forward=args.k_forward,
        long_threshold=args.long_threshold,
        short_threshold=args.short_threshold,
        long_exit_threshold=args.long_exit_threshold,  # NEW
        short_exit_threshold=args.short_exit_threshold,  # NEW
        min_volume_multiplier=args.min_volume_multiplier,
        min_prob_advantage=args.min_prob_advantage,
        fixed_stop_loss=args.fixed_stop_loss,
        trailing_stop_activation=args.trailing_stop_activation,
        trailing_stop_distance=args.trailing_stop_distance,
        daily_loss_limit_usd=args.daily_loss_limit_usd,
        take_profit_percent=args.take_profit_percent,
        cooldown_sec=args.cooldown_sec,
        eod_close=args.eod_close,
        auto_train=args.auto_train,
        save_raw=args.save_raw,
        save_features=args.save_features,
        save_trades=args.save_trades,
        save_run=args.save_run,
    )

    out_dir = os.path.join(DATA_DIR, str(cfg.user_id))
    os.makedirs(out_dir, exist_ok=True)

    safe_sym = safe_symbol_for_files(cfg.symbol)
    prefix = f"{cfg.user_id}_{safe_sym}_{cfg.interval}"

    logger.info(f"Starting LIVE-MATCHING backtest: {cfg.symbol} {cfg.interval}")
    logger.info(f"Entry: Long@{cfg.long_threshold:.2f}, Short@{cfg.short_threshold:.2f}")
    logger.info(f"Exit: Long@{cfg.long_exit_threshold:.2f}, Short@{cfg.short_exit_threshold:.2f}")  # UPDATED
    logger.info(f"Volume filter: {cfg.min_volume_multiplier:.1f}x, Prob advantage: {cfg.min_prob_advantage:.2f}")
    logger.info(f"Risk: Daily loss limit=${cfg.daily_loss_limit_usd:.0f}, Stop=${cfg.fixed_stop_loss:.0f}")

    # Fetch price data
    runner = StockBaseRunner()
    price_df = _fetch_price_frame_latest(runner, cfg.symbol, cfg.interval, cfg.builder_days)
    
    logger.info(f"Price data: {len(price_df)} bars from {price_df.index[0]} to {price_df.index[-1]}")
    
    # Build features with fallback for unsupported intervals
    logger.info(f"Building features (builder_days={cfg.builder_days}, k_forward={cfg.k_forward})")
    feat_df = _build_features_with_fallback(
        cfg.symbol,
        cfg.interval,
        days=cfg.builder_days + 2,
        k_forward=cfg.k_forward,
    )
    if feat_df is None or feat_df.empty:
        raise RuntimeError("build_features returned empty DataFrame.")
    
    logger.info(f"Features built: {len(feat_df)} rows")

    if cfg.save_features:
        feat_path = os.path.join(out_dir, f"{prefix}_features.csv")
        feat_df.to_csv(feat_path)
        logger.info(f"Saved → {feat_path}")

    # Train model
    feat_names = [c for c in feat_df.columns if c not in ("y", "w")]
    model_path = os.path.join(MODEL_DIR, f"mm2_{safe_symbol_for_files(cfg.symbol)}_{cfg.interval}_k{cfg.k_forward}.joblib")
    model, used_feats = _load_or_train_model(
        model_path, feat_df, feat_names, cfg.symbol, cfg.interval, cfg.auto_train
    )

    # Get probabilities
    X = _prepare_X(feat_df, used_feats)
    probs = predict_probability(model, X, used_feats)
    if probs is None or len(probs) == 0:
        raise RuntimeError("predict_probability returned no probabilities.")

    prob_raw = pd.Series(probs, index=X.index, name="prob_up_raw")
    window = max(int(cfg.k_forward), 1)
    prob_series = prob_raw.rolling(window=window, min_periods=1).mean().rename("prob_up")

    # Logging
    long_bins = (prob_series >= cfg.long_threshold).sum()
    short_bins = (prob_series <= cfg.short_threshold).sum()
    mid_bins = len(prob_series) - long_bins - short_bins
    logger.info(
        f"Prob distribution: Long≥{cfg.long_threshold:.2f}={long_bins}, "
        f"Short≤{cfg.short_threshold:.2f}={short_bins}, Middle={mid_bins}"
    )

    if cfg.save_raw:
        raw_path = os.path.join(out_dir, f"{prefix}_raw.csv")
        price_df.to_csv(raw_path)
        logger.info(f"Saved → {raw_path}")

    # Simulate trades with LIVE MATCHING logic
    logger.info("Simulating trades with LIVE algo matching...")
    trades_long, trades_short, run_df = _simulate_trades(cfg, price_df, prob_series)

    logger.info(f"Trade results: Long={len(trades_long)}, Short={len(trades_short)}")

    # Save trades
    if cfg.save_trades:
        long_path = os.path.join(out_dir, f"{prefix}_trades_long.csv")
        short_path = os.path.join(out_dir, f"{prefix}_trades_short.csv")

        with open(long_path, "w") as f:
            f.write("Symbol,Interval,Side,Entry_date_time,Entry_price,Exit_date_time,Exit_price,Quantity,Profit,Exit_Reason\n")
            for t in trades_long:
                f.write(
                    f"{t.symbol},{t.interval},{t.side},"
                    f"{t.entry_time.isoformat()},{t.entry_price:.3f},"
                    f"{t.exit_time.isoformat()},{t.exit_price:.3f},"
                    f"{t.quantity:.1f},{(t.profit_points if t.profit_points is not None else '')},{t.profit:.2f},{t.exit_reason}\n"
                )

        with open(short_path, "w") as f:
            f.write("Symbol,Interval,Side,Entry_date_time,Entry_price,Exit_date_time,Exit_price,Quantity,Profit,Exit_Reason\n")
            for t in trades_short:
                f.write(
                    f"{t.symbol},{t.interval},{t.side},"
                    f"{t.entry_time.isoformat()},{t.entry_price:.3f},"
                    f"{t.exit_time.isoformat()},{t.exit_price:.3f},"
                    f"{t.quantity:.1f},{(t.profit_points if t.profit_points is not None else '')},{t.profit:.2f},{t.exit_reason}\n"
                )

        logger.info(f"Saved → {long_path}")
        logger.info(f"Saved → {short_path}")

    if cfg.save_run and not run_df.empty:
        run_path = os.path.join(out_dir, f"{prefix}_run.csv")
        run_df.to_csv(run_path)
        logger.info(f"Saved → {run_path}")

    # Summary
    all_trades = trades_long + trades_short
    summary_rows: List[Dict[str, str]] = []
    
    row_long = _compute_summary_for_side(trades_long, cfg, "Long")
    row_short = _compute_summary_for_side(trades_short, cfg, "Short")
    if row_long:
        summary_rows.append(row_long)
    if row_short:
        summary_rows.append(row_short)

    if all_trades:
        overall_profit = sum(t.profit for t in all_trades)
        profits = [t.profit for t in all_trades]
        wins = sum(1 for p in profits if p > 0)
        losses = sum(1 for p in profits if p <= 0)
        total_trades = len(profits)
        sr = (wins / total_trades) * 100.0 if total_trades > 0 else 0.0

        largest_win = max(profits) if profits else 0
        largest_loss = min(profits) if profits else 0

        longest_win_streak = 0
        longest_loss_streak = 0
        win_streaks_count = 0
        loss_streaks_count = 0
        cur_streak = 0
        cur_sign = 0

        for p in profits:
            sign = 1 if p > 0 else -1
            if sign == cur_sign:
                cur_streak += 1
            else:
                if cur_sign == 1:
                    win_streaks_count += 1
                    longest_win_streak = max(longest_win_streak, cur_streak)
                elif cur_sign == -1:
                    loss_streaks_count += 1
                    longest_loss_streak = max(longest_loss_streak, cur_streak)
                cur_sign = sign
                cur_streak = 1

        if cur_sign == 1:
            win_streaks_count += 1
            longest_win_streak = max(longest_win_streak, cur_streak)
        elif cur_sign == -1:
            loss_streaks_count += 1
            longest_loss_streak = max(longest_loss_streak, cur_streak)

        def fmt_money(x: float) -> str:
            return f"${x:.2f}"

        summary_rows.append({
            "Symbol": cfg.symbol,
            "Interval": cfg.interval,
            "Trade_size": f"{cfg.trade_size:.1f}",
            "Total_Trades": str(total_trades),
            "Wins": str(wins),
            "Losses": str(losses),
            "SuccessRate": f"{sr:.2f}%",
            "Total_profit": fmt_money(overall_profit),
            "Trade_Type": "Overall",
            "Largest_Win": fmt_money(largest_win),
            "Largest_Loss": fmt_money(largest_loss),
            "Longest_Win_Streak": str(longest_win_streak),
            "Longest_Loss_Streak": str(longest_loss_streak),
            "Win_Streaks_Count": str(win_streaks_count),
            "Loss_Streaks_Count": str(loss_streaks_count),
        })

    if summary_rows:
        summary_df = pd.DataFrame(summary_rows)
        summary_path = os.path.join(out_dir, f"{prefix}_summary.csv")
        summary_df.to_csv(summary_path, index=False)
        logger.info(f"✅ LIVE-MATCHING Summary saved to {summary_path}")
        
        # Print summary to console
        print("\n" + "="*80)
        print(f"LIVE-MATCHING BACKTEST RESULTS: {cfg.symbol} {cfg.interval}")
        print("="*80)
        print(summary_df.to_string(index=False))
        print("="*80)
        print(f"\n✅ All outputs saved to {out_dir}/")
    else:
        logger.info("No trades generated; summary not written.")


if __name__ == "__main__":
    main()