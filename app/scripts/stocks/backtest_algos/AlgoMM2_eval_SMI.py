#!/usr/bin/env python3
"""
AlgoMM2_eval — Next 3 Bars Probability using ONLY SMI + Volume Factors

KEY FEATURES:
- Pure SMI (Stochastic Momentum Index) for momentum
- Pure Volume factors for confirmation  
- Minimal feature set for clear signal interpretation
- Predicts probability for next 3 bars movement
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

# ---- Logging -----------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s (AlgoMM2) %(message)s")
logger = logging.getLogger("AlgoMM2_eval")

DATA_DIR = os.getenv("DATA_DIR", "/var/www/stockwicks/data")
MODEL_DIR = os.getenv("MODEL_DIR", "/var/www/stockwicks/models")


# ==== PURE SMI + Volume Feature Engineering ===================================

def calculate_smi(close: pd.Series, high: pd.Series, low: pd.Series, 
                 period: int = 14, smooth_k: int = 3, smooth_d: int = 3) -> pd.Series:
    """
    Calculate Stochastic Momentum Index (SMI)
    SMI measures where the close is relative to the midpoint of the high-low range.
    """
    # Calculate the midpoint of the high-low range
    hl_range = (high.rolling(period).max() + low.rolling(period).min()) / 2
    diff = close - hl_range
    
    # Calculate the range
    price_range = high.rolling(period).max() - low.rolling(period).min()
    price_range = price_range.replace(0, np.nan).ffill().bfill()
    
    # Calculate raw SMI
    smi_raw = (diff / price_range) * 100
    
    # Apply smoothing
    smi_k = smi_raw.rolling(smooth_k).mean()
    smi = smi_k.rolling(smooth_d).mean()
    
    return smi.fillna(0.0)

def calculate_volume_factors(volume: pd.Series) -> pd.DataFrame:
    """Calculate pure volume-based factors."""
    volume = volume.replace(0, np.nan).ffill().bfill()
    
    features = pd.DataFrame(index=volume.index)
    
    # Basic volume indicators
    features['volume_ma_5'] = volume.rolling(5).mean()
    features['volume_ma_10'] = volume.rolling(10).mean()
    features['volume_ma_20'] = volume.rolling(20).mean()
    
    # Volume ratios (current vs average)
    features['volume_ratio_5'] = volume / features['volume_ma_5']
    features['volume_ratio_10'] = volume / features['volume_ma_10']
    features['volume_ratio_20'] = volume / features['volume_ma_20']
    
    # Volume momentum
    features['volume_momentum_5'] = volume / volume.shift(5) - 1
    features['volume_momentum_10'] = volume / volume.shift(10) - 1
    
    # Volume zones
    features['high_volume_zone'] = (volume > volume.rolling(20).quantile(0.7)).astype(float)
    features['low_volume_zone'] = (volume < volume.rolling(20).quantile(0.3)).astype(float)
    
    return features.fillna(1.0)  # Default to 1.0 (average) when NaN

def build_pure_smi_volume_features(df: pd.DataFrame, k_forward: int = 3) -> pd.DataFrame:
    """
    Build minimal feature set using ONLY SMI and Volume factors.
    """
    # Calculate multiple SMI timeframes
    smi_fast = calculate_smi(df['close'], df['high'], df['low'], period=10, smooth_k=2, smooth_d=2)
    smi_medium = calculate_smi(df['close'], df['high'], df['low'], period=14, smooth_k=3, smooth_d=3)
    smi_slow = calculate_smi(df['close'], df['high'], df['low'], period=20, smooth_k=3, smooth_d=3)
    
    # SMI-based features ONLY
    smi_features = pd.DataFrame({
        'smi_fast': smi_fast,
        'smi_medium': smi_medium,
        'smi_slow': smi_slow,
        
        # SMI momentum and relationships
        'smi_trend': smi_fast - smi_slow,
        'smi_momentum': smi_fast.diff(3),
        'smi_acceleration': smi_fast.diff(3).diff(2),
        
        # SMI levels
        'smi_overbought': (smi_fast > 40).astype(float),
        'smi_oversold': (smi_fast < -40).astype(float),
        'smi_strong_bullish': (smi_fast > 20).astype(float),
        'smi_strong_bearish': (smi_fast < -20).astype(float),
    }, index=df.index)
    
    # Volume factors ONLY
    volume_features = calculate_volume_factors(df['volume'])
    
    # Combine ONLY SMI + Volume features
    all_features = pd.concat([smi_features, volume_features], axis=1)
    
    # Create labels for next k_forward bars
    future_return = (df['close'].shift(-k_forward) / df['close'] - 1.0)
    all_features['y'] = (future_return > 0).astype(float)
    all_features['w'] = future_return.abs().fillna(0.0)
    
    # Clean up
    all_features = all_features.replace([np.inf, -np.inf], np.nan)
    all_features = all_features.ffill().bfill().fillna(0.0)
    
    # Remove rows where we don't have future returns (end of dataset)
    all_features = all_features[all_features['w'].notna()]
    
    logger.info(f"Built PURE SMI+Volume features: {len(all_features)} rows, {len(all_features.columns)-2} features")
    logger.info(f"SMI Features: {list(smi_features.columns)}")
    logger.info(f"Volume Features: {list(volume_features.columns)}")
    
    return all_features

def predict_smi_volume_probability(model, features_df: pd.DataFrame, feature_names: List[str]) -> np.ndarray:
    """Predict probability using pure SMI+Volume model."""
    X = features_df[feature_names].copy()
    X = X.replace([np.inf, -np.inf], np.nan).ffill().bfill().fillna(0.0)
    
    if hasattr(model, 'predict_proba'):
        proba = model.predict_proba(X)
        return proba[:, 1]  # Probability of up movement
    else:
        # For models without predict_proba, use decision function
        decisions = model.decision_function(X)
        # Convert to probability using sigmoid
        from scipy.special import expit
        return expit(decisions)


# ==== Configuration ===========================================================

@dataclass
class AlgoMM2Config:
    symbol: str
    interval: str
    trade_size: float
    user_id: int
    builder_days: int = 60
    k_forward: int = 3
    
    # Entry thresholds
    long_threshold: float = 0.60
    short_threshold: float = 0.40
    
    # SMI-specific parameters
    min_smi_strength: float = 15.0  # Minimum |SMI| value for valid signals
    volume_confirmation_required: bool = True
    min_volume_ratio: float = 1.1   # Minimum volume vs average
    
    # Risk management
    fixed_stop_loss: float = 200.0
    daily_loss_limit_usd: float = 500.0
    
    # General
    eod_close: bool = True
    auto_train: bool = False
    save_raw: bool = False
    save_features: bool = False
    save_trades: bool = False
    save_run: bool = False


# ==== Pure SMI + Volume Trading Logic ========================================

def should_enter_pure_smi_trade(prob_up: float, prob_down: float, df: pd.DataFrame, 
                               current_index: int, cfg: AlgoMM2Config) -> Tuple[bool, str]:
    """
    Pure SMI + Volume entry logic.
    """
    if current_index < 20:  # Need enough history for SMI calculation
        return False, "INSUFFICIENT_HISTORY"
    
    # Get current SMI values
    current_smi_fast = df.get('smi_fast', pd.Series(0, index=df.index)).iloc[current_index]
    current_smi_medium = df.get('smi_medium', pd.Series(0, index=df.index)).iloc[current_index]
    
    # SMI strength filter
    smi_strength = abs(current_smi_fast)
    if smi_strength < cfg.min_smi_strength:
        return False, f"WEAK_SMI_{smi_strength:.1f}"
    
    # Volume confirmation
    if cfg.volume_confirmation_required:
        volume_ratio = df.get('volume_ratio_5', pd.Series(1, index=df.index)).iloc[current_index]
        if volume_ratio < cfg.min_volume_ratio:
            return False, f"LOW_VOLUME_{volume_ratio:.2f}"
    
    # Long entry: Probability high + SMI bullish + SMI alignment
    if prob_up >= cfg.long_threshold:
        if current_smi_fast > 0 and current_smi_fast > current_smi_medium:
            return True, f"LONG_SMI_{current_smi_fast:.1f}"
    
    # Short entry: Probability low + SMI bearish + SMI alignment  
    elif prob_down >= (1 - cfg.short_threshold):
        if current_smi_fast < 0 and current_smi_fast < current_smi_medium:
            return True, f"SHORT_SMI_{current_smi_fast:.1f}"
    
    return False, "NO_SMI_CONFIRMATION"

def should_exit_pure_smi_trade(position_side: str, entry_price: float, current_price: float, 
                              prob_up: float, prob_down: float, df: pd.DataFrame,
                              current_index: int, cfg: AlgoMM2Config) -> Tuple[bool, str]:
    """
    Pure SMI + Volume exit logic.
    """
    if not position_side:
        return False, ""
    
    # Get current SMI values
    current_smi_fast = df.get('smi_fast', pd.Series(0, index=df.index)).iloc[current_index]
    current_smi_medium = df.get('smi_medium', pd.Series(0, index=df.index)).iloc[current_index]
    
    # SMI reversal exits
    if position_side == "long":
        # Exit if SMI turns bearish or loses momentum
        if current_smi_fast < 0 or current_smi_fast < current_smi_medium:
            return True, f"SMI_REVERSAL_{current_smi_fast:.1f}"
        # Exit if probability drops
        if prob_up < 0.5:  # Simple 50% threshold for exit
            return True, "PROBABILITY_DROP"
            
    elif position_side == "short":
        # Exit if SMI turns bullish or loses momentum
        if current_smi_fast > 0 or current_smi_fast > current_smi_medium:
            return True, f"SMI_REVERSAL_{current_smi_fast:.1f}"
        # Exit if probability drops
        if prob_down < 0.5:  # Simple 50% threshold for exit
            return True, "PROBABILITY_DROP"
    
    return False, "HOLD"


# ==== Data Fetching ==========================================================

def _fetch_price_frame_latest(runner, symbol: str, interval: str, builder_days: int) -> pd.DataFrame:
    """Fetch price data for SMI calculation."""
    df_raw = runner.fetch_source_bars(symbol)
    if df_raw is None or df_raw.empty:
        raise RuntimeError(f"No df_raw for symbol={symbol}")
    
    latest_time, _, df = runner.resample_interval(df_raw, interval, symbol)
    if df is None or df.empty:
        raise RuntimeError(f"Resample failed: symbol={symbol}, interval={interval}")
    
    df = df.sort_index()
    
    # Filter by calendar days
    if builder_days and builder_days > 0:
        cutoff_date = df.index[-1].date() - timedelta(days=builder_days)
        mask = df.index.date >= cutoff_date
        df = df.loc[mask]
        
    return df


# ==== Backtest Simulation ====================================================

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
    profit: float
    exit_reason: str

def simulate_smi_trades(
    cfg: AlgoMM2Config,
    price_df: pd.DataFrame,
    prob_series: pd.Series,
) -> Tuple[List[TradeRecord], List[TradeRecord], pd.DataFrame]:
    """
    Backtest simulation using pure SMI + Volume logic.
    """
    prob_aligned = prob_series.reindex(price_df.index).ffill()

    run_rows = []
    trades_long: List[TradeRecord] = []
    trades_short: List[TradeRecord] = []

    position_side: Optional[str] = None
    entry_price: float = 0.0
    entry_time: Optional[datetime] = None
    quantity: float = cfg.trade_size

    idx = price_df.index
    
    if len(idx) < 2:
        logger.warning("Not enough bars to simulate.")
        return trades_long, trades_short, pd.DataFrame()

    for i in range(1, len(idx)):
        cur_ts = idx[i]
        prev_ts = idx[i - 1]

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
        
        # Use current close for execution
        exec_price = float(price_df["close"].iloc[i])
        
        action = "HOLD"
        exit_reason = ""
        just_closed = False

        # ========== EXIT LOGIC ==========
        if position_side is not None and not just_closed:
            # Fixed stop loss
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
                            entry_time, entry_price, cur_ts, exec_price, quantity, profit, reason
                        ))
                    else:
                        trades_short.append(TradeRecord(
                            cfg.symbol, cfg.interval, "short",
                            entry_time, entry_price, cur_ts, exec_price, quantity, profit, reason
                        ))
                    
                    position_side = None
                    entry_price = 0.0
                    entry_time = None
                    action = "EXIT_STOP_LOSS"
                    just_closed = True

            # SMI Exit Logic
            if position_side is not None and not just_closed:
                should_exit, exit_reason = should_exit_pure_smi_trade(
                    position_side, entry_price, exec_price, prob_up, prob_down, price_df, i, cfg
                )
                if should_exit:
                    profit = (exec_price - entry_price) * quantity if position_side == "long" else (entry_price - exec_price) * quantity
                    
                    if position_side == "long":
                        trades_long.append(TradeRecord(
                            cfg.symbol, cfg.interval, "long",
                            entry_time, entry_price, cur_ts, exec_price, quantity, profit, exit_reason
                        ))
                    else:
                        trades_short.append(TradeRecord(
                            cfg.symbol, cfg.interval, "short",
                            entry_time, entry_price, cur_ts, exec_price, quantity, profit, exit_reason
                        ))
                    
                    position_side = None
                    entry_price = 0.0
                    entry_time = None
                    action = f"EXIT_{exit_reason}"
                    just_closed = True

        # ========== ENTRY LOGIC ==========
        if position_side is None and not just_closed:
            should_enter, enter_direction = should_enter_pure_smi_trade(
                prob_up, prob_down, price_df, i, cfg
            )
            if should_enter:
                position_side = enter_direction.lower().split('_')[0]  # Extract 'long' or 'short'
                entry_price = exec_price
                entry_time = cur_ts
                quantity = cfg.trade_size
                action = f"OPEN_{enter_direction}"

        # ========== EOD CLOSE ==========
        if cfg.eod_close and position_side is not None and not just_closed:
            # Check if this is end of day (3:58 PM ET or later)
            cur_time_et = cur_ts.astimezone(_ET) if cur_ts.tzinfo else _ET.localize(cur_ts)
            if cur_time_et.hour >= 15 and (cur_time_et.hour > 15 or cur_time_et.minute >= 58):
                profit = (exec_price - entry_price) * quantity if position_side == "long" else (entry_price - exec_price) * quantity
                reason = "END_OF_DAY_CLOSE"
                
                if position_side == "long":
                    trades_long.append(TradeRecord(
                        cfg.symbol, cfg.interval, "long",
                        entry_time, entry_price, cur_ts, exec_price, quantity, profit, reason
                    ))
                else:
                    trades_short.append(TradeRecord(
                        cfg.symbol, cfg.interval, "short",
                        entry_time, entry_price, cur_ts, exec_price, quantity, profit, reason
                    ))
                
                position_side = None
                entry_price = 0.0
                entry_time = None
                action = "EXIT_EOD"
                just_closed = True

        run_rows.append({
            "timestamp": cur_ts,
            "open": exec_price,
            "high": price_df["high"].iloc[i],
            "low": price_df["low"].iloc[i],
            "close": float(price_df["close"].iloc[i]),
            "prob_up": prob_up,
            "prob_down": prob_down,
            "position": position_side or "flat",
            "action": action,
            "quantity": quantity if position_side else 0,
        })

    # Force close at market close
    if position_side is not None:
        last_ts = idx[-1]
        last_close = float(price_df["close"].iloc[-1])
        profit = (last_close - entry_price) * quantity if position_side == "long" else (entry_price - last_close) * quantity
        reason = "MARKET_CLOSE"
        
        if position_side == "long":
            trades_long.append(TradeRecord(
                cfg.symbol, cfg.interval, "long",
                entry_time, entry_price, last_ts, last_close, quantity, profit, reason
            ))
        else:
            trades_short.append(TradeRecord(
                cfg.symbol, cfg.interval, "short",
                entry_time, entry_price, last_ts, last_close, quantity, profit, reason
            ))
        
        logger.info(f"Closed {position_side} position at market close: {last_ts} @ {last_close}")

    run_df = pd.DataFrame(run_rows).set_index("timestamp")
    return trades_long, trades_short, run_df


# ==== Main Evaluation ========================================================

def main(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(description="AlgoMM2 Pure SMI+Volume - Next 3 Bars Probability")
    
    parser.add_argument("-s", "--symbol", required=True)
    parser.add_argument("-i", "--interval", required=True)
    parser.add_argument("-q", "--trade-size", type=float, required=True)
    parser.add_argument("-u", "--user-id", type=int, required=True)
    
    parser.add_argument("--builder-days", type=int, default=60)
    parser.add_argument("--k-forward", type=int, default=3)
    parser.add_argument("--long-threshold", type=float, default=0.60)
    parser.add_argument("--short-threshold", type=float, default=0.40)
    
    parser.add_argument("--min-smi-strength", type=float, default=15.0)
    parser.add_argument("--volume-confirmation", action="store_true", default=True)
    parser.add_argument("--min-volume-ratio", type=float, default=1.1)
    
    parser.add_argument("--fixed-stop-loss", type=float, default=200.0)
    parser.add_argument("--daily-loss-limit-usd", type=float, default=500.0)
    
    parser.add_argument("--eod-close", action="store_true", default=True)
    parser.add_argument("--auto-train", action="store_true", default=False)
    
    parser.add_argument("--save-raw", action="store_true", default=False)
    parser.add_argument("--save-features", action="store_true", default=False)
    parser.add_argument("--save-trades", action="store_true", default=False)
    parser.add_argument("--save-run", action="store_true", default=False)
    
    args = parser.parse_args(argv)
    
    cfg = AlgoMM2Config(
        symbol=args.symbol.upper(),
        interval=args.interval,
        trade_size=args.trade_size,
        user_id=args.user_id,
        builder_days=args.builder_days,
        k_forward=args.k_forward,
        long_threshold=args.long_threshold,
        short_threshold=args.short_threshold,
        min_smi_strength=args.min_smi_strength,
        volume_confirmation_required=args.volume_confirmation,
        min_volume_ratio=args.min_volume_ratio,
        fixed_stop_loss=args.fixed_stop_loss,
        daily_loss_limit_usd=args.daily_loss_limit_usd,
        eod_close=args.eod_close,
        auto_train=args.auto_train,
        save_raw=args.save_raw,
        save_features=args.save_features,
        save_trades=args.save_trades,
        save_run=args.save_run,
    )
    
    out_dir = os.path.join(DATA_DIR, str(cfg.user_id))
    os.makedirs(out_dir, exist_ok=True)
    
    prefix = f"algomm2_pure_{cfg.user_id}_{cfg.symbol}_{cfg.interval}"
    
    logger.info(f"Starting PURE SMI+Volume evaluation: {cfg.symbol} {cfg.interval}")
    logger.info(f"Predicting next {cfg.k_forward} bars using ONLY SMI + Volume")
    logger.info(f"SMI Strength: min {cfg.min_smi_strength}, Volume Ratio: min {cfg.min_volume_ratio}")
    
    # Fetch price data
    runner = StockBaseRunner()
    price_df = _fetch_price_frame_latest(runner, cfg.symbol, cfg.interval, cfg.builder_days)
    logger.info(f"Price data: {len(price_df)} bars from {price_df.index[0]} to {price_df.index[-1]}")
    
    # Build PURE SMI+Volume features
    logger.info("Building PURE SMI+Volume features...")
    feat_df = build_pure_smi_volume_features(price_df, cfg.k_forward)
    
    if feat_df is None or feat_df.empty:
        raise RuntimeError("SMI+Volume feature building returned empty DataFrame")
    
    logger.info(f"Pure features built: {len(feat_df)} rows")
    
    if cfg.save_features:
        feat_path = os.path.join(out_dir, f"{prefix}_features.csv")
        feat_df.to_csv(feat_path)
        logger.info(f"Saved pure features → {feat_path}")
    
    # Prepare features for training/prediction
    feat_names = [c for c in feat_df.columns if c not in ("y", "w")]
    X = feat_df[feat_names].copy()
    X = X.replace([np.inf, -np.inf], np.nan).ffill().bfill().fillna(0.0)
    y = feat_df['y'].values
    
    # Train or load model
    model_path = os.path.join(MODEL_DIR, f"algomm2_pure_{cfg.symbol}_{cfg.interval}_k{cfg.k_forward}.joblib")
    
    if os.path.exists(model_path) and not cfg.auto_train:
        try:
            pack = joblib.load(model_path)
            model = pack["model"]
            features_used = pack.get("features", feat_names)
            logger.info(f"Loaded Pure SMI+Volume model from {model_path}")
        except Exception as e:
            logger.warning(f"Failed to load model, training new: {e}")
            cfg.auto_train = True
    
    if cfg.auto_train or not os.path.exists(model_path):
        from sklearn.ensemble import HistGradientBoostingClassifier
        
        model = HistGradientBoostingClassifier(
            max_depth=4,
            learning_rate=0.1,
            max_iter=300,
            l2_regularization=1.0,
            random_state=42
        )
        model.fit(X, y, sample_weight=feat_df['w'].values)
        
        os.makedirs(os.path.dirname(model_path), exist_ok=True)
        joblib.dump(
            {"model": model, "features": feat_names, "interval": cfg.interval, "symbol": cfg.symbol},
            model_path
        )
        logger.info(f"Trained & saved Pure SMI+Volume model to {model_path}")
        features_used = feat_names
    
    # Get probabilities
    probs = predict_smi_volume_probability(model, feat_df, features_used)
    prob_series = pd.Series(probs, index=feat_df.index, name="prob_up")
    
    # Merge SMI features back to price_df for trading logic
    for col in feat_names:
        price_df[col] = feat_df[col]
    
    # Analyze signal quality
    long_signals = (prob_series >= cfg.long_threshold).sum()
    short_signals = (prob_series <= cfg.short_threshold).sum()
    total_bars = len(prob_series)
    
    logger.info(f"Signal Analysis:")
    logger.info(f"  Long signals (≥{cfg.long_threshold}): {long_signals} ({long_signals/total_bars*100:.1f}%)")
    logger.info(f"  Short signals (≤{cfg.short_threshold}): {short_signals} ({short_signals/total_bars*100:.1f}%)")
    logger.info(f"  No signal: {total_bars - long_signals - short_signals} ({(total_bars - long_signals - short_signals)/total_bars*100:.1f}%)")
    
    # Run backtest simulation
    logger.info("Running SMI+Volume backtest simulation...")
    trades_long, trades_short, run_df = simulate_smi_trades(cfg, price_df, prob_series)
    
    logger.info(f"Backtest results: Long trades={len(trades_long)}, Short trades={len(trades_short)}")
    
    # Calculate performance
    all_trades = trades_long + trades_short
    if all_trades:
        total_profit = sum(t.profit for t in all_trades)
        winning_trades = sum(1 for t in all_trades if t.profit > 0)
        win_rate = (winning_trades / len(all_trades)) * 100
        
        logger.info(f"Performance: Total Profit=${total_profit:.2f}, Win Rate={win_rate:.1f}%")
        
        # Save trades if requested
        if cfg.save_trades:
            trades_path = os.path.join(out_dir, f"{prefix}_trades.csv")
            trades_df = pd.DataFrame([
                {
                    'symbol': t.symbol, 'interval': t.interval, 'side': t.side,
                    'entry_time': t.entry_time, 'entry_price': t.entry_price,
                    'exit_time': t.exit_time, 'exit_price': t.exit_price,
                    'quantity': t.quantity, 'profit': t.profit, 'exit_reason': t.exit_reason
                }
                for t in all_trades
            ])
            trades_df.to_csv(trades_path, index=False)
            logger.info(f"Saved trades → {trades_path}")
    
    if cfg.save_run and not run_df.empty:
        run_path = os.path.join(out_dir, f"{prefix}_run.csv")
        run_df.to_csv(run_path)
        logger.info(f"Saved run data → {run_path}")
    
    if cfg.save_raw:
        raw_path = os.path.join(out_dir, f"{prefix}_data.csv")
        price_df.to_csv(raw_path)
        logger.info(f"Saved full data → {raw_path}")
    
    logger.info("Pure SMI+Volume evaluation completed successfully!")
    logger.info(f"Model ready for {cfg.symbol} {cfg.interval} - Pure SMI+Volume strategy")
    
    return {
        'probabilities': prob_series,
        'features': feat_df,
        'price_data': price_df,
        'trades_long': trades_long,
        'trades_short': trades_short,
        'model': model,
        'config': cfg
    }


if __name__ == "__main__":
    main()