# app/utils/options_data.py
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Tuple
import requests
import pytz

SCHWAB_API_URL = "https://api.schwabapi.com/marketdata/v1"
logger = logging.getLogger(__name__)

# ---- auth header (matches your reference) ----
def _schwab_headers() -> Dict[str, str]:
    from app.utils.stock.schwab_token import get_valid_access_token
    token = get_valid_access_token()
    if not token:
        raise ValueError("Schwab access token missing/unavailable.")
    return {"Authorization": f"Bearer {token}"}

# ---- helpers ----
def _mid(bid: float, ask: float) -> float:
    try:
        if bid is None or ask is None: return 0.0
        if bid <= 0 and ask <= 0: return 0.0
        return max(0.01, (bid + ask) / 2.0)
    except Exception:
        return 0.0

def _flatten_map(exp_map: Dict, side: str) -> List[Dict[str, Any]]:
    """
    Schwab chains returns nested maps callExpDateMap/putExpDateMap.
    We flatten to list of contracts with a uniform schema.
    """
    out: List[Dict[str, Any]] = []
    for exp_key, strikes in exp_map.items():   # exp_key like "2025-08-22:7"
        exp_date = exp_key.split(":")[0]
        for strike_str, contracts in strikes.items():
            for c in contracts:
                out.append({
                    "occ": c.get("symbol"),
                    "putCall": side.upper(),  # CALL/PUT
                    "strike": float(c.get("strikePrice")),
                    "bid": float(c.get("bid", 0) or 0),
                    "ask": float(c.get("ask", 0) or 0),
                    "mid": _mid(float(c.get("bid", 0) or 0), float(c.get("ask", 0) or 0)),
                    "iv": float(c.get("volatility", 0) or c.get("impliedVolatility", 0) or 0),
                    "delta": float(c.get("delta", 0) or 0),
                    "gamma": float(c.get("gamma", 0) or 0),
                    "theta": float(c.get("theta", 0) or 0),
                    "vega": float(c.get("vega", 0) or 0),
                    "open_interest": int(c.get("openInterest", 0) or 0),
                    "volume": int(c.get("totalVolume", 0) or 0),
                    "expiration": exp_date,
                })
    return out

def _get_expirations(symbol: str) -> List[str]:
    url = f"{SCHWAB_API_URL}/expirationchain"
    params = {"symbol": symbol}
    r = requests.get(url, headers=_schwab_headers(), params=params, timeout=10)
    logger.info(f"[Schwab] expirationchain {symbol} -> {r.status_code}")
    if r.status_code != 200:
        return []
    data = r.json() or {}
    return [e["expirationDate"] for e in data.get("expirationList", [])]

def _filter_expirations(expirations: List[str], dte_min: int, dte_max: int) -> List[str]:
    et = pytz.timezone("US/Eastern")
    today = datetime.now(et).date()
    keep = []
    for s in expirations:
        try:
            d = datetime.strptime(s, "%Y-%m-%d").date()
            dte = (d - today).days
            if dte_min <= dte <= dte_max:
                keep.append(s)
        except Exception:
            continue
    return keep

def _fetch_chain_day(symbol: str, exp_date: str) -> Tuple[List[Dict], List[Dict]]:
    url = f"{SCHWAB_API_URL}/chains"
    params = {
        "symbol": symbol,
        "fromDate": exp_date,
        "toDate": exp_date,
        "includeUnderlyingQuote": "true",
        "contractType": "ALL",
    }
    r = requests.get(url, headers=_schwab_headers(), params=params, timeout=12)
    logger.info(f"[Schwab] chains {symbol} {exp_date} -> {r.status_code}")
    if r.status_code != 200:
        return [], []
    data = r.json() or {}
    calls = _flatten_map(data.get("callExpDateMap", {}) or {}, "CALL")
    puts  = _flatten_map(data.get("putExpDateMap", {}) or {}, "PUT")
    return calls, puts

# ---- public API for algos/runner ----

# In app/utils/options/options_data.py

def fetch_option_chain(symbol: str,
                       dte_min: int = 3,
                       dte_max: int = 21,
                       min_oi: int = 100,
                       min_vol: int = 50,
                       limit_expiries: int = 6,
                       from_date: str = None,
                       to_date: str = None) -> Dict[datetime, Dict[str, Any]]:
    """
    Returns a normalized option chain, accepting either DTE ranges or specific date ranges.
    """
    et = pytz.timezone("US/Eastern")
    raw_exps = _get_expirations(symbol)
    
    # --- THIS IS THE FIX ---
    # It now correctly filters based on which arguments are provided.
    if from_date and to_date:
        # If specific dates are given, filter by them
        from_d = datetime.strptime(from_date, "%Y-%m-%d").date()
        to_d = datetime.strptime(to_date, "%Y-%m-%d").date()
        filtered = [exp for exp in raw_exps if from_d <= datetime.strptime(exp, "%Y-%m-%d").date() <= to_d]
    else:
        # Otherwise, fall back to the DTE range
        filtered = _filter_expirations(raw_exps, dte_min, dte_max)

    exps = filtered[:limit_expiries]
    out: Dict[datetime, Dict[str, Any]] = {}

    for exp in exps:
        calls, puts = _fetch_chain_day(symbol, exp)
        logger.info(f"[Schwab] {symbol} {exp}: calls={len(calls)}, puts={len(puts)}")
        calls = [c for c in calls if c["ask"] > 0 and c["bid"] >= 0]
        puts  = [p for p in puts  if p["ask"] > 0 and p["bid"] >= 0]
        key = et.localize(datetime.strptime(exp, "%Y-%m-%d"))
        out[key] = {
            "min_oi": min_oi,
            "min_vol": min_vol,
            "calls": calls,
            "puts": puts
        }
    return out