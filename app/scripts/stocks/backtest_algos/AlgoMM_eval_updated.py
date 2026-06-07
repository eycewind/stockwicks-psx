#!/usr/bin/env python3
"""
AlgoMM_eval — Backtest (cleaned) with:
- Optional 3-class labels (UP / FLAT / DOWN) to reduce chop sensitivity
- Time-safe probability calibration (train early, calibrate on later slice)
- Probability-threshold entries/exits (NO fixed stop-loss / take-profit blocks)

Notes:
- Keeps your existing feature builder (build_features) for X columns.
- Recomputes labels from forward returns on the same timeline so you can switch
  between binary and ternary without touching the feature builder.
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

# ---- Repo path / imports -----------------------------------------------------

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app.scripts.stock_algos.base_wiring import StockBaseRunner, _ET
from app.scripts.research.mm_features2_builder import build_features

# Try to reuse the same chunked price-history fetcher used by the feature builder,
# so labels and backtest timeline are aligned (avoids the '312 bars' issue).
try:
    from app.scripts.research.mm_features2_builder import _fetch_price_history_range as _fetch_price_history_range  # type: ignore
    from app.scripts.research.mm_features2_builder import _to_ohlcv_frame as _to_ohlcv_frame  # type: ignore
except Exception:
    # Fallback to the 'predict' builder module if the research builder hides helpers.
    from app.scripts.research.mm_predict_features2_builder import (  # type: ignore
        _fetch_price_history_range as _fetch_price_history_range,
        _to_ohlcv_frame as _to_ohlcv_frame,
    )


# ---- Logging -----------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s (AlgoMM_eval) %(message)s")
logger = logging.getLogger("AlgoMM_eval")

DATA_DIR = os.getenv("DATA_DIR", "/var/www/stockwicks/data")
MODEL_DIR = os.getenv("MODEL_DIR", "/var/www/stockwicks/models")


# =============================================================================
# Config
# =============================================================================

@dataclass
class EvalConfig:
    symbol: str
    interval: str
    trade_size: float
    user_id: int

    builder_days: int = 60
    k_forward: int = 3

    # Entry thresholds (probability-based)
    long_threshold: float = 0.60          # enter long if prob_up >= this
    short_threshold: float = 0.40         # enter short if prob_up <= this  (equiv prob_down >= 0.60)

    # Exit thresholds (probability-based)
    long_exit_threshold: float = 0.55     # exit long if prob_up < this
    short_exit_threshold: float = 0.45    # exit short if prob_up > this   (equiv prob_down < 0.55)

    # Probability advantage requirement
    min_prob_advantage: float = 0.01

    # Optional volume filter (simple; multiplier on last-20-bar average)
    min_volume_multiplier: float = 0.0    # 0 disables

    # Risk management (kept minimal; NOT a stop-loss / take-profit system)
    daily_loss_limit_usd: float = 0.0     # 0 disables

    # Session behavior
    eod_close: bool = True

    # Labeling / training
    label_mode: str = "binary"
    use_feat_labels: bool = True          # if feat_df has y/w, use them (matches old AlgoMM_eval)            # "binary" or "ternary"
    flat_band_pct: float = 0.15           # only used for ternary; FLAT if abs(fwd_ret) < flat_band_pct (%)
    flat_weight: float = 0.35             # weight multiplier for FLAT class (ternary only)
    min_edge_pct: float = 0.00            # optional: treat small moves as FLAT/ignore

    # Calibration (recommended for threshold-based trading)
    calibrate: bool = False
    calib_method: str = "sigmoid"         # "sigmoid" or "isotonic"
    calib_frac: float = 0.20              # last fraction reserved for calibration

    # Trading signal source
    trade_on: str = "raw"               # "raw" or "calibrated" (when calibration is enabled)

    # General
    auto_train: bool = False
    save_raw: bool = False
    save_features: bool = False
    save_trades: bool = False
    save_run: bool = False

    # --- Out-of-sample / sanity-test controls ---
    oos_days: int = 0         # if >0, simulate only last N trading days
    test_frac: float = 0.0    # alternative: simulate last fraction of bars (0<frac<1)
    smooth_window: int = 0    # 0 => use k_forward; 1 disables smoothing


# =============================================================================
# Helpers
# =============================================================================

def _prepare_X(feat_df: pd.DataFrame, feat_names: List[str]) -> pd.DataFrame:
    X = feat_df.reindex(columns=feat_names).copy()
    X = X.replace([np.inf, -np.inf], np.nan).ffill().bfill()
    med = X.median(numeric_only=True)
    X = X.fillna(med)

    all_nan = [c for c in X.columns if X[c].isna().all()]
    if all_nan:
        X[all_nan] = 0.0

    return X.fillna(0.0).astype(float)


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
    supported_intervals = {"1min", "5min", "15min", "30min", "1h", "4h", "1d"}
    interval_map = {
        "1m": "1min",
        "5m": "5min",
        "15m": "15min",
        "30m": "30min",
        "1h": "1h",
        "4h": "4h",
        "1d": "1d",
    }
    mapped = interval_map.get(interval, interval)
    if mapped not in supported_intervals:
        logger.warning(f"Interval '{interval}' not supported; falling back to '5min'")
        mapped = "5min"

    logger.info(f"Using interval '{mapped}' for feature building (requested: '{interval}')")
    return build_features(symbol, mapped, days=days, k_forward=k_forward)


def _fetch_price_frame_for_features(symbol: str, interval: str, feat_index: pd.Index, buffer_days: int = 2) -> pd.DataFrame:
    """
    Fetch an OHLCV price frame that spans the same time window as feat_index.

    IMPORTANT (your repo):
      _fetch_price_history_range(symbol, interval, days) -> candles/dict

    So we:
      1) compute how many *calendar* days are needed to cover feat_index (+ buffer)
      2) fetch that many days ending "now" (builder's behavior)
      3) slice down to [start_dt, end_dt] and return in ET timezone
    """
    if feat_index is None or len(feat_index) == 0:
        raise RuntimeError("Cannot fetch price frame: empty feature index.")

    idx0 = pd.to_datetime(feat_index.min())
    idx1 = pd.to_datetime(feat_index.max())

    # normalize to ET tz-aware
    if idx0.tzinfo is None:
        idx0 = idx0.tz_localize(_ET)
    else:
        idx0 = idx0.tz_convert(_ET)

    if idx1.tzinfo is None:
        idx1 = idx1.tz_localize(_ET)
    else:
        idx1 = idx1.tz_convert(_ET)

    start_dt = idx0 - pd.Timedelta(days=buffer_days)
    end_dt   = idx1 + pd.Timedelta(days=buffer_days)

    days_needed = int((end_dt.date() - start_dt.date()).days) + 1
    # small safety buffer to ensure we cover holidays/weekends
    days_needed = max(days_needed + 3, 10)

    candles = _fetch_price_history_range(symbol, interval, days_needed)
    ohlcv = _to_ohlcv_frame(candles)

    # normalize timezone to ET
    if getattr(ohlcv.index, "tz", None) is None:
        ohlcv.index = pd.to_datetime(ohlcv.index).tz_localize("UTC").tz_convert(_ET)
    else:
        ohlcv.index = ohlcv.index.tz_convert(_ET)

    ohlcv = ohlcv.sort_index()

    # slice to window we actually need
    ohlcv = ohlcv.loc[(ohlcv.index >= start_dt) & (ohlcv.index <= end_dt)]

    if len(ohlcv) == 0:
        raise RuntimeError(
            f"Price fetch returned 0 rows after slicing to {start_dt} → {end_dt}. "
            f"(days_needed={days_needed})"
        )

    return ohlcv

def _compute_forward_return(price_df: pd.DataFrame, k_forward: int) -> pd.Series:
    close = price_df["close"].astype(float)
    fwd = close.shift(-k_forward) / close - 1.0
    return fwd.rename("fwd_ret")


def _make_labels_and_weights(
    feat_df: pd.DataFrame,
    price_df: pd.DataFrame,
    cfg: EvalConfig,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Create y, w aligned to feat_df.index using forward returns computed on price_df.
    - binary: y=1 if fwd_ret >= min_edge, else 0
    - ternary: y in {0=DOWN, 1=FLAT, 2=UP} using flat_band/min_edge
    """
    # base weights
    if "w" in feat_df.columns:
        w = feat_df["w"].astype(float).values.copy()
    else:
        w = np.ones(len(feat_df), dtype=float)

    # If feature builder provided labels, use them for binary mode (matches old AlgoMM_eval)
    if cfg.label_mode.lower() != "ternary" and getattr(cfg, "use_feat_labels", True) and ("y" in feat_df.columns):
        y = feat_df["y"].astype(int).values
        return y, w

    # forward returns aligned
    fwd_ret = _compute_forward_return(price_df, cfg.k_forward).reindex(feat_df.index)
    fwd_ret = fwd_ret.ffill()  # best-effort alignment; avoids losing many rows

    min_edge = float(cfg.min_edge_pct) / 100.0
    flat_band = float(cfg.flat_band_pct) / 100.0

    if cfg.label_mode.lower() == "ternary":
        # treat min_edge as at least as strict as flat_band (if provided)
        band = max(flat_band, min_edge)

        y = np.full(len(feat_df), 1, dtype=int)  # FLAT by default
        y[(fwd_ret.values <= -band)] = 0         # DOWN
        y[(fwd_ret.values >= +band)] = 2         # UP

        # downweight FLAT class so it doesn't dominate
        w = w * np.where(y == 1, float(cfg.flat_weight), 1.0)

        return y, w

    # binary (default): 1 if forward return clears min_edge, else 0
    y = (fwd_ret.values >= min_edge).astype(int)
    return y, w


