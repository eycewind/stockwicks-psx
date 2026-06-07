# /var/www/stockwicks/app/scripts/stock_algos/prediction_strategies.py
"""
Prediction Strategies — fixes the k_forward=5 problem
=======================================================

The old approach: train model with k_forward=5, which labels each row by
looking 5 bars into the future. In live, those future bars don't exist,
so labels are garbage. In replay, it's look-ahead bias (cheating).

All three strategies below use k_forward=1 ONLY. One bar ahead is all
you can ever label honestly in live. The confidence improvement comes
from HOW we combine/filter that 1-bar prediction — not from peeking
further into the future.

Usage:
    from prediction_strategies import PredictionEngine

    # In your config_json:
    # {"prediction_strategy": "multi_horizon"}   — 3 models vote
    # {"prediction_strategy": "confirmation"}    — wait for streak
    # {"prediction_strategy": "rolling"}         — smoothed signal
    # {"prediction_strategy": "single"}          — original (1 model, no filter)

    engine = PredictionEngine(strategy="multi_horizon")
    prob_up, prob_down, meta = engine.predict(
        feat_df_labeled, feat_df_current, symbol, interval, bot_id, cfg
    )
    # prob_up/prob_down are the filtered probabilities
    # meta dict has debug info (per-horizon probs, streak count, etc.)
"""
from __future__ import annotations

import os
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("PredictionStrategies")

# We import the model training machinery from the live runner
# so all strategies use the exact same model code
from app.scripts.stock_algos.algoMM_runner import (
    _train_model,
    _prepare_X,
    MODEL_DIR,
)
from app.scripts.research.mm_features3_builder import build_features_from_df


# =============================================================================
# Strategy 1: MULTI-HORIZON CONSENSUS
# =============================================================================
# Train 3 models: k=1 (next bar), k=2 (2 bars out), k=3 (3 bars out).
# Each model answers a different question:
#   k=1: "Will price be up in 5 minutes?"
#   k=2: "Will price be up in 10 minutes?"
#   k=3: "Will price be up in 15 minutes?"
#
# All 3 use current features to predict. We only enter when
# the consensus (majority or all) agrees.
#
# Why this works in live: k=1 labels need 1 future bar (always exists
# in historical training data, never needed at prediction time).
# k=2 and k=3 same — labels use 2-3 past bars during training,
# prediction only needs current features.
#
# Why this improves accuracy: a 1-bar spike fools k=1 but not k=2/k=3.
# A real trend shows up across all horizons. False signals get filtered.
# =============================================================================

_MULTI_HORIZON_CACHE: Dict[int, Dict[int, Dict[str, Any]]] = {}
# bot_id -> {k -> {model, features, trained_at_tick}}


