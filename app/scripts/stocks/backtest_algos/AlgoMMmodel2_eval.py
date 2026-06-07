#!/usr/bin/env python3
"""
AlgoMM_eval v4 — Backtest with LIVE ALGO MATCHING + SGD Online Training

- Uses SGDClassifier for online/incremental learning
- Supports continuous model updates during backtest
- Saves checkpoints for continued training
"""

from __future__ import annotations

import os
import sys
import argparse
import logging
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import joblib

# --------------------------------------------------------------------------- #
# Repo wiring
# --------------------------------------------------------------------------- #

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app.scripts.stock_algos.base_wiring import StockBaseRunner, _ET
from app.scripts.research.mm_features2_builder import build_features

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s (AlgoMM) %(message)s")
logger = logging.getLogger("AlgoMM_eval")

DATA_DIR = os.getenv("DATA_DIR", "/var/www/stockwicks/data")
MODEL_DIR = os.getenv("MODEL_DIR", "/var/www/stockwicks/models")

from sklearn.linear_model import SGDClassifier
from sklearn.preprocessing import StandardScaler


# ============================================================================ #
# Dataclasses
# ============================================================================ #

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


@dataclass
class EvalConfig:
    symbol: str
    interval: str
    trade_size: float
    user_id: int
    builder_days: int = 60
    k_forward: int = 3

    # SGD-specific
    sgd_learning_rate: str = "optimal"
    sgd_alpha: float = 0.0001
    sgd_l1_ratio: float = 0.15
    sgd_max_iter: int = 1000
    sgd_tol: float = 1e-3
    sgd_penalty: str = "elasticnet"

    # Entry thresholds (slightly loosened for testing)
    long_threshold: float = 0.55
    short_threshold: float = 0.45

    # Exit thresholds
    long_exit_threshold: float = 0.52
    short_exit_threshold: float = 0.48

    # Volume filtering
    min_volume_multiplier: float = 1.0  # 1.0 = at/above avg volume

    # Risk management
    fixed_stop_loss: float = 200.0
    trailing_stop_activation: float = 0.8
    trailing_stop_distance: float = 1.0
    daily_loss_limit_usd: float = 500.0
    take_profit_percent: float = 1.5
    cooldown_sec: int = 60

    # Probability advantage
    min_prob_advantage: float = 0.00  # disabled for now to let trades happen

    # Online training
    online_update_frequency: int = 10
    warm_start: bool = True

    # General
    eod_close: bool = True
    auto_train: bool = False
    save_raw: bool = False
    save_features: bool = False
    save_trades: bool = False
    save_run: bool = False


# ============================================================================ #
# Feature prep / model helpers
# ============================================================================ #

def _prepare_X(feat_df: pd.DataFrame, feat_names: List[str]) -> pd.DataFrame:
    X = feat_df.reindex(columns=feat_names).copy()
    X = X.replace([np.inf, -np.inf], np.nan).ffill().bfill()
    med = X.median(numeric_only=True)
    X = X.fillna(med)

    all_nan = [c for c in X.columns if X[c].isna().all()]
    if all_nan:
        X[all_nan] = 0.0

    return X.fillna(0.0).astype(float)


def _create_sgd_model(cfg: EvalConfig) -> SGDClassifier:
    return SGDClassifier(
        loss="log_loss",
        penalty=cfg.sgd_penalty,
        alpha=cfg.sgd_alpha,
        l1_ratio=cfg.sgd_l1_ratio,
        fit_intercept=True,
        max_iter=cfg.sgd_max_iter,
        tol=cfg.sgd_tol,
        shuffle=True,
        epsilon=0.1,
        n_jobs=-1,
        random_state=42,
        learning_rate=cfg.sgd_learning_rate,
        eta0=0.01,
        power_t=0.25,
        early_stopping=False,
        validation_fraction=0.1,
        n_iter_no_change=5,
        class_weight="balanced",
        warm_start=cfg.warm_start,
        average=False,
    )