def _calibrate_prefit_time_safe(
    base_model,
    X: pd.DataFrame,
    y: np.ndarray,
    w: np.ndarray,
    method: str,
    calib_frac: float,
):
    """
    Time-safe calibration:
    - Fit base model on early slice
    - Calibrate on later slice using CalibratedClassifierCV(cv="prefit")
    """
    from sklearn.calibration import CalibratedClassifierCV
    try:
        from sklearn.frozen import FrozenEstimator  # type: ignore
    except Exception:
        FrozenEstimator = None  # type: ignore

    n = len(X)
    if n < 200:
        # Too small to do sensible calibration splits
        logger.warning("Not enough samples for calibration (n<200). Skipping calibration.")
        base_model.fit(X, y, sample_weight=w)
        return base_model, base_model

    calib_n = int(max(50, round(n * float(calib_frac))))
    calib_n = min(calib_n, n - 50)  # keep at least 50 for training
    train_n = n - calib_n

    X_train = X.iloc[:train_n]
    y_train = y[:train_n]
    w_train = w[:train_n]

    X_cal = X.iloc[train_n:]
    y_cal = y[train_n:]
    w_cal = w[train_n:]

    # Fit base model
    base_model.fit(X_train, y_train, sample_weight=w_train)

    # Calibrate on later slice
    cal = CalibratedClassifierCV(base_model, method=method, cv="prefit")
    try:
        cal.fit(X_cal, y_cal, sample_weight=w_cal)
        logger.info(f"Calibrated probabilities using method='{method}' on last {calib_n} samples.")
        return cal, base_model
    except TypeError:
        # Some sklearn versions don't accept sample_weight here
        cal.fit(X_cal, y_cal)
        logger.info(f"Calibrated probabilities using method='{method}' (no sample_weight) on last {calib_n} samples.")
        return cal, base_model


