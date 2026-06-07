# /var/www/stockwicks/app/scripts/stocks/bots/algo2_logic.py
import pandas as pd
import numpy as np

def compute_atr_trailing_stop(df: pd.DataFrame, period: int = 14, multiplier: float = 3.0) -> pd.Series:
    df = df.copy()
    df['h-l'] = df['high'] - df['low']
    df['h-pc'] = abs(df['high'] - df['close'].shift(1))
    df['l-pc'] = abs(df['low'] - df['close'].shift(1))
    df['tr'] = df[['h-l', 'h-pc', 'l-pc']].max(axis=1)
    df['atr'] = df['tr'].ewm(span=period, adjust=False).mean()
    
    atr_stop = [0.0] * len(df)
    for i in range(1, len(df)):
        close, prev_close = df['close'].iloc[i], df['close'].iloc[i-1]
        atr = df['atr'].iloc[i]
        prev_atr_stop = atr_stop[i-1]

        if close > prev_atr_stop and prev_close > prev_atr_stop:
            atr_stop[i] = max(prev_atr_stop, close - multiplier * atr)
        elif close < prev_atr_stop and prev_close < prev_atr_stop:
            atr_stop[i] = min(prev_atr_stop, close + multiplier * atr)
        elif close > prev_atr_stop:
            atr_stop[i] = close - multiplier * atr
        else:
            atr_stop[i] = close + multiplier * atr
            
    return pd.Series(atr_stop, index=df.index)

def determine_signals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df['atr_stop'] = compute_atr_trailing_stop(df)
    
    # Buy Signal (Go Long / Exit Short): Close crosses ABOVE the ATR Stop
    df['Buy_Signal'] = (df['close'].shift(1) <= df['atr_stop'].shift(1)) & (df['close'] > df['atr_stop'])
    
    # Sell Signal (Go Short / Exit Long): Close crosses BELOW the ATR Stop
    df['Sell_Signal'] = (df['close'].shift(1) >= df['atr_stop'].shift(1)) & (df['close'] < df['atr_stop'])
    
    return df