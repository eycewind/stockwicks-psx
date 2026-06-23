# app/utils/stock/schwab_token.py
"""
Commercial Schwab market token helper.

Used by:
- market_price.py
- data_fetch.py
- schwab_price_history.py
- paper_trade_engine.py

For market data, always use:
  /var/stockwicks/clients/ashakil/data/{SCHWAB_REFRESH_USER_ID}/schwab_market_token.json

Trade order code should use schwab_trade_token.py separately.
Never log raw tokens.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("schwab_token")

CLIENT_ROOT = os.getenv("CLIENT_ROOT", "/var/stockwicks/clients/ashakil")
DATA_DIR = Path(os.getenv("DATA_DIR", f"{CLIENT_ROOT}/data"))
SCHWAB_REFRESH_USER_ID = int(os.getenv("SCHWAB_REFRESH_USER_ID", "3"))

MARKET_TOKEN_PATH = DATA_DIR / str(SCHWAB_REFRESH_USER_ID) / "schwab_market_token.json"


def _market_token_path(user_id: int | None = None) -> Path:
    uid = int(user_id if user_id is not None else SCHWAB_REFRESH_USER_ID)
    return DATA_DIR / str(uid) / "schwab_market_token.json"


def _load_market_token(user_id: int | None = None) -> Optional[dict]:
    token_path = _market_token_path(user_id)
    if not token_path.exists():
        log.warning("[SCHWAB TOKEN] Market token file missing: %s", token_path)
        return None

    try:
        with token_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        log.exception("[SCHWAB TOKEN] Failed reading market token file: %s", token_path)
        return None


def _token_is_expired(token_data: dict, safety_seconds: int = 120) -> bool:
    """
    Return True if token is missing/expired/near expiry.

    Supports:
    - token_time + expires_in
    - created_at_epoch / created_at + expires_in
    - access_token_expires_at_epoch
    """
    now = int(time.time())

    exp_at = token_data.get("access_token_expires_at_epoch")
    if exp_at:
        try:
            return now >= int(exp_at) - safety_seconds
        except Exception:
            pass

    created = token_data.get("token_time") or token_data.get("created_at_epoch") or token_data.get("created_at")
    expires_in = token_data.get("expires_in")

    if created and expires_in:
        try:
            return now >= int(float(created)) + int(expires_in) - safety_seconds
        except Exception:
            pass

    # If no expiry metadata, assume usable if access_token exists.
    # The scheduled refresh task updates the file every 5 minutes.
    return False


def _access_token_safety_seconds() -> int:
    try:
        return int(os.getenv("SCHWAB_REFRESH_SAFETY_SECONDS", "600"))
    except Exception:
        return 600


def refresh_token(user_id: int | None = None) -> Optional[dict]:
    """
    Compatibility wrapper.

    Refreshes the commercial market token using the existing market-token utility.
    """
    try:
        from app.utils.stock.schwab_market_token import refresh_market_token

        result = refresh_market_token(user_id)
        return result if isinstance(result, dict) else _load_market_token(user_id)
    except Exception:
        log.exception("[SCHWAB TOKEN] Market refresh failed")
        return None


def get_valid_access_token(user_id: int | None = None, *, force_refresh: bool = False) -> Optional[str]:
    """
    Return valid market-data access token from the commercial user token file.

    If token appears expired, attempts one refresh using schwab_market_token.py.
    When force_refresh=True, refreshes even if the local expiry metadata says
    the token is still valid. This handles Schwab-side invalidation/401s.
    """
    token_path = _market_token_path(user_id)
    token_data = _load_market_token(user_id)

    if force_refresh or not token_data:
        token_data = refresh_token(user_id)

    if not token_data:
        log.warning("[SCHWAB TOKEN] No market token available from %s", token_path)
        return None

    if _token_is_expired(token_data, safety_seconds=_access_token_safety_seconds()):
        log.info("[SCHWAB TOKEN] Market token near expiry, refreshing")
        token_data = refresh_token(user_id)

    if not token_data:
        return None

    access_token = token_data.get("access_token")
    if not access_token:
        log.warning("[SCHWAB TOKEN] Market token file has no access_token: %s", token_path)
        return None

    return access_token


def get_valid_user_access_token(user_id: int = SCHWAB_REFRESH_USER_ID) -> Optional[str]:
    """
    Backward-compatible alias for old callers.
    For market data, user_id selects DATA_DIR/{user_id}/schwab_market_token.json.
    """
    return get_valid_access_token(user_id)
