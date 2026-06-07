# /var/www/stockwicks/app/scripts/stocks/bots/algo3_logic.py
import pandas as pd
import numpy as np

def compute_smi(df: pd.DataFrame, period: int = 14, smooth_k: int = 3, smooth_d: int = 3) -> pd.Series:
    df = df.copy()
    highest_high = df['high'].rolling(window=period).max()
    lowest_low = df['low'].rolling(window=period).min()
    price_range = highest_high - lowest_low
    midpoint = (highest_high + lowest_low) / 2
    
    # Replace 0 in price_range to avoid division by zero
    smi_raw = 100 * (df['close'] - midpoint) / (price_range.replace(0, np.nan) / 2)
    
    smi_smoothed = smi_raw.ewm(span=smooth_k, adjust=False).mean()
    df['SMI'] = smi_smoothed.ewm(span=smooth_d, adjust=False).mean()
    return df['SMI']

def determine_signals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df['SMI'] = compute_smi(df)

    # Buy Signal (Go Long / Exit Short): SMI crosses ABOVE the -40 level
    df['Buy_Signal'] = (df['SMI'].shift(1) <= -40) & (df['SMI'] > -40)

    # Sell Signal (Go Short / Exit Long): SMI crosses BELOW the +40 level
    df['Sell_Signal'] = (df['SMI'].shift(1) >= 40) & (df['SMI'] < 40)

    return df