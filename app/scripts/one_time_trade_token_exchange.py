#!/usr/bin/env python3
import json, requests, argparse, time
from pathlib import Path

# Your Trade App credentials
CLIENT_ID = "prod-adnanshakilicloudcom-fbd63867-ce57-4ecf-ab53-f988612b3389"
REDIRECT_URI = "https://www.stockwicks.com/clients/ashakil/auth/schwab/db/callback"
TOKEN_URL = "https://api.schwab.com/oauth/token"
SAVE_PATH = Path("/var/www/stockwicks/data/116/trade_token.json")

def exchange_code_for_token(auth_code: str):
    print(f"[INFO] Exchanging code for new Trade token...")
    data = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": REDIRECT_URI,
        "client_id": f"{CLIENT_ID}@AMER.OAUTHAP"
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    r = requests.post(TOKEN_URL, headers=headers, data=data, timeout=15)
    print(f"[DEBUG] Status={r.status_code}")
    print(f"[DEBUG] Response={r.text[:300]}...")
    if r.status_code != 200:
        raise SystemExit(f"[ERROR] Token exchange failed ({r.status_code})")

    token = r.json()
    token["token_time"] = int(time.time())
    SAVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SAVE_PATH, "w") as f:
        json.dump(token, f, indent=2)
    print(f"[OK] Saved new trade_token.json → {SAVE_PATH}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Exchange Schwab trade auth code for access token.")
    parser.add_argument("--code", required=True, help="Authorization code from Schwab redirect URL")
    args = parser.parse_args()
    exchange_code_for_token(args.code)