def _load_or_train_model(
    model_path: str,
    feat_df: pd.DataFrame,
    feat_names: List[str],
    price_df: pd.DataFrame,
    cfg: EvalConfig,
):
    """
    Load saved model or train a new one.
    Trains a HistGradientBoostingClassifier, optionally with time-safe calibration.
    """
    # Try loading model if allowed
    if os.path.exists(model_path) and not cfg.auto_train:
        try:
            pack = joblib.load(model_path)
            model = pack["model"]
            base_model = pack.get("base_model", None)
            features = pack.get("features", feat_names)
            meta = pack.get("meta", {})
            # Enforce model compatibility with current run settings
            saved_cal = bool(meta.get("calibrate", False))
            if saved_cal != bool(cfg.calibrate):
                raise RuntimeError(f"Saved model calibrate={saved_cal} != requested {cfg.calibrate}")
            if saved_cal:
                if str(meta.get("calib_method", "sigmoid")).lower() != str(cfg.calib_method).lower():
                    raise RuntimeError("Saved model calib_method mismatch")
                # Need base_model if trading on raw
                if getattr(cfg, "trade_on", "raw") == "raw" and base_model is None:
                    raise RuntimeError("Saved calibrated model missing base_model for raw trading")
            # If label_mode differs, prefer retrain
            if str(meta.get("label_mode", "binary")).lower() != cfg.label_mode.lower():
                raise RuntimeError(f"Saved model label_mode={meta.get('label_mode')} != requested {cfg.label_mode}")
            logger.info(f"Loaded model from {model_path}")
            return model, features, base_model
        except Exception as e:
            logger.warning(
                "Failed to load existing model '%s' (%s). Will retrain with current settings.",
                model_path, repr(e)
            )

    from sklearn.ensemble import HistGradientBoostingClassifier

    if feat_df is None or feat_df.empty:
        raise RuntimeError("Cannot train: feat_df is empty.")

    X = _prepare_X(feat_df, feat_names)
    y, w = _make_labels_and_weights(feat_df, price_df, cfg)
    try:
        vals, cnts = np.unique(y, return_counts=True)
        dist = {int(v): int(c) for v, c in zip(vals, cnts)}
        logger.info(f"Label distribution (mode={cfg.label_mode}): {dist}")
    except Exception:
        pass

    clf = HistGradientBoostingClassifier(
        max_depth=4,
        learning_rate=0.06,
        max_iter=400,
        l2_regularization=1.0,
    )

    if cfg.calibrate:
        model, base_model = _calibrate_prefit_time_safe(
            clf, X, y, w,
            method=str(cfg.calib_method).lower(),
            calib_frac=float(cfg.calib_frac),
        )
    else:
        clf.fit(X, y, sample_weight=w)
        model = clf
        base_model = clf

    os.makedirs(os.path.dirname(model_path), exist_ok=True)
    joblib.dump(
        {
            "model": model,
            "base_model": base_model,
            "features": feat_names,
            "interval": cfg.interval,
            "symbol": cfg.symbol,
            "meta": {
                "label_mode": cfg.label_mode,
                "flat_band_pct": cfg.flat_band_pct,
                "min_edge_pct": cfg.min_edge_pct,
                "calibrate": cfg.calibrate,
                "calib_method": cfg.calib_method,
                "calib_frac": cfg.calib_frac,
            },
        },
        model_path,
    )
    logger.info(f"Trained & saved model to {model_path}")
    return model, feat_names, base_model


