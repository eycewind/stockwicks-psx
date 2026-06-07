# exchange_token.py
import requests

CLIENT_ID = kI9oDoNC4WNXzp7AJRpAAIvDoE9GxJGz
CLIENT_SECRET = x2tV8ksOGGh9cUXA
REDIRECT_URI = "https://www.stockwicks.com/clients/ashakil/auth/schwab/callback"
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
