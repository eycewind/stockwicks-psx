# app/routes/paper_trade_option_bot.py
from typing import Optional
from datetime import datetime
import json

from fastapi import APIRouter, Depends, HTTPException, Request, Form, Path
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from pydantic import BaseModel

# DB session dependency
try:
    from app.database.connection import get_db  # preferred
except Exception:
    from app.database.connection import SessionLocal
    def get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

# Models
from app.models.user import User
from app.models.paper_option_trading_bot import (
    PaperOptionTradeBot,
    PaperOptionBotOpenTrade,
    PaperOptionBotTradeHistory,
)

# Runner (sync fallback)
from app.scripts.options.option_bots_runner import run_option_bots_tick, manage_open_trades

# Celery tasks (optional)
try:
    from app.tasks.option_tasks import (
        run_all_bots,
        manage_all_open_trades,
        start_option_paper_bot_task,
        stop_option_paper_bot_task,
    )
    _HAS_CELERY = True
except Exception:
    _HAS_CELERY = False

# Pricing / close utils
from app.utils.options.options_pricing import mark_open_trade
from app.utils.options.option_trade_utils import close_option_trade


def pick_template(user: User, default_template: str, td_template: str) -> str:
    """Use TD template only for Schwab-enabled users."""
    return td_template if getattr(user, "schwab_allowed", "N") == "Y" else default_template

def user_can_mirror_to_schwab(user: User) -> bool:
    """Gate mirroring with BOTH profile permission + any additional flags you may have."""
    if not user:
        return False
    if getattr(user, "schwab_allowed", "N") != "Y":
        return False
    # Optional additional profile flags (supports multiple possible column names)
    for attr in ("mirror_to_schwab", "mirror_live", "mirror_trades_to_schwab", "schwab_mirror_enabled"):
        v = getattr(user, attr, None)
        if v in (True, "Y", "y", "1", 1, "true", "True"):
            return True
    # If you only use schwab_allowed as the gate, keep mirroring allowed:
    return True

templates = Jinja2Templates(directory="app/templates")
router = APIRouter()

# Pydantic model for trade update
class UpdateTradeParams(BaseModel):
    stop_loss: Optional[float] = None
    exit_target1: Optional[float] = None
    # exit_target2: Optional[float] = None


# =========================
# Helpers
# =========================
def _get_current_user(request: Request, db: Session) -> Optional[User]:
    user = getattr(request.state, "user", None)
    if user:
        return user

    try:
        uid = request.session.get("user_id") if hasattr(request, "session") else None
    except Exception:
        uid = None

    if uid:
        return db.get(User, uid)
    return None

def _require_user(request: Request, db: Session) -> User:
    user = _get_current_user(request, db)
    if not user or not getattr(user, "id", None):
        raise HTTPException(status_code=401, detail="Login required")
    return user

def _owns_bot_or_404(db: Session, user_id: int, bot_id: int) -> PaperOptionTradeBot:
    bot = db.get(PaperOptionTradeBot, bot_id)
    if not bot:
        raise HTTPException(404, "Bot not found")
    if bot.user_id != user_id:
        raise HTTPException(403, "Not allowed")
    return bot

def _as_float_or_none(v) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    try:
        return float(s)
    except Exception:
        return None

# =========================
# PAGE
# =========================
@router.get("/auth/papertradebot/options", name="paper_trade_option_bot_page")
def paper_trade_option_bot_page(request: Request, db: Session = Depends(get_db)):
    user = _get_current_user(request, db)

    if user and user.id:
        bots = (
            db.query(PaperOptionTradeBot)
            .filter(PaperOptionTradeBot.user_id == user.id)
            .order_by(PaperOptionTradeBot.id.asc())
            .all()
        )

        open_trades = (
            db.query(PaperOptionBotOpenTrade)
            .join(PaperOptionTradeBot, PaperOptionBotOpenTrade.bot_id == PaperOptionTradeBot.id)
            .filter(PaperOptionTradeBot.user_id == user.id)
            .order_by(PaperOptionBotOpenTrade.id.desc())
            .all()
        )

        closed_trades = (
            db.query(PaperOptionBotTradeHistory)
            .join(PaperOptionTradeBot, PaperOptionBotTradeHistory.bot_id == PaperOptionTradeBot.id)
            .filter(PaperOptionTradeBot.user_id == user.id)
            .order_by(PaperOptionBotTradeHistory.id.desc())
            .limit(500)
            .all()
        )
    else:
        bots, open_trades, closed_trades = [], [], []

    bot_account = type("Acct", (), {"current_balance": 10000000.00})()
    total_pl = 0.0
    pl_percent = 0.0

    template_name = pick_template(user, "paper_trade_option_bot.html", "td_paper_trade_option_bot.html")

    return templates.TemplateResponse(
        template_name,
        {
            "request": request,
            "user": user,
            "all_bots": bots,
            "bot_account": bot_account,
            "total_pl": total_pl,
            "pl_percent": pl_percent,
            "open_trades": open_trades,
            "closed_trades": closed_trades,
            "show_mirror_controls": (user is not None and user_can_mirror_to_schwab(user)),
        },
    )


