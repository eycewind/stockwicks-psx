import base64
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database.connection import get_db
from app.models.schwab import BrokerConnection, SchwabAccount
from app.models.user import User
from app.routes.auth import get_current_user
from app.utils.client_context import client_slug, data_dir, public_base_url

router = APIRouter(prefix="/broker", tags=["Broker"])
legacy_router = APIRouter(tags=["Broker Legacy Compatibility"])
templates = Jinja2Templates(directory="app/templates")
log = logging.getLogger("broker_schwab")

SCHWAB_BASE_URL = "https://api.schwabapi.com"
SCHWAB_AUTH_URL = f"{SCHWAB_BASE_URL}/v1/oauth/authorize"
SCHWAB_TOKEN_URL = f"{SCHWAB_BASE_URL}/v1/oauth/token"
SCHWAB_TRADER_BASE = "https://api.schwabapi.com/trader/v1"
SCHWAB_TOKEN_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
    "User-Agent": "StockWicks/1.0",
}


def _data_dir() -> Path:
    return data_dir()


def _plain_store_token(raw_token: str | None) -> str | None:
    # MVP/debug behavior: plain token storage, matching old production behavior.
    # Existing DB columns may still be named *_encrypted for now.
    return raw_token


def _unsanitize_token(s: str | None) -> str | None:
    return s.replace(" ", "+") if s else s


def _mask_account_number(account_number: str | None) -> str | None:
    if not account_number:
        return None
    digits = "".join(ch for ch in str(account_number) if ch.isdigit())
    if len(digits) >= 4:
        return f"XXXX-{digits[-4:]}"
    return "Linked"


def _get_connection(db: Session, user_id: int) -> BrokerConnection | None:
    return (
        db.query(BrokerConnection)
        .filter(BrokerConnection.user_id == user_id)
        .order_by(BrokerConnection.id.desc())
        .first()
    )


def _get_default_account(db: Session, user_id: int) -> SchwabAccount | None:
    return (
        db.query(SchwabAccount)
        .filter(SchwabAccount.user_id == user_id)
        .order_by(SchwabAccount.is_default.desc(), SchwabAccount.id.desc())
        .first()
    )


def _token_status(connection: BrokerConnection | None) -> str:
    if not connection:
        return "Not connected"
    if connection.reauth_required:
        return "Reconnect required"
    if connection.access_expires_at and connection.access_expires_at <= datetime.utcnow():
        return "Access token expired"
    if connection.connected:
        return "Connected"
    return "Not connected"


def _token_file_expires_at(user_id: int, api_kind: str) -> datetime | None:
    token_path = _data_dir() / str(user_id) / f"schwab_{api_kind}_token.json"
    if not token_path.exists():
        return None

    try:
        payload = json.loads(token_path.read_text())
        expires_in = int(payload.get("expires_in", 1800))
        issued_at = payload.get("token_time")

        if issued_at is not None:
            return datetime.utcfromtimestamp(int(float(issued_at))) + timedelta(seconds=expires_in)

        updated_at = payload.get("updated_at")
        if updated_at:
            return datetime.fromisoformat(str(updated_at).replace("Z", "+00:00")).replace(tzinfo=None) + timedelta(seconds=expires_in)
    except Exception as exc:
        log.warning("Could not read Schwab %s token expiry for user_id=%s: %s", api_kind, user_id, exc)

    return None


def _effective_access_expires_at(user_id: int, connection: BrokerConnection | None, api_kind: str = "trade") -> datetime | None:
    return _token_file_expires_at(user_id, api_kind) or (connection.access_expires_at if connection else None)


def _token_status_for_user(user_id: int, connection: BrokerConnection | None, api_kind: str = "trade") -> str:
    if not connection and not (_data_dir() / str(user_id) / f"schwab_{api_kind}_token.json").exists():
        return "Not connected"
    if connection and connection.reauth_required:
        return "Reconnect required"

    access_expires_at = _effective_access_expires_at(user_id, connection, api_kind)
    if access_expires_at and access_expires_at <= datetime.utcnow():
        return "Access token expired"
    if (_data_dir() / str(user_id) / f"schwab_{api_kind}_token.json").exists() or (connection and connection.connected):
        return "Connected"
    return "Not connected"


