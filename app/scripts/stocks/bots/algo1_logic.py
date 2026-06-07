# /var/www/stockwicks/app/scripts/stocks/bots/algo1_logic.py
import pandas as pd
import numpy as np

def compute_vwap_and_bands(df: pd.DataFrame, std_dev_mult: float = 1.5):
    df = df.copy()
    def calc_vwap_daily(day_df):
        if 'volume' not in day_df.columns or day_df['volume'].sum() == 0:
            # If no volume, VWAP cannot be calculated. Return NaNs.
            for col in ['VWAP', 'VWAP_UpperBand', 'VWAP_LowerBand']:
                day_df[col] = np.nan
            return day_df
            
        tp = (day_df['high'] + day_df['low'] + day_df['close']) / 3
        cum_tp_vol = (tp * day_df['volume']).cumsum()
        cum_vol = day_df['volume'].cumsum()
        vwap = cum_tp_vol / cum_vol
        std_sq = ((tp - vwap) ** 2) * day_df['volume']
        mean_sq_error = std_sq.cumsum() / cum_vol
        std_dev = np.sqrt(mean_sq_error)
        day_df['VWAP'] = vwap
        day_df['VWAP_UpperBand'] = vwap + std_dev * std_dev_mult
        day_df['VWAP_LowerBand'] = vwap - std_dev * std_dev_mult
        return day_df
    return df.groupby(df.index.normalize(), group_keys=False).apply(calc_vwap_daily)

def determine_signals(df: pd.DataFrame) -> pd.DataFrame:
    """IMPROVED VWAP Mean Reversion with a 200-period EMA trend filter."""
    df = df.copy()
    df = compute_vwap_and_bands(df)

    # Handle cases where VWAP could not be calculated (e.g., no volume data)
    if 'VWAP' not in df.columns or df['VWAP'].isnull().all():
        df['Buy_Signal'] = False
        df['Sell_Signal'] = False
        return df

    df["EMA200"] = df['close'].ewm(span=200, adjust=False).mean()
    
    price_crosses_up = (df["close"].shift(1) < df["VWAP_LowerBand"].shift(1)) & (df["close"] > df["VWAP_LowerBand"])
    price_crosses_down = (df["close"].shift(1) > df["VWAP_UpperBand"].shift(1)) & (df["close"] < df["VWAP_UpperBand"])
    
    is_uptrend = df["close"] > df["EMA200"]
    is_downtrend = df["close"] < df["EMA200"]

    df["Buy_Signal"] = price_crosses_up & is_uptrend
    df["Sell_Signal"] = price_crosses_down & is_downtrend
    
    long_exit_trigger = (df["close"].shift(1) <= df["VWAP"].shift(1)) & (df["close"] > df["VWAP"])
    short_exit_trigger = (df["close"].shift(1) >= df["VWAP"].shift(1)) & (df["close"] < df["VWAP"])
    
    df["Sell_Signal"] = df["Sell_Signal"] | (long_exit_trigger & ~df['Sell_Signal'])
    df["Buy_Signal"] = df["Buy_Signal"] | (short_exit_trigger & ~df['Buy_Signal'])
    
    df.loc[df['Buy_Signal'] & df['Sell_Signal'], 'Sell_Signal'] = False
    
    return df