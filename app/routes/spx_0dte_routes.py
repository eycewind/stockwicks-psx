"""
/var/www/stockwicks/app/routes/spx_0dte_routes.py

SPX 0DTE Guru page + manual close for SPX 0DTE paper trades.

Routes:
- GET  /options/spx-0dte          : form (date picker + mode) + shows user's open SPX trades
- POST /options/spx-0dte          : runs spx_guru_dashboard.py --json and renders result + open trades
- POST /options/spx-0dte/close    : manual close of a user's OPEN SPX trade

Templates commonly used:
- app/templates/spx_0dte.html        (guru form + results)
- app/templates/spx_0dte_bot.html    (bot dashboard)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.status import HTTP_303_SEE_OTHER

from app.database.connection import SessionLocal
from app.models.paper_spx_0dte import PaperSPXOpenTrade
from app.models.user import User
from app.routes.auth import get_current_user

# Reuse close helper + mark helper from runner
from app.scripts.options.spx_0dte_bot_runner import close_spx_trade, _get_mark_for_occ


templates = Jinja2Templates(directory="app/templates")
router = APIRouter()

SPX_GURU_SCRIPT = str(Path(__file__).resolve().parents[1] / "scripts" / "options" / "spx_guru_dashboard.py")


def _get_user_from_request(request: Request):
    """
    Best-effort user resolver (works with both request.state.user and session dicts).
    """
    u = getattr(request.state, "user", None)
    if u:
        return u

    sess = getattr(request, "session", None) or {}
    if isinstance(sess, dict):
        for key in ("user", "current_user"):
            if sess.get(key):
                return sess.get(key)

        for key in ("user_id", "uid", "email", "username"):
            if sess.get(key):
                return {
                    "id": sess.get("user_id") or sess.get("uid"),
                    "email": sess.get("email"),
                    "username": sess.get("username"),
                }
    return None


def _extract_user_id(user) -> int:
    if user is None:
        return 0
    if isinstance(user, int):
        return int(user)
    if isinstance(user, dict):
        uid = user.get("id") or user.get("user_id") or user.get("uid")
        try:
            return int(uid or 0)
        except Exception:
            return 0
    for attr in ("id", "user_id", "uid"):
        if hasattr(user, attr):
            try:
                return int(getattr(user, attr) or 0)
            except Exception:
                pass
    return 0


def require_user(request: Request) -> Optional[RedirectResponse]:
    """
    Return a redirect to login if not authenticated; otherwise None.
    """
    user = _get_user_from_request(request)
    if not user:
        try:
            login_url = request.url_for("login_page")
        except Exception:
            login_url = "/login"
        return RedirectResponse(url=str(login_url), status_code=HTTP_303_SEE_OTHER)
    return None


def _fetch_open_trades_for_user(db, user_id: int):
    q = (
        db.query(PaperSPXOpenTrade)
        .filter(PaperSPXOpenTrade.status == "OPEN")
        .order_by(PaperSPXOpenTrade.id.desc())
    )
    if user_id:
        q = q.filter(PaperSPXOpenTrade.user_id == int(user_id))
    return q.all()


def _safe_return_to(return_to: Optional[str], default_path: str = "/options/spx-0dte-bot") -> str:
    """
    Prevent open redirects. Only allow absolute *paths* like "/options/spx-0dte-bot?...".
    If missing/invalid, return default_path.
    """
    rt = (return_to or "").strip()
    if not rt:
        return default_path
    # Allow only same-site path (must start with "/"), block scheme and "//"
    if not rt.startswith("/"):
        return default_path
    if rt.startswith("//"):
        return default_path
    if "://" in rt:
        return default_path
    return rt


def run_spx_guru(
    date_str: str,
    mode: str,
    max_risk: float = 100.0,
    contracts: Optional[int] = None,
) -> Dict[str, Any]:
    script_path = Path(SPX_GURU_SCRIPT)
    if not script_path.exists():
        raise FileNotFoundError(f"SPX guru script not found at: {SPX_GURU_SCRIPT}")

    cmd = [
        sys.executable,
        str(script_path),
        "--json",
        "--date",
        date_str,
        "--mode",
        mode,
        "--max-risk",
        str(max_risk),
    ]
    if contracts is not None and int(contracts) > 0:
        cmd += ["--contracts", str(int(contracts))]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if proc.returncode != 0:
        # Some scripts still print valid json to stdout with error info—try it first.
        try:
            return json.loads(proc.stdout or "{}")
        except Exception:
            raise RuntimeError(
                "SPX guru script failed.\n"
                f"cmd: {' '.join(cmd)}\n"
                f"stdout: {proc.stdout[-2000:]}\n"
                f"stderr: {proc.stderr[-2000:]}\n"
            )

    try:
        return json.loads(proc.stdout)
    except Exception:
        raise RuntimeError(
            "Could not parse JSON from script output.\n"
            f"stdout:\n{proc.stdout[-2000:]}\n"
            f"stderr:\n{proc.stderr[-2000:]}"
        )


@router.get("/options/spx-0dte", response_class=HTMLResponse, name="spx_0dte_form")
async def spx_0dte_form(
    request: Request,
    user: User = Depends(get_current_user),
):
    user_id = _extract_user_id(user)

    db = SessionLocal()
    try:
        open_trades = _fetch_open_trades_for_user(db, user_id=user_id)
    finally:
        db.close()

    return templates.TemplateResponse(
        "spx_0dte.html",
        {
            "request": request,
            "user": user,
            "result": None,
            "error": None,
            "default_date": None,
            "default_mode": "both",
            "default_max_risk": 100,
            "default_contracts": "",
            "open_trades": open_trades,
            "bot_msg": None,
        },
    )


@router.post("/options/spx-0dte", response_class=HTMLResponse, name="spx_0dte_run")
async def spx_0dte_run(
    request: Request,
    date_input: str = Form(""),
    mode: str = Form("both"),
    max_risk: float = Form(100.0),
    contracts: str = Form(""),
    user: User = Depends(get_current_user),
):
    user_id = _extract_user_id(user)

    date_str = (date_input or "").strip() or "today"
    mode = (mode or "both").strip().lower()

    try:
        contracts_i = int(contracts) if str(contracts).strip() else None
    except Exception:
        contracts_i = None

    db = SessionLocal()
    try:
        open_trades = _fetch_open_trades_for_user(db, user_id=user_id)
    finally:
        db.close()

    try:
        result = run_spx_guru(
            date_str=date_str,
            mode=mode,
            max_risk=float(max_risk) if max_risk else 100.0,
            contracts=contracts_i,
        )
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(result.get("error"))

        return templates.TemplateResponse(
            "spx_0dte.html",
            {
                "request": request,
                "user": user,
                "result": result,
                "error": None,
                "default_date": date_input,
                "default_mode": mode,
                "default_max_risk": max_risk,
                "default_contracts": contracts,
                "open_trades": open_trades,
                "bot_msg": None,
            },
        )
    except Exception as e:
        return templates.TemplateResponse(
            "spx_0dte.html",
            {
                "request": request,
                "user": user,
                "result": None,
                "error": str(e),
                "default_date": date_input,
                "default_mode": mode,
                "default_max_risk": max_risk,
                "default_contracts": contracts,
                "open_trades": open_trades,
                "bot_msg": None,
            },
        )


@router.post("/options/spx-0dte/close", name="spx0dte_form_close_trade")
def spx0dte_form_close_trade(
    request: Request,
    trade_id: int = Form(...),
    return_to: str = Form(""),
    user: User = Depends(get_current_user),
):
    """
    Manual close button handler for OPEN trades.

    Fix: after closing, redirect back to the page that initiated the close
    using `return_to` (safe, same-site path only). Default is /options/spx-0dte-bot.
    """
    user_id = _extract_user_id(user)
    success_url = _safe_return_to(return_to, default_path="/options/spx-0dte-bot") + "?ok=MANUAL_CLOSED"
    fail_url = _safe_return_to(return_to, default_path="/options/spx-0dte-bot") + "?err=CLOSE_FAILED"

    db = SessionLocal()
    try:
        ot = (
            db.query(PaperSPXOpenTrade)
            .filter(PaperSPXOpenTrade.id == int(trade_id))
            .first()
        )
        if not ot:
            return RedirectResponse(
                url=_safe_return_to(return_to) + "?err=TRADE_NOT_FOUND",
                status_code=HTTP_303_SEE_OTHER,
            )

        if int(ot.user_id or 0) != int(user_id) or (ot.status or "").upper() != "OPEN":
            return RedirectResponse(
                url=_safe_return_to(return_to) + "?err=NOT_ALLOWED",
                status_code=HTTP_303_SEE_OTHER,
            )

        # Prefer cached mark if present
        exit_price = None
        if ot.current_mark_price is not None:
            try:
                exit_price = float(ot.current_mark_price)
            except Exception:
                exit_price = None

        # Otherwise fetch a mark
        if exit_price is None:
            expd = None
            try:
                expd = ot.expiration.date() if hasattr(ot.expiration, "date") else ot.expiration
            except Exception:
                expd = None

            m = _get_mark_for_occ(ot.occ_symbol, exp_date=expd)
            if m is not None:
                try:
                    exit_price = float(m)
                except Exception:
                    exit_price = None

        # Last resort fallback
        if exit_price is None:
            exit_price = float(ot.entry_price or 0.0)

        close_spx_trade(
            db,
            ot,
            exit_price=float(exit_price),
            reason="MANUAL",
            details={"manual": True, "exit_price": float(exit_price)},
        )
        db.commit()
        return RedirectResponse(url=success_url, status_code=HTTP_303_SEE_OTHER)

    except Exception:
        db.rollback()
        return RedirectResponse(url=fail_url, status_code=HTTP_303_SEE_OTHER)
    finally:
        db.close()
