# indicators.py
import pandas as pd
import numpy as np

def compute_rsi(df, period=14):
    """Compute Relative Strength Index (RSI) for given data."""
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    avg_gain = gain.rolling(window=period, min_periods=1).mean()
    avg_loss = loss.rolling(window=period, min_periods=1).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    df['RSI'] = rsi
    return df

def compute_moving_averages(df, periods):
    """Compute simple moving averages for given periods."""
    for period in periods:
        df[f'SMA_{period}'] = df['close'].rolling(window=period).mean()
    return df

def compute_emas(df, periods):
    """Compute exponential moving averages for given periods."""
    for period in periods:
        df[f'EMA_{period}'] = df['close'].ewm(span=period, adjust=False).mean()
    return df

def compute_macd(df, fast_period=12, slow_period=26, signal_period=9):
    """Compute Moving Average Convergence Divergence (MACD)."""
    df['EMA_Fast'] = df['close'].ewm(span=fast_period, adjust=False).mean()
    df['EMA_Slow'] = df['close'].ewm(span=slow_period, adjust=False).mean()
    df['MACD'] = df['EMA_Fast'] - df['EMA_Slow']
    df['MACD_Signal'] = df['MACD'].ewm(span=signal_period, adjust=False).mean()
    df['MACD_Histogram'] = df['MACD'] - df['MACD_Signal']
    return df

def compute_atr(df, period=14):
    """Compute Average True Range (ATR)."""
    df['High-Low'] = df['high'] - df['low']
    df['High-Close'] = np.abs(df['high'] - df['close'].shift())
    df['Low-Close'] = np.abs(df['low'] - df['close'].shift())
    df['TR'] = df[['High-Low', 'High-Close', 'Low-Close']].max(axis=1)
    df['ATR'] = df['TR'].rolling(window=period).mean()
    return df

def compute_obv(df):
    """Compute On-Balance Volume (OBV)."""
    df['OBV'] = np.where(df['close'] > df['close'].shift(), df['volume'], -df['volume']).cumsum()
    return df

def compute_atr_trailing_stops(df, multiplier=3, period=14):
    """Compute ATR Trailing Stops."""
    atr = compute_atr(df, period)['ATR']
    high = df['high']
    low = df['low']
    close = df['close']
    
    # Initialize trailing stops
    df['Trailing_Stop_Loss'] = 0.0
    
    for i in range(len(df)):
        if i == 0:
            df.loc[df.index[i], 'Trailing_Stop_Loss'] = close[i] - atr[i] * multiplier
        else:
            previous_stop = df.loc[df.index[i - 1], 'Trailing_Stop_Loss']
            if close[i - 1] > previous_stop:
                df.loc[df.index[i], 'Trailing_Stop_Loss'] = max(previous_stop, close[i] - atr[i] * multiplier)
            else:
                df.loc[df.index[i], 'Trailing_Stop_Loss'] = close[i] - atr[i] * multiplier
    return df
