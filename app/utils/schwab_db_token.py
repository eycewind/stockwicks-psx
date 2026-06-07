#/var/www/stockwicks/app/utils/schwab_db_token.py
# /var/www/stockwicks/app/utils/schwab_db_token.py

import os
import base64
import logging
from typing import Optional
from datetime import datetime, timedelta

import requests
from sqlalchemy.orm import Session

from app.models.schwab_tokens import SchwabToken

log = logging.getLogger("schwab_db_token")

# Read from env if present; fall back to your current hard-coded values
TRADE_CLIENT_ID = os.getenv("SCHWAB_CLIENT_ID", "kI9oDoNC4WNXzp7AJRpAAIvDoE9GxJGz").strip()
TRADE_CLIENT_SECRET = os.getenv("SCHWAB_CLIENT_SECRET", "x2tV8ksOGGh9cUXA").strip()
TRADE_REDIRECT_URI = os.getenv("SCHWAB_TRADE_REDIRECT_URI", os.getenv("TRADE_REDIRECT_URI", "https://www.stockwicks.com/clients/ashakil/auth/schwab/db/callback")).strip()

BASE_URL = "https://api.schwabapi.com"
TOKEN_URL = f"{BASE_URL}/v1/oauth/token"
REQ_TIMEOUT = 20  # seconds


def _unsanitize_token(s: Optional[str]) -> Optional[str]:
    """
    Reverse any storage/transport sanitization:
      - spaces -> '+'
      - '@' -> '=' (padding often mangled by some pipelines)
    """
    if not s:
        return s
    s = s.replace(" ", "+")
    if "@" in s:
        s = s.replace("@", "=")
    return s


def _apply_new_tokens(db: Session, row: SchwabToken, payload: dict, fallback_refresh: Optional[str]) -> str:
    """
    Save tokens from a refresh response into DB, normalizing values.
    Returns the normalized access token.
    """
    access = _unsanitize_token(payload.get("access_token"))
    new_refresh = _unsanitize_token(payload.get("refresh_token")) or _unsanitize_token(fallback_refresh)
    expires_in = int(payload.get("expires_in", 1800))

    row.access_token = access
    row.refresh_token = new_refresh
    row.expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
    # optionally carry extra fields if present
    row.scope = payload.get("scope", row.scope)
    row.schwab_user_guid = payload.get("schwab_user_guid", row.schwab_user_guid)

    db.add(row)
    db.commit()
    return access or ""


def _refresh_access_token(db: Session, row: SchwabToken) -> Optional[str]:
    """
    Try to refresh the access token using the stored refresh_token.
    Uses two strategies:
      1) Basic auth header (client_id:client_secret base64)
      2) Fallback: send client_id/client_secret in the body (no Authorization header)
    """
    if not row or not row.refresh_token:
        return None

    refresh = _unsanitize_token(row.refresh_token)
    if not refresh:
        return None

    # Strategy 1: Basic auth header
    b64 = base64.b64encode(f"{TRADE_CLIENT_ID}:{TRADE_CLIENT_SECRET}".encode()).decode()
    headers1 = {"Authorization": f"Basic {b64}", "Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "redirect_uri": TRADE_REDIRECT_URI,  # some providers require this on refresh
    }

    try:
        resp = requests.post(TOKEN_URL, headers=headers1, data=data, timeout=REQ_TIMEOUT)
        if resp.status_code < 400:
            td = resp.json()
            return _apply_new_tokens(db, row, td, fallback_refresh=refresh)

        # Log and fall back to strategy 2
        log.warning("Schwab refresh (Basic) failed: %s", resp.text)
    except Exception as e:
        log.warning("Schwab refresh (Basic) exception: %s", e)

    # Strategy 2: creds in form body (no Authorization header)
    headers2 = {"Content-Type": "application/x-www-form-urlencoded"}
    data2 = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "redirect_uri": TRADE_REDIRECT_URI,
        "client_id": TRADE_CLIENT_ID,
        "client_secret": TRADE_CLIENT_SECRET,
    }
    try:
        resp2 = requests.post(TOKEN_URL, headers=headers2, data=data2, timeout=REQ_TIMEOUT)
        if resp2.status_code >= 400:
            log.error("Schwab refresh (body) failed: %s", resp2.text)
            return None
        td2 = resp2.json()
        return _apply_new_tokens(db, row, td2, fallback_refresh=refresh)
    except Exception as e:
        log.error("Schwab refresh (body) exception: %s", e)
        return None


def get_valid_token(db: Session, user_id: int) -> Optional[str]:
    """
    Return a valid Schwab access_token for the user (refresh if needed).
    Always returns a normalized token (padding restored).
    """
    row = db.query(SchwabToken).filter_by(user_id=user_id).first()
    if not row:
        return None

    # If not near expiry, return current (normalized) token
    if row.expires_at and row.expires_at > datetime.utcnow() + timedelta(seconds=120):
        return _unsanitize_token(row.access_token)

    # Otherwise refresh
    new_access = _refresh_access_token(db, row)
    if not new_access:
        return None
    return _unsanitize_token(new_access)