def _normalize_labels_to_binary(y_raw: np.ndarray) -> np.ndarray:
    """
    Map labels from {-1, 1} or {0, 1} to {0, 1} cleanly.
    """
    uniq = np.unique(y_raw)
    uniq_sorted = np.sort(uniq)

    if np.array_equal(uniq_sorted, np.array([-1, 1])):
        # Map -1 -> 0, +1 -> 1
        y = (y_raw > 0).astype(int)
        logger.info("Label normalization: mapped {-1,1} → {0,1}")
        return y
    elif np.array_equal(uniq_sorted, np.array([0, 1])):
        logger.info("Label normalization: labels already {0,1}")
        return y_raw.astype(int)
    else:
        logger.warning(f"Unexpected label set in y: {uniq}. Will map to {0,1} via rank.")
        # Generic fallback: rank-based mapping
        mapping = {lab: i for i, lab in enumerate(uniq_sorted)}
        return np.array([mapping[val] for val in y_raw], dtype=int)


def _load_or_train_sgd_model(
    model_path: str,
    feat_df: pd.DataFrame,
    feat_names: List[str],
    symbol: str,
    interval: str,
    cfg: EvalConfig,
):
    # Try load existing model
    if os.path.exists(model_path) and not cfg.auto_train:
        try:
            pack = joblib.load(model_path)
            model = pack["model"]
            features = pack.get("features", feat_names)
            scaler = pack.get("scaler", None)

            # Check that classes_ are {0,1}. If not, we will retrain with normalized labels.
            if hasattr(model, "classes_"):
                cls_sorted = np.sort(model.classes_)
                if np.array_equal(cls_sorted, np.array([0, 1])):
                    logger.info(f"Loaded SGD model from {model_path} with classes {model.classes_}")
                    if cfg.warm_start and hasattr(model, "partial_fit"):
                        logger.info("Model supports warm start/partial_fit for online updates")
                    return model, features, scaler
                else:
                    logger.warning(
                        "Loaded model from '%s' has incompatible classes %s. "
                        "Expected {0,1}. Forcing retrain with normalized labels.",
                        model_path, model.classes_
                    )
            else:
                logger.warning(
                    "Loaded model from '%s' has no 'classes_' attribute. "
                    "Forcing retrain with normalized labels.",
                    model_path,
                )
        except Exception as e:
            logger.warning(
                "Failed to load existing model '%s' (%s). Will retrain.",
                model_path, repr(e)
            )

    # Train fresh with normalized 0/1 labels
    if feat_df is None or feat_df.empty or "y" not in feat_df or "w" not in feat_df:
        raise RuntimeError("Cannot train: feature labels (y, w) missing in feat_df.")

    X = _prepare_X(feat_df, feat_names)
    y_raw = feat_df.loc[X.index, "y"].astype(int).values
    y = _normalize_labels_to_binary(y_raw)
    w = feat_df.loc[X.index, "w"].astype(float).values

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    model = _create_sgd_model(cfg)
    model.fit(X_scaled, y, sample_weight=w)

    os.makedirs(os.path.dirname(model_path), exist_ok=True)
    joblib.dump(
        {
            "model": model,
            "features": feat_names,
            "interval": interval,
            "symbol": symbol,
            "scaler": scaler,
            "config": {
                "learning_rate": cfg.sgd_learning_rate,
                "alpha": cfg.sgd_alpha,
                "penalty": cfg.sgd_penalty,
            },
        },
        model_path,
    )
    logger.info(f"Trained & saved SGD model to {model_path} (classes={model.classes_})")

    return model, feat_names, scaler


def _online_update_model(
    model: SGDClassifier,
    scaler: Optional[StandardScaler],
    X_new: pd.DataFrame,
    y_new: np.ndarray,
    sample_weight: Optional[np.ndarray] = None,
) -> bool:
    """
    Online update wrapper.

    IMPORTANT:
    - We do NOT pass `classes` here at all — model was already
      fully fit so it has `classes_` set from the initial training.
    - We assume y_new ∈ {0,1}.
    """
    try:
        if not hasattr(model, "partial_fit"):
            return False

        if not isinstance(X_new, pd.DataFrame):
            raise ValueError("X_new must be a DataFrame with feature names")

        if scaler is not None:
            X_scaled = scaler.transform(X_new)
        else:
            X_scaled = X_new.values

        uniq = np.unique(y_new)
        if not np.all(np.isin(uniq, [0, 1])):
            raise ValueError(f"Unexpected labels in y_new: {uniq}")

        # Final safety: if model.classes_ are not {0,1}, don't update
        if hasattr(model, "classes_"):
            cls_sorted = np.sort(model.classes_)
            if not np.array_equal(cls_sorted, np.array([0, 1])):
                raise ValueError(f"Model.classes_={model.classes_} incompatible with y_new∈{{0,1}}")

        model.partial_fit(X_scaled, y_new, sample_weight=sample_weight)
        return True

    except Exception as e:
        logger.error(f"Online update failed: {e}")
        return False


