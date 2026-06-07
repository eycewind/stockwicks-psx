# /var/www/stockwicks/app/utils/options/options_pricing.py

import logging
from typing import Optional, Dict, Any, List, Tuple
import json
import requests
from datetime import datetime

from app.utils.stock.schwab_token import get_valid_access_token
from app.utils.options.options_data import fetch_option_chain  # used for fallback lookup

log = logging.getLogger(__name__)

SCHWAB_API_URL = "https://api.schwabapi.com/marketdata/v1"


# ---------------------------
# Schwab helpers
# ---------------------------
def _schwab_headers() -> Dict[str, str]:
    token = get_valid_access_token()
    if not token:
        raise RuntimeError("Could not obtain Schwab access token.")
    return {"Authorization": f"Bearer {token}"}


def _safe_mid(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    try:
        b = float(bid) if bid is not None else None
        a = float(ask) if ask is not None else None
        if b is None and a is None:
            return None
        if b is None:
            return a if a and a > 0 else None
        if a is None:
            return b if b and b > 0 else None
        if a <= 0 and b <= 0:
            return None
        return (a + b) / 2.0
    except Exception:
        return None


def _extract_quote_mark(item: Dict[str, Any]) -> Optional[float]:
    """
    Schwab option quote payloads can vary. Try explicit mark, then mid from bid/ask, then last.
    """
    m = item.get("mark")
    if isinstance(m, (int, float)) and m > 0:
        return float(m)

    mid = _safe_mid(item.get("bid"), item.get("ask"))
    if isinstance(mid, (int, float)) and mid > 0:
        return float(mid)

    last = item.get("last") or item.get("lastPrice")
    if isinstance(last, (int, float)) and last > 0:
        return float(last)

    return None


def _get_option_quotes_by_occ(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Fetch option quotes by OCC symbols via Schwab quotes endpoint.
    Returns a dict: { occ_symbol_without_spaces: quote_dict }
    """
    out: Dict[str, Dict[str, Any]] = {}
    syms = [s for s in (symbols or []) if s]
    if not syms:
        return out

    url = f"{SCHWAB_API_URL}/quotes"
    params = {"symbols": ",".join(s.replace(" ", "") for s in syms)}
    try:
        resp = requests.get(url, headers=_schwab_headers(), params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json() or {}

        # Many responses: {'quotes': [ {...}, ... ]}; some dict keyed by symbol.
        quotes = data.get("quotes") or data.get("data") or data.get("items") or []
        if isinstance(quotes, dict):
            for k, v in quotes.items():
                sym = (v.get("symbol") or k or "").replace(" ", "")
                if sym:
                    out[sym] = v
        else:
            for q in quotes:
                sym = (q.get("symbol") or "").replace(" ", "")
                if sym:
                    out[sym] = q
    except Exception as e:
        log.error(f"[pricing] Schwab /quotes error: {e}", exc_info=True)
    return out


# ---------------------------
# Chain fallback helpers
# ---------------------------
def _fetch_chain(symbol: str) -> Dict[datetime, Dict[str, List[Dict[str, Any]]]]:
    """
    Wrapper around fetch_option_chain with a wide window so we can find the contract by OCC.
    Expected structure per expiry key: {'calls': [...], 'puts': [...]}
    """
    try:
        return fetch_option_chain(symbol, dte_min=0, dte_max=400, limit_expiries=100) or {}
    except Exception as e:
        log.error(f"[pricing] fetch_option_chain error for {symbol}: {e}", exc_info=True)
        return {}


def _find_contract_in_chain_by_occ(chain: Dict[datetime, Dict[str, List[Dict[str, Any]]]],
                                   occ: str) -> Optional[Dict[str, Any]]:
    occ_norm = (occ or "").replace(" ", "")
    if not occ_norm:
        return None
    try:
        for _, books in chain.items():
            for side_key in ("calls", "puts"):
                for c in books.get(side_key, []) or []:
                    c_occ = (c.get("occ") or c.get("symbol") or "").replace(" ", "")
                    if c_occ == occ_norm:
                        return c
    except Exception:
        pass
    return None


def _find_contract_in_chain_by_exp_strike(chain: Dict[datetime, Dict[str, List[Dict[str, Any]]]],
                                          expiry_date: Optional[datetime],
                                          strike: Optional[float],
                                          typ: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Fallback when OCC not found. Try to match by nearest expiry date and strike+type.
    """
    if not expiry_date or strike is None or not typ:
        return None
    try:
        # choose expiry key with exact date match or nearest
        keys = list(chain.keys())
        if not keys:
            return None
        target_date = expiry_date.date()
        # exact
        key = next((k for k in keys if getattr(k, "date", lambda: None)() == target_date), None)
        if key is None:
            # nearest by days
            key = min(keys, key=lambda k: abs((k.date() - target_date).days))
        book = chain.get(key, {})
        side_key = "calls" if typ.lower() == "call" else "puts"
        contracts = book.get(side_key, []) or []
        if not contracts:
            return None
        # find closest by strike
        return min(contracts, key=lambda c: abs(float(c.get("strike", 0)) - float(strike)))
    except Exception:
        return None


def _mark_from_chain_contract(c: Dict[str, Any]) -> Optional[float]:
    if not c:
        return None
    try:
        # try explicit mid/mark then bid/ask then last
        if "mid" in c and c["mid"]:
            m = float(c["mid"])
            if m > 0:
                return m
        m = _safe_mid(c.get("bid"), c.get("ask"))
        if m and m > 0:
            return float(m)
        last = c.get("last") or c.get("lastPrice")
        if last and float(last) > 0:
            return float(last)
    except Exception:
        return None
    return None


def _leg_marks_with_fallback(underlying: str,
                             occ_list: List[Tuple[str, str]],
                             expiry: Optional[datetime],
                             strikes_types: List[Tuple[Optional[float], Optional[str]]]
                             ) -> List[Optional[float]]:
    """
    For each leg: try /quotes(occ). If missing, fallback to chain match by OCC,
    else by (expiry,strike,type). Returns list of marks (or None) aligned to legs.
    occ_list: [(occ_symbol, label), ...]
    strikes_types: [(strike, type), ...] where type is 'call' or 'put'
    """
    # 1) Try quotes for all OCCs we have
    occ_symbols = [o for o, _ in occ_list if o]
    quote_map = _get_option_quotes_by_occ(occ_symbols) if occ_symbols else {}

    marks: List[Optional[float]] = []
    chain_cache = None  # lazy fetch

    for idx, (occ_sym, _label) in enumerate(occ_list):
        # prefer quote by OCC
        mark = None
        if occ_sym:
            q = quote_map.get(occ_sym.replace(" ", ""))
            if q:
                mark = _extract_quote_mark(q)

        if mark is None:
            # fallback to chain by OCC, then expiry/strike/type
            if chain_cache is None:
                chain_cache = _fetch_chain(underlying)
            c = None
            if occ_sym:
                c = _find_contract_in_chain_by_occ(chain_cache, occ_sym)
            if c is None:
                strike, typ = strikes_types[idx]
                c = _find_contract_in_chain_by_exp_strike(chain_cache, expiry, strike, typ)
            mark = _mark_from_chain_contract(c) if c else None

        marks.append(mark if (isinstance(mark, (int, float)) and mark > 0) else None)

    return marks


# ---------------------------
# PUBLIC: mark_open_trade
# ---------------------------
def mark_open_trade(open_trade) -> Optional[float]:
    """
    Compute the current 'mark' for an open SINGLE or SPREAD from your DB row.
    Schema used:
      - option_symbol (OCC for single OR long leg for spread)
      - option_symbol_short (OCC for short leg if spread)
      - side (BUY/SELL) – used by manager to decide TP/SL direction, not here
      - position_side ('call'/'put') – for logging / fallback
      - expiry_date (timestamp)
    Spread determination:
      - If option_symbol_short exists -> SPREAD
      - Else if legs_json exists -> derive short/long from it
      - Else -> SINGLE

    Returns float mark (>=0) or None on failure.
    """
    trade_id = getattr(open_trade, "id", "N/A")
    try:
        underlying = getattr(open_trade, "underlying_symbol", None)
        side = (getattr(open_trade, "side", "") or "").upper()
        pos_side = getattr(open_trade, "position_side", None)
        expiry_dt = getattr(open_trade, "expiry_date", None)

        long_occ = (getattr(open_trade, "option_symbol", "") or "").replace(" ", "")
        short_occ = (getattr(open_trade, "option_symbol_short", "") or "").replace(" ", "")
        legs_json_str = getattr(open_trade, "legs_json", None)

        # If spread not indicated by column, try legs_json
        is_spread = bool(short_occ)
        long_strike = getattr(open_trade, "strike_price", None)
        long_type = pos_side

        short_strike = getattr(open_trade, "strike_price_short", None)
        short_type = pos_side  # if legs_json gives type per leg, we’ll override below

        if not is_spread and legs_json_str:
            try:
                legs = json.loads(legs_json_str)
                if isinstance(legs, list) and len(legs) >= 2:
                    # identify short/long via 'side'
                    short_leg = next((l for l in legs if (l.get("side", "").lower() in ["sell", "short"])), None)
                    long_leg = next((l for l in legs if (l.get("side", "").lower() in ["buy", "long"])), None)
                    if short_leg and long_leg:
                        short_occ = (short_leg.get("symbol") or short_leg.get("occ") or "").replace(" ", "")
                        long_occ = (long_leg.get("symbol") or long_leg.get("occ") or "").replace(" ", "")
                        short_strike = float(short_leg.get("strike")) if short_leg.get("strike") is not None else None
                        long_strike = float(long_leg.get("strike")) if long_leg.get("strike") is not None else None
                        short_type = (short_leg.get("type") or short_leg.get("putCall") or short_type or "").lower()
                        long_type = (long_leg.get("type") or long_leg.get("putCall") or long_type or "").lower()
                        # expiry may be string; keep DB expiry_date for chain fallback matching
                        is_spread = True
            except Exception as e:
                log.warning(f"[mark_open_trade] Trade #{trade_id}: legs_json parse error: {e}")

        if is_spread:
            log.info(f"[mark_open_trade] SPREAD | id={trade_id} | underly={underlying} | side={side} | expiry={expiry_dt}")
            # order matters: SPREAD value = short_mid - long_mid
            occs = [(short_occ, "short"), (long_occ, "long")]
            strikes_types = [(short_strike, short_type), (long_strike, long_type)]
            marks = _leg_marks_with_fallback(underlying, occs, expiry_dt, strikes_types)
            short_mark, long_mark = marks[0], marks[1]

            if short_mark is None or long_mark is None:
                log.info(f"[mark_open_trade] SPREAD | id={trade_id} | missing marks -> short={short_mark}, long={long_mark}")
                return None

            value = float(short_mark) - float(long_mark)
            if value < 0 and abs(value) < 0.01:
                value = 0.0
            value = round(value, 4)
            log.info(f"[mark_open_trade] SPREAD | id={trade_id} | short={short_mark:.4f} - long={long_mark:.4f} => {value:.4f}")
            return value

        # SINGLE
        if not long_occ:
            log.warning(f"[mark_open_trade] SINGLE | id={trade_id} | missing option_symbol (OCC).")
            return None

        log.info(f"[mark_open_trade] SINGLE | id={trade_id} | underly={underlying} | side={side} | pos={pos_side} | expiry={expiry_dt} | occ={long_occ}")

        marks = _leg_marks_with_fallback(
            underlying=underlying,
            occ_list=[(long_occ, "long")],
            expiry=expiry_dt,
            strikes_types=[(long_strike, long_type)]
        )
        m = marks[0]
        if m is None:
            log.info(f"[mark_open_trade] SINGLE | id={trade_id} | no positive mark for {long_occ}")
            return None

        m = round(float(m), 4)
        log.info(f"[mark_open_trade] SINGLE | id={trade_id} | occ={long_occ} -> mark={m:.4f}")
        return m

    except Exception as e:
        log.error(f"[mark_open_trade] Exception for trade #{trade_id}: {e}", exc_info=True)
        return None