def _predict_probs(model, X: pd.DataFrame, label_mode: str) -> Tuple[np.ndarray, Optional[np.ndarray]]:
    """
    Returns:
      prob_up (n,), prob_down (n,) or None for prob_down in binary (computed as 1-prob_up).
    """
    proba = model.predict_proba(X)
    classes = getattr(model, "classes_", None)

    # CalibratedClassifierCV stores classes_ too; if missing, infer from proba shape
    if classes is None:
        classes = np.arange(proba.shape[1])

    classes = list(classes)

    if label_mode.lower() == "ternary":
        # y: 0=DOWN, 1=FLAT, 2=UP
        try:
            idx_up = classes.index(2)
            idx_dn = classes.index(0)
        except ValueError:
            # fallback if classes aren't {0,1,2}
            idx_dn, idx_up = 0, proba.shape[1] - 1
        prob_up = proba[:, idx_up].astype(float)
        prob_dn = proba[:, idx_dn].astype(float)
        return prob_up, prob_dn

    # binary: y in {0,1}
    try:
        idx_up = classes.index(1)
    except ValueError:
        idx_up = min(1, proba.shape[1] - 1)
    prob_up = proba[:, idx_up].astype(float)
    return prob_up, None


# =============================================================================
# Trade simulation
# =============================================================================

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


def _volume_ok(price_df: pd.DataFrame, i: int, mult: float) -> bool:
    if mult <= 0:
        return True
    if i < 20:
        return True
    cur_v = float(price_df["volume"].iloc[i])
    avg_v = float(price_df["volume"].iloc[i-20:i].mean())
    if avg_v <= 0:
        return True
    return cur_v >= avg_v * mult


def _should_enter(prob_up: float, prob_dn: float, price_df: pd.DataFrame, i: int, cfg: EvalConfig) -> Tuple[bool, str]:
    if not _volume_ok(price_df, i, cfg.min_volume_multiplier):
        return False, "LOW_VOLUME"

    # Long
    if prob_up >= cfg.long_threshold and prob_up > (prob_dn + cfg.min_prob_advantage):
        return True, "LONG"

    # Short (using prob_up threshold semantics for backwards compatibility)
    # Equivalent: prob_dn >= (1 - short_threshold)
    if prob_up <= cfg.short_threshold and prob_dn > (prob_up + cfg.min_prob_advantage):
        return True, "SHORT"

    return False, ""


def _should_exit(position_side: str, prob_up: float, prob_dn: float, cfg: EvalConfig) -> Tuple[bool, str]:
    if position_side == "long":
        if prob_up < cfg.long_exit_threshold:
            return True, "PROBABILITY_DROP"
    elif position_side == "short":
        # exit short if prob_up > short_exit_threshold (equiv prob_dn < 1-short_exit_threshold)
        if prob_up > cfg.short_exit_threshold:
            return True, "PROBABILITY_DROP"
    return False, ""


