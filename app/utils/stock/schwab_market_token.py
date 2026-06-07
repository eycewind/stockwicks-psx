#!/usr/bin/env python3
import os, json, time, base64, logging, requests
from pathlib import Path

log = logging.getLogger("schwab_market_token")

# --- CONFIG ---
def _infer_client_root() -> str:
    # app/utils/stock/schwab_market_token.py -> client root
    return str(Path(__file__).resolve().parents[3])


CLIENT_ROOT = os.getenv("CLIENT_ROOT", _infer_client_root())
DATA_DIR = Path(os.getenv("DATA_DIR", f"{CLIENT_ROOT}/data"))
SCHWAB_REFRESH_USER_ID = int(os.getenv("SCHWAB_REFRESH_USER_ID", "1"))
TOKEN_PATH = DATA_DIR / str(SCHWAB_REFRESH_USER_ID) / "schwab_market_token.json"

CLIENT_ID = os.getenv("SCHWAB_MARKET_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("SCHWAB_MARKET_CLIENT_SECRET", "").strip()
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
REDIRECT_URI = os.getenv(
    "SCHWAB_MARKET_REDIRECT_URI",
    f"https://www.stockwicks.com/clients/{os.getenv('CLIENT_SLUG', Path(CLIENT_ROOT).name)}/auth/schwab/callback",
).strip()

# --- CORE FUNCS ---
def get_user_token_path(user_id: int | None = None) -> Path:
    uid = int(user_id or SCHWAB_REFRESH_USER_ID)
    return DATA_DIR / str(uid) / "schwab_market_token.json"


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


def refresh_market_token(user_id: int | None = None):
    old = load_token(user_id)
    if not old or "refresh_token" not in old:
        log.error("[MARKET TOKEN] missing token or refresh_token")
        return None
    if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
        log.error("[MARKET TOKEN] missing SCHWAB_MARKET_CLIENT_ID / SCHWAB_MARKET_CLIENT_SECRET / SCHWAB_MARKET_REDIRECT_URI")
        return None

    refresh_token = old["refresh_token"]
    b64 = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    headers = {
        "Authorization": f"Basic {b64}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "redirect_uri": REDIRECT_URI,
    }

    r = requests.post(TOKEN_URL, headers=headers, data=data, timeout=15)
    log.info("[MARKET TOKEN] refresh status=%s", r.status_code)
    if r.status_code != 200:
        log.error(f"[MARKET TOKEN] refresh failed ({r.status_code})")
        return None

    new = r.json()
    if not new.get("refresh_token"):
        new["refresh_token"] = refresh_token
    save_token(new, user_id)
    log.info(f"[MARKET TOKEN] refreshed successfully (expires_in={new.get('expires_in')})")
    return new

if __name__ == "__main__":
    refresh_market_token()
