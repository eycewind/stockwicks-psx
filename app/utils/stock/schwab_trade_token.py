#!/usr/bin/env python3
import base64
import json
import logging
import os
import time
from pathlib import Path

import requests

log = logging.getLogger("schwab_trade_token")


def _infer_client_root() -> str:
    """
    Infer the commercial client root from this file location.

    Deployed shape:
      /var/stockwicks/clients/<client>/app/utils/stock/schwab_trade_token.py
    Local workspace shape:
      <workspace>/utils/stock/schwab_trade_token.py
    """
    path = Path(__file__).resolve()
    for parent in path.parents:
        if parent.name == "app":
            return str(parent.parent)
    return str(path.parents[2])


CLIENT_ROOT = os.getenv("CLIENT_ROOT", _infer_client_root())
DATA_DIR = Path(os.getenv("DATA_DIR", f"{CLIENT_ROOT}/data"))
SCHWAB_REFRESH_USER_ID = int(os.getenv("SCHWAB_REFRESH_USER_ID", "1"))
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
TOKEN_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
    "User-Agent": "StockWicks/1.0",
}

CLIENT_ID = os.getenv("SCHWAB_TRADE_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("SCHWAB_TRADE_CLIENT_SECRET", "").strip()
REDIRECT_URI = os.getenv(
    "SCHWAB_TRADE_REDIRECT_URI",
    f"https://www.stockwicks.com/clients/{os.getenv('CLIENT_SLUG', Path(CLIENT_ROOT).name)}/auth/schwab/db/callback",
).strip()


def get_user_token_path(user_id: int) -> Path:
    return DATA_DIR / str(user_id) / "schwab_trade_token.json"


def load_user_token(user_id: int):
    path = get_user_token_path(user_id)
    if not path.exists():
        log.error("[TRADE TOKEN] No file for user %s: %s", user_id, path)
        return None
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_user_token(user_id: int, data: dict) -> None:
    path = get_user_token_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["token_time"] = int(time.time())
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    log.info("[TRADE TOKEN] Saved for user %s -> %s", user_id, path)


def _post_token(headers: dict, data: dict, timeout: int = 15):
    merged_headers = dict(TOKEN_HEADERS)
    merged_headers.update(headers)
    session = requests.Session()
    session.trust_env = os.getenv("SCHWAB_TRUST_ENV_PROXIES", "0").strip().lower() in {"1", "true", "yes"}
    return session.post(TOKEN_URL, headers=merged_headers, data=data, timeout=timeout)


def refresh_user_trade_token(user_id: int):
    """Refresh one user's Schwab trading token from this client's data dir."""
    old = load_user_token(user_id)
    if not old or "refresh_token" not in old:
        log.error("[TRADE TOKEN] Missing refresh_token for user %s", user_id)
        return None
    if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
        log.error(
            "[TRADE TOKEN] missing SCHWAB_TRADE_CLIENT_ID / "
            "SCHWAB_TRADE_CLIENT_SECRET / SCHWAB_TRADE_REDIRECT_URI"
        )
        return None

    refresh_token = old["refresh_token"]
    b64_auth = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()

    resp = _post_token(
        headers={"Authorization": f"Basic {b64_auth}"},
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=15,
    )
    log.info("[TRADE TOKEN] refresh status=%s", resp.status_code)

    if resp.status_code != 200:
        log.error("[TRADE TOKEN] refresh failed (%s): %s", resp.status_code, resp.text[:300])
        return None

    new_token = resp.json()
    if not new_token.get("refresh_token"):
        new_token["refresh_token"] = refresh_token

    save_user_token(user_id, new_token)
    log.info("[TRADE TOKEN] Refreshed user %s expires_in=%s", user_id, new_token.get("expires_in"))
    return new_token


if __name__ == "__main__":
    refresh_user_trade_token(SCHWAB_REFRESH_USER_ID)