def _simulate_trades(
    cfg: EvalConfig,
    price_df: pd.DataFrame,
    prob_up_s: pd.Series,
    prob_dn_s: pd.Series,
) -> Tuple[List[TradeRecord], List[TradeRecord], pd.DataFrame]:
    """
    Core backtest loop:
    - entry/exit ONLY via probabilities (+ optional EOD close / daily loss limit)
    - NO fixed stop-loss / take-profit blocks
    """
    p_up = prob_up_s.reindex(price_df.index).ffill()
    p_dn = prob_dn_s.reindex(price_df.index).ffill()

    run_rows = []
    trades_long: List[TradeRecord] = []
    trades_short: List[TradeRecord] = []

    position_side: Optional[str] = None
    entry_price: float = 0.0
    entry_time: Optional[datetime] = None
    quantity: float = cfg.trade_size

    current_day = None
    daily_profit = 0.0

    idx = price_df.index
    if len(idx) < 2:
        logger.warning("Not enough bars to simulate.")
        return trades_long, trades_short, pd.DataFrame()

    for i in range(1, len(idx)):
        cur_ts = idx[i]
        prev_prob_up = p_up.iloc[i - 1]
        prev_prob_dn = p_dn.iloc[i - 1]

        # reset daily P&L tracker
        if current_day != cur_ts.date():
            current_day = cur_ts.date()
            daily_profit = 0.0

        if pd.isna(prev_prob_up) or pd.isna(prev_prob_dn):
            run_rows.append({
                "timestamp": cur_ts,
                "open": float(price_df["open"].iloc[i]),
                "high": float(price_df["high"].iloc[i]),
                "low": float(price_df["low"].iloc[i]),
                "close": float(price_df["close"].iloc[i]),
                "prob_up": np.nan,
                "prob_down": np.nan,
                "position": position_side or "flat",
                "action": "SKIP_NO_PROB",
            })
            continue

        prob_up = float(prev_prob_up)
        prob_dn = float(prev_prob_dn)

        exec_price = float(price_df["open"].iloc[i])  # simple, deterministic execution

        action = "HOLD"
        just_closed = False

        # Daily loss limit (optional)
        if cfg.daily_loss_limit_usd > 0 and daily_profit <= -abs(cfg.daily_loss_limit_usd):
            if position_side is not None:
                profit = (exec_price - entry_price) * quantity if position_side == "long" else (entry_price - exec_price) * quantity
                reason = f"DAILY_LOSS_LIMIT_{daily_profit:.2f}"
                if position_side == "long":
                    trades_long.append(TradeRecord(cfg.symbol, cfg.interval, "long", entry_time, entry_price, cur_ts, exec_price, quantity, profit, reason))
                else:
                    trades_short.append(TradeRecord(cfg.symbol, cfg.interval, "short", entry_time, entry_price, cur_ts, exec_price, quantity, profit, reason))
                daily_profit += profit
                position_side = None
                entry_price = 0.0
                entry_time = None
                action = "EXIT_DAILY_LOSS_LIMIT"
                just_closed = True
            else:
                action = "SKIP_DAILY_LOSS_LIMIT"

        # Exit logic (probability-based)
        if position_side is not None and not just_closed:
            should_exit, exit_reason = _should_exit(position_side, prob_up, prob_dn, cfg)
            if should_exit:
                profit = (exec_price - entry_price) * quantity if position_side == "long" else (entry_price - exec_price) * quantity
                if position_side == "long":
                    trades_long.append(TradeRecord(cfg.symbol, cfg.interval, "long", entry_time, entry_price, cur_ts, exec_price, quantity, profit, exit_reason))
                else:
                    trades_short.append(TradeRecord(cfg.symbol, cfg.interval, "short", entry_time, entry_price, cur_ts, exec_price, quantity, profit, exit_reason))
                daily_profit += profit
                position_side = None
                entry_price = 0.0
                entry_time = None
                action = f"EXIT_{exit_reason}"
                just_closed = True

        # Entry logic
        if position_side is None and not just_closed:
            should_enter, direction = _should_enter(prob_up, prob_dn, price_df, i, cfg)
            if should_enter:
                position_side = direction.lower()
                entry_price = exec_price
                entry_time = cur_ts
                quantity = cfg.trade_size
                action = f"OPEN_{direction}"

        # EOD close (optional)
        if cfg.eod_close and position_side is not None and not just_closed:
            cur_time_et = cur_ts.astimezone(_ET) if cur_ts.tzinfo else _ET.localize(cur_ts)
            if cur_time_et.hour >= 15 and (cur_time_et.hour > 15 or cur_time_et.minute >= 58):
                profit = (exec_price - entry_price) * quantity if position_side == "long" else (entry_price - exec_price) * quantity
                reason = "END_OF_DAY_CLOSE"
                if position_side == "long":
                    trades_long.append(TradeRecord(cfg.symbol, cfg.interval, "long", entry_time, entry_price, cur_ts, exec_price, quantity, profit, reason))
                else:
                    trades_short.append(TradeRecord(cfg.symbol, cfg.interval, "short", entry_time, entry_price, cur_ts, exec_price, quantity, profit, reason))
                daily_profit += profit
                position_side = None
                entry_price = 0.0
                entry_time = None
                action = "EXIT_EOD"
                just_closed = True

        run_rows.append({
            "timestamp": cur_ts,
            "open": exec_price,
            "high": float(price_df["high"].iloc[i]),
            "low": float(price_df["low"].iloc[i]),
            "close": float(price_df["close"].iloc[i]),
            "prob_up": prob_up,
            "prob_down": prob_dn,
            "position": position_side or "flat",
            "action": action,
            "quantity": quantity if position_side else 0,
            "daily_profit": float(daily_profit),
        })

    # Force close at final bar (deterministic)
    if position_side is not None:
        last_ts = idx[-1]
        last_open = float(price_df["open"].iloc[-1])
        profit = (last_open - entry_price) * quantity if position_side == "long" else (entry_price - last_open) * quantity
        reason = "FINAL_BAR_CLOSE"
        if position_side == "long":
            trades_long.append(TradeRecord(cfg.symbol, cfg.interval, "long", entry_time, entry_price, last_ts, last_open, quantity, profit, reason))
        else:
            trades_short.append(TradeRecord(cfg.symbol, cfg.interval, "short", entry_time, entry_price, last_ts, last_open, quantity, profit, reason))
        logger.info(f"Closed {position_side} position at final bar: {last_ts} @ {last_open}")

    run_df = pd.DataFrame(run_rows).set_index("timestamp")
    return trades_long, trades_short, run_df


# =============================================================================
# Summary
# =============================================================================

def _compute_summary(trades: List[TradeRecord], cfg: EvalConfig, label: str) -> Optional[Dict[str, str]]:
    if not trades:
        return None
    profits = [t.profit for t in trades]
    wins = sum(1 for p in profits if p > 0)
    losses = sum(1 for p in profits if p <= 0)
    total = len(profits)
    sr = (wins / total) * 100.0 if total else 0.0

    def fmt(x: float) -> str:
        return f"${x:.2f}"

    return {
        "Symbol": cfg.symbol,
        "Interval": cfg.interval,
        "LabelMode": cfg.label_mode,
        "Calibrated": str(cfg.calibrate),
        "Trades": str(total),
        "Wins": str(wins),
        "Losses": str(losses),
        "SuccessRate": f"{sr:.2f}%",
        "TotalProfit": fmt(sum(profits)),
        "LargestWin": fmt(max(profits)),
        "LargestLoss": fmt(min(profits)),
        "Side": label,
    }


# =============================================================================
# Main
# =============================================================================


