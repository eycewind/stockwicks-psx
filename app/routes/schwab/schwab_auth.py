#/var/www/stockwicks/app/routes/schwab_auth.py
# /var/www/stockwicks/app/routes/schwab_auth.py
import json
import base64
import time
import logging
from pathlib import Path
from urllib.parse import quote
import requests
from fastapi import APIRouter, Request, Form, Depends, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime, timedelta

from app.database.connection import get_db
from app.models.schwab_tokens import SchwabToken
from app.routes.auth import get_current_user
from app.utils.schwab_db_token import get_valid_token

router = APIRouter()
log = logging.getLogger("schwab_auth")

# ---------- Config ----------
CLIENT_ID = "v99zYt7xLUTWcBXLfPgwsYOKXAgGEllN"
CLIENT_SECRET = "Z7CFceh6PpREoH1Q"
REDIRECT_URI = "https://www.stockwicks.com/clients/ashakil/auth/callback"

TRADE_CLIENT_ID = "kI9oDoNC4WNXzp7AJRpAAIvDoE9GxJGz"
TRADE_CLIENT_SECRET = "x2tV8ksOGGh9cUXA"
TRADE_REDIRECT_URI = "https://www.stockwicks.com/clients/ashakil/auth/schwab/db/callback"

BASE_URL = "https://api.schwabapi.com"
AUTH_URL = f"{BASE_URL}/v1/oauth/authorize"
TOKEN_URL = f"{BASE_URL}/v1/oauth/token"
TOKEN_PATH = "/var/www/stockwicks/data/schwab_token.json"

templates = Jinja2Templates(directory="app/templates")

# ---------- Token Helpers ----------
def _unsanitize_token(s: str | None) -> str | None:
    return s.replace(" ", "+") if s else s

def save_token(data: dict):
    Path(TOKEN_PATH).parent.mkdir(parents=True, exist_ok=True)
    data["access_token"] = _unsanitize_token(data.get("access_token"))
    data["refresh_token"] = _unsanitize_token(data.get("refresh_token"))
    with open(TOKEN_PATH, "w") as f:
        json.dump(data, f, indent=2)

def load_token() -> dict:
    with open(TOKEN_PATH, "r") as f:
        return json.load(f)

def token_expired(token_data: dict) -> bool:
    issued_at = token_data.get("token_time")
    expires_in = int(token_data.get("expires_in", 1800))
    return not issued_at or time.time() > (issued_at + expires_in - 120)

