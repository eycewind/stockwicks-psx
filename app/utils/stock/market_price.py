#/var/www/stockwicks/app/utils/market_price.py
import os
import time
from datetime import datetime

import pandas as pd
import requests

from app.utils.stock.schwab_token import get_valid_access_token

SCHWAB_API_URL = "https://api.schwabapi.com/marketdata/v1/quotes"
_PRICE_CACHE: dict[str, tuple[float, float]] = {}


def _price_cache_ttl() -> int:
    return max(60, int(os.getenv("SCHWAB_QUOTE_CACHE_SECONDS", "60")))


def get_live_price(symbol: str):
    """
    Fetch live price for a symbol, with a short in-process cache to collapse
    duplicate bot/risk checks for the same symbol.
    """
    symbol_key = symbol.upper().strip()
    now = time.time()
    cached = _PRICE_CACHE.get(symbol_key)
    if cached and now - cached[0] <= _price_cache_ttl():
        return cached[1]

    access_token = get_valid_access_token()
    if not access_token:
        print("[ERROR] Schwab token missing or expired. Please re-authenticate using /auth/schwab/start")
        return None

    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"symbols": symbol_key}

    try:
        resp = requests.get(SCHWAB_API_URL, headers=headers, params=params, timeout=10)

        if resp.status_code == 401:
            print("[ERROR] Schwab token expired. Please refresh via /auth/schwab/start")
            return None

        resp.raise_for_status()
        data = resp.json()

        if symbol_key in data and "quote" in data[symbol_key]:
            last_price = data[symbol_key]["quote"].get("lastPrice")
            if last_price is not None:
                last_price = float(last_price)
                _PRICE_CACHE[symbol_key] = (now, last_price)
            print(f"[INFO] Schwab live price for {symbol_key} = {last_price}")
            return last_price

        print(f"[ERROR] Unexpected Schwab API response: {data}")
        return None

    except Exception as e:
        print(f"[ERROR] Failed to fetch Schwab price for {symbol}: {e}")
        return None


def get_price_history(symbol: str, days: int = 7, interval: str = "1d") -> pd.DataFrame | None:
    """
    FAKE historical price fetch for testing SMA logic.
    Replace with Schwab API call if/when supported.
    """
    try:
        dates = pd.date_range(end=datetime.today(), periods=days)
        prices = [180 + (i % 5) for i in range(days)]
        return pd.DataFrame({"date": dates, "close": prices})
    except Exception as e:
        print(f"[ERROR] get_price_history failed: {e}")
        return None