def _client_prefix(request: Request) -> str:
    return request.headers.get("x-forwarded-prefix", "").rstrip("/")


def _prefixed_url(request: Request, path: str) -> str:
    prefix = _client_prefix(request)
    if not path.startswith("/"):
        path = "/" + path
    return f"{prefix}{path}" if prefix else path


def _api_settings(api_kind: str) -> dict:
    """
    api_kind:
      - market
      - trade
    """
    kind = api_kind.lower().strip()

    if kind == "market":
        cfg = {
            "kind": "market",
            "label": "Market Data",
            "client_id": os.getenv("SCHWAB_MARKET_CLIENT_ID", "").strip(),
            "client_secret": os.getenv("SCHWAB_MARKET_CLIENT_SECRET", "").strip(),
            "redirect_uri": os.getenv("SCHWAB_MARKET_REDIRECT_URI", "").strip(),
            "scope": os.getenv("SCHWAB_MARKET_SCOPE", "").strip(),
        }
    elif kind == "trade":
        cfg = {
            "kind": "trade",
            "label": "Trading",
            "client_id": os.getenv("SCHWAB_TRADE_CLIENT_ID", "").strip(),
            "client_secret": os.getenv("SCHWAB_TRADE_CLIENT_SECRET", "").strip(),
            "redirect_uri": os.getenv("SCHWAB_TRADE_REDIRECT_URI", "").strip(),
            "scope": os.getenv("SCHWAB_TRADE_SCOPE", "api read_accounts read_trade").strip(),
        }
    else:
        raise HTTPException(status_code=400, detail=f"Unknown Schwab API kind: {api_kind}")

    missing = [
        name
        for name, value in {
            f"SCHWAB_{kind.upper()}_CLIENT_ID": cfg["client_id"],
            f"SCHWAB_{kind.upper()}_CLIENT_SECRET": cfg["client_secret"],
            f"SCHWAB_{kind.upper()}_REDIRECT_URI": cfg["redirect_uri"],
        }.items()
        if not value
    ]
    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"Broker setup missing env values: {', '.join(missing)}",
        )

    return cfg


def _build_auth_url(cfg: dict, state: str) -> str:
    auth_url = (
        f"{SCHWAB_AUTH_URL}?response_type=code"
        f"&client_id={cfg['client_id']}"
        f"&redirect_uri={quote(cfg['redirect_uri'], safe='')}"
    )
    if cfg.get("scope"):
        auth_url += f"&scope={quote(cfg['scope'], safe='')}"
    auth_url += f"&state={quote(state, safe='')}"
    return auth_url


def _schwab_token_post(headers: dict, data: dict, timeout: int = 30) -> requests.Response:
    merged_headers = dict(SCHWAB_TOKEN_HEADERS)
    merged_headers.update(headers)
    session = requests.Session()
    # Avoid inherited proxy env vars rewriting Schwab OAuth traffic in hosted deploys.
    session.trust_env = os.getenv("SCHWAB_TRUST_ENV_PROXIES", "0").strip().lower() in {"1", "true", "yes"}
    return session.post(SCHWAB_TOKEN_URL, headers=merged_headers, data=data, timeout=timeout)


def _schwab_error_preview(resp: requests.Response) -> str:
    content_type = resp.headers.get("content-type", "")
    body = resp.text[:500].replace("\n", " ").strip()
    if "text/html" in content_type.lower():
        return f"HTML response from Schwab edge: {body[:300]}"
    return body[:300]


