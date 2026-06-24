#!/usr/bin/env python3
#/var/www/stockwicks/app/scripts/stock_algos/algoMM_runner.py
"""
AlgoMM Runner (LIVE) — FIXED WITH ADAPTIVE THRESHOLDS

Uses backtest-matching logic but with adaptive thresholds for live trading
- Entry thresholds: Adaptive based on market conditions
- min_prob_advantage: 0.10 (reduced from 0.15 for live trading)
- Volume filtering: 1.2x average volume
- Same probability advantage calculation but more realistic

LIVE FLOW:
- Uses paper_trade_service.open_position / close_position,
  just like algo1_runner.py, so the existing mirror-live flag
  on the bot/user controls whether orders are sent to Schwab.
"""

import warnings
warnings.filterwarnings("ignore", category=RuntimeWarning)

import os
os.environ["LOKY_MAX_CPU_COUNT"] = "4"

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime
from typing import Optional, Any, Dict, Tuple, List

import numpy as np
import pandas as pd
import joblib
from typing import Optional
from datetime import datetime

from sqlalchemy.orm import Session

from app.database.connection import SessionLocal
from app.models.paper_trading_bot import (
    PaperStockTradeBot,
    PaperStockBotOpenTrade,
    PaperStockBotTradeHistory,
)
from app.scripts.stock_algos.base_wiring import (
    StockBaseRunner,
    _ET,
)
from app.scripts.research import Featureset_4 as mm2
build_features = mm2.build_features
from app.scripts.ml.mm_live_helpers import predict_probability
from app.scripts.ml.model_refresh_policy import (
    DEFAULT_MIN_NEW_BARS_BEFORE_RETRAIN,
    DEFAULT_MODEL_MAX_AGE_MINUTES,
    DEFAULT_MODEL_REFRESH_MODE,
    load_or_train_model_with_policy,
    normalize_model_refresh_mode,
)
# IMPORTANT: use same flow as algo1_runner
from app.services.paper_trade_service import open_position, close_position
from app.services.mm_core_engine import (
    MMCorePosition,
    MMCoreState,
    config_from_obj,
    evaluate_entry,
    evaluate_exit,
)
from app.utils.client_context import client_root

logger = logging.getLogger("Algo4_MM_Live")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [Algo4_MM_Live] %(message)s"
)

CLIENT_ROOT = str(client_root())
DATA_ROOT = os.getenv("DATA_DIR", os.path.join(CLIENT_ROOT, "data"))
MODEL_DIR = os.getenv("MODEL_DIR", os.path.join(CLIENT_ROOT, "models"))

# ---------- DEFAULTS (TUNED FOR LIVE) ----------
DEFAULTS = {
    "builder_days": 30,
    "long_threshold": 0.60,
    "short_threshold": 0.40,
    "long_exit_threshold": 0.55,      # hard floor for longs
    "short_exit_threshold": 0.45,     # hard floor for shorts
    "min_prob_advantage": 0.0,
    "hard_stop_usd": 300.0,
    "per_share_stop_pct": 0.0,
    "stop_loss_usd": 300.0,
    "trailing_profit_usd": 75.0,
    "stop_loss_pct": 0.0,
    "trailing_profit_pct": 0.0,
    "eod_close": True,
    "once_per_bar": True,
    "cooldown_sec": 0,
    "k_forward": 3,                   # predict 15 min ahead (TREND not noise)
    "model_refresh_mode": DEFAULT_MODEL_REFRESH_MODE,
    "model_max_age_minutes": DEFAULT_MODEL_MAX_AGE_MINUTES,
    "model_max_age_hours": DEFAULT_MODEL_MAX_AGE_MINUTES / 60.0,
    "min_new_bars_before_retrain": DEFAULT_MIN_NEW_BARS_BEFORE_RETRAIN,
    "daily_loss_limit_usd": 500.0,
    "take_profit_percent": 1,
    "atr_multiplier": 1.0,
    "trailing_stop_activation": 0.5,
    "trailing_stop_distance": 1.0,
    "min_volume_multiplier": 0.0,
    "max_same_direction_losses": 5,   # NEW: block after 3 same-dir losses
    "prob_trail_drop": 0.02,          # NEW: trailing prob drop from peak
    "prob_exit_mode": "trailing",
    "long_fixed_exit_prob": 0.40,
    "short_fixed_exit_prob": 0.60,
    "obv_slope_threshold": 0.0,
}


@dataclass
class BotConfig:
    symbol: str = ""
    algo_name: str = "Algo4_MM"
    feature_set: str = "Featureset_4"
    builder_days: int = DEFAULTS["builder_days"]
    long_threshold: float = DEFAULTS["long_threshold"]
    short_threshold: float = DEFAULTS["short_threshold"]
    long_exit_threshold: float = DEFAULTS["long_exit_threshold"]
    short_exit_threshold: float = DEFAULTS["short_exit_threshold"]
    min_prob_advantage: float = DEFAULTS["min_prob_advantage"]
    hard_stop_usd: float = DEFAULTS["hard_stop_usd"]
    eod_close: bool = DEFAULTS["eod_close"]
    once_per_bar: bool = DEFAULTS["once_per_bar"]
    cooldown_sec: int = DEFAULTS["cooldown_sec"]
    k_forward: int = DEFAULTS["k_forward"]
    model_refresh_mode: str = DEFAULTS["model_refresh_mode"]
    model_max_age_minutes: float = DEFAULTS["model_max_age_minutes"]
    model_max_age_hours: float = DEFAULTS["model_max_age_hours"]
    min_new_bars_before_retrain: int = DEFAULTS["min_new_bars_before_retrain"]
    daily_loss_limit_usd: float = DEFAULTS["daily_loss_limit_usd"]
    take_profit_percent: float = DEFAULTS["take_profit_percent"]
    atr_multiplier: float = DEFAULTS["atr_multiplier"]
    trailing_stop_activation: float = DEFAULTS["trailing_stop_activation"]
    trailing_stop_distance: float = DEFAULTS["trailing_stop_distance"]
    min_volume_multiplier: float = DEFAULTS["min_volume_multiplier"]
    per_share_stop_pct: float = DEFAULTS["per_share_stop_pct"]
    stop_loss_usd: float = DEFAULTS["stop_loss_usd"]
    trailing_profit_usd: float = DEFAULTS["trailing_profit_usd"]
    stop_loss_pct: float = DEFAULTS["stop_loss_pct"]
    trailing_profit_pct: float = DEFAULTS["trailing_profit_pct"]
    max_same_direction_losses: int = DEFAULTS["max_same_direction_losses"]
    prob_trail_drop: float = DEFAULTS["prob_trail_drop"]
    prob_exit_mode: str = DEFAULTS["prob_exit_mode"]
    long_fixed_exit_prob: float = DEFAULTS["long_fixed_exit_prob"]
    short_fixed_exit_prob: float = DEFAULTS["short_fixed_exit_prob"]
    obv_slope_threshold: float = DEFAULTS["obv_slope_threshold"]


