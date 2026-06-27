# app/routes/spx_0dte_trades.py
"""
SPX 0DTE Paper Trades Dashboard

URL:
  - GET  /options/spx-0dte-trades
  - POST /options/spx-0dte-trades/run-now
  - POST /options/spx-0dte-trades/close/{trade_id}
  - POST /options/spx-0dte-trades/alerts/subscribe
  - POST /options/spx-0dte-trades/alerts/unsubscribe

Shared dashboard + subscriber alerts:
- The SPX 0DTE bot still runs under one owner user_id (default 116)
- All logged-in users VIEW that shared owner's open trades / picks / history
- Each logged-in user can subscribe/unsubscribe their own email alerts
"""

import logging
import os
import json
from datetime import datetime
from datetime import timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.database.connection import get_db
from app.routes.auth import get_current_user
from app.models.user import User
from app.models.paper_spx_0dte import PaperSPXPick, PaperSPXOpenTrade, PaperSPXTradeHistory
from app.models.spx_0dte_alert_subscription import SPX0DTEAlertSubscription

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
log = logging.getLogger(__name__)

ET_TZ = ZoneInfo("America/New_York")
SPX0DTE_SHARED_USER_ID = int(os.getenv("SPX0DTE_SHARED_USER_ID", "116"))


def _spx_status_path(user_id: int) -> str:
    base_dir = os.getenv("STOCKWICKS_DATA_DIR", "/var/www/stockwicks/data")
    return os.path.join(base_dir, str(int(user_id)), "spx0dte_status.jsonl")


def _parse_status_ts(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _load_bot_status(user_id: int) -> dict:
    path = _spx_status_path(user_id)
    default = {
        "is_running": False,
        "status_reason": "No SPX bot status file yet.",
        "status_path": path,
        "age_seconds": None,
        "last": {},
        "last_runs": [],
    }

    if not os.path.exists(path):
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f.readlines() if line.strip()]
    except Exception as exc:
        log.warning("Failed reading SPX status file %s: %s", path, exc)
        default["status_reason"] = f"Could not read status file: {exc}"
        return default

    rows = []
    for line in lines[-5:]:
        try:
            row = json.loads(line)
        except Exception:
            continue
        ts = _parse_status_ts(row.get("ts_et"))
        if ts:
            row["ts_et_display"] = ts.strftime("%I:%M %p").lstrip("0")
        else:
            row["ts_et_display"] = row.get("ts_et") or ""
        rows.append(row)

    if not rows:
        default["status_reason"] = "Status file exists but has no readable rows."
        return default

    last = rows[-1]
    last_ts = _parse_status_ts(last.get("ts_et"))
    age_seconds = None
    if last_ts:
        now_et = datetime.now(ET_TZ)
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=ET_TZ)
        age_seconds = max(0.0, (now_et - last_ts.astimezone(ET_TZ)).total_seconds())

    return {
        "is_running": age_seconds is not None and age_seconds <= 600,
        "status_reason": last.get("reason") or last.get("status") or "Status loaded.",
        "status_path": path,
        "age_seconds": age_seconds,
        "last": last,
        "last_runs": list(reversed(rows)),
    }


def _utc_to_et(dt):
    if not dt:
        return None
    try:
        if getattr(dt, "tzinfo", None) is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ET_TZ)
    except Exception:
        return dt


def _fmt_dt(dt):
    if not dt:
        return "—"
    try:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(dt)


def _decorate_time_fields(open_trades, history_rows):
    for ot in open_trades or []:
        ot.opened_at_et = _fmt_dt(_utc_to_et(getattr(ot, "opened_at", None)))
    for h in history_rows or []:
        h.opened_at_et = _fmt_dt(_utc_to_et(getattr(h, "opened_at", None)))
        h.closed_at_et = _fmt_dt(_utc_to_et(getattr(h, "closed_at", None)))


def _pl_sums(hist_rows):
    net = 0.0
    wins = 0
    losses = 0
    for r in hist_rows:
        pnl = float(getattr(r, "pnl_usd", 0.0) or 0.0)
        net += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
    total = len(hist_rows)
    win_rate = (wins / (wins + losses) * 100.0) if (wins + losses) else 0.0
    return {
        "net": net,
        "wins": wins,
        "losses": losses,
        "total": total,
        "win_rate": win_rate,
    }


def _get_alert_subscription(db: Session, user_id: int):
    return (
        db.query(SPX0DTEAlertSubscription)
        .filter(SPX0DTEAlertSubscription.user_id == int(user_id))
        .first()
    )


