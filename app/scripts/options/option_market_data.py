# app/utils/options/option_market_data.py

"""
Utility functions for fetching and normalizing option chain data
from Schwab API. Replaces scattered logic from options_picker.py.
"""

import logging
import requests
from datetime import datetime
import pandas as pd

from app.utils.stock.schwab_token import get_valid_access_token


SCHWAB_BASE = "https://api.schwabapi.com/marketdata/v1"


def get_expiration_chain(symbol: str) -> list[str]:
    """
    Fetch all expiration dates for a given underlying symbol.
    Returns list of YYYY-MM-DD strings (Fridays preferred).
    """
    access_token = get_valid_access_token()
    if not access_token:
        logging.error("[OPTION DATA] No valid Schwab token.")
        return []

    url = f"{SCHWAB_BASE}/expirationchain"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"symbol": symbol.upper()}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        expiration_list = resp.json().get("expirationList", [])
        expiries = [e["expirationDate"] for e in expiration_list if "expirationDate" in e]
        return expiries
    except Exception as e:
        logging.error(f"[OPTION DATA] Expiration chain fetch failed: {e}")
        return []


def get_option_chain(symbol: str, expiry: str) -> pd.DataFrame:
    """
    Fetch the option chain for a given symbol + expiry.
    Returns normalized pandas DataFrame.
    """
    access_token = get_valid_access_token()
    if not access_token:
        logging.error("[OPTION DATA] No valid Schwab token.")
        return pd.DataFrame()

    url = f"{SCHWAB_BASE}/chains"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {
        "symbol": symbol.upper(),
        "fromDate": expiry,
        "toDate": expiry,
        "includeUnderlyingQuote": "true",
        "contractType": "ALL",
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        chain = resp.json()
    except Exception as e:
        logging.error(f"[OPTION DATA] Chain fetch failed for {symbol}@{expiry}: {e}")
        return pd.DataFrame()

    options = []
    for side, exp_key in [("call", "callExpDateMap"), ("put", "putExpDateMap")]:
        exp_map = chain.get(exp_key, {})
        if not exp_map:
            continue
        for exp_date, strikes in exp_map.items():
            for strike, contracts in strikes.items():
                for c in contracts:
                    options.append({
                        "optionSymbol": c.get("symbol"),
                        "strike": c.get("strikePrice"),
                        "side": side,
                        "ask": c.get("askPrice"),
                        "bid": c.get("bidPrice"),
                        "volume": c.get("totalVolume"),
                        "openInterest": c.get("openInterest"),
                        "expiration": exp_date.split(":")[0],  # YYYY-MM-DD
                        "dte": c.get("daysToExpiration"),
                        "iv": c.get("volatility"),
                        "delta": c.get("delta"),
                        "gamma": c.get("gamma"),
                        "theta": c.get("theta"),
                        "vega": c.get("vega"),
                        "mark": c.get("mark"),
                    })

    df = pd.DataFrame(options)
    if df.empty:
        logging.warning(f"[OPTION DATA] No contracts for {symbol}@{expiry}")
        return df

    # Basic filtering
    df = df.dropna(subset=["optionSymbol", "strike", "side", "ask", "bid", "openInterest"])
    df = df[(df["strike"] > 0) & (df["ask"] > 0) & (df["bid"] >= 0)]
    df["mid"] = (df["ask"] + df["bid"]) / 2

    return df.reset_index(drop=True)


def get_nearest_expiry(symbol: str, weeks_ahead: int = 1) -> str | None:
    """
    Get the expiry date (Friday) N weeks ahead.
    """
    expiries = get_expiration_chain(symbol)
    if not expiries:
        return None
    fridays = [
        e for e in expiries
        if datetime.strptime(e, "%Y-%m-%d").weekday() == 4
    ]
    if len(fridays) < weeks_ahead:
        return None
    return fridays[weeks_ahead - 1]


def get_filtered_chain(symbol: str, expiry: str, min_oi: int = 50, min_vol: int = 20) -> pd.DataFrame:
    """
    Convenience wrapper: fetch chain and filter by OI/volume.
    """
    df = get_option_chain(symbol, expiry)
    if df.empty:
        return df
    return df[(df["openInterest"] >= min_oi) & (df["volume"] >= min_vol)]