def _multi_horizon_predict(
    feat_df_labeled_base: Optional[pd.DataFrame],
    feat_df_current: pd.DataFrame,
    df_for_labels: pd.DataFrame,
    symbol: str,
    interval: str,
    bot_id: int,
    horizons: List[int] = None,
    consensus_mode: str = "majority",  # "majority" = 2/3, "unanimous" = 3/3
    retrain: bool = True,
) -> Tuple[float, float, Dict[str, Any]]:
    """
    Train one model per horizon, predict with each, combine.

    Returns:
        prob_up, prob_down: consensus probability
        meta: {per_horizon: {k: prob_up}, agreement: float, models_trained: int}
    """
    if horizons is None:
        horizons = [1, 2, 3]

    meta = {
        "strategy": "multi_horizon",
        "horizons": horizons,
        "per_horizon": {},
        "agreement": 0.0,
        "models_trained": 0,
        "consensus_mode": consensus_mode,
    }

    if feat_df_current is None or feat_df_current.empty:
        return 0.5, 0.5, meta

    if bot_id not in _MULTI_HORIZON_CACHE:
        _MULTI_HORIZON_CACHE[bot_id] = {}

    probs_up = []

    for k in horizons:
        model = None
        feat_names = []

        if retrain and df_for_labels is not None and not df_for_labels.empty:
            # Build labels for this specific horizon
            try:
                feat_df_k = build_features_from_df(
                    df_for_labels,
                    symbol=symbol,
                    interval=interval,
                    k_forward=k,
                    history_days=30,
                )
            except Exception as e:
                logger.warning(f"Multi-horizon k={k} label build failed: {e}")
                feat_df_k = None

            if feat_df_k is not None and not feat_df_k.empty and "y" in feat_df_k.columns:
                model_path = os.path.join(
                    MODEL_DIR,
                    f"mh_{bot_id}_{symbol}_{interval}_k{k}.joblib",
                )
                model, feat_names = _train_model(feat_df_k, symbol, interval, k, model_path)

                if model is not None:
                    _MULTI_HORIZON_CACHE[bot_id][k] = {
                        "model": model,
                        "features": feat_names,
                    }
                    meta["models_trained"] += 1

        # Fallback to cache
        if model is None and k in _MULTI_HORIZON_CACHE.get(bot_id, {}):
            cached = _MULTI_HORIZON_CACHE[bot_id][k]
            model = cached["model"]
            feat_names = cached["features"]

        if model is not None:
            X = _prepare_X(feat_df_current, feat_names)
            try:
                probs = model.predict_proba(X)
                if probs is not None and len(probs) > 0:
                    p_up = float(probs[-1][1])
                    probs_up.append(p_up)
                    meta["per_horizon"][k] = round(p_up, 4)
            except Exception as e:
                logger.warning(f"Multi-horizon k={k} predict failed: {e}")

    if not probs_up:
        return 0.5, 0.5, meta

    # Consensus
    avg_prob_up = sum(probs_up) / len(probs_up)

    if consensus_mode == "unanimous":
        # All models must agree on direction
        all_up = all(p > 0.5 for p in probs_up)
        all_down = all(p < 0.5 for p in probs_up)
        if all_up:
            prob_up = avg_prob_up
        elif all_down:
            prob_up = avg_prob_up
        else:
            # Disagreement — push toward 0.5 (no signal)
            prob_up = 0.5
    else:
        # Majority: use average, but penalize disagreement
        votes_up = sum(1 for p in probs_up if p > 0.5)
        votes_down = sum(1 for p in probs_up if p < 0.5)
        majority_up = votes_up > len(probs_up) / 2
        majority_down = votes_down > len(probs_up) / 2

        if majority_up or majority_down:
            prob_up = avg_prob_up
        else:
            prob_up = 0.5  # tie — no signal

    meta["agreement"] = round(
        sum(1 for p in probs_up if (p > 0.5) == (avg_prob_up > 0.5)) / len(probs_up),
        2,
    )

    prob_down = 1.0 - prob_up

    logger.info(
        f"🎯 MULTI-HORIZON: horizons={meta['per_horizon']} "
        f"avg={avg_prob_up:.3f} agreement={meta['agreement']:.0%} → UP={prob_up:.3f}"
    )

    return prob_up, prob_down, meta


# =============================================================================
# Strategy 2: CONFIRMATION BARS (wait for streak)
# =============================================================================
# Use a single k=1 model. But don't act on the first signal.
# Track consecutive bars where the model says UP (or DOWN).
# Only enter when the streak hits N bars (default 2).
#
# Why this works: noise is random — it won't produce consistent
# directional signals across multiple bars. A real move will.
# You sacrifice 1-2 bars of entry timing for much fewer false entries.
# =============================================================================

_CONFIRMATION_STATE: Dict[int, Dict[str, Any]] = {}
# bot_id -> {direction: "UP"|"DOWN"|None, streak: int, last_prob: float}


def _confirmation_predict(
    raw_prob_up: float,
    raw_prob_down: float,
    bot_id: int,
    required_streak: int = 2,
    entry_threshold: float = 0.55,  # minimum prob to count toward a streak
) -> Tuple[float, float, Dict[str, Any]]:
    """
    Filter raw model output through a streak counter.

    The raw model says prob_up=0.72 on this bar. But is it real?
    Only pass it through if the model has been saying UP for
    `required_streak` consecutive bars.

    Returns:
        prob_up, prob_down: filtered (0.5/0.5 if streak not met)
        meta: {streak, direction, raw_prob_up, passed}
    """
    if bot_id not in _CONFIRMATION_STATE:
        _CONFIRMATION_STATE[bot_id] = {
            "direction": None,
            "streak": 0,
            "last_prob_up": 0.5,
        }

    state = _CONFIRMATION_STATE[bot_id]
    meta = {
        "strategy": "confirmation",
        "required_streak": required_streak,
        "raw_prob_up": round(raw_prob_up, 4),
        "raw_prob_down": round(raw_prob_down, 4),
    }

    # Determine current bar's signal
    if raw_prob_up >= entry_threshold:
        current_dir = "UP"
    elif raw_prob_down >= entry_threshold:
        current_dir = "DOWN"
    else:
        current_dir = None  # no clear signal

    # Update streak
    if current_dir is None:
        # No signal — reset
        state["direction"] = None
        state["streak"] = 0
    elif current_dir == state["direction"]:
        # Same direction — extend streak
        state["streak"] += 1
    else:
        # Direction changed — start new streak
        state["direction"] = current_dir
        state["streak"] = 1

    state["last_prob_up"] = raw_prob_up

    meta["direction"] = state["direction"]
    meta["streak"] = state["streak"]

    # Only pass signal through if streak is met
    if state["streak"] >= required_streak:
        meta["passed"] = True
        logger.info(
            f"✅ CONFIRMATION: {state['direction']} streak={state['streak']} "
            f"(need {required_streak}) — SIGNAL CONFIRMED, prob_up={raw_prob_up:.3f}"
        )
        return raw_prob_up, raw_prob_down, meta
    else:
        meta["passed"] = False
        logger.info(
            f"⏳ CONFIRMATION: {state['direction'] or 'NONE'} streak={state['streak']} "
            f"(need {required_streak}) — waiting, raw_up={raw_prob_up:.3f}"
        )
        # Return neutral — don't enter yet
        return 0.5, 0.5, meta


