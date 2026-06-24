# app/utils/schwab_trade.py
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from sqlalchemy.orm import Session

from app.database.connection import SessionLocal
from app.models.schwab_accounts import SchwabAccount
from app.utils.client_context import data_dir

log = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Config / Env
# -----------------------------------------------------------------------------
SCHWAB_TRADER_BASE = os.getenv("SCHWAB_TRADER_BASE", "https://api.schwabapi.com/trader/v1")
REQ_TIMEOUT = int(os.getenv("SCHWAB_HTTP_TIMEOUT", "20"))

# DRY RUN: if on, we don't call the broker; we fabricate an id.
_DRY_RUN = os.getenv("SCHWAB_DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"}

# -----------------------------------------------------------------------------
# Errors
# -----------------------------------------------------------------------------
class SchwabOrderError(RuntimeError):
    """Raised when Schwab submission fails."""

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _unsanitize_token(s: Optional[str]) -> Optional[str]:
    return s.replace(" ", "+") if s else s

def _load_user_trade_token(user_id: int) -> Optional[str]:
    """
    Load the most recent trader access_token saved by /auth/schwab/db flow.
    """
    token_path = data_dir() / str(user_id) / "schwab_trade_token.json"
    if not token_path.exists():
        log.error("[SCHWAB] Token file missing for user %s: %s", user_id, token_path)
        return None
    try:
        data = json.loads(token_path.read_text())
        tok = _unsanitize_token(data.get("access_token"))
        if not tok:
            log.error("[SCHWAB] No access_token in %s", token_path)
        return tok
    except Exception as e:
        log.exception("[SCHWAB] Failed reading token file %s: %s", token_path, e)
        return None

def _looks_like_hash(x: str) -> bool:
    # Schwab "accountNumbers" hashValue is long hex; we loosely detect it.
    return bool(re.fullmatch(r"[A-Fa-f0-9]{32,80}", x.replace("-", "")))

@dataclass
class _AcctRow:
    user_id: int
    account_number: str
    account_hash: str

def _resolve_acct_row(db: Session, account_id: str) -> Optional[_AcctRow]:
    """
    Commercial schema stores Schwab hash in schwab_accounts.account_hash.
    Accept either account_hash or masked account display value.
    """
    x = (account_id or "").strip()
    if not x:
        return None

    row = None

    if hasattr(SchwabAccount, "account_hash"):
        row = (
            db.query(SchwabAccount)
            .filter(SchwabAccount.account_hash == x)
            .first()
        )

    if row is None and hasattr(SchwabAccount, "account_number_masked"):
        row = (
            db.query(SchwabAccount)
            .filter(SchwabAccount.account_number_masked == x)
            .first()
        )

    if row is None and hasattr(SchwabAccount, "account_number"):
        clean_x = x.replace("-", "").strip()
        row = (
            db.query(SchwabAccount)
            .filter(
                (SchwabAccount.account_number == x)
                | (SchwabAccount.account_number == clean_x)
            )
            .first()
        )

    if not row:
        log.error("[SCHWAB] Unknown account id/hash: %s", account_id)
        return None

    account_hash = getattr(row, "account_hash", None)
    if not account_hash:
        account_display = (
            getattr(row, "account_number_masked", None)
            or getattr(row, "account_number", None)
            or getattr(row, "account_id", None)
            or "unknown"
        )
        log.error("[SCHWAB] Account %s has no hashValue synced. Run /trade/accounts/sync.", account_display)
        return None

    return _AcctRow(
        user_id=row.user_id,
        account_number=(
            getattr(row, "account_number", None)
            or getattr(row, "account_number_masked", None)
            or account_hash
        ),
        account_hash=account_hash,
    )

def _build_order_payload(
    *,
    symbol: str,
    side: str,
    qty: float,
    order_type: str,
    limit_price: Optional[float],
    time_in_force: str,
    extended_hours: bool,
) -> Dict[str, Any]:
    """
    Build Schwab Trader API order JSON for /accounts/{hash}/orders
    """
    side_u = side.upper()
    otype_u = order_type.upper()
    tif_u = time_in_force.upper()
    session = "EXTENDED" if extended_hours else "NORMAL"

    payload: Dict[str, Any] = {
        "orderType": otype_u,
        "session": session,
        "duration": "GOOD_TILL_CANCEL" if tif_u in {"GTC"} else "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": side_u,  # BUY / SELL
                "quantity": float(qty),
                "instrument": {"symbol": symbol.upper(), "assetType": "EQUITY"},
            }
        ],
    }

    if otype_u in {"LIMIT", "STOP_LIMIT"}:
        if limit_price is None:
            raise SchwabOrderError("limit_price required for LIMIT/STOP_LIMIT")
        payload["price"] = float(limit_price)

    # Note: support for STOP/STOP_LIMIT can be added with stopPrice when needed.
    return payload