def _fetch_price_frame_latest(
    runner: StockBaseRunner,
    symbol: str,
    interval: str,
    builder_days: int,
) -> pd.DataFrame:
    df_raw = runner.fetch_source_bars(symbol)
    if df_raw is None or df_raw.empty:
        raise RuntimeError(f"No df_raw for symbol={symbol}")

    _, _, df = runner.resample_interval(df_raw, interval, symbol)
    if df is None or df.empty:
        raise RuntimeError(f"Resample failed: symbol={symbol}, interval={interval}")

    df = df.sort_index()

    logger.info(f"Fetched {len(df)} bars from {df.index[0]} to {df.index[-1]}")

    if builder_days and builder_days > 0:
        cutoff_date = df.index[-1].date() - timedelta(days=builder_days)
        mask = df.index.date >= cutoff_date
        df = df.loc[mask]
        logger.info(
            f"Filtered to last {builder_days} calendar days: "
            f"{df.index[0].date()} to {df.index[-1].date()} ({len(df)} bars)"
        )

    return df


def _build_features_with_fallback(symbol: str, interval: str, days: int, k_forward: int) -> pd.DataFrame:
    supported_intervals = ["1min", "5min", "15min", "30min", "1h", "4h", "1d"]

    interval_map = {
        "1m": "1min",
        "5m": "5min",
        "5mim": "5min",
        "15m": "15min",
        "30m": "30min",
        "1h": "1h",
        "4h": "4h",
        "1d": "1d",
    }

    mapped_interval = interval_map.get(interval, interval)
    if mapped_interval not in supported_intervals:
        logger.warning(f"Interval '{interval}' not supported, falling back to '5min'")
        mapped_interval = "5min"

    logger.info(f"Using interval '{mapped_interval}' for feature building (requested: '{interval}')")

    return build_features(
        symbol,
        mapped_interval,
        days=days,
        k_forward=k_forward,
    )


# ============================================================================ #
# LIVE-like helpers (entry/exit etc.)
# ============================================================================ #

def check_volume_requirement(df: pd.DataFrame, current_index: int, min_volume_multiplier: float = 1.0) -> bool:
    try:
        if current_index < 20:
            return True
        current_volume = df["volume"].iloc[current_index]
        avg_volume = df["volume"].iloc[max(0, current_index - 20):current_index].mean()
        return current_volume >= (avg_volume * min_volume_multiplier)
    except Exception:
        return True


def calculate_trailing_stop(entry_price: float, current_price: float,
                            position_side: str, activation: float, distance: float) -> Optional[float]:
    if position_side == "long":
        profit_pct = (current_price - entry_price) / entry_price * 100
        if profit_pct >= activation:
            return current_price * (1 - distance / 100)
    else:
        profit_pct = (entry_price - current_price) / entry_price * 100
        if profit_pct >= activation:
            return current_price * (1 + distance / 100)
    return None


def should_exit_trade(position_side: str, entry_price: float, current_price: float,
                      prob_up: float, prob_down: float, cfg: EvalConfig,
                      entry_time: datetime, current_time: datetime) -> Tuple[bool, str]:
    if not position_side:
        return False, ""

    trailing_stop = calculate_trailing_stop(
        entry_price, current_price, position_side,
        cfg.trailing_stop_activation, cfg.trailing_stop_distance
    )
    if trailing_stop:
        if (position_side == "long" and current_price <= trailing_stop) or \
           (position_side == "short" and current_price >= trailing_stop):
            return True, "TRAILING_STOP"

    if position_side == "long" and prob_up < cfg.long_exit_threshold:
        return True, "PROBABILITY_DROP"
    elif position_side == "short" and prob_down < (1 - cfg.short_exit_threshold):
        return True, "PROBABILITY_DROP"

    if position_side == "long":
        profit_pct = (current_price - entry_price) / entry_price * 100
    else:
        profit_pct = (entry_price - current_price) / entry_price * 100

    trade_duration = current_time - entry_time
    if trade_duration.total_seconds() < 300 and profit_pct >= 0.5:
        return True, "QUICK_PROFIT"

    return False, ""


