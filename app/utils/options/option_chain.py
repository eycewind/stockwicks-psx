# app/utils/option_chain.py
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Literal

import requests
import pandas as pd

from app.utils.stock.schwab_token import get_valid_access_token
from app.utils.stock.market_price import get_live_price

# ---- Public datatypes --------------------------------------------------------

@dataclass
class ChainRow:
    strike: float
    delta: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    mid: Optional[float]
    iv: Optional[float]
    volume: Optional[int]
    oi: Optional[int]

NormalizedChain = Dict[str, Dict[str, List[ChainRow]]]  # {"YYYY-MM-DD": {"calls":[...], "puts":[...]}}

# ---- Internal helpers --------------------------------------------------------

def _mid(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if bid is None or ask is None:
        return None
    if bid <= 0 and ask <= 0:
        return None
    try:
        return round((float(bid) + float(ask)) / 2.0, 2)
    except Exception:
        return None

def _to_float(v) -> Optional[float]:
    try:
        f = float(v)
        if pd.isna(f):
            return None
        return f
    except Exception:
        return None

def _to_int(v) -> Optional[int]:
    try:
        i = int(v)
        return i
    except Exception:
        return None

def _tznow_et() -> datetime:
    # Simple ET clock without pytz dependency here
    # Server typically runs UTC, so ET = UTC-4 or UTC-5; we keep it UTC and use minutes-left heuristic by session clock elsewhere.
    return datetime.now(timezone.utc)

def _minutes_left_today(now_utc: datetime) -> int:
    # Market regular session 13:30–20:00 UTC (09:30–16:00 ET)
    open_utc  = now_utc.replace(hour=13, minute=30, second=0, microsecond=0)
    close_utc = now_utc.replace(hour=20, minute=0,  second=0, microsecond=0)
    if now_utc < open_utc:
        return int((close_utc - open_utc).total_seconds() // 60)
    if now_utc > close_utc:
        return 0
    return int((close_utc - now_utc).total_seconds() // 60)

# ---- Expiry selection --------------------------------------------------------

def pick_expiry(symbol: str, prefer_0dte: bool = True, min_minutes_left: int = 90) -> str:
    """
    Choose an expiry (ISO date) purely by date rules.
    - If prefer_0dte and >= min_minutes_left in today’s session -> today.
    - Else -> the next trading day (not weekend).
    NOTE: We do not inspect chain here; we keep it deterministic and cheap.
    """
    now = _tznow_et()
    mins_left = _minutes_left_today(now)
    today = now.date()

    if prefer_0dte and mins_left >= min_minutes_left:
        return today.isoformat()

    # next trading day (skip Sat/Sun)
    d = today + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()

# ---- Schwab fetch + normalization -------------------------------------------

def _schwab_headers() -> Optional[Dict[str, str]]:
    token = get_valid_access_token()
    if not token:
        logging.error("[CHAIN] Missing Schwab access token.")
        return None
    return {"Authorization": f"Bearer {token}"}

def fetch_chain_raw(symbol: str, expiry: str) -> Optional[dict]:
    """
    Calls Schwab Option Chains endpoint.
    We request a single expiration to keep payload small.
    """
    headers = _schwab_headers()
    if headers is None:
        return None

    url = "https://api.schwabapi.com/marketdata/v1/optionchains"
    params = {
        "symbol": symbol.upper(),
        "includeQuotes": "TRUE",
        "strategy": "SINGLE",           # we want the whole chain as single legs
        "contractType": "ALL",          # CALL/PUT
        "fromDate": expiry,
        "toDate": expiry,
        "range": "ALL",                 # we’ll filter by delta later
    }
    try:
        r = requests.get(url, headers=headers, params=params, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.exception(f"[CHAIN] Schwab chain fetch failed for {symbol} {expiry}: {e}")
        return None

def _norm_side(rows: List[dict]) -> List[ChainRow]:
    out: List[ChainRow] = []
    for c in rows or []:
        # Schwab JSON fields (typical):
        # "strikePrice", "delta", "bid", "ask", "volatility", "totalVolume", "openInterest"
        strike = _to_float(c.get("strikePrice"))
        if strike is None:
            continue
        bid = _to_float((c.get("bid")) or (c.get("quote", {}).get("bidPrice")))
        ask = _to_float((c.get("ask")) or (c.get("quote", {}).get("askPrice")))
        row = ChainRow(
            strike=float(strike),
            delta=_to_float(c.get("delta")),
            bid=bid,
            ask=ask,
            mid=_mid(bid, ask),
            iv=_to_float(c.get("volatility")),
            volume=_to_int(c.get("totalVolume")),
            oi=_to_int(c.get("openInterest")),
        )
        out.append(row)
    # Keep rows with at least a mid price; liquidity filters can be applied by the caller
    return [r for r in out if r.mid is not None]

def normalize_chain(symbol: str, expiry: str, raw: dict) -> Optional[dict]:
    """
    Produces the shape expected by option_selector.pick_option_selection(...).
    """
    if not raw:
        return None

    # Schwab typically nests by "callExpDateMap" and "putExpDateMap"
    calls_map: dict = raw.get("callExpDateMap") or {}
    puts_map:  dict = raw.get("putExpDateMap") or {}

    # Keys in *ExpDateMap are like "2025-09-03:0" (date:daysToExp)
    def _collect_side(exp_map: dict, target_date: str) -> List[ChainRow]:
        rows: List[dict] = []
        for key, strikes in exp_map.items():
            if not key.startswith(target_date):
                continue
            # strikes is a dict of strike -> [contracts]
            for _, contracts in strikes.items():
                # contracts is a list; take first (there’s only one per strike per expiry)
                if contracts:
                    rows.append(contracts[0])
        return _norm_side(rows)

    calls = _collect_side(calls_map, expiry)
    puts  = _collect_side(puts_map,  expiry)

    chain = {
        "underlying": _to_float(raw.get("underlyingPrice")) or get_live_price(symbol) or None,
        "expiries": {
            expiry: {
                "calls": [r.__dict__ for r in calls],
                "puts":  [r.__dict__ for r in puts],
            }
        }
    }
    return chain

# ---- Convenience API for the bot/selector -----------------------------------

def fetch_normalized_chain(
    *,
    symbol: str,
    prefer_0dte: bool = True,
    min_minutes_left: int = 90,
    explicit_expiry: Optional[str] = None,
) -> Optional[dict]:
    """
    High level one-shot:
      1) choose expiry (or use explicit)
      2) fetch Schwab chain JSON
      3) normalize into selector-friendly structure
    """
    expiry = explicit_expiry or pick_expiry(symbol, prefer_0dte=prefer_0dte, min_minutes_left=min_minutes_left)
    raw = fetch_chain_raw(symbol, expiry)
    if raw is None:
        return None
    return normalize_chain(symbol, expiry, raw)

def filter_liquidity(
    chain: dict,
    expiry: str,
    *,
    min_volume: int = 50,
    max_spread_pct: float = 0.15
) -> dict:
    """
    Optional post-filter to keep only reasonably liquid strikes.
    Removes rows with volume < min_volume or (ask-bid)/mid > max_spread_pct.
    """
    out = {"underlying": chain.get("underlying"), "expiries": {expiry: {"calls": [], "puts": []}}}
    def keep(row: dict) -> bool:
        vol = row.get("volume") or 0
        bid = row.get("bid"); ask = row.get("ask"); mid = row.get("mid")
        if mid in (None, 0):
            return False
        spread = (ask - bid) / mid if (ask is not None and bid is not None and mid) else 999
        return (vol >= min_volume) and (spread <= max_spread_pct)

    for side in ("calls", "puts"):
        rows = chain["expiries"][expiry][side]
        out["expiries"][expiry][side] = [r for r in rows if keep(r)]
    return out
