#!/usr/bin/env python3
import os, json, time, base64, logging
from pathlib import Path
from app.utils.schwab_circuit import schwab_session

log = logging.getLogger("schwab_market_token")

# --- CONFIG ---
def _infer_client_root() -> str:
    # app/utils/stock/schwab_market_token.py -> client root
    return str(Path(__file__).resolve().parents[3])


TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
TOKEN_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
    "User-Agent": "StockWicks/1.0",
}

def _client_root() -> str:
    return os.getenv("CLIENT_ROOT", _infer_client_root())


def _data_dir() -> Path:
    return Path(os.getenv("DATA_DIR", f"{_client_root()}/data"))


def _refresh_user_id() -> int:
    return int(os.getenv("SCHWAB_REFRESH_USER_ID", "1"))


def _client_id() -> str:
    return os.getenv("SCHWAB_MARKET_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.getenv("SCHWAB_MARKET_CLIENT_SECRET", "").strip()


def _redirect_uri() -> str:
    return os.getenv(
        "SCHWAB_MARKET_REDIRECT_URI",
        f"https://www.stockwicks.com/clients/{os.getenv('CLIENT_SLUG', Path(_client_root()).name)}/auth/schwab/callback",
    ).strip()

# --- CORE FUNCS ---
def get_user_token_path(user_id: int | None = None) -> Path:
    uid = int(user_id if user_id is not None else _refresh_user_id())
    return _data_dir() / str(uid) / "schwab_market_token.json"


def load_token(user_id: int | None = None):
    path = get_user_token_path(user_id)
    if not path.exists():
        log.error(f"[MARKET TOKEN] File not found: {path}")
        return None
    return json.load(open(path))


def save_token(tok: dict, user_id: int | None = None):
    path = get_user_token_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tok["token_time"] = int(time.time())
    json.dump(tok, open(path, "w"), indent=2)
    log.info(f"[MARKET TOKEN] saved -> {path}")


def _post_token(headers: dict, data: dict, timeout: int = 15):
    merged_headers = dict(TOKEN_HEADERS)
    merged_headers.update(headers)
    session = schwab_session()
    return session.post(TOKEN_URL, headers=merged_headers, data=data, timeout=timeout)


def refresh_market_token(user_id: int | None = None):
    old = load_token(user_id)
    if not old or "refresh_token" not in old:
        log.error("[MARKET TOKEN] missing token or refresh_token")
        return None

    client_id = _client_id()
    client_secret = _client_secret()
    redirect_uri = _redirect_uri()
    if not client_id or not client_secret or not redirect_uri:
        log.error("[MARKET TOKEN] missing SCHWAB_MARKET_CLIENT_ID / SCHWAB_MARKET_CLIENT_SECRET / SCHWAB_MARKET_REDIRECT_URI")
        return None

    refresh_token = old["refresh_token"]
    b64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {
        "Authorization": f"Basic {b64}",
    }
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    r = _post_token(headers=headers, data=data, timeout=15)
    log.info("[MARKET TOKEN] refresh status=%s", r.status_code)
    if r.status_code != 200:
        log.error("[MARKET TOKEN] refresh failed (%s): %s", r.status_code, r.text[:300])
        return None

    new = r.json()
    if not new.get("refresh_token"):
        new["refresh_token"] = refresh_token
    save_token(new, user_id)
    log.info(f"[MARKET TOKEN] refreshed successfully (expires_in={new.get('expires_in')})")
    return new

if __name__ == "__main__":
    refresh_market_token()
