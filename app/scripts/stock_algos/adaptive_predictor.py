#!/usr/bin/env python3
"""
adaptive_predictor.py — Self-Scoring Adaptive Prediction Engine
================================================================

Replaces the ML model entirely. No historical training. No overfitting.

Architecture:
  1. SIGNAL LAYER: 5 MM-resistant indicators vote +1/-1/0 each bar
  2. BAYESIAN LAYER: Weight votes by each signal's real-time reliability
  3. SCORING LAYER: Track prediction accuracy over rolling window
  4. REGIME LAYER: Detect trending vs choppy (HMM-inspired)
  5. OUTPUT: direction (+1/-1/0), confidence (0-100%), details

Signals chosen because MMs RESPECT them (can't fake sustained volume):
  - OBV slope (1-min): real volume commitment, can't be spoofed sustainably
  - VWAP position: institutional execution benchmark
  - Price structure: higher highs/lower lows (pure price action)
  - Volume confirmation: real participation
  - ATR momentum: is the move accelerating or exhausting?

Usage:
  predictor = AdaptivePredictor()
  result = predictor.tick(df, current_index)
  # result.direction = +1/-1/0
  # result.confidence = 0-100
  # result.should_trade = True/False
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from collections import deque
import logging

logger = logging.getLogger("AdaptivePredictor")


# ============================================================================
# PREDICTION RESULT
# ============================================================================

@dataclass
class PredictionResult:
    direction: int          # +1 (up), -1 (down), 0 (no signal)
    confidence: float       # 0-100%
    should_trade: bool      # True if confidence > threshold
    regime: str             # "trending_up", "trending_down", "choppy", "unknown"

    # Signal details
    obv_vote: int = 0
    vwap_vote: int = 0
    structure_vote: int = 0
    volume_vote: int = 0
    momentum_vote: int = 0
    vote_sum: int = 0

    # Scoring
    prediction_accuracy: float = 0.0   # rolling hit rate 0-100%
    predictions_made: int = 0
    predictions_correct: int = 0

    # Bayesian weights (how reliable each signal has been today)
    obv_reliability: float = 0.5
    vwap_reliability: float = 0.5
    structure_reliability: float = 0.5
    volume_reliability: float = 0.5
    momentum_reliability: float = 0.5

    # Price info
    price: float = 0.0
    obv_slope: float = 0.0
    vwap_distance: float = 0.0


# ============================================================================
# SIGNAL GENERATORS
# ============================================================================

def _obv_signal(df: pd.DataFrame, idx: int, lookback: int = 10) -> Tuple[int, float]:
    """
    OBV slope direction — the signal MMs can't fake.
    Sustained volume commitment in one direction = real institutional flow.

    Returns (vote: +1/-1/0, slope: float)
    """
    if idx < lookback + 1:
        return 0, 0.0

    close = df["close"].values
    volume = df["volume"].values

    # Calculate OBV over lookback window
    obv = 0.0
    obv_values = []
    start = max(0, idx - lookback)
    for i in range(start, idx + 1):
        if i > start:
            if close[i] > close[i - 1]:
                obv += volume[i]
            elif close[i] < close[i - 1]:
                obv -= volume[i]
        obv_values.append(obv)

    if len(obv_values) < 3:
        return 0, 0.0

    # OBV slope: normalize by average volume
    avg_vol = np.mean(volume[start:idx + 1])
    if avg_vol <= 0:
        return 0, 0.0

    # Slope over last N bars
    obv_arr = np.array(obv_values)
    slope = (obv_arr[-1] - obv_arr[0]) / (len(obv_arr) * avg_vol + 1e-8)

    # Strong threshold: slope > 0.3 = clear direction
    if slope > 0.3:
        return 1, slope
    elif slope < -0.3:
        return -1, slope
    else:
        return 0, slope


def _vwap_signal(df: pd.DataFrame, idx: int) -> Tuple[int, float]:
    """
    VWAP position + direction — institutional reference level.
    Price above VWAP with VWAP rising = bullish.
    Price below VWAP with VWAP falling = bearish.
    Price crossing VWAP back and forth = choppy.

    Returns (vote: +1/-1/0, distance: float)
    """
    if idx < 20:
        return 0, 0.0

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    volume = df["volume"].values

    # Find today's start (same day bars)
    # Simple: look back until we find a gap > 2 hours
    day_start = idx
    if hasattr(df.index, '__getitem__'):
        try:
            for i in range(idx, max(0, idx - 400), -1):
                if i > 0:
                    time_diff = (df.index[i] - df.index[i - 1]).total_seconds()
                    if time_diff > 7200:  # > 2 hour gap = new day
                        day_start = i
                        break
        except Exception:
            day_start = max(0, idx - 390)  # fallback: ~390 1-min bars per day
    else:
        day_start = max(0, idx - 390)

    # Calculate VWAP from day start
    typical = (high[day_start:idx + 1] + low[day_start:idx + 1] + close[day_start:idx + 1]) / 3.0
    vol_slice = volume[day_start:idx + 1].astype(float)
    cum_pv = np.cumsum(typical * vol_slice)
    cum_v = np.cumsum(vol_slice)
    vwap = cum_pv[-1] / (cum_v[-1] + 1e-8) if len(cum_v) > 0 else close[idx]

    # Distance from VWAP as % of price
    price = close[idx]
    distance = (price - vwap) / (price + 1e-8) * 100  # percentage

    # VWAP slope (is VWAP itself trending?)
    if len(cum_v) > 5:
        vwap_recent = cum_pv[-1] / (cum_v[-1] + 1e-8)
        vwap_5ago = cum_pv[-6] / (cum_v[-6] + 1e-8) if cum_v[-6] > 0 else vwap_recent
        vwap_slope = (vwap_recent - vwap_5ago) / (price + 1e-8) * 100
    else:
        vwap_slope = 0.0

    # Vote: price position + VWAP direction must agree
    above = price > vwap
    rising = vwap_slope > 0.01

    if above and rising:
        return 1, distance
    elif not above and not rising:
        return -1, distance
    else:
        return 0, distance


def _structure_signal(df: pd.DataFrame, idx: int, lookback: int = 10) -> int:
    """
    Price structure: higher highs + higher lows = uptrend.
    Lower highs + lower lows = downtrend.
    Mixed = choppy.

    Uses swing points over last N bars.
    Returns vote: +1/-1/0
    """
    if idx < lookback + 2:
        return 0

    high = df["high"].values
    low = df["low"].values
    start = idx - lookback

    # Find local swing highs and lows (simple: compare to neighbors)
    swing_highs = []
    swing_lows = []
    for i in range(start + 1, idx):
        if high[i] >= high[i - 1] and high[i] >= high[i + 1]:
            swing_highs.append(high[i])
        if low[i] <= low[i - 1] and low[i] <= low[i + 1]:
            swing_lows.append(low[i])

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        # Not enough swings — use simple higher/lower check
        first_half_high = np.max(high[start:start + lookback // 2])
        second_half_high = np.max(high[start + lookback // 2:idx + 1])
        first_half_low = np.min(low[start:start + lookback // 2])
        second_half_low = np.min(low[start + lookback // 2:idx + 1])

        higher_highs = second_half_high > first_half_high
        higher_lows = second_half_low > first_half_low

        if higher_highs and higher_lows:
            return 1
        elif not higher_highs and not higher_lows:
            return -1
        return 0

    # Check if swing highs are rising and swing lows are rising
    hh = all(swing_highs[i] >= swing_highs[i - 1] for i in range(1, len(swing_highs)))
    hl = all(swing_lows[i] >= swing_lows[i - 1] for i in range(1, len(swing_lows)))
    lh = all(swing_highs[i] <= swing_highs[i - 1] for i in range(1, len(swing_highs)))
    ll = all(swing_lows[i] <= swing_lows[i - 1] for i in range(1, len(swing_lows)))

    if hh and hl:
        return 1   # uptrend
    elif lh and ll:
        return -1  # downtrend
    return 0       # mixed/choppy


def _volume_signal(df: pd.DataFrame, idx: int) -> int:
    """
    Volume confirmation — is volume supporting the move?
    High volume on up bars + low volume on down bars = bullish.
    High volume on down bars + low volume on up bars = bearish.

    Returns vote: +1/-1/0
    """
    if idx < 10:
        return 0

    close = df["close"].values
    volume = df["volume"].values

    # Look at last 10 bars
    up_volume = 0.0
    down_volume = 0.0
    for i in range(max(0, idx - 9), idx + 1):
        if i > 0:
            if close[i] > close[i - 1]:
                up_volume += volume[i]
            elif close[i] < close[i - 1]:
                down_volume += volume[i]

    total = up_volume + down_volume
    if total == 0:
        return 0

    up_pct = up_volume / total

    if up_pct > 0.60:
        return 1   # buyers dominating
    elif up_pct < 0.40:
        return -1  # sellers dominating
    return 0       # balanced


def _momentum_signal(df: pd.DataFrame, idx: int) -> int:
    """
    ATR-based momentum — is the move accelerating or exhausting?
    Uses rate of change of ATR-normalized price movement.

    Returns vote: +1/-1/0
    """
    if idx < 15:
        return 0

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values

    # ATR over last 14 bars
    tr_list = []
    for i in range(max(1, idx - 13), idx + 1):
        tr = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
        tr_list.append(tr)
    atr = np.mean(tr_list) if tr_list else 1.0

    if atr <= 0:
        return 0

    # Price change over last 5 bars, normalized by ATR
    price_change = close[idx] - close[idx - 5]
    norm_change = price_change / atr

    # Is momentum accelerating? Compare last 3 bars vs previous 3
    recent_move = abs(close[idx] - close[idx - 3])
    earlier_move = abs(close[idx - 3] - close[idx - 6]) if idx >= 6 else recent_move

    accelerating = recent_move > earlier_move * 1.1

    if norm_change > 0.5 and accelerating:
        return 1   # strong upward momentum, accelerating
    elif norm_change < -0.5 and accelerating:
        return -1  # strong downward momentum, accelerating
    return 0       # weak or decelerating


# ============================================================================
# ADAPTIVE PREDICTOR
# ============================================================================

class AdaptivePredictor:
    """
    Self-scoring adaptive prediction engine.

    Every bar:
      1. Check if last prediction was correct → update scores
      2. Collect votes from 5 signals
      3. Weight votes by each signal's recent reliability (Bayesian)
      4. Determine regime (trending/choppy)
      5. Output direction + confidence + should_trade

    The predictor gets BETTER during the session as it learns
    which signals are working on THIS stock TODAY.
    """

    def __init__(
        self,
        min_confidence: float = 70.0,     # minimum confidence to trade
        stop_confidence: float = 50.0,    # stop trading below this
        scoring_window: int = 15,         # rolling window for accuracy
        reliability_window: int = 20,     # rolling window for signal reliability
        min_predictions: int = 5,         # minimum predictions before trading
    ):
        self.min_confidence = min_confidence
        self.stop_confidence = stop_confidence
        self.scoring_window = scoring_window
        self.reliability_window = reliability_window
        self.min_predictions = min_predictions

        # Prediction history
        self.predictions: deque = deque(maxlen=200)  # (bar_idx, predicted_dir, actual_dir)

        # Per-signal reliability tracking
        self.signal_history: Dict[str, deque] = {
            "obv": deque(maxlen=reliability_window),
            "vwap": deque(maxlen=reliability_window),
            "structure": deque(maxlen=reliability_window),
            "volume": deque(maxlen=reliability_window),
            "momentum": deque(maxlen=reliability_window),
        }

        # State
        self.last_prediction_idx: int = -1
        self.last_prediction_dir: int = 0
        self.last_close: float = 0.0

        # Regime detection
        self.recent_directions: deque = deque(maxlen=20)

    def reset(self):
        """Reset for new trading session."""
        self.predictions.clear()
        for k in self.signal_history:
            self.signal_history[k].clear()
        self.recent_directions.clear()
        self.last_prediction_idx = -1
        self.last_prediction_dir = 0
        self.last_close = 0.0

    def _score_last_prediction(self, df: pd.DataFrame, current_idx: int):
        """Check if the last prediction was correct and update scores."""
        if self.last_prediction_idx < 0 or self.last_prediction_dir == 0:
            return

        current_close = float(df["close"].iloc[current_idx])
        actual_dir = 1 if current_close > self.last_close else (-1 if current_close < self.last_close else 0)

        if actual_dir == 0:
            return  # flat bar, skip scoring

        correct = (self.last_prediction_dir == actual_dir)
        self.predictions.append((self.last_prediction_idx, self.last_prediction_dir, actual_dir, correct))

    def _update_signal_reliability(self, signal_name: str, vote: int, actual_dir: int):
        """Update how reliable a signal has been recently."""
        if vote == 0 or actual_dir == 0:
            return
        correct = (vote == actual_dir)
        self.signal_history[signal_name].append(1.0 if correct else 0.0)

    def _get_reliability(self, signal_name: str) -> float:
        """Get rolling reliability for a signal (0.0 to 1.0)."""
        history = self.signal_history[signal_name]
        if len(history) < 3:
            return 0.5  # prior: assume 50% until we have data
        return sum(history) / len(history)

    def _get_prediction_accuracy(self) -> float:
        """Rolling prediction accuracy over last N predictions."""
        recent = list(self.predictions)[-self.scoring_window:]
        if len(recent) < 3:
            return 50.0
        correct = sum(1 for _, _, _, c in recent if c)
        return correct / len(recent) * 100

    def _detect_regime(self) -> str:
        """
        Detect market regime from recent signal consistency.
        If signals consistently agree → trending.
        If signals flip-flop → choppy.
        """
        if len(self.recent_directions) < 5:
            return "unknown"

        recent = list(self.recent_directions)[-10:]
        up_count = sum(1 for d in recent if d > 0)
        down_count = sum(1 for d in recent if d < 0)
        neutral_count = sum(1 for d in recent if d == 0)

        total = len(recent)
        if up_count / total > 0.7:
            return "trending_up"
        elif down_count / total > 0.7:
            return "trending_down"
        elif neutral_count / total > 0.5:
            return "choppy"
        else:
            return "choppy"

    def tick(self, df: pd.DataFrame, current_idx: int) -> PredictionResult:
        """
        Main prediction function. Call once per bar.

        Returns PredictionResult with direction, confidence, and details.
        """
        result = PredictionResult(
            direction=0,
            confidence=0.0,
            should_trade=False,
            regime="unknown",
            price=float(df["close"].iloc[current_idx]),
        )

        if current_idx < 15:
            return result

        # ── Step 1: Score last prediction ──
        self._score_last_prediction(df, current_idx)

        # Also update signal reliabilities against actual result
        if self.last_prediction_idx >= 0 and self.last_close > 0:
            actual_close = float(df["close"].iloc[current_idx])
            actual_dir = 1 if actual_close > self.last_close else (-1 if actual_close < self.last_close else 0)

            if actual_dir != 0:
                # We need to re-compute what each signal voted last bar
                # For efficiency, we'll update reliability on current signals vs current result
                # (1 bar lag, close enough)
                pass

        # ── Step 2: Collect signal votes ──
        obv_vote, obv_slope = _obv_signal(df, current_idx, lookback=10)
        vwap_vote, vwap_dist = _vwap_signal(df, current_idx)
        structure_vote = _structure_signal(df, current_idx, lookback=10)
        volume_vote = _volume_signal(df, current_idx)
        momentum_vote = _momentum_signal(df, current_idx)

        result.obv_vote = obv_vote
        result.vwap_vote = vwap_vote
        result.structure_vote = structure_vote
        result.volume_vote = volume_vote
        result.momentum_vote = momentum_vote
        result.obv_slope = obv_slope
        result.vwap_distance = vwap_dist

        # ── Step 3: Update signal reliability (delayed by 1 bar) ──
        if len(self.predictions) > 0:
            last_pred = self.predictions[-1]
            actual_dir = last_pred[2]  # actual direction from last prediction
            # Update each signal's reliability based on whether it agreed with actual
            # We use current votes as proxy (1-bar lag)
            self._update_signal_reliability("obv", obv_vote, actual_dir)
            self._update_signal_reliability("vwap", vwap_vote, actual_dir)
            self._update_signal_reliability("structure", structure_vote, actual_dir)
            self._update_signal_reliability("volume", volume_vote, actual_dir)
            self._update_signal_reliability("momentum", momentum_vote, actual_dir)

        # ── Step 4: Bayesian-weighted vote ──
        obv_rel = self._get_reliability("obv")
        vwap_rel = self._get_reliability("vwap")
        struct_rel = self._get_reliability("structure")
        vol_rel = self._get_reliability("volume")
        mom_rel = self._get_reliability("momentum")

        result.obv_reliability = round(obv_rel, 3)
        result.vwap_reliability = round(vwap_rel, 3)
        result.structure_reliability = round(struct_rel, 3)
        result.volume_reliability = round(vol_rel, 3)
        result.momentum_reliability = round(mom_rel, 3)

        # Weighted score: each vote × reliability
        weighted_sum = (
            obv_vote * obv_rel +
            vwap_vote * vwap_rel +
            structure_vote * struct_rel +
            volume_vote * vol_rel +
            momentum_vote * mom_rel
        )

        total_weight = obv_rel + vwap_rel + struct_rel + vol_rel + mom_rel
        if total_weight > 0:
            normalized_score = weighted_sum / total_weight  # -1 to +1
        else:
            normalized_score = 0.0

        # Raw vote sum (unweighted)
        result.vote_sum = obv_vote + vwap_vote + structure_vote + volume_vote + momentum_vote

        # Direction from weighted score
        if normalized_score > 0.15:
            result.direction = 1
        elif normalized_score < -0.15:
            result.direction = -1
        else:
            result.direction = 0

        self.recent_directions.append(result.direction)

        # ── Step 5: Confidence calculation ──
        # Confidence = f(vote agreement, prediction accuracy, signal reliability)

        # Component 1: Vote agreement (0-40 points)
        abs_votes = abs(result.vote_sum)
        vote_confidence = min(40, abs_votes * 10)  # 4+ votes = 40 pts

        # Component 2: Prediction accuracy (0-40 points)
        accuracy = self._get_prediction_accuracy()
        result.prediction_accuracy = round(accuracy, 1)
        accuracy_confidence = max(0, (accuracy - 50) * 0.8)  # 50%=0, 100%=40

        # Component 3: Average signal reliability (0-20 points)
        avg_reliability = np.mean([obv_rel, vwap_rel, struct_rel, vol_rel, mom_rel])
        reliability_confidence = avg_reliability * 20  # 1.0 = 20 pts

        result.confidence = round(min(100, vote_confidence + accuracy_confidence + reliability_confidence), 1)

        # Count total predictions made
        result.predictions_made = len(self.predictions)
        result.predictions_correct = sum(1 for _, _, _, c in self.predictions if c)

        # ── Step 6: Regime detection ──
        result.regime = self._detect_regime()

        # ── Step 7: Should trade? ──
        result.should_trade = (
            result.direction != 0
            and result.confidence >= self.min_confidence
            and len(self.predictions) >= self.min_predictions
            and result.regime != "choppy"
        )

        # ── Step 8: Store prediction for next bar's scoring ──
        self.last_prediction_idx = current_idx
        self.last_prediction_dir = result.direction
        self.last_close = float(df["close"].iloc[current_idx])

        return result

    def get_state_summary(self) -> Dict:
        """Get current predictor state for logging."""
        total = len(self.predictions)
        correct = sum(1 for _, _, _, c in self.predictions if c)
        return {
            "total_predictions": total,
            "correct": correct,
            "accuracy": round(correct / total * 100, 1) if total > 0 else 0,
            "reliabilities": {
                k: round(self._get_reliability(k), 3)
                for k in self.signal_history
            },
            "regime": self._detect_regime(),
        }
