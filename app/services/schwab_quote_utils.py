# /var/www/stockwicks/app/services/schwab_quote_utils.py
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _as_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        if x != x:  # NaN
            return None
        return x
    except Exception:
        return None


def extract_schwab_price(quote_resp: Any, symbol: str) -> Optional[float]:
    """
    Robustly extract a usable live price from Schwab quote response.

    Supports both response shapes:
      A) {"LUNR": {...}}  (dict keyed by symbol)
      B) {...}            (already the symbol payload)

    Price selection order:
      1) payload["quote"]["mark"]
      2) payload["quote"]["lastPrice"]
      3) payload["regular"]["regularMarketLastPrice"]
      4) payload["quote"]["bidPrice"]/["askPrice"] midpoint (if >0)
      5) payload["extended"]["lastPrice"] (only if >0)
    Never uses closePrice as "live price".
    """
    if not isinstance(quote_resp, dict):
        return None

    payload = quote_resp.get(symbol, quote_resp)
    if not isinstance(payload, dict):
        return None

    q = payload.get("quote") or {}
    r = payload.get("regular") or {}
    e = payload.get("extended") or {}

    # 1) mark
    v = _as_float(q.get("mark"))
    if v and v > 0:
        return v

    # 2) lastPrice
    v = _as_float(q.get("lastPrice"))
    if v and v > 0:
        return v

    # 3) regularMarketLastPrice
    v = _as_float(r.get("regularMarketLastPrice"))
    if v and v > 0:
        return v

    # 4) bid/ask midpoint
    bid = _as_float(q.get("bidPrice"))
    ask = _as_float(q.get("askPrice"))
    if bid and ask and bid > 0 and ask > 0:
        return (bid + ask) / 2.0

    # 5) extended lastPrice (do NOT use extended mark; often 0)
    v = _as_float(e.get("lastPrice"))
    if v and v > 0:
        return v

    return None


def validate_schwab_quote_shape(quote_resp: Any, symbol: str) -> bool:
    """
    Returns True if quote_resp looks like a Schwab quote payload
    we can parse for this symbol.
    """
    if not isinstance(quote_resp, dict):
        return False
    payload = quote_resp.get(symbol, quote_resp)
    if not isinstance(payload, dict):
        return False
    # Schwab quote payload usually has at least one of these:
    return any(k in payload for k in ("quote", "regular", "extended", "fundamental", "reference"))


def debug_quote_keys(quote_resp: Any, symbol: str) -> str:
    """
    Helpful for logging. Returns short info about top-level and payload keys.
    """
    try:
        if not isinstance(quote_resp, dict):
            return f"type={type(quote_resp)}"
        top_keys = list(quote_resp.keys())[:20]
        payload = quote_resp.get(symbol, quote_resp)
        payload_keys = list(payload.keys())[:20] if isinstance(payload, dict) else []
        return f"top_keys={top_keys} payload_keys={payload_keys}"
    except Exception:
        return "debug_quote_keys_failed"
