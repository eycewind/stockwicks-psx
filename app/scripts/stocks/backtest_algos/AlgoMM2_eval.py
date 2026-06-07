#!/usr/bin/env python3
"""
AlgoMM2_eval_SMI — Backtest using ONLY SMI + Volume factor as features

- Features:
    * smi:         Stochastic Momentum Index (14,5,5)
    * vol_rel_20:  volume / 20-bar average volume
- Label:
    * y = 1 if close in k_forward bars > current close (same ET session)
    * y = 0 otherwise
- Model:
    * HistGradientBoostingClassifier
- Entry logic:
    * LONG  when:
        - SMI >= smi_long_level (default +60)
        - prob_up >= long_threshold
        - prob_up > prob_down + min_prob_advantage
    * SHORT when:
        - SMI <= smi_short_level (default -60)
        - prob_down >= short_threshold
        - prob_down > prob_up + min_prob_advantage

All risk, EOD close, and trailing stop logic is aligned with AlgoMM_eval v4.
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
from app.scripts.ml.mm_live_helpers import predict_probability

# ---- Logging -----------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s (AlgoMM2_SMI) %(message)s")
logger = logging.getLogger("AlgoMM2_eval_SMI")

DATA_DIR = os.getenv("DATA_DIR", "/var/www/stockwicks/data")
MODEL_DIR = os.getenv("MODEL_DIR", "/var/www/stockwicks/models")

ET_TZ = "America/New_York"


# ==== Config ==================================================================

@dataclass
class EvalConfig:
    symbol: str
    interval: str
    trade_size: float
    user_id: int

    builder_days: int = 60
    k_forward: int = 3      # probability horizon: next 3 bars

    # Entry thresholds (probability)
    long_threshold: float = 0.60
    short_threshold: float = 0.40

    # Exit thresholds (probability)
    long_exit_threshold: float = 0.55
    short_exit_threshold: float = 0.45

    # SMI thresholds
    smi_length: int = 14
    smi_smooth: int = 5
    smi_double_smooth: int = 5
    smi_long_level: float = 60.0
    smi_short_level: float = -60.0

    # Volume filter
    min_volume_multiplier: float = 1.1    # same idea as AlgoMM_eval

    # Risk management
    fixed_stop_loss: float = 200.0
    trailing_stop_activation: float = 0.8
    trailing_stop_distance: float = 1.0
    daily_loss_limit_usd: float = 500.0
    take_profit_percent: float = 1.5
    cooldown_sec: int = 60

    # Probability advantage requirement
    min_prob_advantage: float = 0.01

    # General
    eod_close: bool = True
    auto_train: bool = False
    save_raw: bool = False
    save_features: bool = False
    save_trades: bool = False
    save_run: bool = False


# ==== Helpers =================================================================

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
    Load saved model or train a new one using ONLY the given feat_names,
    which here are: ["smi", "vol_rel_20"].
    """
    # Try to load existing model
    if os.path.exists(model_path) and not auto_train:
        try:
            pack = joblib.load(model_path)
            model = pack["model"]
            features = pack.get("features", feat_names)
            logger.info(f"Loaded model from {model_path}")
            return model, features
        except Exception as e:
            logger.warning(
                "Failed to load existing model '%s' (%s). Will retrain.",
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
    """
    Fetch price data aligned with live runner.
    Copied from AlgoMM_eval v4, but we will reuse it for SMI-building.
    """
    df_raw = runner.fetch_source_bars(symbol)
    if df_raw is None or df_raw.empty:
        raise RuntimeError(f"No df_raw for symbol={symbol}")

    latest_time, _, df = runner.resample_interval(df_raw, interval, symbol)
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
            f"{df.index[0].date()} to {df.index[-1].date()} "
            f"({len(df)} bars)"
        )

    return df


# ==== SMI + Volume feature builder ===========================================

