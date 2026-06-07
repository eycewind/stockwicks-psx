# /var/www/stockwicks/app/scripts/schwab_oauth_requests.py
import os
import json
from requests_oauthlib import OAuth2Session
from oauthlib.oauth2 import BackendApplicationClient
from dotenv import load_dotenv

load_dotenv(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.env")))

CLIENT_ID = os.getenv("SCHWAB_CLIENT_ID")
CLIENT_SECRET = os.getenv("SCHWAB_CLIENT_SECRET")
REDIRECT_URI = os.getenv("SCHWAB_REDIRECT_URI")
AUTH_BASE = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
SCOPE = ["marketdata"]

TOKEN_PATH = "/var/www/stockwicks/data/schwab_token.json"

def main():
    # Create OAuth session (PKCE enabled automatically)
    oauth = OAuth2Session(
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        scope=SCOPE
    )

    # Step 1: Generate authorization URL with PKCE
    auth_url, state = oauth.authorization_url(
        AUTH_BASE,
        code_challenge_method="S256"
    )
    print("\n➡️  Authorization URL:\n", auth_url)

    callback_url = input("\n🔁 Paste full callback URL here: ").strip()

    # Step 2: Exchange code for token
    token = oauth.fetch_token(
        TOKEN_URL,
        authorization_response=callback_url,
        client_secret=CLIENT_SECRET,
        include_client_id=True
    )

    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    with open(TOKEN_PATH, "w") as f:
        json.dump(token, f, indent=2)
    print(f"\n✅ Tokens saved to {TOKEN_PATH}")
    print(json.dumps(token, indent=2))

if __name__ == "__main__":
    main()