def _compute_oos_split(feat_df: pd.DataFrame, oos_days: int, test_frac: float, k_forward: int):
    """Return (train_end_pos, sim_mask, tag) or (None, None, None) if no OOS requested.

    IMPORTANT: We subtract k_forward bars from the training end so labels in training cannot peek into OOS.
    """
    if oos_days is None:
        oos_days = 0
    if test_frac is None:
        test_frac = 0.0

    n = len(feat_df)
    if n < 10:
        return None, None, None

    if oos_days > 0:
        # Use distinct trading dates present in the feature index.
        dates = pd.Index(feat_df.index.date)
        uniq_days = pd.Index(sorted(dates.unique()))
        if oos_days >= len(uniq_days):
            raise ValueError(f"--oos-days={oos_days} is too large for available days={len(uniq_days)}")
        oos_set = set(uniq_days[-oos_days:])
        sim_mask = np.asarray(dates.isin(oos_set), dtype=bool)
        sim_pos0 = int(np.where(sim_mask)[0][0])
        tag = f"last_{oos_days}_days"
    elif test_frac and test_frac > 0:
        if not (0.0 < float(test_frac) < 1.0):
            raise ValueError("--test-frac must be between 0 and 1 (exclusive)")
        sim_pos0 = int(max(1, round(n * (1.0 - float(test_frac)))))
        sim_mask = np.zeros(n, dtype=bool)
        sim_mask[sim_pos0:] = True
        tag = f"last_{int(round(100*float(test_frac)))}pct"
    else:
        return None, None, None

    # Guard against leakage via k_forward labels.
    train_end_pos = max(1, sim_pos0 - int(max(k_forward, 0)))
    return train_end_pos, sim_mask, tag

