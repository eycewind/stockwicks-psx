# /var/www/stockwicks/app/scripts/stocks/bots/algo5_logic.py
import numpy as np
import pandas as pd


def _ema_sma_seed(series: pd.Series, length: int) -> pd.Series:
    """Thinkorswim-style EMA seeded with the first available SMA."""
    s = series.astype(float).copy()
    ema = pd.Series(index=s.index, dtype="float64")
    if length <= 0:
        return ema

    alpha = 2.0 / (length + 1.0)
    sma = s.rolling(length).mean()
    first_valid = sma.first_valid_index()
    if first_valid is None:
        return ema

    ema.loc[first_valid] = sma.loc[first_valid]
    started = False
    prev = np.nan
    for idx in s.index:
        if idx == first_valid:
            started = True
            prev = float(ema.loc[idx])
            continue
        if not started:
            continue
        val = float(s.loc[idx])
        prev = (val - prev) * alpha + prev
        ema.loc[idx] = prev

    return ema


def compute_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    df = df.copy()
    if "close" not in df.columns or df["close"].isna().all():
        df["macd"] = np.nan
        df["macd_signal"] = np.nan
        df["macd_hist"] = np.nan
        return df

    close = df["close"].astype(float)
    macd_line = _ema_sma_seed(close, fast) - _ema_sma_seed(close, slow)
    macd_signal = _ema_sma_seed(macd_line, signal)

    df["macd"] = macd_line
    df["macd_signal"] = macd_signal
    df["macd_hist"] = macd_line - macd_signal
    return df


def determine_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Pure MACD crossover signals, no model probability layer."""
    df = compute_macd(df)
    df["macd_prev"] = df["macd"].shift(1)
    df["signal_prev"] = df["macd_signal"].shift(1)
    df["Buy_Signal"] = (df["macd"] > df["macd_signal"]) & (df["macd_prev"] <= df["signal_prev"])
    df["Sell_Signal"] = (df["macd"] < df["macd_signal"]) & (df["macd_prev"] >= df["signal_prev"])
    return df