def _exchange_code_for_token(cfg: dict, code: str) -> dict:
    basic = base64.b64encode(f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()
    headers = {
        "Authorization": f"Basic {basic}",
    }
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg["redirect_uri"],
    }

    resp = _schwab_token_post(headers=headers, data=data, timeout=30)

    # Production-compatible fallback: client_id/client_secret in form body.
    if resp.status_code in {400, 401}:
        first_error = _schwab_error_preview(resp)
        fallback_data = dict(data)
        fallback_data["client_id"] = cfg["client_id"]
        fallback_data["client_secret"] = cfg["client_secret"]
        fallback_resp = _schwab_token_post(headers={}, data=fallback_data, timeout=30)
        if fallback_resp.status_code < 400:
            resp = fallback_resp
        else:
            log.warning(
                "Schwab %s token exchange failed with Basic first: status=%s body=%s; body fallback status=%s body=%s",
                cfg["label"],
                resp.status_code,
                first_error,
                fallback_resp.status_code,
                _schwab_error_preview(fallback_resp),
            )

    if resp.status_code >= 400:
        raise HTTPException(
            status_code=400,
            detail=f"Schwab {cfg['label']} token exchange failed: {resp.status_code} {_schwab_error_preview(resp)}",
        )

    token_data = resp.json()
    token_data["access_token"] = _unsanitize_token(token_data.get("access_token"))
    token_data["refresh_token"] = _unsanitize_token(token_data.get("refresh_token"))
    token_data["token_time"] = int(time.time())
    return token_data


def _save_token_file(user_id: int, api_kind: str, token_data: dict):
    token_path = _data_dir() / str(user_id) / f"schwab_{api_kind}_token.json"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps(token_data, indent=2))
    log.info("Saved Schwab %s token file for user_id=%s at %s", api_kind, user_id, token_path)


def _user_from_oauth_session(request: Request, db: Session) -> User:
    user_id = request.session.get("schwab_oauth_user_id")
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Schwab callback lost login session. Start Schwab connection again from Broker Setup.",
        )
    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        raise HTTPException(status_code=401, detail="Schwab callback user not found.")
    return user



def _state_secret() -> str:
    return os.getenv("SECRET_KEY") or os.getenv("APP_SECRET_KEY") or os.getenv("JWT_SECRET_KEY") or "stockwicks-dev-secret"


