# routes/schwab_login.py
from fastapi import APIRouter
import os
import base64
import hashlib
import urllib.parse
import secrets

router = APIRouter()

@router.get("/login_schwab")
def login_schwab():
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).decode().rstrip("=")

    auth_url = (
        "https://api.schwabapi.com/oauth2/authorize?"
        f"response_type=code&client_id={os.getenv('SCHWAB_CLIENT_ID')}"
        f"&redirect_uri={urllib.parse.quote(os.getenv('SCHWAB_REDIRECT_URI'))}"
        f"&code_challenge_method=S256&code_challenge={code_challenge}"
    )

    return {"auth_url": auth_url, "code_verifier": code_verifier}
