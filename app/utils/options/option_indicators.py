# app/utils/options/option_indicators.py

"""
Option-specific indicators and helper calculations.
Provides IV rank, SMI/RSI-based trend signals, and greeks utilities.
"""

import numpy as np
import pandas as pd
import logging


def calc_iv_rank(df: pd.DataFrame, lookback: int = 252) -> float:
    """
    Calculate Implied Volatility Rank (IV Rank) over given lookback period.
    df must have a column "iv" (implied volatility).
    Returns a value between 0 and 1.
    """
    if df.empty or "iv" not in df.columns:
        logging.warning("[OPTION INDICATORS] Missing IV data.")
        return 0.0

    iv_series = df["iv"].dropna().tail(lookback)
    if iv_series.empty:
        return 0.0

    current_iv = iv_series.iloc[-1]
    min_iv, max_iv = iv_series.min(), iv_series.max()
    if max_iv == min_iv:
        return 0.0

    iv_rank = (current_iv - min_iv) / (max_iv - min_iv)
    return float(round(iv_rank, 3))


def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index (RSI).
    """
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = -delta.where(delta < 0, 0).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def detect_trend(df: pd.DataFrame) -> str:
    """
    Detect trend using SMAs and RSI.
    Returns "bullish", "bearish", or "neutral".
    Expects df['close'].
    """
    if df.empty or "close" not in df.columns:
        return "neutral"

    df["SMA10"] = df["close"].rolling(10).mean()
    df["SMA30"] = df["close"].rolling(30).mean()
    df["RSI"] = calc_rsi(df["close"])

    sma10, sma30 = df["SMA10"].iloc[-1], df["SMA30"].iloc[-1]
    rsi = df["RSI"].iloc[-1]

    if np.isnan(sma10) or np.isnan(sma30) or np.isnan(rsi):
        return "neutral"

    if sma10 > sma30 and rsi > 55:
        return "bullish"
    elif sma10 < sma30 and rsi < 45:
        return "bearish"
    return "neutral"


def calc_greeks_summary(df: pd.DataFrame) -> dict:
    """
    Return summary stats for greeks in a chain DataFrame.
    """
    if df.empty:
        return {"delta_mean": 0, "gamma_mean": 0, "theta_mean": 0, "vega_mean": 0}

    return {
        "delta_mean": float(df["delta"].mean(skipna=True)) if "delta" in df else 0,
        "gamma_mean": float(df["gamma"].mean(skipna=True)) if "gamma" in df else 0,
        "theta_mean": float(df["theta"].mean(skipna=True)) if "theta" in df else 0,
        "vega_mean": float(df["vega"].mean(skipna=True)) if "vega" in df else 0,
    }
