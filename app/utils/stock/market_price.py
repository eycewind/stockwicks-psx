#/var/www/stockwicks/app/utils/market_price.py
import requests
from app.utils.stock.schwab_token import get_valid_access_token

SCHWAB_API_URL = "https://api.schwabapi.com/marketdata/v1/quotes"

def get_live_price(symbol: str):
    """
    Fetches live price for a symbol directly from Schwab API.
    It automatically handles token refresh via get_valid_access_token().
    """

    # ✅ 1. Get a valid access token (refresh if expired)
    access_token = get_valid_access_token()
    if not access_token:
        print("[ERROR] Schwab token missing or expired. Please re-authenticate using /auth/schwab/start")
        return None

    # ✅ 2. Prepare API request
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"symbols": symbol.upper()}

    try:
        # ✅ 3. Call Schwab API
        resp = requests.get(SCHWAB_API_URL, headers=headers, params=params)

        # If token invalid, return None (can later trigger re-auth)
        if resp.status_code == 401:
            print("[ERROR] Schwab token expired. Please refresh via /auth/schwab/start")
            return None

        resp.raise_for_status()
        data = resp.json()

        # ✅ 4. Extract last price safely
        if symbol.upper() in data and "quote" in data[symbol.upper()]:
            last_price = data[symbol.upper()]["quote"].get("lastPrice")
            print(f"[INFO] Schwab live price for {symbol.upper()} = {last_price}")
            return last_price

        print(f"[ERROR] Unexpected Schwab API response: {data}")
        return None

    except Exception as e:
        print(f"[ERROR] Failed to fetch Schwab price for {symbol}: {e}")
        return None

import pandas as pd
from datetime import datetime, timedelta

def get_price_history(symbol: str, days: int = 7, interval: str = "1d") -> pd.DataFrame | None:
    """
    FAKE historical price fetch for testing SMA logic.
    Replace with Schwab API call if/when supported.
    """
    try:
        # Generate dummy price data (e.g., closing prices for 5 days)
        dates = pd.date_range(end=datetime.today(), periods=days)
        prices = [180 + (i % 5) for i in range(days)]  # Fake data: 180, 181, ...
        df = pd.DataFrame({"date": dates, "close": prices})
        return df
    except Exception as e:
        print(f"[ERROR] get_price_history failed: {e}")
        return None