def should_enter_trade(prob_up: float, prob_down: float, df: pd.DataFrame,
                       current_index: int, cfg: EvalConfig, allow_short: bool = True) -> Tuple[bool, str]:
    if not check_volume_requirement(df, current_index, cfg.min_volume_multiplier):
        return False, "LOW_VOLUME"

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
    if position_side == "long":
        gain_percent = (current_price - entry_price) / entry_price * 100
    else:
        gain_percent = (entry_price - current_price) / entry_price * 100

    return gain_percent >= threshold_percent, gain_percent


def get_smart_execution_price(df: pd.DataFrame, current_index: int, execution_type: str = "vwap") -> float:
    try:
        if execution_type == "vwap" and "vwap" in df.columns:
            return float(df["vwap"].iloc[current_index])
        elif execution_type == "twap":
            start_idx = max(0, current_index - 4)
            return float(df["close"].iloc[start_idx:current_index + 1].mean())
        else:
            return float(df["open"].iloc[current_index])
    except Exception:
        return float(df["open"].iloc[current_index])


# ============================================================================ #
# SGD prediction + label for online learning
# ============================================================================ #

def predict_probability_sgd(model: SGDClassifier, scaler: Optional[StandardScaler],
                            X: pd.DataFrame, feat_names: List[str]) -> np.ndarray:
    X_prepared = X.reindex(columns=feat_names).fillna(0).astype(float)

    if scaler:
        X_scaled = scaler.transform(X_prepared)
    else:
        X_scaled = X_prepared.values

    if hasattr(model, "predict_proba"):
        probas = model.predict_proba(X_scaled)
        return probas[:, 1]
    else:
        scores = model.decision_function(X_scaled)
        return 1 / (1 + np.exp(-scores))


def get_label_from_price_movement(df: pd.DataFrame, current_idx: int, k_forward: int) -> int:
    if current_idx + k_forward >= len(df):
        return 0

    current_price = df["close"].iloc[current_idx]
    future_price = df["close"].iloc[current_idx + k_forward]
    price_change_pct = (future_price - current_price) / current_price * 100

    threshold = 0.05  # 0.05% for more label variation
    return 1 if price_change_pct > threshold else 0


# ============================================================================ #
# Core simulation with online learning
# ============================================================================ #