# ---------------- VOLUME & ENTRY LOGIC ----------------


# ============== ADAPTIVE VOLUME FILTER (FROM BACKTEST) ==============

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
    from datetime import time  # Add this import inside the function
    import numpy as np
    
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
        
        # Current volume percentile (simple percentile calculation without scipy)
        sorted_volumes = np.sort(volume_slice)
        vol_percentile = (np.searchsorted(sorted_volumes, current_volume) / len(sorted_volumes)) * 100
        
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


def check_volume_requirement(
    df: pd.DataFrame,
    current_index: int,
    min_volume_multiplier: float = 0,
    symbol: str = "TSLA",
    use_adaptive: bool = True
) -> bool:
    """
    Enhanced volume filter with adaptive logic from backtest.
    
    Args:
        use_adaptive: If True, uses smart adaptive filter
                      If False, uses simple multiplier filter
    """
    # If volume filter is disabled, always return True
    if min_volume_multiplier == 0:
        return True
    
    if use_adaptive:
        # Use the smart adaptive filter
        return adaptive_volume_filter(df, current_index, symbol)
    else:
        # Fall back to original simple logic
        try:
            if current_index < 20:
                return True

            current_volume = df["volume"].iloc[current_index]
            avg_volume = df["volume"].iloc[max(0, current_index - 20):current_index].mean()
            return current_volume >= (avg_volume * min_volume_multiplier)
        except Exception:
            # If anything goes wrong, don't block trading based on volume
            return True


# ============== OBV DIRECTION GATE ==============

def calculate_obv_slope(df, current_index, lookback=6):
    """OBV slope - tells you who's in control: buyers or sellers."""
    if current_index < lookback:
        return 0.0
    try:
        close = df["close"].values
        volume = df["volume"].values
        start = current_index - lookback
        obv = 0.0
        for i in range(start, current_index + 1):
            if i > start:
                if close[i] > close[i-1]:
                    obv += volume[i]
                elif close[i] < close[i-1]:
                    obv -= volume[i]
        avg_vol = np.mean(volume[start:current_index+1])
        if avg_vol <= 0:
            return 0.0
        return float(np.clip(obv / (lookback * avg_vol), -2.0, 2.0))
    except Exception:
        return 0.0


def should_enter_trade(
    prob_up: float,
    prob_down: float,
    df: pd.DataFrame,
    current_index: int,
    cfg: BotConfig,
    allow_short: bool = True
) -> Tuple[bool, str, str]:
    """
    Entry logic:
    - Long:
        prob_up >= long_threshold
        AND prob_up > prob_down + min_prob_advantage

    - Short:
        prob_down >= (1 - short_threshold)
        AND prob_down > prob_up + min_prob_advantage

    Returns:
        (should_enter, direction, reason)
        direction: "LONG", "SHORT", or ""
        reason:    e.g. "LONG_CONDITIONS_MET", "LOW_VOLUME",
                   "BOTH_PROBS_BELOW_THRESHOLDS", "INSUFFICIENT_PROB_ADVANTAGE"
    """
    # 1. Volume requirement
    # if not check_volume_requirement(df, current_index, cfg.min_volume_multiplier):
    if not check_volume_requirement(df, current_index, cfg.min_volume_multiplier, symbol=cfg.symbol, use_adaptive=True):
        logger.info("ENTRY_CHECK: rejected due to LOW_VOLUME")
        return False, "", "LOW_VOLUME"

    long_threshold = cfg.long_threshold
    short_threshold_value = 1.0 - cfg.short_threshold
    min_advantage = cfg.min_prob_advantage

    # 2. OBV direction gate
    obv_slope = calculate_obv_slope(df, current_index, lookback=6)

    logger.info(
        "ENTRY_CHECK: UP=%.2f DOWN=%.2f Long>=%.2f Short>=%.2f OBV=%.3f",
        round(prob_up, 2), round(prob_down, 2),
        long_threshold, short_threshold_value, obv_slope,
    )

    # 3. Long / Short conditions
    long_condition = (
        prob_up >= long_threshold and
        prob_up > (prob_down + min_advantage)
    )
    short_condition = (
        prob_down >= short_threshold_value and
        prob_down > (prob_up + min_advantage)
    )

    if long_condition:
        if obv_slope < -cfg.obv_slope_threshold:
            logger.info("LONG blocked: OBV slope %.3f < -%.3f", obv_slope, cfg.obv_slope_threshold)
            return False, "", "OBV_BLOCKS_LONG"
        logger.info("LONG_TRIGGER: UP=%.2f >= %.2f OBV=%.3f", prob_up, long_threshold, obv_slope)
        return True, "LONG", "LONG_CONDITIONS_MET"

    if short_condition and allow_short:
        if obv_slope > cfg.obv_slope_threshold:
            logger.info("SHORT blocked: OBV slope %.3f > %.3f", obv_slope, cfg.obv_slope_threshold)
            return False, "", "OBV_BLOCKS_SHORT"
        logger.info("SHORT_TRIGGER: DOWN=%.2f >= %.2f OBV=%.3f", prob_down, short_threshold_value, obv_slope)
        return True, "SHORT", "SHORT_CONDITIONS_MET"

    # 4. Derive rejection reason
    long_ok = prob_up >= long_threshold
    short_ok = prob_down >= short_threshold_value
    advantage_long_ok = prob_up > (prob_down + min_advantage)
    advantage_short_ok = prob_down > (prob_up + min_advantage)

    reason = "UNKNOWN"
    if not long_ok and not short_ok:
        logger.info("NO_ENTRY: Both probabilities below thresholds.")
        reason = "BOTH_PROBS_BELOW_THRESHOLDS"
    elif not (advantage_long_ok or advantage_short_ok):
        logger.info("NO_ENTRY: Insufficient probability advantage.")
        reason = "INSUFFICIENT_PROB_ADVANTAGE"
    else:
        reason = "OTHER_FILTERS"

    return False, "", reason


