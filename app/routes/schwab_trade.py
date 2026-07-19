from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path as FSPath, Path
from typing import Any, Dict, Optional, Literal

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database.connection import get_db
from app.routes.auth import get_current_user
from app.models.schwab_accounts import SchwabAccount
from app.utils.client_context import data_dir, public_base_url

log = logging.getLogger(__name__)

# --------------------------------------------------------------------
# Config
# --------------------------------------------------------------------
SCHWAB_TRADER_BASE = os.getenv("SCHWAB_TRADER_BASE", "https://api.schwabapi.com/trader/v1")
SCHWAB_ACCOUNTS_URL = f"{SCHWAB_TRADER_BASE}/accounts"
SCHWAB_ACCOUNT_NUMBERS_URL = f"{SCHWAB_TRADER_BASE}/accounts/accountNumbers"
REQ_TIMEOUT = 20  # seconds

PUBLIC_BASE_URL = os.getenv(
    "CLIENT_PUBLIC_BASE_URL",
    os.getenv("PUBLIC_BASE_URL", public_base_url())
).rstrip("/")

SCHWAB_LINK_START_URL = os.getenv(
    "SCHWAB_LINK_START_URL",
    f"{PUBLIC_BASE_URL}/broker/schwab/connect"
)

SCHWAB_RELINK_NEXT = "/broker/trading"

DATA_ROOT = str(data_dir())

# --------------------------------------------------------------------
# Routers & UI
# --------------------------------------------------------------------
trade_router = APIRouter(prefix="/trade", tags=["Schwab Trade"], dependencies=[Depends(get_current_user)])
ui_router = APIRouter(tags=["Schwab Trade UI"])