def _simulate_trades_with_online_learning(
    cfg: EvalConfig,
    price_df: pd.DataFrame,
    feat_df: pd.DataFrame,
    model: SGDClassifier,
    scaler: Optional[StandardScaler],
    feat_names: List[str],
) -> Tuple[List[TradeRecord], List[TradeRecord], pd.DataFrame, SGDClassifier]:
    X_all = _prepare_X(feat_df, feat_names)
    initial_probs = predict_probability_sgd(model, scaler, X_all, feat_names)

    prob_series = pd.Series(initial_probs, index=feat_df.index, name="prob_up")
    window = max(int(cfg.k_forward), 1)
    prob_series = prob_series.rolling(window=window, min_periods=1).mean().rename("prob_up_smooth")

    prob_aligned = prob_series.reindex(price_df.index).ffill()

    # Probability diagnostics
    logger.info(f"Probability range: {prob_series.min():.3f} to {prob_series.max():.3f}")
    logger.info(f"Mean probability: {prob_series.mean():.3f}")
    logger.info(f"Probability > {cfg.long_threshold:.2f}: {(prob_series > cfg.long_threshold).sum()} bars")
    logger.info(f"Probability < {cfg.short_threshold:.2f}: {(prob_series < cfg.short_threshold).sum()} bars")

    run_rows = []
    trades_long: List[TradeRecord] = []
    trades_short: List[TradeRecord] = []

    position_side: Optional[str] = None
    entry_price: float = 0.0
    entry_time: Optional[datetime] = None
    quantity: float = cfg.trade_size

    daily_trades: List[TradeRecord] = []
    current_day = None

    online_update_buffer = []
    update_counter = 0

    idx = price_df.index
    if len(idx) < 2:
        logger.warning("Not enough bars to simulate.")
        return trades_long, trades_short, pd.DataFrame(), model

    for i in range(1, len(idx)):
        cur_ts = idx[i]

        # New day → recompute daily trades
        if current_day != cur_ts.date():
            current_day = cur_ts.date()
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
        exec_price = get_smart_execution_price(price_df, i, "vwap")

        action = "HOLD"
        just_closed = False

        # Daily loss limit
        daily_loss = sum(t.profit for t in daily_trades if t.profit < 0)
        if daily_loss <= -abs(cfg.daily_loss_limit_usd):
            if position_side:
                profit = (exec_price - entry_price) * quantity if position_side == "long" else (entry_price - exec_price) * quantity
                trec = TradeRecord(
                    cfg.symbol, cfg.interval, position_side,
                    entry_time, entry_price, cur_ts, exec_price, quantity, profit,
                    f"DAILY_LOSS_LIMIT_{daily_loss:.2f}"
                )
                if position_side == "long":
                    trades_long.append(trec)
                else:
                    trades_short.append(trec)

                position_side = None
                entry_price = 0.0
                entry_time = None
                action = "EXIT_DAILY_LOSS_LIMIT"
                just_closed = True
            else:
                action = "SKIP_DAILY_LOSS_LIMIT"

        # Exit logic
        if position_side is not None and not just_closed:
            # Fixed stop-loss
            if cfg.fixed_stop_loss > 0:
                unrealized = (exec_price - entry_price) * quantity if position_side == "long" else (entry_price - exec_price) * quantity
                if unrealized <= -abs(cfg.fixed_stop_loss):
                    profit = unrealized
                    trec = TradeRecord(
                        cfg.symbol, cfg.interval, position_side,
                        entry_time, entry_price, cur_ts, exec_price, quantity, profit,
                        f"Fixed Stop Loss ${cfg.fixed_stop_loss:.0f}",
                    )
                    if position_side == "long":
                        trades_long.append(trec)
                    else:
                        trades_short.append(trec)

                    position_side = None
                    entry_price = 0.0
                    entry_time = None
                    action = "EXIT_STOP_LOSS"
                    just_closed = True

            # Probability exits
            if position_side is not None and not just_closed:
                should_exit, reason = should_exit_trade(
                    position_side, entry_price, exec_price, prob_up, prob_down, cfg, entry_time, cur_ts
                )
                if should_exit:
                    profit = (exec_price - entry_price) * quantity if position_side == "long" else (entry_price - exec_price) * quantity
                    trec = TradeRecord(
                        cfg.symbol, cfg.interval, position_side,
                        entry_time, entry_price, cur_ts, exec_price, quantity, profit, reason
                    )
                    if position_side == "long":
                        trades_long.append(trec)
                    else:
                        trades_short.append(trec)

                    position_side = None
                    entry_price = 0.0
                    entry_time = None
                    action = f"EXIT_{reason}"
                    just_closed = True

            # Partial profits
            if position_side is not None and not just_closed and quantity >= 2:
                should_profit, gain_pct = should_take_partial_profit(
                    entry_price, exec_price, position_side, cfg.take_profit_percent
                )
                if should_profit:
                    partial_qty = quantity // 2
                    partial_profit = (exec_price - entry_price) * partial_qty if position_side == "long" else (entry_price - exec_price) * partial_qty
                    trec = TradeRecord(
                        cfg.symbol, cfg.interval, position_side,
                        entry_time, entry_price, cur_ts, exec_price, partial_qty, partial_profit,
                        f"PARTIAL_PROFIT_{gain_pct:.1f}%",
                    )
                    if position_side == "long":
                        trades_long.append(trec)
                    else:
                        trades_short.append(trec)

                    quantity = quantity - partial_qty
                    action = f"PARTIAL_PROFIT_{gain_pct:.1f}%"

        # Entry logic
        if position_side is None and not just_closed:
            should_enter, enter_dir = should_enter_trade(
                prob_up, prob_down, price_df, i, cfg, allow_short=True
            )
            if should_enter:
                position_side = enter_dir.lower()
                entry_price = exec_price
                entry_time = cur_ts
                quantity = cfg.trade_size
                action = f"OPEN_{enter_dir}"

        # EOD close
        if cfg.eod_close and position_side is not None and not just_closed:
            cur_time_et = cur_ts.astimezone(_ET) if cur_ts.tzinfo else _ET.localize(cur_ts)
            if cur_time_et.hour > 15 or (cur_time_et.hour == 15 and cur_time_et.minute >= 58):
                profit = (exec_price - entry_price) * quantity if position_side == "long" else (entry_price - exec_price) * quantity
                trec = TradeRecord(
                    cfg.symbol, cfg.interval, position_side,
                    entry_time, entry_price, cur_ts, exec_price, quantity, profit,
                    "END_OF_DAY_CLOSE",
                )
                if position_side == "long":
                    trades_long.append(trec)
                else:
                    trades_short.append(trec)

                position_side = None
                entry_price = 0.0
                entry_time = None
                action = "EXIT_EOD"
                just_closed = True

        # Online learning
        if cfg.online_update_frequency > 0:
            update_counter += 1

            if i < len(feat_df) and cur_ts in feat_df.index:
                feat_idx = feat_df.index.get_loc(cur_ts)
                X_current = _prepare_X(feat_df.iloc[[feat_idx]], feat_names)
                label = get_label_from_price_movement(price_df, i, cfg.k_forward)
                weight = 1.0
                online_update_buffer.append((X_current, label, weight))

            if update_counter >= cfg.online_update_frequency and online_update_buffer:
                try:
                    X_batch = pd.concat([x for x, _, _ in online_update_buffer], axis=0)
                    y_batch = np.array([y for _, y, _ in online_update_buffer])
                    w_batch = np.array([w for _, _, w in online_update_buffer])

                    if _online_update_model(model, scaler, X_batch, y_batch, w_batch):
                        logger.debug(f"Online model update at {cur_ts}: {len(online_update_buffer)} samples")

                        if i < len(feat_df):
                            X_current_all = _prepare_X(feat_df.iloc[: i + 1], feat_names)
                            updated_probs = predict_probability_sgd(model, scaler, X_current_all, feat_names)
                            prob_series.iloc[: i + 1] = updated_probs
                            prob_aligned = prob_series.reindex(price_df.index).ffill()

                    online_update_buffer = []
                    update_counter = 0

                except Exception as e:
                    logger.error(f"Online update failed: {e}")
                    online_update_buffer = []

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

        if just_closed:
            if trades_long:
                daily_trades.append(trades_long[-1])
            if trades_short:
                daily_trades.append(trades_short[-1])

    # Force close at last bar
    if position_side is not None:
        last_ts = idx[-1]
        last_close = float(price_df["close"].iloc[-1])
        profit = (last_close - entry_price) * quantity if position_side == "long" else (entry_price - last_close) * quantity
        trec = TradeRecord(
            cfg.symbol, cfg.interval, position_side,
            entry_time, entry_price, last_ts, last_close, quantity, profit,
            "MARKET_CLOSE",
        )
        if position_side == "long":
            trades_long.append(trec)
        else:
            trades_short.append(trec)
        logger.info(f"Closed {position_side} position at market close: {last_ts} @ {last_close}")

    run_df = pd.DataFrame(run_rows).set_index("timestamp")
    return trades_long, trades_short, run_df, model