def _compute_smi(
    df: pd.DataFrame,
    length: int = 14,
    smooth: int = 5,
    double_smooth: int = 5,
) -> pd.Series:
    """
    Compute Stochastic Momentum Index (SMI) on OHLC data.
    Formula: 100 * double-smoothed distance from mid-range / double-smoothed half-range.
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]

    highest_high = high.rolling(length).max()
    lowest_low = low.rolling(length).min()
    hl_range = highest_high - lowest_low

    mid = (highest_high + lowest_low) / 2.0
    diff = close - mid
    half_range = hl_range / 2.0

    # first smoothing
    diff1 = diff.ewm(span=smooth, adjust=False).mean()
    range1 = half_range.ewm(span=smooth, adjust=False).mean()

    # second smoothing
    diff2 = diff1.ewm(span=double_smooth, adjust=False).mean()
    range2 = range1.ewm(span=double_smooth, adjust=False).mean()

    smi = 100.0 * (diff2 / range2.replace(0, np.nan))
    smi = smi.replace([np.inf, -np.inf], np.nan).fillna(0.0).clip(-100, 100)
    return smi


def _label_forward_same_session(close: pd.Series, k: int, tz: str) -> tuple[pd.Series, pd.Series]:
    """
    Forward return/label restricted to same ET session (no overnight),
    following the logic from mm_features2_builder.
    """
    if not isinstance(close.index, pd.DatetimeIndex):
        raise ValueError("close index must be a DatetimeIndex")
    idx_utc = close.index if close.index.tz is not None else close.index.tz_localize("UTC")
    et = idx_utc.tz_convert(tz)

    et_s = pd.Series(et, index=close.index)
    same_day_next = et_s.dt.date.eq(et_s.shift(-k).dt.date)

    fwd = (close.shift(-k) / close - 1.0).where(same_day_next)
    y = (fwd > 0).astype(float)      # keep NaN where crossing sessions
    w = fwd.abs()
    return y, w


def _build_smi_volume_features(
    price_df: pd.DataFrame,
    cfg: EvalConfig,
) -> pd.DataFrame:
    """
    Build feature table using ONLY:
      - smi
      - vol_rel_20 (volume / 20-bar avg volume)
    And labels:
      - y, w using k_forward and same-session constraint.
    """
    df = price_df.copy()

    # SMI
    smi = _compute_smi(
        df,
        length=cfg.smi_length,
        smooth=cfg.smi_smooth,
        double_smooth=cfg.smi_double_smooth,
    )

    # Volume factor: relative to rolling 20-bar mean
    vol = df["volume"].fillna(0.0)
    vol_ma20 = vol.rolling(20).mean().replace(0, np.nan)
    vol_rel_20 = (vol / vol_ma20).replace([np.inf, -np.inf], np.nan).fillna(0.0)

    # Labels (k_forward)
    y, w = _label_forward_same_session(df["close"], cfg.k_forward, ET_TZ)

    feat = pd.DataFrame(
        {
            "smi": smi,
            "vol_rel_20": vol_rel_20,
            "y": y,
            "w": w.fillna(0.0),
        },
        index=df.index,
    )

    feat = feat.dropna(subset=["y"])
    for c in feat.columns:
        try:
            feat[c] = pd.to_numeric(feat[c])
        except (ValueError, TypeError):
            pass

    if feat.empty:
        logger.error("Final SMI+Volume feature set is empty.")
        return pd.DataFrame()

    logger.info(
        f"Final SMI+Vol feature set: {len(feat)} rows, "
        f"{int((feat['y'] == 1).sum())} up labels, "
        f"{int((feat['y'] == 0).sum())} down labels"
    )
    return feat


# ==== LIVE-MATCHING helpers (adapted to use SMI gates) ========================

def check_volume_requirement(df: pd.DataFrame, current_index: int, min_volume_multiplier: float = 1.1) -> bool:
    """Simple volume requirement based on last 20 bars."""
    try:
        if current_index < 20:
            return True
        current_volume = df["volume"].iloc[current_index]
        avg_volume = df["volume"].iloc[max(0, current_index - 20) : current_index].mean()
        return current_volume >= (avg_volume * min_volume_multiplier)
    except Exception:
        return True


def calculate_trailing_stop(entry_price: float, current_price: float, 
                            position_side: str, activation: float, distance: float) -> Optional[float]:
    if position_side == "long":
        profit_pct = (current_price - entry_price) / entry_price * 100
        if profit_pct >= activation:
            return current_price * (1 - distance / 100.0)
    else:
        profit_pct = (entry_price - current_price) / entry_price * 100
        if profit_pct >= activation:
            return current_price * (1 + distance / 100.0)
    return None


def should_exit_trade(
    position_side: str,
    entry_price: float,
    current_price: float,
    prob_up: float,
    prob_down: float,
    cfg: EvalConfig,
    entry_time: datetime,
    current_time: datetime,
) -> Tuple[bool, str]:
    if not position_side:
        return False, ""

    # 1. Trailing stop
    trailing_stop = calculate_trailing_stop(
        entry_price,
        current_price,
        position_side,
        cfg.trailing_stop_activation,
        cfg.trailing_stop_distance,
    )
    if trailing_stop:
        if (position_side == "long" and current_price <= trailing_stop) or \
           (position_side == "short" and current_price >= trailing_stop):
            return True, "TRAILING_STOP"

    # 2. Probability flip exits
    if position_side == "long" and prob_up < cfg.long_exit_threshold:
        return True, "PROBABILITY_DROP"
    elif position_side == "short" and prob_down < (1 - cfg.short_exit_threshold):
        return True, "PROBABILITY_DROP"

    # 3. Quick profit in first 5 mins
    if position_side == "long":
        profit_pct = (current_price - entry_price) / entry_price * 100
    else:
        profit_pct = (entry_price - current_price) / entry_price * 100

    trade_duration = current_time - entry_time
    if trade_duration.total_seconds() < 300 and profit_pct >= 0.5:
        return True, "QUICK_PROFIT"

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

    should_take = gain_percent >= threshold_percent
    return should_take, gain_percent


def get_smart_execution_price(df: pd.DataFrame, current_index: int) -> float:
    """Use close as execution price here (simpler)."""
    try:
        return float(df["close"].iloc[current_index])
    except Exception:
        return float(df["close"].iloc[current_index])


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


def _simulate_trades(
    cfg: EvalConfig,
    price_df: pd.DataFrame,
    prob_series: pd.Series,
    smi_series: pd.Series,
) -> Tuple[List[TradeRecord], List[TradeRecord], pd.DataFrame]:
    """
    Core backtest loop with SMI-gated entries and live-style risk logic.
    """
    prob_aligned = prob_series.reindex(price_df.index).ffill()

    run_rows = []
    trades_long: List[TradeRecord] = []
    trades_short: List[TradeRecord] = []

    position_side: Optional[str] = None
    entry_price: float = 0.0
    entry_time: Optional[datetime] = None
    quantity: float = cfg.trade_size

    daily_trades: List[TradeRecord] = []
    current_day = None

    idx = price_df.index
    if len(idx) < 2:
        logger.warning("Not enough bars to simulate.")
        return trades_long, trades_short, pd.DataFrame()

    for i in range(1, len(idx)):
        cur_ts = idx[i]
        prev_ts = idx[i - 1]

        # Daily tracking
        if current_day != cur_ts.date():
            current_day = cur_ts.date()
            daily_trades = [t for t in trades_long + trades_short if t.entry_time.date() == current_day]

        prev_prob = prob_aligned.iloc[i - 1]
        if pd.isna(prev_prob):
            run_rows.append(
                {
                    "timestamp": cur_ts,
                    "close": price_df["close"].iloc[i],
                    "prob_up": np.nan,
                    "position": position_side or "flat",
                    "action": "SKIP_NO_PROB",
                }
            )
            continue

        prob_up = float(prev_prob)
        prob_down = 1.0 - prob_up
        exec_price = get_smart_execution_price(price_df, i)

        smi_val = float(smi_series.iloc[i - 1]) if not pd.isna(smi_series.iloc[i - 1]) else 0.0

        action = "HOLD"
        exit_reason = ""
        just_closed = False

        # ===== DAILY LOSS LIMIT =============================================
        daily_loss = sum(t.profit for t in daily_trades if t.profit < 0)
        if daily_loss <= -abs(cfg.daily_loss_limit_usd):
            if position_side:
                if position_side == "long":
                    profit = (exec_price - entry_price) * quantity
                    trades_long.append(
                        TradeRecord(
                            cfg.symbol,
                            cfg.interval,
                            "long",
                            entry_time,
                            entry_price,
                            cur_ts,
                            exec_price,
                            quantity,
                            profit,
                            f"DAILY_LOSS_LIMIT_{daily_loss:.2f}",
                        )
                    )
                else:
                    profit = (entry_price - exec_price) * quantity
                    trades_short.append(
                        TradeRecord(
                            cfg.symbol,
                            cfg.interval,
                            "short",
                            entry_time,
                            entry_price,
                            cur_ts,
                            exec_price,
                            quantity,
                            profit,
                            f"DAILY_LOSS_LIMIT_{daily_loss:.2f}",
                        )
                    )

                position_side = None
                entry_price = 0.0
                entry_time = None
                action = "EXIT_DAILY_LOSS_LIMIT"
                just_closed = True
            else:
                action = "SKIP_DAILY_LOSS_LIMIT"
                run_rows.append(
                    {
                        "timestamp": cur_ts,
                        "close": price_df["close"].iloc[i],
                        "prob_up": prob_up,
                        "prob_down": prob_down,
                        "position": "flat",
                        "action": action,
                    }
                )
                continue

        # ===== EXIT LOGIC ===================================================
        if position_side is not None and not just_closed:
            # 1) Fixed stop-loss
            if cfg.fixed_stop_loss > 0:
                unrealized = (
                    (exec_price - entry_price) * quantity
                    if position_side == "long"
                    else (entry_price - exec_price) * quantity
                )
                if unrealized <= -abs(cfg.fixed_stop_loss):
                    profit = unrealized
                    reason = f"Fixed Stop Loss ${cfg.fixed_stop_loss:.0f}"
                    if position_side == "long":
                        trades_long.append(
                            TradeRecord(
                                cfg.symbol,
                                cfg.interval,
                                "long",
                                entry_time,
                                entry_price,
                                cur_ts,
                                exec_price,
                                quantity,
                                profit,
                                reason,
                            )
                        )
                    else:
                        trades_short.append(
                            TradeRecord(
                                cfg.symbol,
                                cfg.interval,
                                "short",
                                entry_time,
                                entry_price,
                                cur_ts,
                                exec_price,
                                quantity,
                                profit,
                                reason,
                            )
                        )
                    position_side = None
                    entry_price = 0.0
                    entry_time = None
                    action = "EXIT_STOP_LOSS"
                    just_closed = True

            # 2) Prob-based & trailing exits
            if position_side is not None and not just_closed:
                should_exit, exit_reason = should_exit_trade(
                    position_side,
                    entry_price,
                    exec_price,
                    prob_up,
                    prob_down,
                    cfg,
                    entry_time,
                    cur_ts,
                )
                if should_exit:
                    profit = (exec_price - entry_price) * quantity if position_side == "long" else (
                        entry_price - exec_price
                    ) * quantity
                    if position_side == "long":
                        trades_long.append(
                            TradeRecord(
                                cfg.symbol,
                                cfg.interval,
                                "long",
                                entry_time,
                                entry_price,
                                cur_ts,
                                exec_price,
                                quantity,
                                profit,
                                exit_reason,
                            )
                        )
                    else:
                        trades_short.append(
                            TradeRecord(
                                cfg.symbol,
                                cfg.interval,
                                "short",
                                entry_time,
                                entry_price,
                                cur_ts,
                                exec_price,
                                quantity,
                                profit,
                                exit_reason,
                            )
                        )
                    position_side = None
                    entry_price = 0.0
                    entry_time = None
                    action = f"EXIT_{exit_reason}"
                    just_closed = True

            # 3) Partial profit
            if position_side is not None and not just_closed and quantity >= 2:
                should_profit, gain_pct = should_take_partial_profit(
                    entry_price, exec_price, position_side, cfg.take_profit_percent
                )
                if should_profit:
                    partial_qty = quantity // 2
                    partial_profit = (
                        (exec_price - entry_price) * partial_qty
                        if position_side == "long"
                        else (entry_price - exec_price) * partial_qty
                    )
                    if position_side == "long":
                        trades_long.append(
                            TradeRecord(
                                cfg.symbol,
                                cfg.interval,
                                "long",
                                entry_time,
                                entry_price,
                                cur_ts,
                                exec_price,
                                partial_qty,
                                partial_profit,
                                f"PARTIAL_PROFIT_{gain_pct:.1f}%",
                            )
                        )
                    else:
                        trades_short.append(
                            TradeRecord(
                                cfg.symbol,
                                cfg.interval,
                                "short",
                                entry_time,
                                entry_price,
                                cur_ts,
                                exec_price,
                                partial_qty,
                                partial_profit,
                                f"PARTIAL_PROFIT_{gain_pct:.1f}%",
                            )
                        )
                    quantity = quantity - partial_qty
                    action = f"PARTIAL_PROFIT_{gain_pct:.1f}%"

        # ===== ENTRY LOGIC (SMI-gated) ======================================
        if position_side is None and not just_closed:
            # Additional raw volume guard
            if check_volume_requirement(price_df, i, cfg.min_volume_multiplier):
                # LONG: SMI high + prob_up strong
                if (
                    smi_val >= cfg.smi_long_level
                    and prob_up >= cfg.long_threshold
                    and prob_up > prob_down + cfg.min_prob_advantage
                ):
                    position_side = "long"
                    entry_price = exec_price
                    entry_time = cur_ts
                    quantity = cfg.trade_size
                    action = "OPEN_LONG_SMI"
                # SHORT: SMI low + prob_down strong
                elif (
                    smi_val <= cfg.smi_short_level
                    and prob_down >= cfg.short_threshold
                    and prob_down > prob_up + cfg.min_prob_advantage
                ):
                    position_side = "short"
                    entry_price = exec_price
                    entry_time = cur_ts
                    quantity = cfg.trade_size
                    action = "OPEN_SHORT_SMI"

        # ===== EOD CLOSE =====================================================
        if cfg.eod_close and position_side is not None and not just_closed:
            cur_time_et = cur_ts.astimezone(_ET) if cur_ts.tzinfo else _ET.localize(cur_ts)
            if cur_time_et.hour >= 15 and (cur_time_et.hour > 15 or cur_time_et.minute >= 58):
                profit = (exec_price - entry_price) * quantity if position_side == "long" else (
                    entry_price - exec_price
                ) * quantity
                reason = "END_OF_DAY_CLOSE"
                if position_side == "long":
                    trades_long.append(
                        TradeRecord(
                            cfg.symbol,
                            cfg.interval,
                            "long",
                            entry_time,
                            entry_price,
                            cur_ts,
                            exec_price,
                            quantity,
                            profit,
                            reason,
                        )
                    )
                else:
                    trades_short.append(
                        TradeRecord(
                            cfg.symbol,
                            cfg.interval,
                            "short",
                            entry_time,
                            entry_price,
                            cur_ts,
                            exec_price,
                            quantity,
                            profit,
                            reason,
                        )
                    )
                position_side = None
                entry_price = 0.0
                entry_time = None
                action = "EXIT_EOD"
                just_closed = True

        run_rows.append(
            {
                "timestamp": cur_ts,
                "close": float(price_df["close"].iloc[i]),
                "prob_up": prob_up,
                "prob_down": prob_down,
                "smi": smi_val,
                "position": position_side or "flat",
                "action": action,
                "quantity": quantity if position_side else 0,
            }
        )

        if just_closed:
            daily_trades.extend(trades_long[-1:] if trades_long else [])
            daily_trades.extend(trades_short[-1:] if trades_short else [])

    # Force close at final bar
    if position_side is not None:
        last_ts = idx[-1]
        last_close = float(price_df["close"].iloc[-1])
        profit = (last_close - entry_price) * quantity if position_side == "long" else (
            entry_price - last_close
        ) * quantity
        reason = "MARKET_CLOSE"
        if position_side == "long":
            trades_long.append(
                TradeRecord(
                    cfg.symbol,
                    cfg.interval,
                    "long",
                    entry_time,
                    entry_price,
                    last_ts,
                    last_close,
                    quantity,
                    profit,
                    reason,
                )
            )
        else:
            trades_short.append(
                TradeRecord(
                    cfg.symbol,
                    cfg.interval,
                    "short",
                    entry_time,
                    entry_price,
                    last_ts,
                    last_close,
                    quantity,
                    profit,
                    reason,
                )
            )
        logger.info(f"Closed {position_side} position at market close: {last_ts} @ {last_close}")

    run_df = pd.DataFrame(run_rows).set_index("timestamp")
    return trades_long, trades_short, run_df


# ==== Summary helpers =========================================================

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


# ==== Main entry =============================================================

def main(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(description="AlgoMM2 SMI+Volume backtest evaluator")

    parser.add_argument("-s", "--symbol", required=True)
    parser.add_argument("-i", "--interval", required=True)
    parser.add_argument("-q", "--trade-size", type=float, required=True)
    parser.add_argument("-u", "--user-id", type=int, required=True)

    parser.add_argument("--builder-days", type=int, default=60)
    parser.add_argument("--k-forward", type=int, default=3)

    parser.add_argument("--long-threshold", type=float, default=0.60)
    parser.add_argument("--short-threshold", type=float, default=0.40)
    parser.add_argument("--long-exit-threshold", type=float, default=0.55)
    parser.add_argument("--short-exit-threshold", type=float, default=0.45)

    parser.add_argument("--smi-length", type=int, default=14)
    parser.add_argument("--smi-smooth", type=int, default=5)
    parser.add_argument("--smi-double-smooth", type=int, default=5)
    parser.add_argument("--smi-long-level", type=float, default=60.0)
    parser.add_argument("--smi-short-level", type=float, default=-60.0)

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
        smi_length=args.smi_length,
        smi_smooth=args.smi_smooth,
        smi_double_smooth=args.smi_double_smooth,
        smi_long_level=args.smi_long_level,
        smi_short_level=args.smi_short_level,
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

    prefix = f"{cfg.user_id}_{cfg.symbol}_{cfg.interval}_SMI"

    logger.info(f"Starting SMI+Volume backtest: {cfg.symbol} {cfg.interval}")
    logger.info(f"SMI: length={cfg.smi_length} smooth={cfg.smi_smooth} double={cfg.smi_double_smooth}")
    logger.info(f"SMI gates: LONG≥{cfg.smi_long_level:.1f}, SHORT≤{cfg.smi_short_level:.1f}")
    logger.info(f"Entry prob: Long@{cfg.long_threshold:.2f}, Short@{cfg.short_threshold:.2f}")
    logger.info(f"Exit prob:  Long@{cfg.long_exit_threshold:.2f}, Short@{cfg.short_exit_threshold:.2f}")
    logger.info(f"Risk: Daily loss limit=${cfg.daily_loss_limit_usd:.0f}, Stop=${cfg.fixed_stop_loss:.0f}")

    runner = StockBaseRunner()
    price_df = _fetch_price_frame_latest(runner, cfg.symbol, cfg.interval, cfg.builder_days)
    logger.info(f"Price data: {len(price_df)} bars from {price_df.index[0]} to {price_df.index[-1]}")

    # Build SMI+Volume features directly from price_df
    feat_df = _build_smi_volume_features(price_df, cfg)
    if feat_df is None or feat_df.empty:
        raise RuntimeError("SMI+Volume feature builder returned empty DataFrame.")

    if cfg.save_features:
        feat_path = os.path.join(out_dir, f"{prefix}_features.csv")
        feat_df.to_csv(feat_path)
        logger.info(f"Saved → {feat_path}")

    if cfg.save_raw:
        raw_path = os.path.join(out_dir, f"{prefix}_raw.csv")
        price_df.to_csv(raw_path)
        logger.info(f"Saved → {raw_path}")

    # Train model
    feat_names = ["smi", "vol_rel_20"]
    model_path = os.path.join(MODEL_DIR, f"mm2_{cfg.symbol}_{cfg.interval}_SMI_k{cfg.k_forward}.joblib")
    model, used_feats = _load_or_train_model(
        model_path, feat_df, feat_names, cfg.symbol, cfg.interval, cfg.auto_train
    )

    # Probabilities
    X = _prepare_X(feat_df, used_feats)
    probs = predict_probability(model, X, used_feats)
    if probs is None or len(probs) == 0:
        raise RuntimeError("predict_probability returned no probabilities.")

    prob_raw = pd.Series(probs, index=X.index, name="prob_up_raw")
    window = max(int(cfg.k_forward), 1)
    prob_series = prob_raw.rolling(window=window, min_periods=1).mean().rename("prob_up")

    long_bins = (prob_series >= cfg.long_threshold).sum()
    short_bins = (prob_series <= cfg.short_threshold).sum()
    mid_bins = len(prob_series) - long_bins - short_bins
    logger.info(
        f"Prob distribution: Long≥{cfg.long_threshold:.2f}={long_bins}, "
        f"Short≤{cfg.short_threshold:.2f}={short_bins}, Middle={mid_bins}"
    )

    # SMI series aligned to price_df
    smi_series = feat_df["smi"].reindex(price_df.index).ffill()

    # Simulate trades
    logger.info("Simulating trades with SMI-gated LIVE-style logic...")
    trades_long, trades_short, run_df = _simulate_trades(cfg, price_df, prob_series, smi_series)
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
                    f"{t.quantity:.1f},{t.profit:.2f},{t.exit_reason}\n"
                )

        with open(short_path, "w") as f:
            f.write("Symbol,Interval,Side,Entry_date_time,Entry_price,Exit_date_time,Exit_price,Quantity,Profit,Exit_Reason\n")
            for t in trades_short:
                f.write(
                    f"{t.symbol},{t.interval},{t.side},"
                    f"{t.entry_time.isoformat()},{t.entry_price:.3f},"
                    f"{t.exit_time.isoformat()},{t.exit_price:.3f},"
                    f"{t.quantity:.1f},{t.profit:.2f},{t.exit_reason}\n"
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

        summary_rows.append(
            {
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
            }
        )

    if summary_rows:
        summary_df = pd.DataFrame(summary_rows)
        summary_path = os.path.join(out_dir, f"{prefix}_summary.csv")
        summary_df.to_csv(summary_path, index=False)
        logger.info(f"✅ SMI+Volume Summary saved to {summary_path}")

        print("\n" + "=" * 80)
        print(f"SMI+VOLUME BACKTEST RESULTS: {cfg.symbol} {cfg.interval}")
        print("=" * 80)
        print(summary_df.to_string(index=False))
        print("=" * 80)
        print(f"\n✅ All outputs saved to {out_dir}/")
    else:
        logger.info("No trades generated; summary not written.")


if __name__ == "__main__":
    main()