# Peak probability tracking per bot
_PEAK_PROB: Dict[int, float] = {}
_PROFIT_PEAK: Dict[int, float] = {}

def should_exit_trade(
    open_trade: PaperStockBotOpenTrade,
    current_price: float,
    prob_up: float,
    prob_down: float,
    cfg: BotConfig,
    now_et: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """
    Exit logic priority:
      1. Per-share stop (1% of entry price)
      2. Hard USD stop ($300)
      3. Probability exit: trailing drop or fixed conviction floor
      4. Optional EOD close
    """
    if not open_trade:
        return False, ""

    position_side = open_trade.position_side
    entry_price = open_trade.entry_price
    quantity = open_trade.quantity
    bot_id = open_trade.bot_id

    # 1. Per-share stop. Set per_share_stop_pct=0 to disable.
    per_share_stop_pct = float(getattr(cfg, "per_share_stop_pct", 0.0) or 0.0)
    if per_share_stop_pct > 0:
        stop_per_share = entry_price * per_share_stop_pct
        if position_side == "long":
            per_share_loss = entry_price - current_price
        else:
            per_share_loss = current_price - entry_price
        if per_share_loss >= stop_per_share:
            logger.info(f"STOP: ${per_share_loss:.2f}/share >= ${stop_per_share:.2f}")
            _PEAK_PROB.pop(bot_id, None)
            return True, "PER_SHARE_STOP"

    # 2. Hard USD stop
    if position_side == "long":
        loss_usd = (entry_price - current_price) * quantity
    else:
        loss_usd = (current_price - entry_price) * quantity
    if float(cfg.hard_stop_usd or 0.0) > 0 and loss_usd >= cfg.hard_stop_usd:
        _PEAK_PROB.pop(bot_id, None)
        return True, "HARD_STOP"

    # 3. Probability exit.
    if position_side == "long":
        conviction = prob_up
    else:
        conviction = prob_down

    prob_exit_mode = str(getattr(cfg, "prob_exit_mode", "trailing") or "trailing").strip().lower()
    if prob_exit_mode == "fixed":
        fixed_attr = "long_fixed_exit_prob" if position_side == "long" else "short_fixed_exit_prob"
        fixed_exit = float(
            getattr(cfg, fixed_attr, getattr(cfg, "prob_fixed_exit_prob", 0.0)) or 0.0
        )
        if fixed_exit > 0 and conviction <= fixed_exit:
            _PEAK_PROB.pop(bot_id, None)
            return True, "PROB_FIXED_EXIT"
    else:
        peak = _PEAK_PROB.get(bot_id, conviction)
        peak = max(peak, conviction)
        _PEAK_PROB[bot_id] = peak

        drop = peak - conviction
        if drop >= cfg.prob_trail_drop:
            logger.info(f"TRAIL: conv={conviction:.2f} peak={peak:.2f} drop={drop:.2f}>={cfg.prob_trail_drop}")
            _PEAK_PROB.pop(bot_id, None)
            return True, "PROB_TRAIL_DROP"

    if cfg.eod_close:
        now_et = _as_et_aware(now_et or datetime.now(_ET)) or datetime.now(_ET)
        if now_et.time() >= dtime(15, 50):
            _PEAK_PROB.pop(bot_id, None)
            return True, "EOD_CLOSE"

    return False, ""


def debug_probability_analysis(prob_up: float, prob_down: float, cfg: BotConfig):
    """Extra debug to understand why entries are rejected (stdout logs)."""
    long_threshold = cfg.long_threshold
    short_threshold_value = 1.0 - cfg.short_threshold
    min_advantage = cfg.min_prob_advantage

    long_ok = prob_up >= long_threshold
    short_ok = prob_down >= short_threshold_value
    advantage_long_ok = prob_up > (prob_down + min_advantage)
    advantage_short_ok = prob_down > (prob_up + min_advantage)

    logger.info(
        "DEBUG_ENTRY: UP=%.3f, DOWN=%.3f",
        prob_up,
        prob_down
    )
    logger.info(
        "DEBUG_THRESH: Long=%.3f, ShortProbDown>=%.3f, Advantage=%.3f",
        long_threshold,
        short_threshold_value,
        min_advantage
    )
    logger.info(
        "DEBUG_CHECKS: LongThreshOK=%s, ShortThreshOK=%s, LongAdvantageOK=%s, ShortAdvantageOK=%s",
        long_ok,
        short_ok,
        advantage_long_ok,
        advantage_short_ok,
    )


# ---------------- LOGGING HELPERS ----------------

def log_trade_decision(
    log_file: str,
    decision: str,
    prob_up: float,
    prob_down: float,
    data_len: int,
    bar_close_px: float,
    bar_open_px: float,
    open_trade: Optional[PaperStockBotOpenTrade] = None,
    thresholds: Optional[dict] = None,
    model_path: str = "",
    symbol: str = "",
    interval: str = "",
    cfg: Optional[BotConfig] = None,
    reason: str = "",
    df: Optional[pd.DataFrame] = None,
    X: Optional[Any] = None,
    feat_cols: Optional[List[str]] = None,
    prob_up_avg: Optional[float] = None,
    prob_up_avg_prev: Optional[float] = None,
):
    thresholds = thresholds or {}
    now_et = datetime.now(_ET)

    long_th = thresholds.get("long", DEFAULTS["long_threshold"])
    short_th = thresholds.get("short", DEFAULTS["short_threshold"])
    long_exit_th = cfg.long_exit_threshold if cfg else DEFAULTS["long_exit_threshold"]
    short_exit_th = cfg.short_exit_threshold if cfg else DEFAULTS["short_exit_threshold"]

    position = "FLAT (no open position)"
    if open_trade:
        position = f"{open_trade.position_side.upper()} @ ${open_trade.entry_price:.2f}"
        try:
            if open_trade.position_side == "long":
                pl = (bar_close_px - open_trade.entry_price) * open_trade.quantity
            else:
                pl = (open_trade.entry_price - bar_close_px) * open_trade.quantity
            position += f"  P&L: ${pl:.2f}"
        except Exception:
            pass

    _write_log_block(
        log_file=log_file,
        action=decision,
        data_len=data_len,
        close_px=bar_close_px,
        prob_up=prob_up,
        prob_down=prob_down,
        ts_est=now_et,
        symbol=symbol,
        interval=interval,
        open_px=bar_open_px,
        position=position,
        model_path=model_path,
        long_threshold=long_th,
        short_threshold=short_th,
        long_exit_threshold=long_exit_th,
        short_exit_threshold=short_exit_th,
        reason=reason,
        df=df,
        X=X,
        feat_cols=feat_cols,
    )


def _write_log_block(
    log_file: str,
    *,
    action: str,
    data_len: int,
    close_px: float,
    prob_up: float,
    prob_down: float,
    ts_est: Optional[datetime] = None,
    symbol: str = "",
    interval: str = "",
    open_px: float = 0.0,
    position: str = "FLAT (no open position)",
    model_path: str = "",
    long_threshold: float = 0.60,
    short_threshold: float = 0.40,
    long_exit_threshold: float = 0.55,
    short_exit_threshold: float = 0.45,
    reason: str = "",
    # NEW optional inputs:
    df: Optional[pd.DataFrame] = None,      # dataframe with OHLCV
    X: Optional[np.ndarray] = None,         # feature matrix
    feat_cols: Optional[List[str]] = None,  # feature names
):
    ts_est = ts_est or datetime.now(_ET)
    ts_str = _fmt_est(ts_est)

    # -------------------------
    #  LAST CANDLE DETAILS
    # -------------------------
    candle_str = "No candle data"
    if df is not None and len(df) > 0:
        try:
            last = df.iloc[-1]
            ts_c = getattr(last, "name", None)
            ts_c = str(ts_c) if ts_c is not None else "N/A"

            o = float(last.get("open", float("nan")))
            h = float(last.get("high", float("nan")))
            l = float(last.get("low", float("nan")))
            c = float(last.get("close", float("nan")))
            v = last.get("volume", 0)

            candle_str = (
                f"{ts_c} | "
                f"O:{o:7.2f}  H:{h:7.2f}  L:{l:7.2f}  C:{c:7.2f}  V:{int(v)}"
            )
        except Exception as e:
            candle_str = f"(failed to read candle: {e})"

    # -------------------------
    #  FEATURE SUMMARY
    # -------------------------
    feat_str = "No feature data"
    if X is not None and len(X) > 0 and feat_cols:
        try:
            # X may be a pandas DataFrame or a numpy array/list. Convert the last
            # feature row into a name -> value dictionary safely.
            if isinstance(X, pd.DataFrame):
                last_series = X.iloc[-1]
                feat_map = {str(k): last_series.get(k) for k in feat_cols}
            else:
                arr = np.asarray(X)
                last_row = arr[-1]
                feat_map = {str(name): val for name, val in zip(feat_cols, last_row)}

            # Show the most useful trading diagnostics first, then fill with
            # whatever feature columns exist. Missing names are skipped.
            preferred = [
                "vwap_dist_atr",
                "obv_slope_3_norm",
                "rsi14_daily",
                "session_pct",
                "atr_pct",
                "momentum_3",
                "vol_ratio",
                "close_vs_open",
                "trend_strength_composite",
                "setup_long",
                "setup_short",
                "vwap_dist",
                "obv_slope",
                "rsi14",
            ]

            ordered_names = []
            for name in preferred:
                if name in feat_map and name not in ordered_names:
                    ordered_names.append(name)
            for name in feat_cols:
                name = str(name)
                if name in feat_map and name not in ordered_names:
                    ordered_names.append(name)

            pairs = []
            for name in ordered_names[:12]:
                val = feat_map.get(name)
                try:
                    pairs.append(f"{name}={float(val):0.3f}")
                except Exception:
                    pairs.append(f"{name}={val}")

            feat_str = ", ".join(pairs) if pairs else "No feature data"
        except Exception as e:
            feat_str = f"(failed to read features: {e})"

    # -------------------------
    #   BUILD LOG BLOCK
    # -------------------------
    block = (
        "╔════════════════════════════════════════════════════════════════\n"
        f" [{ts_str}] {action:<40}\n"
        f" Model name: {model_path}\n"
        f" Symbol: {symbol}\n"
        f" Time: {ts_str}\n"
        f" Probabilities:  UP: {prob_up:>6.3f}  |  DOWN: {prob_down:>6.3f}\n"
        f" Thresholds:    LONG: {long_threshold:.2f}/{long_exit_threshold:.2f}  |  "
        f"SHORT: {short_threshold:.2f}/{short_exit_threshold:.2f}\n"
        "\n"
        f" Last Candle:   {candle_str}\n"
        f" Price Used:    OPEN: $ {open_px:>7.2f}  |  CLOSE: $ {close_px:>7.2f}\n"
        "\n"
        f" Position:      {position}\n"
        f" Data:          {data_len} candles\n"
        f" Interval:      {interval}\n"
        "\n"
        f" Features:      {feat_str}\n"
        "\n"
        f" ACTION:        {action}\n"
        f" REASON:        {reason or '-'}\n"
        "╚════════════════════════════════════════════════════════════════\n\n"
    )

    # -------------------------
    #  WRITE TO FILE
    # -------------------------
    try:
        with open(log_file, "a") as f:
            f.write(block)
    except Exception as e:
        logger.warning(f"Failed to write log block: {e}")



def _as_et_aware(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    try:
        if isinstance(ts, pd.Timestamp):
            ts = ts.to_pydatetime()
        if not isinstance(ts, datetime):
            ts = pd.to_datetime(ts).to_pydatetime()
        if ts.tzinfo is None:
            return _ET.localize(ts) if hasattr(_ET, "localize") else ts.replace(tzinfo=_ET)
        return ts.astimezone(_ET)
    except Exception:
        return None


def _fmt_est(ts: datetime) -> str:
    ts_et = _as_et_aware(ts)
    if ts_et is None:
        return "N/A"
    return ts_et.strftime("%Y-%m-%d %H:%M:%S %Z")


def _fetch_source_bars_for_bot(runner: StockBaseRunner, bot: PaperStockTradeBot, cfg: BotConfig) -> pd.DataFrame:
    kwargs = {
        "interval": bot.interval or "1min",
        "lookback_days": cfg.builder_days,
        "user_id": bot.user_id,
    }
    for drop_key in (None, "user_id", "lookback_days", "interval"):
        if drop_key:
            kwargs.pop(drop_key, None)
        try:
            return runner.fetch_source_bars(bot.symbol, **kwargs)
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc):
                raise
    return runner.fetch_source_bars(bot.symbol)


# ---------------- CONFIG LOADERS ----------------

def _load_bot_config(bot: PaperStockTradeBot) -> BotConfig:
    """
    Load config from (highest precedence first):
      1) JSON in: config_json | settings | params | note
      2) Direct columns on the bot (if present)
      3) DEFAULTS
    """
    cfg = BotConfig()
    cfg.symbol = bot.symbol  # ADD THIS LINE

    # 1) JSON blobs
    for json_field in ("config_json", "settings", "params", "note"):
        if hasattr(bot, json_field):
            js = _parse_json_field(getattr(bot, json_field))
            if not js:
                continue

            cfg.builder_days = _safe_int(js.get("builder_days", cfg.builder_days), cfg.builder_days)
            cfg.long_threshold = _safe_float(js.get("long_threshold", cfg.long_threshold), cfg.long_threshold)
            cfg.short_threshold = _safe_float(js.get("short_threshold", cfg.short_threshold), cfg.short_threshold)
            cfg.long_exit_threshold = _safe_float(js.get("long_exit_threshold", cfg.long_exit_threshold), cfg.long_exit_threshold)
            cfg.short_exit_threshold = _safe_float(js.get("short_exit_threshold", cfg.short_exit_threshold), cfg.short_exit_threshold)
            cfg.min_prob_advantage = _safe_float(js.get("min_prob_advantage", cfg.min_prob_advantage), cfg.min_prob_advantage)
            cfg.min_volume_multiplier = _safe_float(js.get("min_volume_multiplier", cfg.min_volume_multiplier), cfg.min_volume_multiplier)
            cfg.stop_loss_usd = _safe_float(js.get("stop_loss_usd", js.get("hard_stop_usd", cfg.stop_loss_usd)), cfg.stop_loss_usd)
            cfg.hard_stop_usd = cfg.stop_loss_usd
            cfg.stop_loss_pct = _safe_float(js.get("stop_loss_pct", js.get("per_share_stop_pct", cfg.stop_loss_pct)), cfg.stop_loss_pct)
            cfg.per_share_stop_pct = cfg.stop_loss_pct
            cfg.trailing_profit_usd = _safe_float(
                js.get(
                    "trailing_profit_usd",
                    js.get("trailing_stop_distance", js.get("trailing_stop_activation", cfg.trailing_profit_usd)),
                ),
                cfg.trailing_profit_usd,
            )
            cfg.trailing_stop_activation = cfg.trailing_profit_usd
            cfg.trailing_stop_distance = cfg.trailing_profit_usd
            cfg.trailing_profit_pct = _safe_float(
                js.get("trailing_profit_pct", js.get("per_share_trailing_profit_pct", cfg.trailing_profit_pct)),
                cfg.trailing_profit_pct,
            )
            cfg.daily_loss_limit_usd = _safe_float(js.get("daily_loss_limit_usd", cfg.daily_loss_limit_usd), cfg.daily_loss_limit_usd)
            cfg.take_profit_percent = _safe_float(js.get("take_profit_percent", cfg.take_profit_percent), cfg.take_profit_percent)
            cfg.eod_close = _safe_bool(js.get("eod_close", cfg.eod_close), cfg.eod_close)
            cfg.once_per_bar = _safe_bool(js.get("once_per_bar", cfg.once_per_bar), cfg.once_per_bar)
            cfg.cooldown_sec = _safe_int(js.get("cooldown_sec", cfg.cooldown_sec), cfg.cooldown_sec)
            cfg.k_forward = _safe_int(js.get("k_forward", cfg.k_forward), cfg.k_forward)
            cfg.model_refresh_mode = normalize_model_refresh_mode(js.get("model_refresh_mode", cfg.model_refresh_mode))
            cfg.model_max_age_minutes = _safe_float(
                js.get("model_max_age_minutes", cfg.model_max_age_minutes),
                cfg.model_max_age_minutes,
            )
            cfg.model_max_age_hours = _safe_float(js.get("model_max_age_hours", cfg.model_max_age_hours), cfg.model_max_age_hours)
            if "model_max_age_minutes" not in js and "model_max_age_hours" in js:
                cfg.model_max_age_minutes = max(0.0, cfg.model_max_age_hours * 60.0)
            cfg.min_new_bars_before_retrain = max(
                0,
                _safe_int(
                    js.get("min_new_bars_before_retrain", cfg.min_new_bars_before_retrain),
                    cfg.min_new_bars_before_retrain,
                ),
            )
            cfg.prob_trail_drop = _safe_float(js.get("prob_trail_drop", cfg.prob_trail_drop), cfg.prob_trail_drop)
            cfg.prob_exit_mode = str(js.get("prob_exit_mode", cfg.prob_exit_mode) or cfg.prob_exit_mode).strip().lower()
            if cfg.prob_exit_mode not in {"trailing", "fixed"}:
                cfg.prob_exit_mode = DEFAULTS["prob_exit_mode"]
            cfg.long_fixed_exit_prob = _safe_float(
                js.get("long_fixed_exit_prob", js.get("prob_fixed_exit_prob", cfg.long_fixed_exit_prob)),
                cfg.long_fixed_exit_prob,
            )
            cfg.short_fixed_exit_prob = _safe_float(
                js.get("short_fixed_exit_prob", js.get("prob_fixed_exit_prob", cfg.short_fixed_exit_prob)),
                cfg.short_fixed_exit_prob,
            )

    # 2) Direct columns
    for col, attr, caster in [
        ("builder_days", "builder_days", _safe_int),
        ("long_threshold", "long_threshold", _safe_float),
        ("short_threshold", "short_threshold", _safe_float),
        ("long_exit_threshold", "long_exit_threshold", _safe_float),
        ("short_exit_threshold", "short_exit_threshold", _safe_float),
        ("min_prob_advantage", "min_prob_advantage", _safe_float),
        ("min_volume_multiplier", "min_volume_multiplier", _safe_float),
        ("hard_stop_usd", "stop_loss_usd", _safe_float),
        ("stop_loss_usd", "stop_loss_usd", _safe_float),
        ("trailing_profit_usd", "trailing_profit_usd", _safe_float),
        ("daily_loss_limit_usd", "daily_loss_limit_usd", _safe_float),
        ("take_profit_percent", "take_profit_percent", _safe_float),
        ("eod_close", "eod_close", _safe_bool),
        ("once_per_bar", "once_per_bar", _safe_bool),
        ("cooldown_sec", "cooldown_sec", _safe_int),
        ("k_forward", "k_forward", _safe_int),
    ]:
        if hasattr(bot, attr):
            val = getattr(bot, attr)
            if val is not None:
                current = getattr(cfg, col)
                try:
                    setattr(cfg, col, caster(val, current))
                    if col == "stop_loss_usd":
                        cfg.hard_stop_usd = cfg.stop_loss_usd
                    if col == "trailing_profit_usd":
                        cfg.trailing_stop_activation = cfg.trailing_profit_usd
                        cfg.trailing_stop_distance = cfg.trailing_profit_usd
                except Exception:
                    pass

    if cfg.k_forward < 1:
        cfg.k_forward = 1

    return cfg


def _safe_float(v: Any, default: float) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _safe_int(v: Any, default: int) -> int:
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default


def _safe_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off"):
            return False
    return default


def _parse_json_field(s: Any) -> Dict[str, Any]:
    if not isinstance(s, str) or not s.strip():
        return {}
    try:
        return json.loads(s)
    except Exception:
        try:
            return json.loads(s.replace("'", '"'))
        except Exception:
            return {}


# ---------------- MODEL HELPERS ----------------

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
    symbol: str,
    interval: str,
    feat_df: pd.DataFrame,
    feat_names: List[str],
    force_retrain: bool = False,
    cfg: Any = None,
):
    """
    Load a saved model if compatible; otherwise train a new one.

    This version gracefully handles sklearn/joblib incompatibility errors
    by retraining the model and overwriting the old file.
    """
    try:
        return load_or_train_model_with_policy(
            model_path=model_path,
            symbol=symbol,
            interval=interval,
            feat_df=feat_df,
            feat_names=feat_names,
            cfg=cfg,
            prepare_X=_prepare_X,
            logger=logger,
            force_retrain=force_retrain,
        )
    except Exception as e:
        logger.error(f"[AlgoMM] Error in _load_or_train_model: {e}", exc_info=True)
        raise


