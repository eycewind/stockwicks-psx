import sys
import os
import json
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
import pandas as pd
from app.utils.stock.market_price import get_price_history

def get_sma_5d(symbol: str) -> float | None:
    """
    Fetches 1d interval historical prices and calculates 5-day SMA.
    """
    df = get_price_history(symbol, days=7, interval="1d")
    if df is not None and "close" in df.columns and len(df) >= 5:
        return df["close"].tail(5).mean()
    return None