# =============================================================================
# Strategy 3: ROLLING PROBABILITY (exponential moving average)
# =============================================================================
# Use a single k=1 model. Smooth its output with an EMA.
# The smoothed probability won't spike on a single noisy bar,
# but will ramp up over 2-3 bars of consistent signal.
#
# This is the gentlest filter — doesn't require hard streak logic,
# just naturally dampens noise while letting persistent signals through.
#
# You can also use this as an EXIT smoother — don't exit on one
# bar of probability drop, wait for the EMA to confirm.
# =============================================================================

_ROLLING_STATE: Dict[int, Dict[str, float]] = {}
# bot_id -> {ema_prob_up: float}


def _rolling_predict(
    raw_prob_up: float,
    raw_prob_down: float,
    bot_id: int,
    ema_alpha: float = 0.4,  # smoothing factor: 0.4 = ~3-bar half-life
) -> Tuple[float, float, Dict[str, Any]]:
    """
    Apply exponential moving average to raw model probabilities.

    Alpha controls smoothing:
      0.3 = heavy smoothing (~4 bars to respond), fewer false signals
      0.4 = moderate (~3 bars), good balance
      0.5 = light (~2 bars), faster but noisier
      1.0 = no smoothing (raw output, same as original)

    Returns:
        prob_up, prob_down: EMA-smoothed
        meta: {raw_prob_up, ema_prob_up, ema_alpha}
    """
    if bot_id not in _ROLLING_STATE:
        # Initialize EMA with first observation
        _ROLLING_STATE[bot_id] = {"ema_prob_up": raw_prob_up}

    state = _ROLLING_STATE[bot_id]

    # EMA update: new = alpha * raw + (1 - alpha) * old
    old_ema = state["ema_prob_up"]
    new_ema = ema_alpha * raw_prob_up + (1.0 - ema_alpha) * old_ema
    state["ema_prob_up"] = new_ema

    meta = {
        "strategy": "rolling",
        "raw_prob_up": round(raw_prob_up, 4),
        "ema_prob_up": round(new_ema, 4),
        "ema_alpha": ema_alpha,
        "ema_shift": round(new_ema - raw_prob_up, 4),
    }

    logger.info(
        f"📊 ROLLING: raw={raw_prob_up:.3f} → ema={new_ema:.3f} "
        f"(α={ema_alpha}, shift={new_ema - raw_prob_up:+.3f})"
    )

    return new_ema, 1.0 - new_ema, meta


# =============================================================================
# Unified Engine
# =============================================================================

# Strategy config defaults
STRATEGY_DEFAULTS = {
    "prediction_strategy": "single",       # single|multi_horizon|confirmation|rolling
    "mh_horizons": [1, 2, 3],             # multi-horizon: which k values
    "mh_consensus": "majority",           # multi-horizon: majority|unanimous
    "confirm_streak": 2,                  # confirmation: bars needed
    "confirm_threshold": 0.55,            # confirmation: min prob for streak
    "rolling_alpha": 0.4,                 # rolling: EMA smoothing factor
}


