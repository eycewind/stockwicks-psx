# app/utils/options/chain_utils.py
from typing import Optional, Dict, List, Any

def _first_key(d: Dict[str, Any], *candidates: str, default=None):
    """Safely get the first non-None value from a dictionary for a list of possible keys."""
    for k in candidates:
        if k in d and d[k] is not None:
            return d[k]
    return default

def canon_contract(c: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Normalizes a raw option contract dictionary into a standardized format.
    Handles multiple key names for the same data (e.g., 'strike' vs 'strikePrice').
    """
    if not isinstance(c, dict): return None
    side = _first_key(c, "putCall", "type", "right", default="").upper()
    if side not in ("CALL", "PUT"): return None
    try: strike = float(_first_key(c, "strike", "strikePrice"))
    except (ValueError, TypeError): return None
    mid = float(_first_key(c, "mid", "mark", default=0.0) or 0.0)
    occ = _first_key(c, "occ", "symbol", "contractSymbol")
    exp = _first_key(c, "expirationDate", "expiration")
    if isinstance(exp, str) and len(exp) > 10: exp = exp.split('T')[0]
    return {"putCall": side, "strike": strike, "mid": mid, "occ": occ, "expiration": exp, "totalVolume": _first_key(c, "totalVolume", "volume", default=0), "delta": _first_key(c, "delta", default=0.0)}

def normalize_chain(result: Any) -> List[Dict[str, Any]]:
    """
    Accepts a raw option chain (either a dict-by-expiry or a flat list)
    and returns a single, clean, flat list of standardized contracts.
    """
    out: List[Dict[str, Any]] = []
    if isinstance(result, dict):
        for expiry_data in result.values():
            if isinstance(expiry_data, dict):
                for leg_type in ("calls", "puts"):
                    for contract in expiry_data.get(leg_type, []):
                        if (canon := canon_contract(contract)): out.append(canon)
    elif isinstance(result, list):
        for contract in result:
            if (canon := canon_contract(contract)): out.append(canon)
    return out