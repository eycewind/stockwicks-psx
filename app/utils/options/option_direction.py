# app/utils/option_direction.py
from __future__ import annotations
import pandas as pd
import numpy as np
from typing import Literal, Optional
from datetime import datetime, timedelta

from app.utils.common.algo_common import load_interval_csv  # already in your project

Bias = Literal["bullish", "bearish", "neutral"]

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def _rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/length, min_periods=length, adjust=False).mean()
    rs = avg_gain / (avg_loss.replace(0, np.nan))
    return 100 - (100 / (1 + rs))

def get_market_bias(
    user_id: int,
    symbol: str,
    interval: str = "5min",
    lookback_bars: int = 120,
) -> Bias:
    """
    Returns 'bullish' | 'bearish' | 'neutral' based on EMAs + RSI.
    Uses your existing CSV loader so it works offline and in prod.
    """
    df = load_interval_csv(user_id, symbol, interval)
    if df is None or df.empty or "close" not in df.columns:
        return "neutral"

    df = df.sort_index().tail(lookback_bars).copy()
    close = df["close"].astype(float)

    ema_fast = _ema(close, 8)
    ema_slow = _ema(close, 21)
    rsi = _rsi(close, 14)

    # Current readings
    fast, slow = ema_fast.iloc[-1], ema_slow.iloc[-1]
    rsi_now = rsi.iloc[-1]

    # Also check a small slope to avoid flat cross noise
    slope_fast = ema_fast.diff().rolling(3).mean().iloc[-1]
    slope_slow = ema_slow.diff().rolling(3).mean().iloc[-1]

    bullish = fast > slow and slope_fast > 0 and slope_slow >= 0
    bearish = fast < slow and slope_fast < 0 and slope_slow <= 0

    # RSI nudges: overbought doesn't auto-bear, oversold doesn't auto-bull.
    # Only use as a tiebreaker when EMAs are flat or overlapping.
    ema_close = abs(fast - slow) < (0.001 * close.iloc[-1])  # within 0.1%
    if ema_close:
        if rsi_now > 60 and slope_fast >= 0:
            bullish = True
            bearish = False
        elif rsi_now < 40 and slope_fast <= 0:
            bearish = True
            bullish = False

    if bullish and not bearish:
        return "bullish"
    if bearish and not bullish:
        return "bearish"
    return "neutral"