@router.get("/options/spx-0dte-trades", name="spx_0dte_trades_dashboard")
def spx_0dte_trades_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    shared_user_id = SPX0DTE_SHARED_USER_ID

    open_trades = (
        db.query(PaperSPXOpenTrade)
        .filter_by(user_id=shared_user_id, status="OPEN")
        .order_by(PaperSPXOpenTrade.opened_at.desc())
        .all()
    )

    picks = (
        db.query(PaperSPXPick)
        .filter(PaperSPXPick.user_id == shared_user_id)
        .filter(
            ~PaperSPXPick.id.in_(
                select(PaperSPXOpenTrade.pick_id).where(
                    PaperSPXOpenTrade.user_id == shared_user_id
                )
            )
        )
        .filter(
            ~PaperSPXPick.id.in_(
                select(PaperSPXTradeHistory.pick_id).where(
                    PaperSPXTradeHistory.user_id == shared_user_id
                )
            )
        )
        .order_by(PaperSPXPick.id.desc())
        .limit(30)
        .all()
    )

    history = (
        db.query(PaperSPXTradeHistory)
        .filter_by(user_id=shared_user_id)
        .order_by(PaperSPXTradeHistory.id.desc())
        .limit(100)
        .all()
    )

    alert_subscription = _get_alert_subscription(db, user.id)
    alert_subscribed = bool(alert_subscription and alert_subscription.is_active)
    alert_email = (
        (alert_subscription.email or "").strip()
        if alert_subscription and getattr(alert_subscription, "email", None)
        else (getattr(user, "email", "") or "").strip()
    )
    alert_message = (request.query_params.get("alert") or "").strip().upper()

    _decorate_time_fields(open_trades, history)
    summary = _pl_sums(history)
    bot_status = _load_bot_status(shared_user_id)

    return templates.TemplateResponse(
        request,
        "spx_0dte_trades.html",
        {
            "request": request,
            "user": user,
            "open_trades": open_trades,
            "picks": picks,
            "history": history,
            "summary": summary,
            "bot_status": bot_status,
            "spx_shared_user_id": shared_user_id,
            "spx_alert_subscribed": alert_subscribed,
            "spx_alert_email": alert_email,
            "spx_alert_message": alert_message,
        },
    )


@router.post("/options/spx-0dte-trades/alerts/subscribe", name="spx_0dte_alerts_subscribe")
def spx_0dte_alerts_subscribe(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    email = (getattr(user, "email", "") or "").strip()
    if not email:
        return RedirectResponse(url="/options/spx-0dte-trades?alert=NO_EMAIL", status_code=302)

    sub = _get_alert_subscription(db, user.id)
    try:
        if sub:
            sub.email = email
            sub.is_active = True
        else:
            sub = SPX0DTEAlertSubscription(
                user_id=int(user.id),
                email=email,
                is_active=True,
            )
            db.add(sub)
        db.commit()
        return RedirectResponse(url="/options/spx-0dte-trades?alert=SUBSCRIBED", status_code=302)
    except Exception as e:
        db.rollback()
        log.exception("Failed to subscribe SPX 0DTE alerts: %s", e)
        return RedirectResponse(url="/options/spx-0dte-trades?alert=ERROR", status_code=302)


@router.post("/options/spx-0dte-trades/alerts/unsubscribe", name="spx_0dte_alerts_unsubscribe")
def spx_0dte_alerts_unsubscribe(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sub = _get_alert_subscription(db, user.id)
    try:
        if sub:
            sub.is_active = False
            db.commit()
        return RedirectResponse(url="/options/spx-0dte-trades?alert=UNSUBSCRIBED", status_code=302)
    except Exception as e:
        db.rollback()
        log.exception("Failed to unsubscribe SPX 0DTE alerts: %s", e)
        return RedirectResponse(url="/options/spx-0dte-trades?alert=ERROR", status_code=302)


@router.post("/options/spx-0dte-trades/run-now")
def spx_0dte_run_now(
    mode: str = Form("both"),
    max_risk: float = Form(100.0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        from app.tasks.spx0dte_tasks import run_spx0dte_tick
        run_spx0dte_tick.delay(SPX0DTE_SHARED_USER_ID)
    except Exception as e:
        log.exception("Failed to enqueue SPX run-now: %s", e)
        raise HTTPException(status_code=400, detail=str(e))

    return RedirectResponse(url="/options/spx-0dte-trades", status_code=302)


@router.post("/options/spx-0dte-trades/close/{trade_id}")
def spx_0dte_close_trade(
    trade_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ot = (
        db.query(PaperSPXOpenTrade)
        .filter_by(id=trade_id, user_id=SPX0DTE_SHARED_USER_ID, status="OPEN")
        .first()
    )
    if not ot:
        return JSONResponse({"ok": True, "alreadyClosed": True}, status_code=200)

    exit_price = float(ot.current_mark_price or ot.entry_price or 0.0)

    from app.scripts.options.spx_0dte_bot_runner import close_spx_trade

    try:
        close_spx_trade(
            db,
            ot,
            exit_price=exit_price,
            reason="MANUAL",
            details={"manual": True, "closed_from_shared_dashboard": True},
        )
        db.commit()
        return RedirectResponse(url="/options/spx-0dte-trades", status_code=302)
    except Exception as e:
        db.rollback()
        log.exception("Manual close failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