def _make_oauth_state(api_kind: str, user_id: int) -> str:
    current_client_slug = client_slug()
    ts = str(int(time.time()))
    nonce = str(int(time.time() * 1000))
    payload = f"{current_client_slug}:{api_kind}:{user_id}:{ts}:{nonce}"
    sig = hmac.new(_state_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{payload}:{sig}"



def _parse_oauth_state(state: str | None) -> tuple[str, int]:
    if not state:
        raise HTTPException(status_code=400, detail="Missing Schwab OAuth state.")

    parts = state.split(":")
    expected_client_slug = client_slug()

    # New commercial dispatcher format:
    # client_slug:market:3:timestamp:nonce:signature
    if len(parts) == 6:
        client_slug, api_kind, user_id_raw, ts, nonce, sig = parts
        payload = f"{client_slug}:{api_kind}:{user_id_raw}:{ts}:{nonce}"

        if client_slug != expected_client_slug:
            raise HTTPException(status_code=400, detail="Invalid Schwab OAuth client.")

    # Old backward-compatible format:
    # market:3:timestamp:nonce:signature
    elif len(parts) == 5:
        api_kind, user_id_raw, ts, nonce, sig = parts
        payload = f"{api_kind}:{user_id_raw}:{ts}:{nonce}"

    else:
        raise HTTPException(status_code=400, detail="Invalid Schwab OAuth state format.")

    expected = hmac.new(
        _state_secret().encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()[:24]

    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=400, detail="Invalid Schwab OAuth state signature.")

    if api_kind not in {"market", "trade"}:
        raise HTTPException(status_code=400, detail="Invalid Schwab OAuth state kind.")

    return api_kind, int(user_id_raw)

def _public_client_url(path: str) -> str:
    base = public_base_url().rstrip("/")

    if not path.startswith("/"):
        path = "/" + path

    return base + path


def _start_oauth(request: Request, current_user: User, api_kind: str):
    cfg = _api_settings(api_kind)

    # Signed state carries api kind + user id so callback survives prefix/session issues.
    state = _make_oauth_state(cfg["kind"], current_user.id)

    request.session["schwab_oauth_state"] = state
    request.session["schwab_oauth_user_id"] = current_user.id
    request.session["schwab_oauth_kind"] = cfg["kind"]

    auth_url = _build_auth_url(cfg, state)

    masked_client = cfg["client_id"][:6] + "***" + cfg["client_id"][-4:] if len(cfg["client_id"]) > 10 else "***"
    safe_auth_url = auth_url.replace(cfg["client_id"], masked_client)
    log.info("Schwab %s authorize URL: %s", cfg["label"], safe_auth_url)

    return RedirectResponse(url=auth_url, status_code=303)


@router.get("/setup", response_class=HTMLResponse, name="broker_setup")
def broker_setup_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    connection = _get_connection(db, current_user.id)
    account = _get_default_account(db, current_user.id)

    market_file = _data_dir() / str(current_user.id) / "schwab_market_token.json"
    trade_file = _data_dir() / str(current_user.id) / "schwab_trade_token.json"
    trade_access_expires_at = _effective_access_expires_at(current_user.id, connection, "trade")
    trade_token_valid = bool(trade_file.exists() and trade_access_expires_at and trade_access_expires_at > datetime.utcnow())

    broker_status = {
        "connected": trade_token_valid or bool(connection and connection.connected and not connection.reauth_required),
        "trading_connected": trade_token_valid or bool(connection and connection.connected and not connection.reauth_required),
        "market_data_connected": market_file.exists(),
        "account_hash_exists": bool(connection and connection.account_hash_exists),
        "account_number_masked": account.account_number_masked if account else None,
        "account_type": account.account_type if account else None,
        "token_status": _token_status_for_user(current_user.id, connection, "trade"),
        "last_refresh_status": connection.last_refresh_status if connection else None,
        "last_refresh_error": connection.last_refresh_error if connection else None,
        "access_expires_at": trade_access_expires_at,
        "refresh_expires_at": connection.refresh_expires_at if connection else None,
        "message": "Connect Schwab Market Data and Schwab Trading one by one.",
    }

    return templates.TemplateResponse(
        request,
        "broker/setup.html",
        {
            "request": request,
            "user": current_user,
            "broker_status": broker_status,
        },
    )


@router.get("/schwab/connect", response_class=HTMLResponse, name="broker_connect")
def broker_connect_page(
    request: Request,
    current_user=Depends(get_current_user),
):
    return templates.TemplateResponse(
        request,
        "broker/schwab_connect.html",
        {
            "request": request,
            "user": current_user,
        },
    )


@router.get("/schwab/market/start", name="broker_schwab_market_start")
def broker_schwab_market_start(
    request: Request,
    current_user=Depends(get_current_user),
):
    return _start_oauth(request, current_user, "market")


@router.get("/schwab/trade/start", name="broker_schwab_trade_start")
def broker_schwab_trade_start(
    request: Request,
    current_user=Depends(get_current_user),
):
    return _start_oauth(request, current_user, "trade")


# Backward-compatible old start route defaults to trading.
@router.get("/schwab/start", include_in_schema=False)
def broker_schwab_start_old(
    request: Request,
    current_user=Depends(get_current_user),
):
    return _start_oauth(request, current_user, "trade")


@router.get("/connect", include_in_schema=False)
def broker_connect_old(
    request: Request,
    current_user=Depends(get_current_user),
):
    return RedirectResponse(url=_prefixed_url(request, "/broker/schwab/connect"), status_code=303)


@router.get("/callback", include_in_schema=False)
def broker_callback_old(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    db: Session = Depends(get_db),
):
    return broker_callback(request=request, code=code, state=state, db=db)



def _token_saved_response(api_kind: str) -> HTMLResponse:
    label = "Market Data" if api_kind == "market" else "Trading"
    dashboard_url = _public_client_url("/auth/dashboard")
    setup_url = _public_client_url("/broker/setup")

    html = f"""
    <!doctype html>
    <html>
      <head>
        <title>Schwab {label} Connected</title>
        <meta http-equiv="refresh" content="3;url={dashboard_url}">
        <style>
          body {{
            background:#07071f;
            color:white;
            font-family:Arial, sans-serif;
            padding:60px;
          }}
          .box {{
            max-width:720px;
            margin:80px auto;
            background:#17173a;
            padding:32px;
            border-radius:14px;
          }}
          a {{ color:#ff7300; }}
        </style>
      </head>
      <body>
        <div class="box">
          <h1>✅ Schwab {label} token saved</h1>
          <p>Your Schwab {label} connection was completed successfully.</p>
          <p>Redirecting to dashboard...</p>
          <p>
            <a href="{dashboard_url}">Go to Dashboard</a>
            &nbsp;|&nbsp;
            <a href="{setup_url}">Back to Broker Setup</a>
          </p>
        </div>
      </body>
    </html>
    """
    return HTMLResponse(html)


@router.get("/schwab/callback", name="broker_callback")
@legacy_router.get("/auth/callback", include_in_schema=False)
@legacy_router.get("/auth/schwab/callback", include_in_schema=False)
@legacy_router.get("/auth/schwab/db/callback", include_in_schema=False)
def broker_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    db: Session = Depends(get_db),
):
    if not code:
        raise HTTPException(status_code=400, detail="Missing Schwab authorization code.")

    expected_state = request.session.get("schwab_oauth_state")
    if expected_state and state != expected_state:
        raise HTTPException(status_code=400, detail="Invalid Schwab OAuth state.")

    # Prefer signed state. Session is only a bonus now.
    api_kind, state_user_id = _parse_oauth_state(state)
    current_user = db.query(User).filter(User.id == state_user_id).first()
    if not current_user:
        raise HTTPException(status_code=401, detail="Schwab callback user not found.")

    request.session.pop("schwab_oauth_state", None)
    request.session.pop("schwab_oauth_user_id", None)
    request.session.pop("schwab_oauth_kind", None)

    cfg = _api_settings(api_kind)
    token_data = _exchange_code_for_token(cfg, code)
    _save_token_file(current_user.id, api_kind, token_data)

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    expires_in = int(token_data.get("expires_in", 1800))
    refresh_expires_in = int(token_data.get("refresh_token_expires_in", 7 * 24 * 3600))

    access_expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
    refresh_expires_at = datetime.utcnow() + timedelta(seconds=refresh_expires_in)

    # Market-data connection only needs token file for now.
    if api_kind == "market":
        connection = _get_connection(db, current_user.id)
        if not connection:
            connection = BrokerConnection(user_id=current_user.id, broker_name="Schwab")
            db.add(connection)

        connection.last_refresh_status = "market_oauth_connected"
        connection.last_refresh_error = None
        db.commit()
        return _token_saved_response(api_kind)

    # Trading connection saves broker connection and account hash.
    account_hash = None
    account_number_masked = None
    account_type = None

    acct_resp = requests.get(
        f"{SCHWAB_TRADER_BASE}/accounts/accountNumbers",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=30,
    )

    if acct_resp.status_code == 200:
        accounts = acct_resp.json()
        if isinstance(accounts, list) and accounts:
            first = accounts[0]
            account_hash = first.get("hashValue") or first.get("accountHash")
            account_number_masked = _mask_account_number(first.get("accountNumber"))
            account_type = first.get("accountType")
    else:
        log.warning("Schwab trading accountNumbers failed: %s %s", acct_resp.status_code, acct_resp.text[:300])

    connection = _get_connection(db, current_user.id)
    if not connection:
        connection = BrokerConnection(user_id=current_user.id, broker_name="Schwab")
        db.add(connection)

    connection.connected = True
    connection.access_token_encrypted = _plain_store_token(access_token)
    connection.refresh_token_encrypted = _plain_store_token(refresh_token)
    connection.access_expires_at = access_expires_at
    connection.refresh_expires_at = refresh_expires_at
    connection.account_hash_exists = bool(account_hash)
    connection.reauth_required = False
    connection.last_refresh_status = "trade_oauth_connected"
    connection.last_refresh_error = None

    if account_hash:
        existing_account = (
            db.query(SchwabAccount)
            .filter(SchwabAccount.user_id == current_user.id)
            .filter(SchwabAccount.account_hash == account_hash)
            .first()
        )
        if not existing_account:
            existing_account = SchwabAccount(user_id=current_user.id)
            db.add(existing_account)

        existing_account.account_hash = account_hash
        existing_account.account_number_masked = account_number_masked
        existing_account.account_type = account_type
        existing_account.is_default = True

    db.commit()
    return _token_saved_response(api_kind)


