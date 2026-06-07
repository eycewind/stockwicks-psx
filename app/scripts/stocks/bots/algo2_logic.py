# /var/www/stockwicks/app/scripts/stocks/bots/algo2_logic.py
import pandas as pd
import numpy as np

def _ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()

def determine_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    EMA 9/14/21 regime with pullback filters:
      Long regime ON  when: EMA9 > EMA14 > EMA21 AND low  > EMA9 (and it just turned true)
      Long regime OFF when: EMA9 <= EMA14

      Short regime ON  when: EMA9 < EMA14 < EMA21 AND high < EMA9 (and it just turned true)
      Short regime OFF when: EMA9 >= EMA14

    Discrete trade triggers:
      Buy_Signal  == bar where long regime flips 0 -> 1
      Sell_Signal == bar where short regime flips 0 -> 1
    """
    df = df.copy()

    # --- EMAs ---
    df["ema9"]  = _ema(df["close"], 9)
    df["ema14"] = _ema(df["close"], 14)
    df["ema21"] = _ema(df["close"], 21)

    # --- Regime conditions (bar-wise) ---
    buy_cond   = (df["ema9"] > df["ema14"]) & (df["ema14"] > df["ema21"]) & (df["low"]  > df["ema9"])
    stop_buy   = (df["ema9"] <= df["ema14"])

    sell_cond  = (df["ema9"] < df["ema14"]) & (df["ema14"] < df["ema21"]) & (df["high"] < df["ema9"])
    stop_sell  = (df["ema9"] >= df["ema14"])

    # --- Persistent regime flags (stateful like ThinkScript CompoundValue) ---
    n = len(df)
    buy_flag  = np.zeros(n, dtype=int)
    sell_flag = np.zeros(n, dtype=int)

    for i in range(1, n):
        # Long regime state machine
        if (not buy_cond.iloc[i-1]) and buy_cond.iloc[i] and (not stop_buy.iloc[i]):
            buy_flag[i] = 1
        elif buy_flag[i-1] == 1 and stop_buy.iloc[i]:
            buy_flag[i] = 0
        else:
            buy_flag[i] = buy_flag[i-1]

        # Short regime state machine (symmetric)
        if (not sell_cond.iloc[i-1]) and sell_cond.iloc[i] and (not stop_sell.iloc[i]):
            sell_flag[i] = 1
        elif sell_flag[i-1] == 1 and stop_sell.iloc[i]:
            sell_flag[i] = 0
        else:
            sell_flag[i] = sell_flag[i-1]

    df["buy_flag"]  = buy_flag
    df["sell_flag"] = sell_flag

    # Discrete entry triggers = flag transitions
    df["Buy_Signal"]  = (df["buy_flag"].shift(1, fill_value=0)  == 0) & (df["buy_flag"]  == 1)
    df["Sell_Signal"] = (df["sell_flag"].shift(1, fill_value=0) == 0) & (df["sell_flag"] == 1)

    # Optional informational momentum flags (not used for exits here)
    df["Momentum_Down"] = (df["buy_flag"].shift(1, fill_value=0)  == 1) & (df["buy_flag"]  == 0)
    df["Momentum_Up"]   = (df["sell_flag"].shift(1, fill_value=0) == 1) & (df["sell_flag"] == 0)

    return df
