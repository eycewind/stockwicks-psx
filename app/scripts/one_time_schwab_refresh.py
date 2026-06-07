#!/usr/bin/env python3
"""
One-time Schwab token upgrade script.

Use this immediately after your FIRST login to exchange the initial refresh_token
for a new one that supports ongoing rotation (fixes 'unsupported_token_type').

Usage:
    python3 app/scripts/one_time_schwab_refresh.py --user-id 116
"""

import os
import json
import base64
import requests
import argparse
import time

DATA_ROOT = "/var/www/stockwicks/data"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"


def load_env(env_path="/var/www/stockwicks/.env"):
    if not os.path.exists(env_path):
        print(f"[WARN] .env not found at {env_path}")
        return
    with open(env_path) as f:
        for line in f:
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ[k.strip()] = v.strip().strip("'").strip('"')


def one_time_refresh(user_id: int):
    # Load environment credentials
    load_env()
    cid = os.getenv("SCHWAB_TRADE_CLIENT_ID")
    csecret = os.getenv("SCHWAB_TRADE_CLIENT_SECRET")
    if not cid or not csecret:
        raise RuntimeError("Missing SCHWAB_TRADE_CLIENT_ID or SCHWAB_TRADE_CLIENT_SECRET in env")

    path = os.path.join(DATA_ROOT, str(user_id), "trade_token.json")
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    with open(path) as f:
        payload = json.load(f)

    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("No refresh_token found in token file")

    print(f"[INFO] Performing one-time Schwab refresh for user {user_id} ...")

    # --- FIRST TRY: client_id + client_secret in body (works for first refresh)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": cid,
        "client_secret": csecret,
    }

    r = requests.post(TOKEN_URL, headers=headers, data=data, timeout=20)
    if r.status_code != 200:
        print(f"[WARN] Body refresh failed: {r.status_code} {r.text.strip()}")
        # --- FALLBACK: use Basic Auth (works for subsequent refreshes)
        basic = base64.b64encode(f"{cid}:{csecret}".encode()).decode()
        headers = {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        r = requests.post(TOKEN_URL, headers=headers, data=data, timeout=20)

    if r.status_code != 200:
        print(f"[ERROR] Refresh failed: {r.status_code} {r.text.strip()}")
        print("[FAIL] Could not upgrade token. Try re-logging into Schwab and retry.")
        return False

    td = r.json()
    if not td.get("refresh_token"):
        print("[WARN] Schwab did not return a new refresh_token (unexpected).")
    else:
        print("[OK] New refresh_token received and saved.")

    # --- Update payload ---
    payload.update({
        "access_token": td.get("access_token"),
        "refresh_token": td.get("refresh_token", refresh_token),
        "expires_in": td.get("expires_in", 1800),
        "token_type": td.get("token_type", "Bearer"),
        "scope": td.get("scope", "api"),
        "id_token": td.get("id_token"),
        "token_time": int(time.time()),
    })

    # --- Backup and save ---
    backup = path + ".bak_first"
    os.system(f"cp '{path}' '{backup}'")
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"[DONE] Token upgraded successfully → {path}")
    print(f"[INFO] Backup created → {backup}")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-id", type=int, required=True, help="Numeric user ID (e.g. 116)")
    args = ap.parse_args()
    one_time_refresh(args.user_id)