@router.post("/disconnect", name="broker_disconnect")
def broker_disconnect(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    db.query(SchwabAccount).filter(SchwabAccount.user_id == current_user.id).delete()
    for token_path in [
        _data_dir() / str(current_user.id) / "schwab_trade_token.json",
        _data_dir() / str(current_user.id) / "trade_token.json",
    ]:
        try:
            token_path.unlink(missing_ok=True)
        except Exception as exc:
            log.warning("Could not delete Schwab trade token file %s: %s", token_path, exc)

    connection = _get_connection(db, current_user.id)
    if connection:
        connection.connected = False
        connection.access_token_encrypted = None
        connection.refresh_token_encrypted = None
        connection.access_expires_at = None
        connection.refresh_expires_at = None
        connection.account_hash_exists = False
        connection.reauth_required = True
        connection.last_refresh_status = "disconnected"
        connection.last_refresh_error = None

    db.commit()
    return RedirectResponse(url=_prefixed_url(request, "/broker/setup?disconnected=1"), status_code=303)




def _trade_token_path_for_user(user_id: int) -> Path:
    return _data_dir() / str(user_id) / "schwab_trade_token.json"


def _read_trade_token_payload(user_id: int) -> dict:
    path = _trade_token_path_for_user(user_id)
    if not path.exists():
        raise HTTPException(status_code=400, detail=f"Trading token file missing: {path}")

    try:
        return json.loads(path.read_text())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read trading token file: {exc}")


def _write_trade_token_payload(user_id: int, payload: dict) -> None:
    path = _trade_token_path_for_user(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def _refresh_trade_token_for_user(user_id: int) -> str:
    """
    Refresh commercial Schwab Trading access token using refresh_token.
    Saves the updated token file and returns fresh access_token.
    """
    payload = _read_trade_token_payload(user_id)

    refresh_token = _unsanitize_token(payload.get("refresh_token"))
    if not refresh_token:
        raise HTTPException(status_code=400, detail="Trading refresh token missing. Reconnect Schwab Trading.")

    cfg = _api_settings("trade")
    basic = base64.b64encode(f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()

    headers = {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    resp = _schwab_token_post(headers=headers, data=data, timeout=30)

    if resp.status_code >= 400:
        log.error("Schwab trade token refresh failed: status=%s body=%s", resp.status_code, _schwab_error_preview(resp))
        raise HTTPException(status_code=400, detail="Schwab Trading token refresh failed. Reconnect Schwab Trading.")

    new_payload = resp.json()

    # Schwab may not return a new refresh_token every time. Preserve the old one if missing.
    if not new_payload.get("refresh_token"):
        new_payload["refresh_token"] = refresh_token

    new_payload["token_time"] = int(time.time())
    new_payload["updated_at"] = datetime.utcnow().isoformat()

    _write_trade_token_payload(user_id, new_payload)

    access_token = _unsanitize_token(new_payload.get("access_token"))
    if not access_token:
        raise HTTPException(status_code=400, detail="Schwab Trading token refresh returned no access token.")

    log.info("Refreshed Schwab trade token for user_id=%s", user_id)
    return access_token


def _get_fresh_trade_access_token(user_id: int) -> str:
    payload = _read_trade_token_payload(user_id)
    token = _unsanitize_token(payload.get("access_token"))
    if not token:
        return _refresh_trade_token_for_user(user_id)
    return token


def _schwab_get_account_numbers_with_refresh(user_id: int):
    """
    Call accountNumbers using current token. If Schwab says token expired/invalid, refresh once and retry.
    """
    token = _get_fresh_trade_access_token(user_id)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    url = f"{SCHWAB_TRADER_BASE}/accounts/accountNumbers"
    resp = requests.get(url, headers=headers, timeout=20)

    if resp.status_code == 401:
        log.warning("Schwab accountNumbers returned 401 for user_id=%s; refreshing trade token and retrying once", user_id)
        token = _refresh_trade_token_for_user(user_id)
        headers["Authorization"] = f"Bearer {token}"
        resp = requests.get(url, headers=headers, timeout=20)

    return resp


def _load_trade_token_for_user(user_id: int) -> str:
    """
    Commercial token lookup. Prefer the commercial token filename created by /broker OAuth.
    """
    user_dir = _data_dir() / str(user_id)
    candidates = [
        user_dir / "schwab_trade_token.json",
        user_dir / "trade_token.json",  # legacy fallback only
    ]

    for path in candidates:
        if not path.exists():
            continue

        try:
            data = json.loads(path.read_text())
            token = _unsanitize_token(data.get("access_token"))
            if token:
                return token
        except Exception as exc:
            log.warning("Failed reading Schwab trade token file %s: %s", path, exc)

    checked = ", ".join(str(p) for p in candidates)
    raise HTTPException(
        status_code=400,
        detail=f"No valid Schwab trading token found. Checked: {checked}",
    )


def _model_columns(model) -> set[str]:
    return {c.name for c in model.__table__.columns}


def _set_if_column(obj, columns: set[str], name: str, value):
    if name in columns:
        setattr(obj, name, value)


@router.post("/schwab/sync-accounts")
def broker_sync_schwab_accounts(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Clean commercial account sync:
    - reads /data/{user_id}/schwab_trade_token.json
    - calls Schwab /trader/v1/accounts/accountNumbers
    - upserts SchwabAccount rows in this client's DB
    - redirects back to broker setup
    """
    try:
        resp = _schwab_get_account_numbers_with_refresh(current_user.id)
        if resp.status_code >= 400:
            log.error("Schwab accountNumbers failed after refresh: status=%s body=%s", resp.status_code, resp.text[:500])
            raise HTTPException(status_code=400, detail="Failed to fetch Schwab account numbers.")
        acct_map = resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Failed to call Schwab accountNumbers: %s", exc)
        raise HTTPException(status_code=400, detail="Failed to fetch Schwab account numbers.")

    if not isinstance(acct_map, list):
        log.error("Unexpected Schwab accountNumbers response: %r", acct_map)
        raise HTTPException(status_code=400, detail="Unexpected Schwab account response.")

    columns = _model_columns(SchwabAccount)
    created = 0
    updated = 0

    for item in acct_map:
        raw = str(item.get("accountNumber") or "").strip()
        hsh = str(item.get("hashValue") or "").strip()
        nick = item.get("displayName") or item.get("name") or "Schwab Account"

        if not raw or not hsh:
            continue

        q = db.query(SchwabAccount).filter(SchwabAccount.user_id == current_user.id)

        if "account_number" in columns:
            existing = q.filter(SchwabAccount.account_number == raw).first()
        elif "account_hash" in columns:
            existing = q.filter(SchwabAccount.account_hash == hsh).first()
        else:
            existing = None

        if existing:
            row = existing
            updated += 1
        else:
            row = SchwabAccount(user_id=current_user.id)
            created += 1

        _set_if_column(row, columns, "account_number", raw)
        _set_if_column(row, columns, "account_hash", hsh)
        _set_if_column(row, columns, "nickname", nick)
        _set_if_column(row, columns, "account_type", item.get("type"))
        _set_if_column(row, columns, "account_number_masked", _mask_account_number(raw))
        _set_if_column(row, columns, "last_synced_at", datetime.utcnow())

        db.add(row)

    db.commit()

    # Ensure one default account if the model supports is_default.
    if "is_default" in columns:
        has_default = (
            db.query(SchwabAccount)
            .filter(SchwabAccount.user_id == current_user.id, SchwabAccount.is_default.is_(True))
            .first()
        )

        if not has_default:
            first = (
                db.query(SchwabAccount)
                .filter(SchwabAccount.user_id == current_user.id)
                .order_by(SchwabAccount.id.asc())
                .first()
            )
            if first:
                first.is_default = True
                db.add(first)
                db.commit()

    # Update broker connection status if present.
    connection = _get_connection(db, current_user.id)
    if connection:
        connection.account_hash_exists = True
        connection.last_refresh_status = f"accounts_synced created={created} updated={updated}"
        connection.last_refresh_error = None
        db.add(connection)
        db.commit()

    log.info(
        "Synced Schwab accounts for user_id=%s created=%s updated=%s total_response=%s",
        current_user.id,
        created,
        updated,
        len(acct_map),
    )

    # Return an app-local path only. NGINX proxy_redirect will add the client prefix.
    # Do NOT use _prefixed_url() here or the browser gets duplicate client prefixes.
    return RedirectResponse(
        url=f"/broker/setup?accounts_synced=1&created={created}&updated={updated}",
        status_code=303,
    )


@router.get("/trading", response_class=HTMLResponse, name="broker_trading")
def broker_trading_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Commercial Schwab trading workspace.
    Reuses existing trade.html UI for MVP.
    """
    connection = _get_connection(db, current_user.id)
    account = _get_default_account(db, current_user.id)
    trade_file = _data_dir() / str(current_user.id) / "schwab_trade_token.json"
    trade_access_expires_at = _effective_access_expires_at(current_user.id, connection, "trade")
    trade_token_valid = bool(trade_file.exists() and trade_access_expires_at and trade_access_expires_at > datetime.utcnow())

    broker_status = {
        "connected": trade_token_valid or bool(connection and connection.connected and not connection.reauth_required),
        "broker_name": connection.broker_name if connection else "Schwab",
        "account_hash_exists": bool(account and account.account_hash),
        "account_number_masked": account.account_number_masked if account else None,
        "trading_connected": trade_file.exists(),
        "market_data_connected": (_data_dir() / str(current_user.id) / "schwab_market_token.json").exists(),
    }

    return templates.TemplateResponse(
        request,
        "trade.html",
        {
            "request": request,
            "user": current_user,
            "broker_status": broker_status,
            "url_prefix": _client_prefix(request),
        },
    )


@router.get("/status", name="broker_status")
def broker_status(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    connection = _get_connection(db, current_user.id)
    account = _get_default_account(db, current_user.id)

    market_file = _data_dir() / str(current_user.id) / "schwab_market_token.json"
    trade_file = _data_dir() / str(current_user.id) / "schwab_trade_token.json"
    trade_access_expires_at = _effective_access_expires_at(current_user.id, connection, "trade")
    trade_token_valid = bool(trade_file.exists() and trade_access_expires_at and trade_access_expires_at > datetime.utcnow())

    return {
        "market_data_connected": market_file.exists(),
        "trading_connected": trade_file.exists(),
        "connected": trade_token_valid or bool(connection and connection.connected and not connection.reauth_required),
        "broker_name": connection.broker_name if connection else "Schwab",
        "account_hash_exists": bool(connection and connection.account_hash_exists),
        "account_number_masked": account.account_number_masked if account else None,
        "access_token_valid": bool(trade_access_expires_at and trade_access_expires_at > datetime.utcnow()),
        "refresh_token_valid": bool(
            connection
            and connection.refresh_expires_at
            and connection.refresh_expires_at > datetime.utcnow()
        ),
        "access_expires_at": trade_access_expires_at,
        "refresh_expires_at": connection.refresh_expires_at if connection else None,
        "reauth_required": bool(connection and connection.reauth_required),
        "last_refresh_status": connection.last_refresh_status if connection else None,
        "last_refresh_error": connection.last_refresh_error if connection else None,
    }