def main(argv: Optional[List[str]] = None):
    p = argparse.ArgumentParser(description="AlgoMM backtest evaluator (cleaned + calibrated + ternary labels)")

    p.add_argument("-s", "--symbol", required=True)
    p.add_argument("-i", "--interval", required=True)
    p.add_argument("-q", "--trade-size", type=float, required=True)
    p.add_argument("-u", "--user-id", type=int, required=True)

    p.add_argument("--builder-days", type=int, default=60)
    p.add_argument("--k-forward", type=int, default=3)

    p.add_argument("--long-threshold", type=float, default=0.60)
    p.add_argument("--short-threshold", type=float, default=0.40)
    p.add_argument("--long-exit-threshold", type=float, default=0.55)
    p.add_argument("--short-exit-threshold", type=float, default=0.45)

    p.add_argument("--min-prob-advantage", type=float, default=0.10)
    p.add_argument("--min-volume-multiplier", type=float, default=1.2)
    p.add_argument("--daily-loss-limit-usd", type=float, default=500.0)

    p.add_argument("--label-mode", choices=["binary", "ternary"], default="binary")
    p.add_argument("--flat-band-pct", type=float, default=0.15)
    p.add_argument("--flat-weight", type=float, default=0.35)
    p.add_argument("--min-edge-pct", type=float, default=0.0)

    p.add_argument("--calibrate", action="store_true", default=False)
    p.add_argument("--no-calibrate", action="store_true", default=False)
    p.add_argument("--calib-method", choices=["sigmoid", "isotonic"], default="sigmoid")
    p.add_argument("--calib-frac", type=float, default=0.20)

    p.add_argument("--trade-on", choices=["raw", "calibrated"], default="raw")
    p.add_argument("--oos-days", type=int, default=0,
                   help="If >0, train on earlier bars and simulate ONLY the last N trading days (leakage-guarded).")
    p.add_argument("--test-frac", type=float, default=0.0,
                   help="Alternative OOS split: simulate the last fraction of bars (0<frac<1). Ignored if --oos-days>0.")
    p.add_argument("--smooth-window", type=int, default=0,
                   help="Rolling mean window for probability smoothing. 0 => use k_forward; 1 disables smoothing.")
    p.add_argument("--no-use-feat-labels", action="store_true", default=False)

    p.add_argument("--eod-close", action="store_true", default=True)
    p.add_argument("--auto-train", action="store_true", default=False)

    p.add_argument("--save-raw", action="store_true", default=False)
    p.add_argument("--save-features", action="store_true", default=False)
    p.add_argument("--save-trades", action="store_true", default=False)
    p.add_argument("--save-run", action="store_true", default=False)

    args = p.parse_args(argv)

    calibrate = bool(args.calibrate) and not bool(args.no_calibrate)

    cfg = EvalConfig(
        symbol=args.symbol.upper(),
        interval=args.interval,
        trade_size=args.trade_size,
        user_id=args.user_id,
        builder_days=args.builder_days,
        k_forward=args.k_forward,
        long_threshold=args.long_threshold,
        short_threshold=args.short_threshold,
        long_exit_threshold=args.long_exit_threshold,
        short_exit_threshold=args.short_exit_threshold,
        min_prob_advantage=args.min_prob_advantage,
        min_volume_multiplier=args.min_volume_multiplier,
        daily_loss_limit_usd=args.daily_loss_limit_usd,
        eod_close=args.eod_close,
        label_mode=args.label_mode,
        flat_band_pct=args.flat_band_pct,
        flat_weight=args.flat_weight,
        min_edge_pct=args.min_edge_pct,
        calibrate=calibrate,
        calib_method=args.calib_method,
        calib_frac=args.calib_frac,
        trade_on=args.trade_on,
        use_feat_labels=not bool(args.no_use_feat_labels),
        auto_train=args.auto_train,
        save_raw=args.save_raw,
        save_features=args.save_features,
        save_trades=args.save_trades,
        save_run=args.save_run,
        oos_days=int(args.oos_days or 0),
        test_frac=float(args.test_frac or 0.0),
        smooth_window=int(args.smooth_window or 0),
    )

    out_dir = os.path.join(DATA_DIR, str(cfg.user_id))
    os.makedirs(out_dir, exist_ok=True)
    prefix = f"{cfg.user_id}_{cfg.symbol}_{cfg.interval}"

    logger.info(f"Starting backtest: {cfg.symbol} {cfg.interval}")
    logger.info(f"Label mode={cfg.label_mode} (flat_band={cfg.flat_band_pct:.2f}%, min_edge={cfg.min_edge_pct:.2f}%)")
    logger.info(f"Calibration={cfg.calibrate} ({cfg.calib_method}, frac={cfg.calib_frac:.2f})")
    logger.info(f"Entry: Long@{cfg.long_threshold:.2f}, Short(prob_up)@{cfg.short_threshold:.2f}")
    logger.info(f"Exit : Long@{cfg.long_exit_threshold:.2f}, Short(prob_up)@{cfg.short_exit_threshold:.2f}")


    # Build features first (this fetches the full chunked history internally)
    logger.info(f"Building features (builder_days={cfg.builder_days}, k_forward={cfg.k_forward})")
    feat_df = _build_features_with_fallback(
        cfg.symbol,
        cfg.interval,
        days=cfg.builder_days + 2,
        k_forward=cfg.k_forward,
    )
    if feat_df is None or feat_df.empty:
        raise RuntimeError("build_features returned empty DataFrame.")

    # Fetch price frame spanning the same time window as features (chunked range fetch)
    price_df_full = _fetch_price_frame_for_features(cfg.symbol, cfg.interval, feat_df.index, buffer_days=2)

    # Filter to last N trading days (by date) for a fair backtest window
    days = sorted({ts.date() for ts in price_df_full.index})
    if len(days) > cfg.builder_days:
        keep_days = set(days[-cfg.builder_days:])
        price_df = price_df_full[price_df_full.index.map(lambda x: x.date() in keep_days)].copy()
    else:
        price_df = price_df_full.copy()

    # Align features to price timeline (strict intersection)
    common_idx = feat_df.index.intersection(price_df.index)
    feat_df = feat_df.loc[common_idx].copy()
    price_df = price_df.loc[common_idx].copy()
    if feat_df.empty or price_df.empty:
        raise RuntimeError("After alignment, no overlapping bars between features and price_df.")

    if cfg.save_raw:
        raw_path = os.path.join(out_dir, f"{prefix}_raw.csv")
        price_df.to_csv(raw_path)
        logger.info(f"Saved → {raw_path}")

    if cfg.save_features:
        feat_path = os.path.join(out_dir, f"{prefix}_features.csv")
        feat_df.to_csv(feat_path)
        logger.info(f"Saved → {feat_path}")

    feat_names = [c for c in feat_df.columns if c not in ("y", "w")]
    suffix = f"{cfg.label_mode}_" + (f"cal_{str(cfg.calib_method).lower()}" if cfg.calibrate else "raw")
    model_path = os.path.join(MODEL_DIR, f"mm2_{cfg.symbol}_{cfg.interval}_k{cfg.k_forward}_{suffix}.joblib")
    train_end_pos, sim_mask, oos_tag = _compute_oos_split(feat_df, cfg.oos_days, cfg.test_frac, cfg.k_forward)
    if sim_mask is not None:
        sim_index = feat_df.index[sim_mask]
        train_feat_df = feat_df.iloc[:train_end_pos].copy()
        train_price_df = price_df.iloc[:train_end_pos].copy()
        # Tag outputs so you can compare OOS vs in-sample quickly
        prefix = f"{prefix}_OOS_{oos_tag}"
        logger.info(
            f"OOS enabled ({oos_tag}): train_rows={len(train_feat_df)} (end={train_feat_df.index[-1]}), "
            f"sim_rows={int(sim_mask.sum())} (start={sim_index[0]}, end={sim_index[-1]}), "
            f"leakage_guard_drop={cfg.k_forward}"
        )
    else:
        sim_index = feat_df.index
        train_feat_df = feat_df
        train_price_df = price_df

    model, used_feats, base_model = _load_or_train_model(model_path, train_feat_df, feat_names, train_price_df, cfg)
    X = _prepare_X(feat_df, used_feats)
    # Choose which probabilities drive entries/exits
    model_for_signal = model
    if cfg.calibrate and str(getattr(cfg, 'trade_on', 'raw')).lower() == 'raw' and base_model is not None:
        model_for_signal = base_model
    prob_up_raw, prob_dn_raw_opt = _predict_probs(model_for_signal, X, cfg.label_mode)

    prob_up_raw_s = pd.Series(prob_up_raw, index=X.index, name="prob_up_raw")
    q_series = prob_up_raw_s.loc[sim_index] if sim_mask is not None else prob_up_raw_s
    try:
        q = prob_up_raw_s.quantile([0.01, 0.1, 0.5, 0.9, 0.99]).to_dict()
        logger.info(f"prob_up quantiles: { {k: float(v) for k,v in q.items()} }")
    except Exception:
        pass

    if prob_dn_raw_opt is None:
        prob_dn_raw_s = (1.0 - prob_up_raw_s).rename("prob_down_raw")
    else:
        prob_dn_raw_s = pd.Series(prob_dn_raw_opt, index=X.index, name="prob_down_raw")

    window = max(int(cfg.smooth_window or cfg.k_forward), 1)
    prob_up_s = prob_up_raw_s.rolling(window=window, min_periods=1).mean().rename("prob_up")
    prob_dn_s = prob_dn_raw_s.rolling(window=window, min_periods=1).mean().rename("prob_down")

    # quick distribution logging
    bin_series = prob_up_s.loc[sim_index] if sim_mask is not None else prob_up_s

    logger.info(
        "Prob bins: prob_up>=%.2f=%d, prob_up<=%.2f=%d, mid=%d",
        cfg.long_threshold,
        int((bin_series >= cfg.long_threshold).sum()),
        cfg.short_threshold,
        int((bin_series <= cfg.short_threshold).sum()),
        int(len(bin_series) - (bin_series >= cfg.long_threshold).sum() - (bin_series <= cfg.short_threshold).sum()),
    )

    logger.info("Simulating trades (probability-only exits; no stop-loss/take-profit blocks)...")
    sim_price_df = price_df.loc[sim_index]
    sim_prob_up_s = prob_up_s.loc[sim_index]
    sim_prob_dn_s = prob_dn_s.loc[sim_index]

    trades_long, trades_short, run_df = _simulate_trades(cfg, sim_price_df, sim_prob_up_s, sim_prob_dn_s)
    logger.info(f"Trade results: Long={len(trades_long)}, Short={len(trades_short)}")

    if cfg.save_trades:
        long_path = os.path.join(out_dir, f"{prefix}_trades_long.csv")
        short_path = os.path.join(out_dir, f"{prefix}_trades_short.csv")

        def _write(path: str, trades: List[TradeRecord]):
            with open(path, "w") as f:
                f.write("Symbol,Interval,Side,Entry_date_time,Entry_price,Exit_date_time,Exit_price,Quantity,Profit,Exit_Reason\n")
                for t in trades:
                    f.write(
                        f"{t.symbol},{t.interval},{t.side},"
                        f"{t.entry_time.isoformat()},{t.entry_price:.3f},"
                        f"{t.exit_time.isoformat()},{t.exit_price:.3f},"
                        f"{t.quantity:.1f},{t.profit:.2f},{t.exit_reason}\n"
                    )

        _write(long_path, trades_long)
        _write(short_path, trades_short)
        logger.info(f"Saved → {long_path}")
        logger.info(f"Saved → {short_path}")

    if cfg.save_run and not run_df.empty:
        run_path = os.path.join(out_dir, f"{prefix}_run.csv")
        run_df.to_csv(run_path)
        logger.info(f"Saved → {run_path}")

    # Summary
    summary_rows: List[Dict[str, str]] = []
    r_long = _compute_summary(trades_long, cfg, "Long")
    r_short = _compute_summary(trades_short, cfg, "Short")
    if r_long: summary_rows.append(r_long)
    if r_short: summary_rows.append(r_short)

    all_trades = trades_long + trades_short
    if all_trades:
        profits = [t.profit for t in all_trades]
        wins = sum(1 for p in profits if p > 0)
        total = len(profits)
        sr = (wins / total) * 100.0 if total else 0.0

        def fmt(x: float) -> str:
            return f"${x:.2f}"

        summary_rows.append({
            "Symbol": cfg.symbol,
            "Interval": cfg.interval,
            "LabelMode": cfg.label_mode,
            "Calibrated": str(cfg.calibrate),
            "Trades": str(total),
            "Wins": str(wins),
            "Losses": str(total - wins),
            "SuccessRate": f"{sr:.2f}%",
            "TotalProfit": fmt(sum(profits)),
            "LargestWin": fmt(max(profits)),
            "LargestLoss": fmt(min(profits)),
            "Side": "Overall",
        })

    if summary_rows:
        summary_df = pd.DataFrame(summary_rows)
        summary_path = os.path.join(out_dir, f"{prefix}_summary.csv")
        summary_df.to_csv(summary_path, index=False)

        print("\n" + "=" * 88)
        print(f"BACKTEST RESULTS: {cfg.symbol} {cfg.interval}  |  label={cfg.label_mode}  |  calibrated={cfg.calibrate}")
        print("=" * 88)
        print(summary_df.to_string(index=False))
        print("=" * 88)
        print(f"\n✅ Outputs saved to {out_dir}/")
        logger.info(f"Summary saved to {summary_path}")
    else:
        logger.info("No trades generated; summary not written.")


if __name__ == "__main__":
    main()