@router.get("/auth/td_papertradebot/options", name="td_paper_trade_option_bot_page")
def td_paper_trade_option_bot_page(request: Request, db: Session = Depends(get_db)):
    """TD landing page for Schwab-enabled users (template starts with td_)."""
    user = _get_current_user(request, db)
    # If not Schwab-enabled, fall back to regular page
    if not user or getattr(user, "schwab_allowed", "N") != "Y":
        return RedirectResponse(url=request.url_for("paper_trade_option_bot_page"), status_code=302)

    # Reuse the same data builder as the normal page
    bots = (
        db.query(PaperOptionTradeBot)
        .filter(PaperOptionTradeBot.user_id == user.id)
        .order_by(PaperOptionTradeBot.id.asc())
        .all()
    )

    open_trades = (
        db.query(PaperOptionBotOpenTrade)
        .join(PaperOptionTradeBot, PaperOptionBotOpenTrade.bot_id == PaperOptionTradeBot.id)
        .filter(PaperOptionTradeBot.user_id == user.id)
        .order_by(PaperOptionBotOpenTrade.id.desc())
        .all()
    )

    closed_trades = (
        db.query(PaperOptionBotTradeHistory)
        .join(PaperOptionTradeBot, PaperOptionBotTradeHistory.bot_id == PaperOptionTradeBot.id)
        .filter(PaperOptionTradeBot.user_id == user.id)
        .order_by(PaperOptionBotTradeHistory.id.desc())
        .limit(500)
        .all()
    )

    bot_account = type("Acct", (), {"current_balance": 10000000.00})()
    total_pl = 0.0
    pl_percent = 0.0

    return templates.TemplateResponse(
        "td_paper_trade_option_bot.html",
        {
            "request": request,
            "user": user,
            "all_bots": bots,
            "bot_account": bot_account,
            "total_pl": total_pl,
            "pl_percent": pl_percent,
            "open_trades": open_trades,
            "closed_trades": closed_trades,
            "show_mirror_controls": True,
        },
    )