# ============================================================================ #
# Summary metrics
# ============================================================================ #

def _compute_summary_for_side(
    trades: List[TradeRecord],
    cfg: EvalConfig,
    trade_type_label: str,
) -> Optional[Dict[str, str]]:
    if not trades:
        return None

    profits = [t.profit for t in trades]
    wins = sum(1 for p in profits if p > 0)
    losses = sum(1 for p in profits if p <= 0)

    total_trades = len(profits)
    total_profit = sum(profits)
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

    return {
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


# ============================================================================ #
# Main
# ============================================================================ #

def main(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(description="AlgoMM SGD backtest evaluator v4")

    parser.add_argument("-s", "--symbol", required=True)
    parser.add_argument("-i", "--interval", required=True)
    parser.add_argument("-q", "--trade-size", type=float, required=True)
    parser.add_argument("-u", "--user-id", type=int, required=True)

    # SGD config
    parser.add_argument("--sgd-learning-rate", type=str, default="optimal")
    parser.add_argument("--sgd-alpha", type=float, default=0.0001)
    parser.add_argument("--sgd-l1-ratio", type=float, default=0.15)
    parser.add_argument("--sgd-max-iter", type=int, default=1000)
    parser.add_argument("--sgd-tol", type=float, default=1e-3)
    parser.add_argument("--sgd-penalty", type=str, default="elasticnet")
    parser.add_argument("--online-update-frequency", type=int, default=10)
    parser.add_argument("--warm-start", action="store_true", default=True)

    # Strategy config
    parser.add_argument("--builder-days", type=int, default=60)
    parser.add_argument("--k-forward", type=int, default=3)
    parser.add_argument("--long-threshold", type=float, default=0.55)
    parser.add_argument("--short-threshold", type=float, default=0.45)
    parser.add_argument("--long-exit-threshold", type=float, default=0.52)
    parser.add_argument("--short-exit-threshold", type=float, default=0.48)
    parser.add_argument("--min-volume-multiplier", type=float, default=1.0)
    parser.add_argument("--min-prob-advantage", type=float, default=0.0)
    parser.add_argument("--fixed-stop-loss", type=float, default=200.0)
    parser.add_argument("--trailing-stop-activation", type=float, default=0.8)
    parser.add_argument("--trailing-stop-distance", type=float, default=1.0)
    parser.add_argument("--daily-loss-limit-usd", type=float, default=500.0)
    parser.add_argument("--take-profit-percent", type=float, default=1.5)
    parser.add_argument("--cooldown-sec", type=int, default=60)
    parser.add_argument("--eod-close", action="store_true", default=True)

    # Output / training
    parser.add_argument("--auto-train", action="store_true", default=False)
    parser.add_argument("--save-raw", action="store_true", default=False)
    parser.add_argument("--save-features", action="store_true", default=False)
    parser.add_argument("--save-trades", action="store_true", default=False)
    parser.add_argument("--save-run", action="store_true", default=False)
    parser.add_argument("--save-updated-model", action="store_true", default=False)

    args = parser.parse_args(argv)

    cfg = EvalConfig(
        symbol=args.symbol.upper(),
        interval=args.interval,
        trade_size=args.trade_size,
        user_id=args.user_id,
        sgd_learning_rate=args.sgd_learning_rate,
        sgd_alpha=args.sgd_alpha,
        sgd_l1_ratio=args.sgd_l1_ratio,
        sgd_max_iter=args.sgd_max_iter,
        sgd_tol=args.sgd_tol,
        sgd_penalty=args.sgd_penalty,
        online_update_frequency=args.online_update_frequency,
        warm_start=args.warm_start,
        builder_days=args.builder_days,
        k_forward=args.k_forward,
        long_threshold=args.long_threshold,
        short_threshold=args.short_threshold,
        long_exit_threshold=args.long_exit_threshold,
        short_exit_threshold=args.short_exit_threshold,
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

    prefix = f"{cfg.user_id}_{cfg.symbol}_{cfg.interval}_sgd"

    logger.info(f"Starting SGD backtest: {cfg.symbol} {cfg.interval}")
    logger.info(f"SGD Config: LR={cfg.sgd_learning_rate}, α={cfg.sgd_alpha}, penalty={cfg.sgd_penalty}")
    logger.info(f"Entry thresholds: Long≥{cfg.long_threshold:.2f}, Short≤{cfg.short_threshold:.2f}")
    logger.info(f"Exit thresholds: Long<{cfg.long_exit_threshold:.2f}, Short>{cfg.short_exit_threshold:.2f}")
    logger.info(f"Online updates: every {cfg.online_update_frequency} bars")
    logger.info(f"Warm start: {cfg.warm_start}")

    # Price data
    runner = StockBaseRunner()
    price_df = _fetch_price_frame_latest(runner, cfg.symbol, cfg.interval, cfg.builder_days)
    logger.info(f"Price data: {len(price_df)} bars from {price_df.index[0]} to {price_df.index[-1]}")

    # Features
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
    logger.info(f"Feature columns: {list(feat_df.columns[:10])}...")
    feat_names = [c for c in feat_df.columns if c not in ("y", "w")]
    logger.info(f"Using {len(feat_names)} features for training")

    if cfg.save_features:
        feat_path = os.path.join(out_dir, f"{prefix}_features.csv")
        feat_df.to_csv(feat_path)
        logger.info(f"Saved features → {feat_path}")

    # Model
    model_path = os.path.join(MODEL_DIR, f"sgd_mm2_{cfg.symbol}_{cfg.interval}_k{cfg.k_forward}.joblib")
    model, used_feats, scaler = _load_or_train_sgd_model(
        model_path, feat_df, feat_names, cfg.symbol, cfg.interval, cfg
    )

    # Simulation
    logger.info("Simulating trades with SGD online learning...")
    trades_long, trades_short, run_df, updated_model = _simulate_trades_with_online_learning(
        cfg, price_df, feat_df, model, scaler, used_feats
    )

    logger.info(f"Trade results: Long={len(trades_long)}, Short={len(trades_short)}")

    # Save updated model if requested
    if args.save_updated_model and updated_model is not model:
        updated_model_path = os.path.join(MODEL_DIR, f"sgd_updated_{cfg.symbol}_{cfg.interval}.joblib")
        joblib.dump(
            {
                "model": updated_model,
                "features": used_feats,
                "scaler": scaler,
                "interval": cfg.interval,
                "symbol": cfg.symbol,
                "last_updated": datetime.now().isoformat(),
            },
            updated_model_path,
        )
        logger.info(f"Saved updated model → {updated_model_path}")

    # Save trades
    if cfg.save_trades:
        long_path = os.path.join(out_dir, f"{prefix}_trades_long.csv")
        short_path = os.path.join(out_dir, f"{prefix}_trades_short.csv")

        for trades, path in [(trades_long, long_path), (trades_short, short_path)]:
            if trades:
                df_trades = pd.DataFrame([{
                    "symbol": t.symbol,
                    "interval": t.interval,
                    "side": t.side,
                    "entry_time": t.entry_time.isoformat(),
                    "entry_price": t.entry_price,
                    "exit_time": t.exit_time.isoformat(),
                    "exit_price": t.exit_price,
                    "quantity": t.quantity,
                    "profit": t.profit,
                    "exit_reason": t.exit_reason,
                } for t in trades])
                df_trades.to_csv(path, index=False)
                logger.info(f"Saved trades → {path}")

    # Save run
    if cfg.save_run and not run_df.empty:
        run_path = os.path.join(out_dir, f"{prefix}_run.csv")
        run_df.to_csv(run_path)
        logger.info(f"Saved run data → {run_path}")

    # Summary
    summary_rows = []
    for trades, label in [(trades_long, "Long"), (trades_short, "Short")]:
        if trades:
            summary_rows.append(_compute_summary_for_side(trades, cfg, label))

    if trades_long or trades_short:
        all_trades = trades_long + trades_short
        overall_summary = _compute_summary_for_side(all_trades, cfg, "Overall")
        if overall_summary:
            summary_rows.append(overall_summary)

    if summary_rows:
        summary_df = pd.DataFrame(summary_rows)
        summary_path = os.path.join(out_dir, f"{prefix}_summary.csv")
        summary_df.to_csv(summary_path, index=False)
        logger.info(f"✅ SGD Summary saved to {summary_path}")

        print("\n" + "=" * 80)
        print(f"SGD BACKTEST RESULTS: {cfg.symbol} {cfg.interval}")
        print("=" * 80)
        print(summary_df.to_string(index=False))
        print("=" * 80)
        print(f"\n✅ All outputs saved to {out_dir}/")
    else:
        logger.info("No trades generated; summary not written.")


if __name__ == "__main__":
    main()