def get_smart_execution_price(df: pd.DataFrame, execution_type: str = "vwap") -> float:
    """
    For now, just use the latest CLOSE.
    If you add vwap/twap columns, you can upgrade this easily.
    """
    try:
        if df is None or df.empty:
            return 0.0
        last_row = df.iloc[-1]
        return float(last_row.get("close", 0.0))
    except Exception:
        return 0.0


def _execute_order(
    db: Session,
    bot: PaperStockTradeBot,
    side: str,
    qty: float,
    price: float | None,
    actor: str,
    symbol: str | None = None,
):
    """
    IMPORTANT: follow same live flow as algo1_runner.

    We don't talk to Schwab directly here.
    We call open_position(...) from paper_trade_service, which:
      - creates the paper trade, AND
      - if the bot/user has mirror-live enabled, also sends
        the corresponding order to Schwab using trade_service.
    """
    symbol = symbol or bot.symbol
    if price is None:
        price = 0.0

    logger.info(
        "EXEC_ORDER: bot=%s symbol=%s side=%s qty=%.2f actor=%s",
        bot.id,
        symbol,
        side,
        qty,
        actor,
    )

    # Convert "BUY"/"SELL" into "long"/"short" like algo1_runner
    position_side = "long" if side.upper() == "BUY" else "short"

    # open_position internally uses bot.trade_size, so qty here is mostly for logging.
    # We still pass the execution price for correct P&L and mirroring.
    open_position(db, bot, position_side, float(price))