# =========================
# CREATE BOT (4exp only)
# =========================
@router.post("/auth/papertradebot/options/create", name="start_option_paper_bot")
def start_option_paper_bot(
    request: Request,
    symbol: str = Form(...),
    trade_size: Optional[str] = Form(None),
    style: str = Form("credit"),
    min_premium: str = Form("0.20"),
    max_premium: str = Form("100"),
    exit_target: Optional[str] = Form(None),
    max_allocation_usd: Optional[str] = Form(None),
    mirror_live: Optional[bool] = Form(False),
    notify_email: Optional[str] = Form(None),
    interval: str = Form("1min"),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)

    # Defaults (when user leaves blank)
    style = (style or "credit").lower().strip()
    if style not in ("credit", "debit"):
        style = "credit"

    min_p = _as_float_or_none(min_premium)
    max_p = _as_float_or_none(max_premium)
    if min_p is None:
        min_p = 0.20
    if max_p is None:
        max_p = 100.00
    # Exit/Allocation overrides (optional)
    exit_t = (exit_target or '').strip().lower() if exit_target is not None else ''
    max_alloc = _as_float_or_none(max_allocation_usd)

    algo_params = {
        "style": style,
        "min_premium": float(min_p),
        "max_premium": float(max_p),
        # NOTE: sizing overrides are OPTIONAL. If user leaves blank, algo defaults apply.
    }
    # Contracts override (highest priority). Blank => do NOT set, keep script defaults.
    if trade_size is not None and str(trade_size).strip() != "":
        try:
            ts = int(float(str(trade_size).strip()))
            if ts > 0:
                algo_params["contracts"] = ts
        except Exception:
            pass

    # Budget-based sizing (USD). If set, algo will cap contracts to this allocation.
    if max_alloc is not None and max_alloc > 0:
        algo_params["max_allocation_usd"] = float(max_alloc)

    # Exit target selection: "target1" or "target2" (blank => script/runner defaults)
    if exit_t in ("target1", "target_1", "t1", "1"):
        algo_params["exit_target"] = "target1"
    elif exit_t in ("target2", "target_2", "t2", "2"):
        algo_params["exit_target"] = "target2"
    if mirror_live:
        algo_params["mirror_live"] = True  # handled later (you said you'll do DB migration later)

    bot = PaperOptionTradeBot(
        user_id=user.id,
        symbol=symbol.upper().strip(),
        interval=interval,
        algo_name="guru_pick_4exp",
        notify_email=(notify_email == "yes"),
        is_active=True,
        status="RUNNING",
        trade_size=int(trade_size or 1),
        algo_params=algo_params,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(bot)
    db.commit()
    db.refresh(bot)

    # Kick scan
    if _HAS_CELERY:
        try:
            run_all_bots.delay(user.id)
        except TypeError:
            run_all_bots.delay()
        except Exception:
            pass
    else:
        try:
            run_option_bots_tick(user_id=user.id)
        except TypeError:
            run_option_bots_tick()

    accept = (request.headers.get("accept") or "").lower()
    is_ajax = (request.headers.get("x-requested-with") == "XMLHttpRequest") or ("application/json" in accept)
    if is_ajax:
        return JSONResponse({"ok": True, "bot_id": bot.id})

    return RedirectResponse(url=request.url_for("paper_trade_option_bot_page"), status_code=303)

# =========================
# START / STOP / RESTART / DELETE
# =========================
@router.post("/auth/papertradebot/options/{bot_id}/start")
def start_option_bot(bot_id: int, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    bot = _owns_bot_or_404(db, user.id, bot_id)

    if _HAS_CELERY:
        try:
            start_option_paper_bot_task.delay(bot_id)
            try:
                run_all_bots.delay(user.id)
            except TypeError:
                run_all_bots.delay()
        except Exception:
            pass
    else:
        bot.is_active = True
        bot.status = "RUNNING"
        db.commit()
        try:
            run_option_bots_tick(user_id=user.id)
        except TypeError:
            run_option_bots_tick()

    return JSONResponse({"ok": True, "bot_id": bot_id, "status": "RUNNING"})

@router.post("/auth/papertradebot/options/{bot_id}/stop")
def stop_option_bot(bot_id: int, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    bot = _owns_bot_or_404(db, user.id, bot_id)

    if _HAS_CELERY:
        try:
            stop_option_paper_bot_task.delay(bot_id)
        except Exception:
            pass
    else:
        bot.is_active = False
        bot.status = "STOPPED"
        db.commit()

    return JSONResponse({"ok": True, "bot_id": bot_id, "status": "STOPPED"})

@router.post("/auth/papertradebot/options/{bot_id}/restart")
def restart_option_bot(bot_id: int, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    bot = _owns_bot_or_404(db, user.id, bot_id)

    bot.pending_trade_details = None
    bot.is_active = True
    bot.status = "RUNNING"
    bot.updated_at = datetime.utcnow()
    db.commit()

    if _HAS_CELERY:
        try:
            run_all_bots.delay(user.id)
        except TypeError:
            run_all_bots.delay()
        except Exception:
            pass
    else:
        try:
            run_option_bots_tick(user_id=user.id)
        except TypeError:
            run_option_bots_tick()

    return JSONResponse({"ok": True, "bot_id": bot_id, "status": "RUNNING"})

@router.delete("/auth/papertradebot/options/{bot_id}")
def delete_option_bot(bot_id: int, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    bot = _owns_bot_or_404(db, user.id, bot_id)

    db.delete(bot)
    db.commit()
    return JSONResponse({"ok": True, "bot_id": bot_id})

# =========================
# Manual ticks
# =========================
@router.post("/auth/papertradebot/options/tick")
def tick_find_trades(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    if _HAS_CELERY:
        try:
            run_all_bots.delay(user.id)
            return JSONResponse({"ok": True, "mode": "celery"})
        except Exception:
            pass
    try:
        run_option_bots_tick(user_id=user.id)
        return JSONResponse({"ok": True, "mode": "sync"})
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/auth/papertradebot/options/manage")
def tick_manage_trades(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    if _HAS_CELERY:
        try:
            manage_all_open_trades.delay(user.id)
            return JSONResponse({"ok": True, "mode": "celery"})
        except Exception:
            pass
    try:
        manage_open_trades(user_id=user.id)
        return JSONResponse({"ok": True, "mode": "sync"})
    except Exception as e:
        raise HTTPException(500, str(e))

# =========================
# RESET (delete THIS user's option bots & trades)
# =========================
@router.post("/auth/papertradebot/options/reset", name="reset_paper_trade_account")
def reset_paper_trade_account(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    try:
        (
            db.query(PaperOptionBotOpenTrade)
            .join(PaperOptionTradeBot, PaperOptionBotOpenTrade.bot_id == PaperOptionTradeBot.id)
            .filter(PaperOptionTradeBot.user_id == user.id)
            .delete(synchronize_session=False)
        )
        (
            db.query(PaperOptionBotTradeHistory)
            .join(PaperOptionTradeBot, PaperOptionBotTradeHistory.bot_id == PaperOptionTradeBot.id)
            .filter(PaperOptionTradeBot.user_id == user.id)
            .delete(synchronize_session=False)
        )
        (
            db.query(PaperOptionTradeBot)
            .filter(PaperOptionTradeBot.user_id == user.id)
            .delete(synchronize_session=False)
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return RedirectResponse(url=request.url_for("paper_trade_option_bot_page"), status_code=303)

# =========================
# Close a single OPEN option trade
# =========================
@router.post("/auth/papertradebot/options/trades/{trade_id}/close")
def close_open_option_trade(
    request: Request,
    trade_id: int = Path(...),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)

    trade = (
        db.query(PaperOptionBotOpenTrade)
        .join(PaperOptionTradeBot, PaperOptionBotOpenTrade.bot_id == PaperOptionTradeBot.id)
        .filter(PaperOptionBotOpenTrade.id == trade_id, PaperOptionTradeBot.user_id == user.id)
        .first()
    )
    if not trade:
        raise HTTPException(404, "Open trade not found")

    try:
        mark = float(mark_open_trade(trade))
        if mark <= 0:
            raise ValueError("mark <= 0")
    except Exception:
        mark = float(getattr(trade, "entry_price", 0) or 0)
        if mark <= 0:
            raise HTTPException(400, "Cannot determine a valid mark price to close")

    try:
        result = close_option_trade(trade, mark)
        if not result or not result.get("hist_id"):
            raise HTTPException(500, "Close failed (no history id)")
    except Exception as e:
        raise HTTPException(500, f"Close failed: {e}")

    bot_id = result.get("bot_id")
    if bot_id:
        bot = db.get(PaperOptionTradeBot, bot_id)
        if bot:
            bot.status = "RUNNING - Scanning..."
            db.commit()

    return JSONResponse({"ok": True, "trade_id": trade_id, "mark": round(mark, 2)})

# =========================
# Update SL/Targets for a single OPEN option trade
# =========================
@router.post("/auth/papertradebot/options/trades/{trade_id}/update")
async def update_open_option_trade(
    request: Request,
    trade_id: int = Path(...),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)

    # Parse JSON body (because your frontend sends JSON)
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    stop_loss = payload.get("stop_loss", None)
    exit_target = payload.get("exit_target", None)
    # Backward/HTML compatibility: frontend may send "profit_target"
    if exit_target is None:
        exit_target = payload.get("profit_target", None)

    # Normalize blanks
    def _to_float_or_none(x):
        if x is None:
            return None
        try:
            s = str(x).strip()
            if s == "":
                return None
            return float(s)
        except Exception:
            return None

    stop_loss = _to_float_or_none(stop_loss)
    exit_target = _to_float_or_none(exit_target)

    trade = (
        db.query(PaperOptionBotOpenTrade)
        .join(PaperOptionTradeBot, PaperOptionBotOpenTrade.bot_id == PaperOptionTradeBot.id)
        .filter(PaperOptionBotOpenTrade.id == trade_id, PaperOptionTradeBot.user_id == user.id)
        .first()
    )
    if not trade:
        raise HTTPException(404, "Open trade not found")

    updated = False

    if stop_loss is not None:
        trade.planned_stop_loss = stop_loss
        updated = True

    if exit_target is not None:
        # ✅ update the same column your bot is currently using
        trade.planned_take_profit = exit_target
        updated = True

        # (optional) keep legacy column in sync so old UI versions still display
        try:
            trade.planned_take_profit_1 = exit_target
        except Exception:
            pass

    if updated:
        db.commit()

    return JSONResponse({"ok": True, "trade_id": trade_id})