def _safe_json(resp: requests.Response) -> Any:
    try:
        if "application/json" in (resp.headers.get("Content-Type") or ""):
            return resp.json()
    except Exception:
        pass
    return None

# -----------------------------------------------------------------------------
# Real submission
# -----------------------------------------------------------------------------
def _real_submit(payload: Dict[str, Any]) -> str:
    """
    Execute HTTPS call to Schwab Trader API using the given payload.
    The payload MUST include:
      - 'account_id' (raw acct number or hash)
      - 'symbol','side','qty','order_type','limit_price','time_in_force','extended_hours'
    Returns Schwab order id (string).
    """
    # 1) Resolve DB row -> get user_id + account_hash
    db = SessionLocal()
    try:
        acct_row = _resolve_acct_row(db, str(payload["account_id"]))
        if not acct_row:
            raise SchwabOrderError("Unknown Schwab account id/hash. Run /trade/accounts/sync first.")

        # 2) Load user's trader access token
        token_user_id = int(payload.get("user_id") or acct_row.user_id)
        token = _load_user_trade_token(token_user_id)
        log.info("[SCHWAB] Using commercial trade token for user_id=%s", token_user_id)
        if not token:
            raise SchwabOrderError("No valid Schwab token for user. Re-link at /auth/schwab/db.")

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        # 3) Build order body
        body = _build_order_payload(
            symbol=payload["symbol"],
            side=payload["side"],
            qty=float(payload["qty"]),
            order_type=payload["order_type"],
            limit_price=payload.get("limit_price"),
            time_in_force=payload.get("time_in_force", "DAY"),
            extended_hours=bool(payload.get("extended_hours", False)),
        )

        url = f"{SCHWAB_TRADER_BASE}/accounts/{acct_row.account_hash}/orders"
        log.info("[SCHWAB] POST %s body=%s", url, json.dumps(body))

        resp = requests.post(url, headers=headers, json=body, timeout=REQ_TIMEOUT)

        if resp.status_code >= 400:
            # Bubble up Schwab JSON for debug
            raise SchwabOrderError(
                f"Schwab rejected order: {resp.status_code} {resp.text or _safe_json(resp)}"
            )

        # Schwab returns order id in Location header
        loc = resp.headers.get("Location") or resp.headers.get("location") or ""
        order_id = loc.rstrip("/").split("/")[-1] if loc else None
        if not order_id:
            # Fallback: some envs return a JSON body; try to extract
            data = _safe_json(resp) or {}
            order_id = data.get("orderId") or data.get("order_id") or None

        if not order_id:
            raise SchwabOrderError("Order placed but no order id returned by Schwab.")

        return str(order_id)

    finally:
        db.close()

# -----------------------------------------------------------------------------
# Public entrypoint (used by trade_service)
# -----------------------------------------------------------------------------
def submit_equity_order(
    *,
    account_id: str,
    symbol: str,
    side: str,
    qty: float,
    order_type: str = "MARKET",
    limit_price: Optional[float] = None,
    time_in_force: str = "DAY",
    extended_hours: bool = False,
    user_id: int | None = None,
) -> str:
    """
    Submit a live equity order to Schwab Trader API.

    Returns the Schwab order id string.
    """
    user_id = user_id or int(os.getenv("SCHWAB_REFRESH_USER_ID", "3"))

    payload = {
        "account_id": account_id,
        "symbol": symbol,
        "side": side,
        "qty": float(qty),
        "order_type": order_type,
        "limit_price": limit_price,
        "time_in_force": time_in_force,
        "extended_hours": bool(extended_hours),
        "user_id": user_id,
    }

    if _DRY_RUN:
        fake_id = f"SIM-{int(__import__('time').time() * 1000)}"
        log.warning("[SCHWAB] DRY_RUN enabled; simulated order_id=%s", fake_id)
        return fake_id

    return _real_submit(payload)