class PredictionEngine:
    """
    Wraps all 3 strategies behind a single predict() call.

    Usage in the tick function:
        engine = get_prediction_engine(bot_id, cfg)
        prob_up, prob_down, meta = engine.predict(...)
    """

    def __init__(
        self,
        strategy: str = "single",
        mh_horizons: List[int] = None,
        mh_consensus: str = "majority",
        confirm_streak: int = 2,
        confirm_threshold: float = 0.55,
        rolling_alpha: float = 0.4,
    ):
        self.strategy = strategy
        self.mh_horizons = mh_horizons or [1, 2, 3]
        self.mh_consensus = mh_consensus
        self.confirm_streak = confirm_streak
        self.confirm_threshold = confirm_threshold
        self.rolling_alpha = rolling_alpha

    def predict(
        self,
        raw_prob_up: float,
        raw_prob_down: float,
        feat_df_labeled: Optional[pd.DataFrame],
        feat_df_current: Optional[pd.DataFrame],
        df_for_labels: Optional[pd.DataFrame],
        symbol: str,
        interval: str,
        bot_id: int,
        retrain: bool = True,
    ) -> Tuple[float, float, Dict[str, Any]]:
        """
        Run the configured strategy.

        For "single": returns raw probs unchanged.
        For "multi_horizon": trains 3 models, returns consensus.
        For "confirmation": filters raw probs through streak logic.
        For "rolling": smooths raw probs with EMA.

        Args:
            raw_prob_up/down: output from the k=1 base model
            feat_df_labeled: labeled features for training (k=1)
            feat_df_current: current bar features for prediction
            df_for_labels: raw OHLCV df for building multi-horizon labels
            symbol, interval, bot_id: identifiers
            retrain: whether to retrain models this tick

        Returns:
            prob_up, prob_down, meta_dict
        """
        if self.strategy == "multi_horizon":
            return _multi_horizon_predict(
                feat_df_labeled_base=feat_df_labeled,
                feat_df_current=feat_df_current,
                df_for_labels=df_for_labels,
                symbol=symbol,
                interval=interval,
                bot_id=bot_id,
                horizons=self.mh_horizons,
                consensus_mode=self.mh_consensus,
                retrain=retrain,
            )

        elif self.strategy == "confirmation":
            return _confirmation_predict(
                raw_prob_up=raw_prob_up,
                raw_prob_down=raw_prob_down,
                bot_id=bot_id,
                required_streak=self.confirm_streak,
                entry_threshold=self.confirm_threshold,
            )

        elif self.strategy == "rolling":
            return _rolling_predict(
                raw_prob_up=raw_prob_up,
                raw_prob_down=raw_prob_down,
                bot_id=bot_id,
                ema_alpha=self.rolling_alpha,
            )

        else:
            # "single" — pass through unchanged
            return raw_prob_up, raw_prob_down, {"strategy": "single"}


def get_prediction_engine_from_config(cfg) -> PredictionEngine:
    """
    Build a PredictionEngine from a BotConfig / ReplayConfig.

    Reads strategy params from config fields. If not set, uses
    STRATEGY_DEFAULTS (which means "single" — no change from current).
    """
    # Read from cfg attributes (set via config_json)
    strategy = getattr(cfg, "prediction_strategy", STRATEGY_DEFAULTS["prediction_strategy"])
    mh_horizons = getattr(cfg, "mh_horizons", STRATEGY_DEFAULTS["mh_horizons"])
    mh_consensus = getattr(cfg, "mh_consensus", STRATEGY_DEFAULTS["mh_consensus"])
    confirm_streak = getattr(cfg, "confirm_streak", STRATEGY_DEFAULTS["confirm_streak"])
    confirm_threshold = getattr(cfg, "confirm_threshold", STRATEGY_DEFAULTS["confirm_threshold"])
    rolling_alpha = getattr(cfg, "rolling_alpha", STRATEGY_DEFAULTS["rolling_alpha"])

    return PredictionEngine(
        strategy=strategy,
        mh_horizons=mh_horizons,
        mh_consensus=mh_consensus,
        confirm_streak=confirm_streak,
        confirm_threshold=confirm_threshold,
        rolling_alpha=rolling_alpha,
    )


def clear_strategy_state(bot_id: Optional[int] = None):
    """Clear all strategy state for a bot (or all bots)."""
    if bot_id is not None:
        _MULTI_HORIZON_CACHE.pop(bot_id, None)
        _CONFIRMATION_STATE.pop(bot_id, None)
        _ROLLING_STATE.pop(bot_id, None)
    else:
        _MULTI_HORIZON_CACHE.clear()
        _CONFIRMATION_STATE.clear()
        _ROLLING_STATE.clear()