# ---------------- COOL DOWN HELPER ----------------

def get_last_trade_time(bot_id: int, db: Session) -> Optional[datetime]:
    """
    Return the timestamp of the latest trade for this bot.
    Adjust the timestamp field name to your actual model (created_at / opened_at).
    """
    try:
        last_hist = (
            db.query(PaperStockBotTradeHistory)
            .filter_by(bot_id=bot_id)
            .order_by(PaperStockBotTradeHistory.id.desc())
            .first()
        )
        if last_hist is None:
            return None

        # Adjust field name as needed
        ts = getattr(last_hist, "created_at", None) or getattr(last_hist, "opened_at", None)
        return ts
    except Exception:
        return None


# ---------------- MAIN TICK FUNCTION ----------------


def run_algoMM_bot_tick(bot_id: int, anchor_dt: Optional[datetime] = None):
    db: Session = SessionLocal()
    runner = StockBaseRunner()

    decision = "NONE"
    reason = ""
    data_len = 0
    prob_up = 0.0
    prob_down = 0.0
    bar_close_px = 0.0
    bar_open_px = 0.0
    open_trade = None
    bot = None
    log_file = ""
    model_path = ""
    X = None
    feat_names: List[str] = []

    try:
        bot = db.query(PaperStockTradeBot).filter_by(id=bot_id).first()
        if not bot:
            logger.warning(f"Bot {bot_id} not found")
            return


        cfg = _load_bot_config(bot)

        # If caller (Celery/runner) passed in a snapped anchor_dt, use that
        # as "now" for this tick; otherwise fall back to real current time.
        if anchor_dt is not None:
            if anchor_dt.tzinfo is None:
                now_et = _ET.localize(anchor_dt)
            else:
                now_et = anchor_dt.astimezone(_ET)
        else:
            now_et = datetime.now(_ET)


        # Set up log file
        log_dir = os.path.join(DATA_ROOT, str(bot.user_id))
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f"bot_{bot.id}_{bot.symbol}_AlgoMM.log")

        # Fetch raw data and resample
        df_raw = _fetch_source_bars_for_bot(runner, bot, cfg)
        if df_raw is None or df_raw.empty:
            decision = "NO_DATA"
            reason = "RAW_DF_EMPTY"
            log_trade_decision(
                log_file,
                decision,
                prob_up,
                prob_down,
                data_len,
                bar_close_px,
                bar_open_px,
                None,
                {"long": cfg.long_threshold, "short": cfg.short_threshold},
                cfg=cfg,
                reason=reason,
            )
            return

        latest_time, _, df = runner.resample_interval(
            df_raw,
            (bot.interval or "1min"),
            bot.symbol,
        )
        if latest_time is None or df is None or df.empty:
            decision = "RESAMPLE_FAILED"
            reason = "RESAMPLE_EMPTY"
            log_trade_decision(
                log_file,
                decision,
                prob_up,
                prob_down,
                data_len,
                bar_close_px,
                bar_open_px,
                None,
                {"long": cfg.long_threshold, "short": cfg.short_threshold},
                cfg=cfg,
                reason=reason,
            )
            return

        df = df.sort_index()
        data_len = len(df)
        bar_close_px = float(df["close"].iloc[-1])
        bar_open_px = float(df["open"].iloc[-1])

        # Validate prices
        if (
            bar_close_px <= 0 or not np.isfinite(bar_close_px) or
            bar_open_px <= 0 or not np.isfinite(bar_open_px)
        ):
            decision = "INVALID_PRICE"
            reason = "NON_POSITIVE_OR_NAN_PRICE"
            log_trade_decision(
                log_file,
                decision,
                prob_up,
                prob_down,
                data_len,
                bar_close_px,
                bar_open_px,
                None,
                {"long": cfg.long_threshold, "short": cfg.short_threshold},
                cfg=cfg,
                reason=reason,
            )
            return

        # Build features
        feat_df = build_features(
            symbol=bot.symbol,
            interval=(bot.interval or "1min"),
            days=cfg.builder_days,
            k_forward=cfg.k_forward,
        )

        if feat_df is None or feat_df.empty or len(feat_df) < 5:
            decision = "NO_FEATURES"
            reason = "FEATURE_DF_TOO_SHORT_OR_EMPTY"
            log_trade_decision(
                log_file,
                decision,
                prob_up,
                prob_down,
                data_len,
                bar_close_px,
                bar_open_px,
                None,
                {"long": cfg.long_threshold, "short": cfg.short_threshold},
                cfg=cfg,
                reason=reason,
                df=df,
            )
            return

        default_feat_names = [c for c in feat_df.columns if c not in ("y", "w")]
        model_path = os.path.join(
            MODEL_DIR,
            f"mm4_{bot.symbol}_{bot.interval}_k{cfg.k_forward}.joblib",
        )

        model, feat_names = _load_or_train_model(
            model_path=model_path,
            symbol=bot.symbol,
            interval=(bot.interval or "1min"),
            feat_df=feat_df,
            feat_names=default_feat_names,
            cfg=cfg,
        )

        X = _prepare_X(feat_df, feat_names)
        probs = predict_probability(model, X, feat_names)
        if probs is None or len(probs) < 2:
            decision = "NO_PROBS"
            reason = "PREDICT_PROBS_EMPTY"
            log_trade_decision(
                log_file,
                decision,
                prob_up,
                prob_down,
                data_len,
                bar_close_px,
                bar_open_px,
                None,
                {"long": cfg.long_threshold, "short": cfg.short_threshold},
                cfg=cfg,
                reason=reason,
                df=df,
                X=X,
                feat_cols=feat_names,
            )
            return

        # Compute k-forward rolling average probabilities
        prob_raw = pd.Series(probs, index=X.index, name="prob_up_raw")
        window = max(int(cfg.k_forward), 1)
        prob_series = prob_raw.rolling(window=window, min_periods=1).mean().rename("prob_up")

        # Align to price-data index (resampled df)
        prob_series_aligned = prob_series.reindex(df.index).ffill().bfill()

        if len(prob_series_aligned) < 2:
            decision = "PROB_ALIGN_SHORT"
            reason = "ALIGNED_PROB_SERIES_TOO_SHORT"
            log_trade_decision(
                log_file,
                decision,
                prob_up,
                prob_down,
                data_len,
                bar_close_px,
                bar_open_px,
                None,
                {"long": cfg.long_threshold, "short": cfg.short_threshold},
                cfg=cfg,
                reason=reason,
                df=df,
                X=X,
                feat_cols=feat_names,
            )
            return

        prev_prob = float(prob_series_aligned.iloc[-2])
        prev_prev_prob = float(prob_series_aligned.iloc[-3]) if len(prob_series_aligned) >= 3 else prev_prob
        if not np.isfinite(prev_prob):
            decision = "NO_VALID_PROB"
            reason = "PREV_PROB_NAN_OR_INF"
            log_trade_decision(
                log_file,
                decision,
                prob_up,
                prob_down,
                data_len,
                bar_close_px,
                bar_open_px,
                None,
                {"long": cfg.long_threshold, "short": cfg.short_threshold},
                cfg=cfg,
                reason=reason,
                df=df,
                X=X,
                feat_cols=feat_names,
            )
            return

        prob_up = prev_prob
        prob_down = 1.0 - prev_prob

        # Debug probabilities and thresholds (stdout logs)
        debug_probability_analysis(prob_up, prob_down, cfg)

        # Check if there is an open trade
        open_trade = db.query(PaperStockBotOpenTrade).filter_by(bot_id=bot.id).first()

        # Cooldown guard: avoid rapid re-entries, but never block exits/EOD.
        if open_trade is None and cfg.cooldown_sec and cfg.cooldown_sec > 0:
            last_trade_time = get_last_trade_time(bot.id, db)
            if last_trade_time is not None:
                last_trade_time = _as_et_aware(last_trade_time)
            if last_trade_time is not None:
                delta_sec = (now_et - last_trade_time).total_seconds()
                if delta_sec < cfg.cooldown_sec:
                    decision = "COOLDOWN"
                    reason = f"LAST_TRADE_AT_{_fmt_est(last_trade_time)}_DELTA_{int(delta_sec)}s"
                    log_trade_decision(
                        log_file,
                        decision,
                        prob_up,
                        prob_down,
                        data_len,
                        bar_close_px,
                        bar_open_px,
                        open_trade,
                        {"long": cfg.long_threshold, "short": cfg.short_threshold},
                        model_path=model_path,
                        symbol=bot.symbol,
                        interval=bot.interval,
                        cfg=cfg,
                        reason=reason,
                        df=df,
                        X=X,
                        feat_cols=feat_names,
                    )
                    return

        # ENTRY / EXIT LOGIC
        if open_trade is None:
            entry_decision = evaluate_entry(
                prob_up_avg=prob_up,
                prob_up_avg_prev=prev_prev_prob,
                cfg=config_from_obj(cfg, allow_short=bool(getattr(bot, "allow_short_selling", True))),
            )
            should_enter = entry_decision.should_act
            enter_direction = entry_decision.action
            entry_reason = entry_decision.reason

            if not should_enter:
                decision = "NO_ENTRY_SIGNAL"
                reason = entry_reason or "ENTRY_CONDITIONS_NOT_MET"
            else:
                # For consistency with algo1_runner, let open_position decide size
                trade_size = getattr(bot, "trade_size", None)
                qty = float(trade_size or 0.0)
                exec_price = get_smart_execution_price(df, execution_type="vwap")
                side = "BUY" if enter_direction == "LONG" else "SELL"
                _execute_order(
                    db=db,
                    bot=bot,
                    side=side,
                    qty=qty,
                    price=exec_price,
                    actor=f"AlgoMM_OPEN_{enter_direction}",
                    symbol=bot.symbol,
                )
                db.commit()
                decision = f"OPEN_{enter_direction}"
                reason = entry_reason or f"{enter_direction}_CONDITIONS_MET"
                open_trade = db.query(PaperStockBotOpenTrade).filter_by(bot_id=bot.id).first()
                _PEAK_PROB[int(bot.id)] = float(prob_up if enter_direction == "LONG" else prob_down)
                _PROFIT_PEAK[int(bot.id)] = 0.0

        elif open_trade is not None:
            side = str(open_trade.position_side or "").lower()
            conviction = prob_up if side == "long" else prob_down
            state = MMCoreState(
                prob_peak=float(_PEAK_PROB.get(int(open_trade.bot_id), conviction)),
                profit_peak=float(_PROFIT_PEAK.get(int(open_trade.bot_id), 0.0)),
            )
            exit_decision = evaluate_exit(
                MMCorePosition(
                    side=side,
                    entry_price=float(open_trade.entry_price or 0.0),
                    quantity=float(open_trade.quantity or 0.0),
                ),
                current_price=bar_close_px,
                prob_up_avg=prob_up,
                cfg=config_from_obj(cfg),
                state=state,
                now_et=now_et,
            )
            should_exit = exit_decision.should_act
            exit_reason = exit_decision.reason
            if not should_exit and exit_decision.state:
                _PEAK_PROB[int(open_trade.bot_id)] = float(exit_decision.state.prob_peak or 0.0)
                _PROFIT_PEAK[int(open_trade.bot_id)] = float(exit_decision.state.profit_peak or 0.0)
            if should_exit:
                exec_price = get_smart_execution_price(df, execution_type="vwap")
                close_position(db, open_trade, exec_price)
                db.commit()
                _PEAK_PROB.pop(int(open_trade.bot_id), None)
                _PROFIT_PEAK.pop(int(open_trade.bot_id), None)
                decision = f"EXIT_{exit_reason}"
                reason = exit_reason
                open_trade = None
            else:
                decision = "HOLD_POSITION"
                reason = "EXIT_CONDITIONS_NOT_MET"

        log_trade_decision(
            log_file,
            decision,
            prob_up,
            prob_down,
            data_len,
            bar_close_px,
            bar_open_px,
            open_trade,
            {"long": cfg.long_threshold, "short": cfg.short_threshold},
            model_path=model_path,
            symbol=bot.symbol,
            interval=bot.interval,
            cfg=cfg,
            reason=reason,
            df=df if 'df' in locals() else None,
            X=X,
            feat_cols=feat_names,
        )

    except Exception as e:
        db.rollback()
        decision = f"ERROR: {e}"
        reason = f"EXCEPTION: {e}"
        logger.error(f"[Algo4_MM] tick error for bot {bot_id}: {e}", exc_info=True)
        if bot:
            log_trade_decision(
                log_file,
                decision,
                prob_up,
                prob_down,
                data_len,
                bar_close_px,
                bar_open_px,
                open_trade,
                {"long": cfg.long_threshold if 'cfg' in locals() else DEFAULTS["long_threshold"],
                 "short": cfg.short_threshold if 'cfg' in locals() else DEFAULTS["short_threshold"]},
                cfg=cfg if 'cfg' in locals() else None,
                reason=reason,
                df=df if 'df' in locals() else None,
                X=X if 'X' in locals() else None,
                feat_cols=feat_names if 'feat_names' in locals() else None,
            )
    finally:
        if db:
            db.close()


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 2:
        run_algoMM_bot_tick(int(sys.argv[1]))
    else:
        print("Usage: python -m app.scripts.stock_algos.Algo4_MM <BOT_ID>")