TEMPLATES_DIR = FSPath(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

@ui_router.get("/trade/ui", name="trade_ui")
def trade_ui(request: Request, current_user=Depends(get_current_user)):
    return templates.TemplateResponse("trade.html", {"request": request, "user": current_user})

# --------------------------------------------------------------------
# Link / Re-link (delete file token so OAuth writes a new one)
# --------------------------------------------------------------------
def _delete_file_token(user_id: int) -> bool:
    ok = True
    for p in [
        Path(DATA_ROOT) / str(user_id) / "schwab_trade_token.json",
        Path(DATA_ROOT) / str(user_id) / "trade_token.json",
    ]:
        try:
            p.unlink(missing_ok=True)
        except Exception as e:
            ok = False
            log.warning(f"[Link/Delete] Could not delete token file {p}: {e}")
    return ok

@ui_router.get("/schwab/connect")
def schwab_connect(next: str = SCHWAB_RELINK_NEXT, current_user=Depends(get_current_user)):
    _delete_file_token(current_user.id)
    if next.startswith("/"):
        next = f"{PUBLIC_BASE_URL}{next}"
    return RedirectResponse(
        url=f"{SCHWAB_LINK_START_URL}?next={requests.utils.quote(next)}",
        status_code=303,
    )

@ui_router.get("/schwab/link")
def schwab_link_alias(next: str = SCHWAB_RELINK_NEXT, current_user=Depends(get_current_user)):
    _delete_file_token(current_user.id)
    if next.startswith("/"):
        next = f"{PUBLIC_BASE_URL}{next}"
    return RedirectResponse(
        url=f"{SCHWAB_LINK_START_URL}?next={requests.utils.quote(next)}",
        status_code=303,
    )

# --------------------------------------------------------------------
# Token helpers (FILE-ONLY — Celery keeps it fresh)
# --------------------------------------------------------------------
def get_token_from_file(user_id: int) -> str | None:
    preferred = FSPath(DATA_ROOT) / str(user_id) / "schwab_trade_token.json"
    legacy = FSPath(DATA_ROOT) / str(user_id) / "trade_token.json"

    path = preferred if preferred.exists() else legacy

    if not path.exists():
        log.warning(f"[Token] Token file missing. Checked: {preferred} and {legacy}")
        return None

    try:
        with open(path, "r") as f:
            data = json.load(f)
            token = data.get("access_token")
            return token.replace(" ", "+") if token else None
    except Exception as e:
        log.error(f"[Token] Failed to read {path}: {e}")
        return None

def _bearer_headers(user_id: int) -> Dict[str, str]:
    token = get_token_from_file(user_id)
    if not token:
        raise HTTPException(status_code=401, detail="No valid Schwab token file. Re-link account.")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

def _safe_json(resp: requests.Response) -> Any:
    try:
        if resp.text and "application/json" in (resp.headers.get("Content-Type") or ""):
            return resp.json()
    except Exception:
        pass
    return None


def _schwab_iso8601(dt: datetime) -> str:
    """Format datetimes the way Schwab's Trader API expects them."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_GET_CACHE: dict[tuple[Any, ...], tuple[float, Any]] = {}


def _cache_ttl(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _cached_get_json(
    cache_key: tuple[Any, ...],
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, str] | None = None,
    ttl: int,
) -> Any:
    now = time.time()
    cached = _GET_CACHE.get(cache_key)
    if cached and now - cached[0] <= ttl:
        return cached[1]

    resp = requests.get(url, headers=headers, params=params, timeout=REQ_TIMEOUT)
    if resp.status_code >= 400:
        log.error(
            "Schwab GET failed: status=%s url=%s params=%s body=%s",
            resp.status_code,
            url,
            params,
            (resp.text or "")[:1000],
        )
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    data = _safe_json(resp) or []
    _GET_CACHE[cache_key] = (now, data)
    return data


def _clear_trade_read_cache(user_id: int) -> None:
    prefix = (user_id,)
    for key in list(_GET_CACHE.keys()):
        if key[:1] == prefix:
            _GET_CACHE.pop(key, None)

# --------------------------------------------------------------------
# DTOs
# --------------------------------------------------------------------
class SubmitOrderRequest(BaseModel):
    symbol: str = Field(..., description="Ticker symbol, e.g. AAPL")
    side: Literal["BUY", "SELL"]
    order_type: Literal["MARKET", "LIMIT", "STOP", "STOP_LIMIT"]
    quantity: float = Field(..., gt=0)
    duration: Literal["DAY", "GOOD_TILL_CANCEL", "FILL_OR_KILL"] = "DAY"
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    asset_type: Literal["EQUITY", "OPTION"] = "EQUITY"

class ReplaceOrderRequest(SubmitOrderRequest):
    pass

def _build_order_payload(p: SubmitOrderRequest) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "orderType": p.order_type,
        "session": "NORMAL",
        "duration": p.duration,
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": p.side,
                "quantity": p.quantity,
                "instrument": {"symbol": p.symbol.upper(), "assetType": p.asset_type},
            }
        ],
    }
    if p.order_type in ("LIMIT", "STOP_LIMIT"):
        if p.limit_price is None:
            raise HTTPException(status_code=422, detail="limit_price required for LIMIT/STOP_LIMIT")
        payload["price"] = p.limit_price
    if p.order_type in ("STOP", "STOP_LIMIT"):
        if p.stop_price is None:
            raise HTTPException(status_code=422, detail="stop_price required for STOP/STOP_LIMIT")
        payload["stopPrice"] = p.stop_price
    return payload

# --------------------------------------------------------------------
# Accounts & helpers
# --------------------------------------------------------------------
@trade_router.post("/accounts/sync")
def sync_accounts(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    user_id = current_user.id
    headers = _bearer_headers(user_id)

    # (A) account numbers + hashes
    try:
        resp = requests.get(SCHWAB_ACCOUNT_NUMBERS_URL, headers=headers, timeout=REQ_TIMEOUT)
        resp.raise_for_status()
        acct_map = resp.json()
    except Exception as e:
        log.error(f"❌ Failed to get accountNumbers from Schwab: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch account numbers.")

    created, updated = 0, 0
    existing = {
        row.account_number: row
        for row in db.query(SchwabAccount).filter(SchwabAccount.user_id == user_id).all()
    }

    for item in acct_map:
        raw = str(item.get("accountNumber", "")).strip()
        hsh = str(item.get("hashValue", "")).strip()
        nick = item.get("displayName") or item.get("name") or "Schwab Account"
        if not raw or not hsh:
            continue

        if raw in existing:
            row = existing[raw]
            row.account_hash = hsh or row.account_hash
            row.nickname = nick or row.nickname
            row.last_synced_at = datetime.utcnow()
            db.add(row); updated += 1
        else:
            db.add(
                SchwabAccount(
                    user_id=user_id,
                    account_number=raw,
                    account_hash=hsh,
                    account_type=None,
                    nickname=nick,
                    is_default=False,
                    last_synced_at=datetime.utcnow(),
                )
            )
            created += 1

    db.commit()

    # (B) ensure one default
    has_default = (
        db.query(SchwabAccount)
        .filter(SchwabAccount.user_id == user_id, SchwabAccount.is_default.is_(True))
        .first()
    )
    if not has_default:
        first = (
            db.query(SchwabAccount)
            .filter(SchwabAccount.user_id == user_id)
            .order_by(SchwabAccount.id.asc())
            .first()
        )
        if first:
            first.is_default = True
            db.add(first); db.commit()

    return {"status": "ok", "created": created, "updated": updated, "accounts": len(acct_map)}

@trade_router.get("/accounts")
def list_accounts_db(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    rows = (
        db.query(SchwabAccount)
        .filter(SchwabAccount.user_id == current_user.id)
        .order_by(SchwabAccount.is_default.desc(), SchwabAccount.id.asc())
        .all()
    )

    return [
        {
            # UI compatibility:
            # account_number is display only in commercial.
            # account_hash is the real value used for Schwab API calls.
            "account_number": r.account_number_masked or "Linked Schwab Account",
            "account_hash": r.account_hash,
            "nickname": r.account_number_masked or r.account_type or "Schwab Account",
            "account_type": r.account_type,
            "is_default": r.is_default,
            "last_synced_at": getattr(r, "last_synced_at", None),
        }
        for r in rows
    ]

@trade_router.post("/accounts/{account_id}/default")
def set_default_account(account_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    rows = db.query(SchwabAccount).filter(SchwabAccount.user_id == current_user.id).all()
    if not rows:
        raise HTTPException(status_code=404, detail="No linked Schwab accounts.")

    found = None
    for r in rows:
        match = (
            r.account_hash == account_id
            or r.account_number_masked == account_id
        )
        r.is_default = bool(match)
        if match:
            found = r
        db.add(r)

    if not found:
        raise HTTPException(status_code=404, detail="Account not found for this user.")

    db.commit()
    return {"status": "ok", "default_account": found.account_hash}

def _resolve_account_hash(db: Session, user_id: int, account_or_hash: str) -> str:
    x = (account_or_hash or "").strip()
    if not x:
        raise HTTPException(status_code=400, detail="Missing Schwab account.")

    rows = (
        db.query(SchwabAccount)
        .filter(SchwabAccount.user_id == user_id)
        .all()
    )

    for r in rows:
        if r.account_hash == x or r.account_number_masked == x:
            if not r.account_hash:
                raise HTTPException(status_code=400, detail="Linked Schwab account has no hash. Run account sync.")
            return r.account_hash

    if len(rows) == 1 and rows[0].account_hash:
        return rows[0].account_hash

    raise HTTPException(status_code=400, detail="Unknown account. Run broker account sync.")


# --------------------------------------------------------------------
# Orders (per-account)
# --------------------------------------------------------------------
@trade_router.get("/accounts/{account_id}/orders")
def list_account_orders(
    account_id: str,
    status: Optional[str] = Query(None, description="Schwab order status"),
    fromEnteredTime: Optional[str] = Query(None, description="ISO-8601"),
    toEnteredTime: Optional[str] = Query(None, description="ISO-8601"),
    maxResults: Optional[int] = Query(3000, ge=1, le=3000),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    headers = _bearer_headers(current_user.id)
    acct_hash = _resolve_account_hash(db, current_user.id, account_id)
    url = f"{SCHWAB_TRADER_BASE}/accounts/{acct_hash}/orders"

    # Defaults: last 30 days if not set
    if not fromEnteredTime or not toEnteredTime:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=30)
        fromEnteredTime = _schwab_iso8601(start)
        toEnteredTime = _schwab_iso8601(end)

    params: Dict[str, str] = {
        "fromEnteredTime": fromEnteredTime,
        "toEnteredTime": toEnteredTime,
        "maxResults": str(maxResults or 3000),
    }
    if status:
        params["status"] = status

    return _cached_get_json(
        (current_user.id, "account_orders", acct_hash, tuple(sorted(params.items()))),
        url,
        headers=headers,
        params=params,
        ttl=max(60, _cache_ttl("SCHWAB_TRADE_ORDERS_CACHE_SECONDS", 60)),
    )

@trade_router.post("/accounts/{account_id}/orders")
def place_account_order(
    account_id: str,
    payload: SubmitOrderRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    headers = _bearer_headers(current_user.id)
    acct_hash = _resolve_account_hash(db, current_user.id, account_id)
    url = f"{SCHWAB_TRADER_BASE}/accounts/{acct_hash}/orders"
    body = _build_order_payload(payload)

    resp = requests.post(url, headers={**headers, "Content-Type": "application/json"}, json=body, timeout=REQ_TIMEOUT)
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    _clear_trade_read_cache(current_user.id)

    order_id = (resp.headers.get("Location") or resp.headers.get("location") or "").rstrip("/").split("/")[-1]
    return {"status": "submitted", "account_id": acct_hash, "order_id": order_id, "broker_response": _safe_json(resp) or {}}

@trade_router.get("/accounts/{account_id}/orders/{order_id}")
def get_account_order(account_id: str, order_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    headers = _bearer_headers(current_user.id)
    acct_hash = _resolve_account_hash(db, current_user.id, account_id)
    url = f"{SCHWAB_TRADER_BASE}/accounts/{acct_hash}/orders/{order_id}"
    resp = requests.get(url, headers=headers, timeout=REQ_TIMEOUT)
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return _safe_json(resp) or {}

@trade_router.delete("/accounts/{account_id}/orders/{order_id}")
def cancel_account_order(account_id: str, order_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    headers = _bearer_headers(current_user.id)
    acct_hash = _resolve_account_hash(db, current_user.id, account_id)
    url = f"{SCHWAB_TRADER_BASE}/accounts/{acct_hash}/orders/{order_id}"
    resp = requests.delete(url, headers=headers, timeout=REQ_TIMEOUT)
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    _clear_trade_read_cache(current_user.id)
    return {"status": "cancel_requested", "account_id": acct_hash, "order_id": order_id}

@trade_router.put("/accounts/{account_id}/orders/{order_id}")
def replace_account_order(
    account_id: str,
    order_id: str,
    payload: ReplaceOrderRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    headers = _bearer_headers(current_user.id)
    acct_hash = _resolve_account_hash(db, current_user.id, account_id)
    url = f"{SCHWAB_TRADER_BASE}/accounts/{acct_hash}/orders/{order_id}"
    body = _build_order_payload(payload)
    resp = requests.put(url, headers={**headers, "Content-Type": "application/json"}, json=body, timeout=REQ_TIMEOUT)
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    _clear_trade_read_cache(current_user.id)
    return {"status": "replaced", "account_id": acct_hash, "order_id": order_id, "broker_response": _safe_json(resp) or {}}

# --------------------------------------------------------------------
# Orders (all accounts)  — corrected params (status, fromEnteredTime, toEnteredTime, maxResults)
# --------------------------------------------------------------------
@trade_router.get("/orders")
def list_all_orders(
    status: Optional[str] = Query(None),
    fromEnteredTime: Optional[str] = Query(None),
    toEnteredTime: Optional[str] = Query(None),
    maxResults: Optional[int] = Query(3000, ge=1, le=3000),
    current_user=Depends(get_current_user),
):
    headers = _bearer_headers(current_user.id)
    url = f"{SCHWAB_TRADER_BASE}/orders"

    if not fromEnteredTime or not toEnteredTime:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=30)
        fromEnteredTime = _schwab_iso8601(start)
        toEnteredTime = _schwab_iso8601(end)

    params: Dict[str, str] = {
        "fromEnteredTime": fromEnteredTime,
        "toEnteredTime": toEnteredTime,
        "maxResults": str(maxResults or 3000),
    }
    if status:
        params["status"] = status

    return _cached_get_json(
        (current_user.id, "all_orders", tuple(sorted(params.items()))),
        url,
        headers=headers,
        params=params,
        ttl=max(60, _cache_ttl("SCHWAB_TRADE_ORDERS_CACHE_SECONDS", 60)),
    )

# --------------------------------------------------------------------
# Positions & Transactions (Trade History)
# --------------------------------------------------------------------
@trade_router.get("/accounts/{account_id}/positions")
def get_account_positions(
    account_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    headers = _bearer_headers(current_user.id)
    acct_hash = _resolve_account_hash(db, current_user.id, account_id)

    # Prefer per-account endpoint with ?fields=positions
    url_one = f"{SCHWAB_TRADER_BASE}/accounts/{acct_hash}"
    data = _cached_get_json(
        (current_user.id, "positions", acct_hash),
        url_one,
        headers=headers,
        params={"fields": "positions"},
        ttl=max(60, _cache_ttl("SCHWAB_TRADE_POSITIONS_CACHE_SECONDS", 60)),
    )
    accounts = data if isinstance(data, list) else [data]
    out = []
    for a in accounts:
        positions = (a.get('securitiesAccount') or {}).get('positions') or a.get('positions') or []
        out.extend(positions)
    return out

@trade_router.get("/accounts/{account_id}/history")
def get_trade_history(
    account_id: str,
    start: Optional[str] = Query(None, description="ISO8601 startDate"),
    end: Optional[str] = Query(None, description="ISO8601 endDate"),
    symbol: Optional[str] = Query(None, description="Optional symbol filter"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Trade history -> Schwab Transactions API:
      GET /accounts/{accountNumber}/transactions?types=TRADE&startDate=...&endDate=...&symbol=...
    """
    headers = _bearer_headers(current_user.id)
    acct_hash = _resolve_account_hash(db, current_user.id, account_id)
    url = f"{SCHWAB_TRADER_BASE}/accounts/{acct_hash}/transactions"

    # Defaults (Schwab requires both, max window 1 year)
    if not start or not end:
        end_dt = datetime.utcnow().replace(tzinfo=timezone.utc)
        start_dt = end_dt - timedelta(days=30)
        start = start_dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        end = end_dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    params: Dict[str, str] = {
        "types": "TRADE",
        "startDate": start,
        "endDate": end,
    }
    if symbol:
        params["symbol"] = symbol.strip().upper()

    return _cached_get_json(
        (current_user.id, "history", acct_hash, tuple(sorted(params.items()))),
        url,
        headers=headers,
        params=params,
        ttl=max(60, _cache_ttl("SCHWAB_TRADE_HISTORY_CACHE_SECONDS", 300)),
    )

# --------------------------------------------------------------------
# Unlink (delete file + clear account rows)
# --------------------------------------------------------------------
@trade_router.post("/unlink", status_code=status.HTTP_200_OK)
def unlink_schwab(
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    uid = current_user.id

    deleted_file = False
    for token_path in [
        Path(DATA_ROOT) / str(uid) / "schwab_trade_token.json",
        Path(DATA_ROOT) / str(uid) / "trade_token.json",
    ]:
        try:
            token_path.unlink(missing_ok=True)
            deleted_file = True
        except Exception as e:
            log.warning(f"[unlink] Could not delete token file {token_path}: {e}")

    acct_deleted = (
        db.query(SchwabAccount)
        .filter(SchwabAccount.user_id == uid)
        .delete(synchronize_session=False)
    )
    db.commit()

    log.info(f"[unlink] user={uid} token_file_deleted={deleted_file} accounts_deleted={acct_deleted}")

    return {
        "status": "ok",
        "deleted_token_file": deleted_file,
        "deleted_accounts": int(acct_deleted or 0),
    }