def try_refresh_token(token_data: dict) -> dict | None:
    refresh_token = _unsanitize_token(token_data.get("refresh_token"))
    if not refresh_token:
        return None

    b64 = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    headers = {"Authorization": f"Basic {b64}", "Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "redirect_uri": REDIRECT_URI,
    }

    try:
        resp = requests.post(TOKEN_URL, headers=headers, data=data, timeout=20)
        if resp.status_code >= 400:
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            data.update({"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
            resp = requests.post(TOKEN_URL, headers=headers, data=data, timeout=20)
        resp.raise_for_status()
        new_token = resp.json()
        new_token["access_token"] = _unsanitize_token(new_token.get("access_token"))
        new_token["refresh_token"] = _unsanitize_token(new_token.get("refresh_token"))
        new_token["token_time"] = int(time.time())
        save_token(new_token)
        log.info("✅ Schwab token refreshed")
        return new_token
    except Exception as e:
        log.error(f"❌ Token refresh error: {e}")
        return None

# ---------- Consumer OAuth Flow ----------
@router.get("/auth/schwab/start")
async def start_schwab_auth(request: Request):
    auth_url = (
        f"{AUTH_URL}?response_type=code"
        f"&client_id={CLIENT_ID}"
        f"&redirect_uri={quote(REDIRECT_URI)}"
        f"&state={int(time.time())}"
    )
    return JSONResponse(content={"auth_url": auth_url})

@router.get("/auth/callback")
async def schwab_callback(request: Request):
    code = request.query_params.get("code")
    if not code:
        return JSONResponse(status_code=400, content={"error": "Missing code"})

    try:
        b64 = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        headers = {"Authorization": f"Basic {b64}", "Content-Type": "application/x-www-form-urlencoded"}
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI}
        resp = requests.post(TOKEN_URL, headers=headers, data=data, timeout=20)
        resp.raise_for_status()
        token_data = resp.json()
        token_data["access_token"] = _unsanitize_token(token_data.get("access_token"))
        token_data["refresh_token"] = _unsanitize_token(token_data.get("refresh_token"))
        token_data["token_time"] = int(time.time())
        save_token(token_data)
        return HTMLResponse("<h3>✅ Schwab auth successful. Token saved.</h3>")
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

# ---------- Market Data Quote ----------
@router.get("/auth/schwab/quotes")
async def schwab_quotes_page(request: Request):
    return templates.TemplateResponse("auth/schwab_quotes.html", {"request": request})

@router.post("/auth/schwab/quotes")
async def schwab_get_quote(request: Request, symbol: str = Form(...)):
    try:
        tokens = load_token()
        if token_expired(tokens):
            tokens = try_refresh_token(tokens) or {}
        access_token = _unsanitize_token(tokens.get("access_token"))
        if not access_token:
            raise Exception("Missing token")
    except Exception as e:
        return templates.TemplateResponse("auth/schwab_quotes.html", {
            "request": request,
            "error": str(e),
            "symbol": symbol,
            "quote": None,
        })

    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    url = f"{BASE_URL}/marketdata/v1/quotes?symbols={symbol.upper()}"
    resp = requests.get(url, headers=headers, timeout=20)
    if resp.status_code == 401:
        return templates.TemplateResponse("auth/schwab_quotes.html", {
            "request": request,
            "error": "Token expired",
            "symbol": symbol,
            "quote": None
        })
    return templates.TemplateResponse("auth/schwab_quotes.html", {
        "request": request,
        "symbol": symbol,
        "quote": json.dumps(resp.json(), indent=2),
        "error": None
    })

# ---------- Trader OAuth Flow ----------
@router.get("/auth/schwab/db/start")
async def start_schwab_auth_db():
    scope = "api read_accounts read_trade"
    auth_url = (
        f"{AUTH_URL}?response_type=code"
        f"&client_id={TRADE_CLIENT_ID}"
        f"&redirect_uri={quote(TRADE_REDIRECT_URI)}"
        f"&scope={quote(scope)}"
        f"&state={int(time.time())}"
    )
    return JSONResponse(content={"auth_url": auth_url})

@router.get("/auth/schwab/db")
async def schwab_db_page(request: Request):
    return templates.TemplateResponse("auth/schwab_db.html", {"request": request})

@router.get("/auth/schwab/db/callback")
async def schwab_callback_db(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    code = request.query_params.get("code")
    if not code:
        return JSONResponse(status_code=400, content={"error": "Missing code"})

    try:
        b64 = base64.b64encode(f"{TRADE_CLIENT_ID}:{TRADE_CLIENT_SECRET}".encode()).decode()
        headers = {"Authorization": f"Basic {b64}", "Content-Type": "application/x-www-form-urlencoded"}
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": TRADE_REDIRECT_URI}
        resp = requests.post(TOKEN_URL, headers=headers, data=data, timeout=20)
        if resp.status_code >= 400:
            headers2 = {"Content-Type": "application/x-www-form-urlencoded"}
            data.update({"client_id": TRADE_CLIENT_ID, "client_secret": TRADE_CLIENT_SECRET})
            resp = requests.post(TOKEN_URL, headers=headers2, data=data, timeout=20)
        resp.raise_for_status()

        token_data = resp.json()
        access_token = _unsanitize_token(token_data.get("access_token"))
        refresh_token = _unsanitize_token(token_data.get("refresh_token"))
        expires_at = datetime.utcnow() + timedelta(seconds=int(token_data.get("expires_in", 1800)))

        log.info("✅ Received Schwab token exchange response")
        log.info(f"🔐 Access Token: {access_token}")
        log.info(f"🔁 Refresh Token: {refresh_token}")
        log.info(f"📦 Full token_data: {json.dumps(token_data, indent=2)}")

     # ✅ Save trade token to permanent user-specific location
        token_path = Path(f"/var/www/stockwicks/data/{current_user.id}/trade_token.json")
        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, "w") as f:
            json.dump(token_data, f, indent=2)
        log.info(f"📝 Saved trade token to: {token_path}")

        # Save to DB
        schwab_token = db.query(SchwabToken).filter_by(user_id=current_user.id).first()
        if schwab_token:
            schwab_token.access_token = access_token
            schwab_token.refresh_token = refresh_token
            schwab_token.expires_at = expires_at
            schwab_token.scope = token_data.get("scope")
            schwab_token.schwab_user_guid = token_data.get("schwab_user_guid")
        else:
            schwab_token = SchwabToken(
                user_id=current_user.id,
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=expires_at,
                scope=token_data.get("scope"),
                schwab_user_guid=token_data.get("schwab_user_guid"),
            )
            db.add(schwab_token)
        db.commit()

        return HTMLResponse("<h3>✅ Schwab Trade authentication successful. Token saved to DB.</h3>")

    except Exception as e:
        log.error(f"❌ Trader token exchange error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

# ---------- Accounts Test ----------
@router.get("/auth/schwab/db/accounts")
async def schwab_get_accounts(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    access_token = _unsanitize_token(get_valid_token(db, current_user.id))
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    resp = requests.get(f"{BASE_URL}/v1/accounts", headers=headers, timeout=20)
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()
