# exchange_token.py
import requests
import os

from app.utils.client_context import public_base_url

CLIENT_ID = os.getenv("SCHWAB_TRADE_CLIENT_ID", os.getenv("SCHWAB_CLIENT_ID", "")).strip()
CLIENT_SECRET = os.getenv("SCHWAB_TRADE_CLIENT_SECRET", os.getenv("SCHWAB_CLIENT_SECRET", "")).strip()
REDIRECT_URI = os.getenv("SCHWAB_TRADE_REDIRECT_URI", f"{public_base_url()}/auth/schwab/callback").strip()
TOKEN_URL = "https://sandbox.schwabapi.com/v1/oauth/token"

# 📝 Paste these manually:
code = input("Enter code from redirect URL: ").strip()
verifier = input("Enter original code_verifier from step 1: ").strip()

data = {
    "grant_type": "authorization_code",
    "code": code,
    "redirect_uri": REDIRECT_URI,
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "code_verifier": verifier
}

print("\n🔄 Requesting access token...")
resp = requests.post(TOKEN_URL, data=data)

if resp.ok:
    token_data = resp.json()
    print("✅ Token response:")
    print(token_data)
else:
    print("❌ Error:")
    print(resp.status_code, resp.text)
