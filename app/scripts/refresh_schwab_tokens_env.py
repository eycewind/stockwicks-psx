#!/usr/bin/env python3
#!/usr/bin/env python3
"""
Refresh Schwab OAuth tokens (global and per-user).

Fixes included:
 - Always replace both access_token and refresh_token (Schwab single-use model)
 - Detect and log invalid_grant errors
 - Safer atomic writes + backup
 - Clear, verbose logging for Celery or manual runs
"""

import os, json, time, base64, argparse, tempfile, shutil, requests
from typing import Optional

DEFAULT_DATA_DIR = "/var/www/stockwicks/data"
DEFAULT_BASE_URL = "https://api.schwabapi.com"


# -------- basic .env loader --------
def load_env_file(path: str) -> None:
    if not path or not os.path.exists(path):
        return
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))


# -------- file utils --------
def now_ts() -> int:
    return int(time.time())


def atomic_write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        shutil.move(tmp_path, path)
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


def safe_load(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        return json.load(f)


def ensure_token_time(payload: dict) -> dict:
    p = dict(payload)
    if "token_time" not in p:
        p["token_time"] = now_ts()
    try:
        p["expires_in"] = int(p.get("expires_in", 1800))
    except Exception:
        p["expires_in"] = 1800
    return p


def apply_refresh(old_payload: dict, td: dict) -> dict:
    """
    Merge token refresh data. Always overwrite both access and refresh tokens.
    """
    merged = dict(old_payload)
    for k in ("access_token", "refresh_token", "expires_in", "scope", "token_type", "id_token"):
        if k in td and td[k]:
            merged[k] = td[k]
    merged["token_time"] = now_ts()
    return merged


# -------- HTTP refresh helpers --------
def refresh_with_basic(token_url: str, client_id: str, client_secret: str, refresh_token: str, timeout: int = 20):
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    try:
        r = requests.post(token_url, headers=headers, data=data, timeout=timeout)
        if r.status_code == 200:
            return r.json(), None
        if "invalid_grant" in r.text:
            return None, "invalid_grant (refresh token already used or expired)"
        return None, f"basic={r.status_code} {r.text}"
    except Exception as e:
        return None, f"basic_exc={e}"


def refresh_with_body(token_url: str, client_id: str, client_secret: str, refresh_token: str, timeout: int = 20):
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    try:
        r = requests.post(token_url, headers=headers, data=data, timeout=timeout)
        if r.status_code == 200:
            return r.json(), None
        if "invalid_grant" in r.text:
            return None, "invalid_grant (refresh token already used or expired)"
        return None, f"body={r.status_code} {r.text}"
    except Exception as e:
        return None, f"body_exc={e}"


def do_refresh(path: str, client_id: str, client_secret: str, token_url: str, verbose: bool) -> bool:
    payload = safe_load(path)
    if not payload:
        if verbose: print(f"[SKIP] {path} (missing)")
        return False
    payload = ensure_token_time(payload)
    rt = payload.get("refresh_token")
    if not rt:
        if verbose: print(f"[SKIP] {path} (no refresh_token)")
        return False

    if verbose:
        print(f"[INFO] Refreshing {path} using {client_id[:4]}… (token_url={token_url})")

    td, err1 = refresh_with_basic(token_url, client_id, client_secret, rt)
    if not td:
        td, err2 = refresh_with_body(token_url, client_id, client_secret, rt)
        if not td:
            if verbose:
                print(f"[FAIL] {path} refresh failed.\n  {err1}\n  {err2}")
            return False

    if verbose:
        if td.get("refresh_token"):
            print(f"[INFO] New refresh_token detected for {path}")
        else:
            print(f"[WARN] Schwab did not return a new refresh_token (may fail next cycle)")

    new_payload = apply_refresh(payload, td)
    try:
        shutil.copy2(path, path + ".bak")
    except Exception:
        pass

    atomic_write_json(path, new_payload)
    if verbose:
        print(f"[OK]   {path} refreshed. expires_in={new_payload.get('expires_in')}s at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    return True


# -------- discovery helpers --------
def iter_user_trade_tokens(data_dir: str):
    # data_dir/<user_id>/trade_token.json
    for name in os.listdir(data_dir):
        sub = os.path.join(data_dir, name)
        if os.path.isdir(sub) and name.isdigit():
            p = os.path.join(sub, "trade_token.json")
            if os.path.exists(p):
                yield int(name), p


def build_token_url(base_url_env: Optional[str]) -> str:
    base = (base_url_env or DEFAULT_BASE_URL).rstrip("/")
    return f"{base}/v1/oauth/token"


# -------- main --------
def main():
    ap = argparse.ArgumentParser(description="Refresh Schwab file tokens using .env or environment variables.")
    ap.add_argument("--env-file", help="Path to .env (optional). If set, values are loaded into env.")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help=f"Base data dir (default: {DEFAULT_DATA_DIR})")
    ap.add_argument("--user-id", type=int, help="Refresh trade_token.json for a specific user id")
    ap.add_argument("--all-users", action="store_true", help="Refresh trade_token.json for all user directories")
    ap.add_argument("--include-global", action="store_true", help="Also refresh the global schwab_token.json")
    ap.add_argument("--verbose", "-v", action="store_true", help="Verbose logs")
    args = ap.parse_args()

    load_env_file(args.env_file)

    token_url = build_token_url(os.getenv("SCHWAB_BASE_URL"))
    if args.verbose:
        print(f"[DEBUG] Using token_url={token_url}")

    SCHWAB_CLIENT_ID = os.getenv("SCHWAB_CLIENT_ID", "")
    SCHWAB_CLIENT_SECRET = os.getenv("SCHWAB_CLIENT_SECRET", "")
    SCHWAB_TRADE_CLIENT_ID = os.getenv("SCHWAB_TRADE_CLIENT_ID", "")
    SCHWAB_TRADE_CLIENT_SECRET = os.getenv("SCHWAB_TRADE_CLIENT_SECRET", "")

    if args.verbose:
        print(f"[ENV] SCHWAB_CLIENT_ID set? {bool(SCHWAB_CLIENT_ID)}")
        print(f"[ENV] SCHWAB_TRADE_CLIENT_ID set? {bool(SCHWAB_TRADE_CLIENT_ID)}")

    did_any = False

    # 1) Global token
    if args.include_global:
        if not SCHWAB_CLIENT_ID or not SCHWAB_CLIENT_SECRET:
            print("ERROR: SCHWAB_CLIENT_ID / SCHWAB_CLIENT_SECRET are required to refresh schwab_token.json")
            return 2
        global_path = os.path.join(args.data_dir, "schwab_token.json")
        ok = do_refresh(global_path, SCHWAB_CLIENT_ID, SCHWAB_CLIENT_SECRET, token_url, args.verbose)
        did_any = did_any or ok

    # 2) Per-user trade tokens
    if args.user_id is not None or args.all_users:
        if not SCHWAB_TRADE_CLIENT_ID or not SCHWAB_TRADE_CLIENT_SECRET:
            print("ERROR: SCHWAB_TRADE_CLIENT_ID / SCHWAB_TRADE_CLIENT_SECRET are required to refresh trade_token.json")
            return 2

    if args.user_id is not None:
        user_path = os.path.join(args.data_dir, str(args.user_id), "trade_token.json")
        ok = do_refresh(user_path, SCHWAB_TRADE_CLIENT_ID, SCHWAB_TRADE_CLIENT_SECRET, token_url, args.verbose)
        did_any = did_any or ok

    if args.all_users:
        for uid, p in iter_user_trade_tokens(args.data_dir):
            ok = do_refresh(p, SCHWAB_TRADE_CLIENT_ID, SCHWAB_TRADE_CLIENT_SECRET, token_url, args.verbose)
            did_any = did_any or ok

    if not (args.include_global or args.user_id is not None or args.all_users):
        print("Nothing to do. Choose one or more: --include-global, --user-id <id>, --all-users")
        return 1

    return 0 if did_any else 3


if __name__ == "__main__":
    raise SystemExit(main())
