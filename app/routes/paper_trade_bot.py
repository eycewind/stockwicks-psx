# app/routes/paper_trade_bot.py
# app/routes/paper_trade_bot.py
import sys, os, logging, json
from typing import Optional
from datetime import datetime
import pytz

from fastapi import APIRouter, Depends, Request, Form, HTTPException, Query
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app.database.connection import get_db
from app.routes.auth import get_current_user
from app.models.user import User
from app.models.paper_trading_bot import (
    PaperStockTradeBot,
    PaperStockBotOpenTrade,
    PaperStockBotTradeHistory,
)
from app.utils.stock.market_price import get_live_price


def client_prefix(request: Request) -> str:
    """
    Path-based commercial deployment helper.

    NGINX should set:
        proxy_set_header X-Forwarded-Prefix /clients/ashakil;

    If the header is absent, CLIENT_PUBLIC_PREFIX can be used from .env.
    Empty prefix is valid for local/root deployments.
    """
    prefix = (
        request.headers.get("x-forwarded-prefix")
        or os.getenv("CLIENT_PUBLIC_PREFIX", "")
        or ""
    )
    return prefix.rstrip("/")


def prefixed_url(request: Request, path: str) -> str:
    """
    Use app-local redirects.

    Current ashakil deployment forwards requests to this app as /auth/...
    not /clients/ashakil/auth/..., so redirects must stay unprefixed.
    """
    if not path.startswith("/"):
        path = "/" + path
    return path


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logging.basicConfig(level=logging.INFO)
ET = pytz.timezone("US/Eastern")
UTC = pytz.UTC

import re
from pathlib import Path

DATA_DIR = os.getenv("DATA_DIR", "/var/stockwicks/clients/ashakil/data")

# --- AlgoMM commercial bot config ------------------------------------------
ALLOWED_MM_ALGOS = {
    "Algo1_MM": "Featureset_1",
    "Algo2_MM": "Featureset_2",
    "Algo3_MM": "Featureset_3",
    "Algo4_MM": "Featureset_4",
    "Algo5_MM": "Featureset_5",
    "Algo_SMI": "SMI",
    "Algo_MACD": "MACD",
}

DEFAULT_BOT_CONFIG = {
    "builder_days": 30,
    "k_forward": 3,
    "model_max_age_hours": 0.25,
    "long_entry_prob": 0.60,
    "short_entry_prob": 0.40,
    "prob_smoothing_bars": 3,
    "prob_trail_drop": 0.05,
    "prob_exit_mode": "trailing",
    "long_fixed_exit_prob": 0.40,
    "short_fixed_exit_prob": 0.60,
    "stop_loss_usd": 300.0,
    "hard_stop_usd": 300.0,
    "trailing_profit_usd": 75.0,
    "stop_loss_pct": 0.02,
    "trailing_profit_pct": 0.005,
    "force_retrain_each_tick": True,
}


def _safe_float_form(value, default: float, min_value: float = 0.0) -> float:
    try:
        f = float(value)
    except Exception:
        f = float(default)
    if f < min_value:
        f = float(default)
    return f


def _bot_config_json(
    *,
    algo_name: str,
    eod_auto_close: str | None,
    stop_loss_usd: float | None,
    trailing_profit_usd: float | None = None,
    stop_loss_pct: float | None = None,
    trailing_profit_pct: float | None = None,
    prob_trail_drop: float | None = None,
    prob_exit_mode: str | None = None,
    long_fixed_exit_prob: float | None = None,
    short_fixed_exit_prob: float | None = None,
    long_entry_prob: float | None = None,
    short_entry_prob: float | None = None,
) -> dict:
    """
    Config consumed by app/scripts/stock_algos/Algo1_MM.py through Algo5_MM.py.

    Entry logic:
      - avg(latest prob_up + previous 2 prob_up values)
      - LONG if avg > 0.50
      - SHORT if avg < 0.50

    Exits:
      - stop_loss_usd
      - trailing_profit_usd
      - probability exit mode: trailing drop from peak or fixed conviction floor
      - optional EOD close
    """
    if algo_name not in ALLOWED_MM_ALGOS:
        raise HTTPException(
            status_code=400,
            detail="Invalid algo selected. Choose an MM algo, Algo_SMI, or Algo_MACD.",
        )

    prob_exit_mode = str(prob_exit_mode or DEFAULT_BOT_CONFIG["prob_exit_mode"]).strip().lower()
    if prob_exit_mode not in {"trailing", "fixed"}:
        prob_exit_mode = DEFAULT_BOT_CONFIG["prob_exit_mode"]

    return {
        "algo_name": algo_name,
        "feature_set": ALLOWED_MM_ALGOS[algo_name],
        "builder_days": DEFAULT_BOT_CONFIG["builder_days"],
        "k_forward": DEFAULT_BOT_CONFIG["k_forward"],
        "model_max_age_hours": DEFAULT_BOT_CONFIG["model_max_age_hours"],

        # Backward-compatible aliases for older config readers.
        "long_threshold": _safe_float_form(
            long_entry_prob,
            DEFAULT_BOT_CONFIG["long_entry_prob"],
        ),
        "short_threshold": _safe_float_form(
            short_entry_prob,
            DEFAULT_BOT_CONFIG["short_entry_prob"],
        ),
        "long_exit_threshold": 0.55,
        "short_exit_threshold": 0.45,
        "min_prob_advantage": 0.03,
        "min_volume_multiplier": 0.1,
        "cooldown_sec": 60,

        # Model-only probability engine.
        "long_entry_prob": _safe_float_form(
            long_entry_prob,
            DEFAULT_BOT_CONFIG["long_entry_prob"],
        ),
        "short_entry_prob": _safe_float_form(
            short_entry_prob,
            DEFAULT_BOT_CONFIG["short_entry_prob"],
        ),
        "prob_smoothing_bars": DEFAULT_BOT_CONFIG["prob_smoothing_bars"],
        "prob_trail_drop": _safe_float_form(
            prob_trail_drop,
            DEFAULT_BOT_CONFIG["prob_trail_drop"],
        ),
        "prob_exit_mode": prob_exit_mode,
        "long_fixed_exit_prob": _safe_float_form(
            long_fixed_exit_prob,
            DEFAULT_BOT_CONFIG["long_fixed_exit_prob"],
        ),
        "short_fixed_exit_prob": _safe_float_form(
            short_fixed_exit_prob,
            DEFAULT_BOT_CONFIG["short_fixed_exit_prob"],
        ),

        # User-configurable guardrails.
        "stop_loss_usd": _safe_float_form(
            stop_loss_usd,
            DEFAULT_BOT_CONFIG["stop_loss_usd"],
        ),
        "hard_stop_usd": _safe_float_form(
            stop_loss_usd,
            DEFAULT_BOT_CONFIG["stop_loss_usd"],
        ),
        "trailing_profit_usd": _safe_float_form(
            trailing_profit_usd,
            DEFAULT_BOT_CONFIG["trailing_profit_usd"],
        ),
        "stop_loss_pct": _safe_float_form(
            stop_loss_pct,
            DEFAULT_BOT_CONFIG["stop_loss_pct"],
        ),
        "per_share_stop_pct": _safe_float_form(
            stop_loss_pct,
            DEFAULT_BOT_CONFIG["stop_loss_pct"],
        ),
        "trailing_profit_pct": _safe_float_form(
            trailing_profit_pct,
            DEFAULT_BOT_CONFIG["trailing_profit_pct"],
        ),
        "per_share_trailing_profit_pct": _safe_float_form(
            trailing_profit_pct,
            DEFAULT_BOT_CONFIG["trailing_profit_pct"],
        ),
        "force_retrain_each_tick": True,

        # User toggle.
        "eod_close": eod_auto_close == "on",

        # Explicitly disabled old blockers.
        "cooldown_sec": 0,
        "min_prob_advantage": 0.0,
        "min_volume_multiplier": 0.0,
        "obv_slope_threshold": 0.0,
    }


def _read_bot_config(bot: PaperStockTradeBot) -> dict:
    raw = getattr(bot, "config_json", None)
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _write_bot_config(bot: PaperStockTradeBot, cfg: dict) -> None:
    """
    Use config_json if present in the DB/model. If the column is missing,
    the bot still starts with defaults in the runner, but user GUI values will
    not persist until config_json is added by migration.
    """
    if hasattr(bot, "config_json"):
        bot.config_json = json.dumps(cfg, separators=(",", ":"), sort_keys=True)


def _ensure_live_mirror_history_table(db: Session) -> None:
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS paper_stock_bot_live_mirror_history (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            bot_id INTEGER NOT NULL,
            history_trade_id INTEGER NOT NULL,
            symbol VARCHAR(50) NOT NULL,
            side VARCHAR(20) NOT NULL,
            quantity DOUBLE PRECISION NOT NULL,
            entry_price DOUBLE PRECISION,
            exit_price DOUBLE PRECISION,
            profit_loss DOUBLE PRECISION,
            entry_time TIMESTAMP NULL,
            exit_time TIMESTAMP NULL,
            schwab_order_id VARCHAR(120),
            mirror_status VARCHAR(40) NOT NULL DEFAULT 'requested',
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """))
    db.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_live_mirror_user_trade
        ON paper_stock_bot_live_mirror_history (user_id, history_trade_id)
    """))


def pick_template(user: User, default_template: str, td_template: str) -> str:
    return td_template if getattr(user, "schwab_allowed", "N") == "Y" else default_template

def order_by_best_ts(query, model, fields=("entry_time", "created_at", "updated_at", "id")):
    for f in fields:
        if hasattr(model, f):
            return query.order_by(getattr(model, f).desc())
    return query

def _hist_pl_value(row) -> float:
    """
    Read realized P/L from whichever column your schema uses.
    """
    for k in ("pnl", "pl", "profit_loss", "realized_pl"):
        v = getattr(row, k, None)
        if v is not None:
            try:
                return float(v)
            except Exception:
                pass
    return 0.0

@router.get("/auth/papertradebot", name="paper_trading_bot_dashboard")
def paper_trade_bot_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _ensure_live_mirror_history_table(db)
    all_bots = (
        db.query(PaperStockTradeBot)
        .filter(PaperStockTradeBot.user_id == user.id)
        .filter(PaperStockTradeBot.status != "DELETED")
        .order_by(PaperStockTradeBot.created_at.desc())
        .all()
    )
    open_trades = order_by_best_ts(
        db.query(PaperStockBotOpenTrade).filter_by(user_id=user.id),
        PaperStockBotOpenTrade
    ).all()
    closed_trades = (
        db.query(PaperStockBotTradeHistory)
        .filter_by(user_id=user.id)
        .order_by(PaperStockBotTradeHistory.id.desc())
        .limit(50)
        .all()
    )

    # Attach bot objects for convenience in templates. Deleted bots stay in the
    # DB for trade-history foreign keys, so include them for history labels.
    visible_bot_map = {b.id: b for b in all_bots}
    history_bot_ids = {t.bot_id for t in open_trades + closed_trades if t.bot_id}
    bot_map = dict(visible_bot_map)
    missing_bot_ids = history_bot_ids - set(bot_map)
    if missing_bot_ids:
        hidden_bots = (
            db.query(PaperStockTradeBot)
            .filter(
                PaperStockTradeBot.user_id == user.id,
                PaperStockTradeBot.id.in_(missing_bot_ids),
            )
            .all()
        )
        bot_map.update({b.id: b for b in hidden_bots})
    for b in all_bots:
        b.runtime_config = _read_bot_config(b)
    for t in open_trades + closed_trades:
        t.bot = bot_map.get(t.bot_id)

    # === LIVE P/L COMPUTE (for UI) ============================================
    # Fetch latest price per open trade and compute live P/L.
    # Optionally persist current_price/unrealized_pl if those columns exist.
    updated = 0
    for t in open_trades:
        try:
            px = get_live_price(t.symbol)
            if not px:
                continue
            t.live_price = float(px)
            side = (t.position_side or "").lower()
            if side == "long":
                t.live_pl = (t.live_price - float(t.entry_price)) * float(t.quantity)
            else:
                t.live_pl = (float(t.entry_price) - t.live_price) * float(t.quantity)

            # Persist to DB if columns exist, so refreshes show data immediately
            if hasattr(t, "current_price"):
                t.current_price = t.live_price
            if hasattr(t, "unrealized_pl"):
                t.unrealized_pl = t.live_pl
            updated += 1
        except Exception as e:
            logging.warning(f"[DASH] live PL calc failed for {getattr(t, 'symbol','?')}: {e}")

    if updated:
        try:
            db.commit()
        except Exception:
            db.rollback()
    # =========================================================================

    # Realized P/L summary (robust to column name differences)
    starting_balance = 10000000.0
    total_pl = sum(_hist_pl_value(t) for t in closed_trades)
    pl_percent = (total_pl / starting_balance) * 100 if starting_balance else 0.0
    paper_account = {"initial_balance": starting_balance, "current_balance": starting_balance + total_pl}

    template_name = "td_paper_trade_bot.html"
    return templates.TemplateResponse(
        request,
        template_name,
        {
            "request": request,
            "user": user,
            "all_bots": all_bots,
            "open_trades": open_trades,     # each row now has .live_price and .live_pl (in-memory)
            "closed_trades": closed_trades,
            "paper_account": paper_account,
            "total_pl": total_pl,
            "pl_percent": pl_percent,
        },
    )

@router.post("/auth/papertradebot/start")
async def start_paper_trade_bot(
    request: Request,
    symbol: str = Form(...),
    interval: str = Form(...),
    algo_name: str = Form(...),
    trade_size: float = Form(...),
    stop_loss_usd: float = Form(DEFAULT_BOT_CONFIG["stop_loss_usd"]),
    trailing_profit_usd: float = Form(DEFAULT_BOT_CONFIG["trailing_profit_usd"]),
    stop_loss_pct: float = Form(DEFAULT_BOT_CONFIG["stop_loss_pct"]),
    trailing_profit_pct: float = Form(DEFAULT_BOT_CONFIG["trailing_profit_pct"]),
    prob_trail_drop: float = Form(DEFAULT_BOT_CONFIG["prob_trail_drop"]),
    prob_exit_mode: str = Form(DEFAULT_BOT_CONFIG["prob_exit_mode"]),
    long_fixed_exit_prob: float = Form(DEFAULT_BOT_CONFIG["long_fixed_exit_prob"]),
    short_fixed_exit_prob: float = Form(DEFAULT_BOT_CONFIG["short_fixed_exit_prob"]),
    long_entry_prob: float = Form(DEFAULT_BOT_CONFIG["long_entry_prob"]),
    short_entry_prob: float = Form(DEFAULT_BOT_CONFIG["short_entry_prob"]),
    notify_email: str = Form(None),
    allow_short_selling: str = Form(None),
    eod_auto_close: str = Form(None),
    mirror_live: str = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    symbol = symbol.upper().strip()
    algo_name = (algo_name or "").strip()

    if algo_name not in ALLOWED_MM_ALGOS:
        raise HTTPException(
            status_code=400,
            detail="Invalid algo selected. Choose an MM algo, Algo_SMI, or Algo_MACD.",
        )

    if db.query(PaperStockTradeBot).filter_by(user_id=user.id, symbol=symbol, is_active=True).first():
        raise HTTPException(status_code=400, detail=f"You already have an active bot for {symbol}.")

    cfg = _bot_config_json(
        algo_name=algo_name,
        eod_auto_close=eod_auto_close,
        stop_loss_usd=stop_loss_usd,
        trailing_profit_usd=trailing_profit_usd,
        stop_loss_pct=stop_loss_pct,
        trailing_profit_pct=trailing_profit_pct,
        prob_trail_drop=prob_trail_drop,
        prob_exit_mode=prob_exit_mode,
        long_fixed_exit_prob=long_fixed_exit_prob,
        short_fixed_exit_prob=short_fixed_exit_prob,
        long_entry_prob=long_entry_prob,
        short_entry_prob=short_entry_prob,
    )

    new_bot = PaperStockTradeBot(
        user_id=user.id,
        symbol=symbol,
        interval=interval,
        algo_name=algo_name,
        trade_size=trade_size,
        notify_email=(notify_email in ["yes", "on", "true", "1"]),
        allow_short_selling=(allow_short_selling == "on"),
        eod_auto_close=(eod_auto_close == "on"),
        is_active=True,
        status="RUNNING",
        mirror_live=(mirror_live == "on"),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    _write_bot_config(new_bot, cfg)

    db.add(new_bot)
    db.commit()
    logging.info(
        "✅ Bot #%s created/activated for %s (%s %s %s)",
        new_bot.id,
        user.email,
        symbol,
        algo_name,
        cfg.get("feature_set"),
    )
    return RedirectResponse(url=prefixed_url(request, "/auth/papertradebot"), status_code=302)

@router.post("/auth/papertradebot/stop/{bot_id}")
def stop_paper_trade_bot(
    bot_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bot = db.query(PaperStockTradeBot).filter_by(id=bot_id, user_id=user.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    bot.is_active = False
    bot.status = "STOPPED"
    bot.updated_at = datetime.utcnow()
    db.commit()
    logging.info(f"⏸ Bot #{bot.id} stopped for {user.email}")
    return RedirectResponse(url=prefixed_url(request, "/auth/papertradebot"), status_code=302)

@router.post("/auth/papertradebot/cancel/{bot_id}")
def cancel_paper_trade_bot(
    bot_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bot = db.query(PaperStockTradeBot).filter_by(id=bot_id, user_id=user.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    db.query(PaperStockBotOpenTrade).filter_by(bot_id=bot_id, user_id=user.id).delete(
        synchronize_session=False
    )
    bot.is_active = False
    bot.status = "DELETED"
    bot.updated_at = datetime.utcnow()
    db.commit()
    logging.info(f"❌ Canceled & deleted bot #{bot_id} for {user.email}")
    return RedirectResponse(url=prefixed_url(request, "/auth/papertradebot"), status_code=302)

# Idempotent close endpoint — supports ?stop=1 to stop bot after close
@router.post("/api/paper/close-trade/{trade_id}")
def api_close_trade(
    trade_id: int,
    stop: bool = Query(False),  # add ?stop=1 to stop bot after closing
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    trade = (
        db.query(PaperStockBotOpenTrade)
        .filter_by(id=trade_id, user_id=user.id)
        .first()
    )
    if not trade:
        # Idempotent close: already closed or not found for this user
        return JSONResponse({"ok": True, "alreadyClosed": True}, status_code=200)

    bot = (
        db.query(PaperStockTradeBot)
        .filter_by(id=trade.bot_id, user_id=user.id)
        .first()
    )
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found for trade")

    last_price = float(get_live_price(trade.symbol) or trade.entry_price)

    try:
        from app.services.paper_trade_service import close_position
        hist = close_position(db, trade, last_price)
        if not hist:
            raise HTTPException(status_code=400, detail="Close failed (see logs)")
        logging.info(f"[CLOSE] ✅ Bot #{bot.id} {trade.symbol} closed @ {last_price}")

        if stop:
            bot.is_active = False
            bot.status = "STOPPED"
            bot.updated_at = datetime.utcnow()
            db.commit()
            return JSONResponse({"ok": True, "stoppedBot": bot.id})

        return JSONResponse({"ok": True})
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logging.exception(f"[CLOSE] ❌ error closing trade {trade_id}: {e}")
        raise HTTPException(status_code=400, detail=f"Close failed: {e}")

@router.post("/api/paper/close-latest/{bot_id}")
def api_close_latest(
    bot_id: int,
    stop: bool = Query(False),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    trade = (
        db.query(PaperStockBotOpenTrade)
        .filter_by(user_id=user.id, bot_id=bot_id)
        .order_by(PaperStockBotOpenTrade.id.desc())
        .first()
    )
    if not trade:
        return JSONResponse({"ok": True, "alreadyClosed": True}, status_code=200)

    last_price = float(get_live_price(trade.symbol) or trade.entry_price)

    from app.services.paper_trade_service import close_position
    hist = close_position(db, trade, last_price)
    if not hist:
        raise HTTPException(status_code=400, detail="Close failed (see logs)")

    if stop:
        bot = db.query(PaperStockTradeBot).filter_by(id=bot_id, user_id=user.id).first()
        if bot:
            bot.is_active = False
            bot.status = "STOPPED"
            bot.updated_at = datetime.utcnow()
            db.commit()
        return {"ok": True, "stoppedBot": bot_id}

    return {"ok": True, "closed_id": trade.id}

@router.post("/api/paper/close-all")
def api_close_all(
    stop_bots: bool = Query(False),  # stop each bot after its close(s)
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services.paper_trade_service import close_position

    open_trades = (
        db.query(PaperStockBotOpenTrade)
        .filter_by(user_id=user.id)
        .order_by(PaperStockBotOpenTrade.id.asc())
        .all()
    )
    closed_ids = []
    for t in open_trades:
        price = float(get_live_price(t.symbol) or t.entry_price)
        hist = close_position(db, t, price)
        if hist:
            closed_ids.append(t.id)
        if stop_bots:
            bot = db.query(PaperStockTradeBot).filter_by(id=t.bot_id, user_id=user.id).first()
            if bot:
                bot.is_active = False
                bot.status = "STOPPED"
                bot.updated_at = datetime.utcnow()
                db.commit()

    return {"ok": True, "closed": closed_ids, "stopped": stop_bots}
# --- ADD: helpers for JSON filtering/summary -------------------------------
from decimal import Decimal, ROUND_HALF_UP

def _row_pl_value(row) -> Decimal:
    """
    If your history row already stores realized P/L in a column (pnl/pl/profit_loss),
    prefer that. Otherwise, compute from side/entry/exit/qty.
    """
    # 1) try stored
    for k in ("pnl", "pl", "profit_loss", "realized_pl"):
        v = getattr(row, k, None)
        if v is not None:
            try:
                return Decimal(str(v))
            except Exception:
                pass

    # 2) compute
    side = (getattr(row, "position_side", "") or "").lower()
    qty = Decimal(str(getattr(row, "quantity", 0) or 0))
    entry = Decimal(str(getattr(row, "entry_price", 0) or 0))
    exit_ = Decimal(str(getattr(row, "exit_price", 0) or 0))
    if side == "long":
        return (exit_ - entry) * qty
    else:
        return (entry - exit_) * qty

def _fmt_money(d: Decimal) -> str:
    q = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "+" if q >= 0 else "-"
    return f"{sign}${abs(q):,.2f}"

def _serialize_hist_row(row) -> dict:
    pl = _row_pl_value(row)
    bot = getattr(row, "bot", None)
    return {
        "id": row.id,
        "bot_id": row.bot_id,
        "algo_name": getattr(bot, "algo_name", None),
        "symbol": row.symbol,
        "side": (row.position_side or "").lower(),
        "qty": float(Decimal(str(row.quantity or 0))),
        "entry": float(Decimal(str(row.entry_price or 0))),
        "exit": float(Decimal(str(row.exit_price or 0))),
        "pl": float(pl),
        "entry_time": (
            row.entry_time.strftime("%Y-%m-%d %H:%M:%S")
            if getattr(row, "entry_time", None)
            else None
        ),
        "exit_time": (
            row.exit_time.strftime("%Y-%m-%d %H:%M:%S")
            if getattr(row, "exit_time", None)
            else None
        ),
    }

def _serialize_open_row(row) -> dict:
    return {
        "id": row.id,
        "bot_id": row.bot_id,
        "symbol": row.symbol,
        "side": (row.position_side or "").lower(),
        "qty": float(Decimal(str(row.quantity or 0))),
        "entry": float(Decimal(str(row.entry_price or 0))),
        "entry_time": (
            row.entry_time.strftime("%Y-%m-%d %H:%M:%S")
            if getattr(row, "entry_time", None)
            else None
        ),
    }


def _summarize_rows(rows) -> dict:
    wins = losses = breakeven = 0
    net = Decimal("0")
    biggest_win = Decimal("0")
    biggest_loss = Decimal("0")
    for r in rows:
        pl = _row_pl_value(r)
        net += pl
        if pl > 0:
            wins += 1
            if pl > biggest_win:
                biggest_win = pl
        elif pl < 0:
            losses += 1
            if pl < biggest_loss:
                biggest_loss = pl
        else:
            breakeven += 1

    total = len(rows)
    win_rate = (Decimal(wins) / Decimal(wins + losses) * Decimal("100")) if (wins + losses) else Decimal("0")

    return {
        "trades": total,
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "win_rate": float(win_rate.quantize(Decimal("0.1"))),
        "net_pl": _fmt_money(net),
        "biggest_win": _fmt_money(biggest_win),
        "biggest_loss": _fmt_money(biggest_loss),
    }
# ---------------------------------------------------------------------------

# === ADD: JSON endpoints the page JS will call ==============================

@router.get("/auth/papertradebot/trades_json")
def trades_json(
    bot_id: str = Query("ALL"),
    limit: int = Query(10000, ge=1, le=20000),  # enough to cover "ALL" history
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Returns filtered closed-trade history and a summary for the user (optionally a specific bot).
    """
    _ensure_live_mirror_history_table(db)
    q = db.query(PaperStockBotTradeHistory).filter_by(user_id=user.id)
    if bot_id != "ALL":
        try:
            q = q.filter(PaperStockBotTradeHistory.bot_id == int(bot_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid bot_id")
    rows = q.order_by(PaperStockBotTradeHistory.id.desc()).limit(limit).all()
    bot_ids = {r.bot_id for r in rows}
    bot_map = {}
    if bot_ids:
        bots = (
            db.query(PaperStockTradeBot)
            .filter(PaperStockTradeBot.user_id == user.id, PaperStockTradeBot.id.in_(bot_ids))
            .all()
        )
        bot_map = {b.id: b for b in bots}
        for r in rows:
            r.bot = bot_map.get(r.bot_id)

    summary = _summarize_rows(rows)
    payload = [_serialize_hist_row(r) for r in rows]
    if payload:
        mirror_rows = db.execute(
            text("""
                SELECT history_trade_id, schwab_order_id, mirror_status
                FROM paper_stock_bot_live_mirror_history
                WHERE user_id = :user_id
                  AND history_trade_id = ANY(:trade_ids)
            """),
            {"user_id": user.id, "trade_ids": [p["id"] for p in payload]},
        ).mappings().all()
        mirror_map = {int(r["history_trade_id"]): r for r in mirror_rows}
        for item in payload:
            mirror = mirror_map.get(int(item["id"]))
            item["mirror_live"] = bool(mirror)
            item["schwab_order_id"] = mirror["schwab_order_id"] if mirror else None
            item["mirror_status"] = mirror["mirror_status"] if mirror else None
    return JSONResponse({"summary": summary, "trades": payload})


@router.delete("/auth/papertradebot/trades_json")
async def delete_history_trades(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _ensure_live_mirror_history_table(db)
    body = await request.json()
    ids = body.get("ids") if isinstance(body, dict) else None
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="Provide at least one trade id.")
    try:
        trade_ids = [int(v) for v in ids]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid trade id list.")

    db.execute(
        text("""
            DELETE FROM paper_stock_bot_live_mirror_history
            WHERE user_id = :user_id AND history_trade_id = ANY(:trade_ids)
        """),
        {"user_id": user.id, "trade_ids": trade_ids},
    )
    deleted = (
        db.query(PaperStockBotTradeHistory)
        .filter(
            PaperStockBotTradeHistory.user_id == user.id,
            PaperStockBotTradeHistory.id.in_(trade_ids),
        )
        .delete(synchronize_session=False)
    )
    db.commit()
    return JSONResponse({"ok": True, "deleted": int(deleted or 0)})


@router.get("/auth/papertradebot/live_mirror_trades_json")
def live_mirror_trades_json(
    limit: int = Query(1000, ge=1, le=5000),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _ensure_live_mirror_history_table(db)
    rows = db.execute(
        text("""
            SELECT id, bot_id, history_trade_id, symbol, side, quantity, entry_price,
                   exit_price, profit_loss, entry_time, exit_time, schwab_order_id,
                   mirror_status, created_at
            FROM paper_stock_bot_live_mirror_history
            WHERE user_id = :user_id
            ORDER BY id DESC
            LIMIT :limit
        """),
        {"user_id": user.id, "limit": limit},
    ).mappings().all()
    payload = []
    for r in rows:
        item = dict(r)
        for key in ("entry_time", "exit_time", "created_at"):
            if item.get(key) is not None:
                item[key] = item[key].strftime("%Y-%m-%d %H:%M:%S")
        payload.append(item)
    return JSONResponse({"trades": payload})

@router.get("/auth/papertradebot/open_positions_json")
def open_positions_json(
    bot_id: str = Query("ALL"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(PaperStockBotOpenTrade).filter_by(user_id=user.id)
    if bot_id != "ALL":
        try:
            q = q.filter(PaperStockBotOpenTrade.bot_id == int(bot_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid bot_id")
    rows = order_by_best_ts(q, PaperStockBotOpenTrade).all()
    payload = [_serialize_open_row(r) for r in rows]
    return JSONResponse({"positions": payload})
# ============================================================================
@router.post("/auth/papertradebot/restart/{bot_id}")
def restart_paper_trade_bot(
    bot_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bot = (
        db.query(PaperStockTradeBot)
        .filter_by(id=bot_id, user_id=user.id)
        .first()
    )
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    # If already active, nothing to do – just bounce back to dashboard
    if bot.is_active:
        return RedirectResponse(url=prefixed_url(request, "/auth/papertradebot"), status_code=302)

    # Optional safety: don’t allow two active bots on same symbol for this user
    dup = (
        db.query(PaperStockTradeBot)
        .filter(
            PaperStockTradeBot.user_id == user.id,
            PaperStockTradeBot.symbol == bot.symbol,
            PaperStockTradeBot.is_active == True,
        )
        .first()
    )
    if dup:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Another active bot for {dup.symbol} is already running "
                f"(#{dup.id}). Stop or cancel it before restarting this one."
            ),
        )

    bot.is_active = True
    bot.status = "RUNNING"
    bot.updated_at = datetime.utcnow()
    db.commit()
    logging.info(f"▶️ Bot #{bot.id} restarted for {user.email}")
    return RedirectResponse(url=prefixed_url(request, "/auth/papertradebot"), status_code=302)


@router.post("/auth/papertradebot/save-size-restart/{bot_id}")
def save_size_and_restart_paper_trade_bot(
    bot_id: int,
    request: Request,
    trade_size: float = Form(...),
    stop_loss_usd: float | None = Form(None),
    trailing_profit_usd: float | None = Form(None),
    stop_loss_pct: float | None = Form(None),
    trailing_profit_pct: float | None = Form(None),
    prob_trail_drop: float | None = Form(None),
    prob_exit_mode: str | None = Form(None),
    long_fixed_exit_prob: float | None = Form(None),
    short_fixed_exit_prob: float | None = Form(None),
    long_entry_prob: float | None = Form(None),
    short_entry_prob: float | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bot = (
        db.query(PaperStockTradeBot)
        .filter_by(id=bot_id, user_id=user.id)
        .first()
    )
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    if bot.is_active or bot.status == "RUNNING":
        raise HTTPException(
            status_code=400,
            detail="Stop the bot before changing trade size."
        )

    if trade_size <= 0:
        raise HTTPException(status_code=400, detail="Trade size must be greater than zero")

    bot.trade_size = float(trade_size)

    # Optional risk setting updates when the restart form provides them.
    cfg = _read_bot_config(bot)
    if stop_loss_usd is not None:
        cfg["stop_loss_usd"] = _safe_float_form(stop_loss_usd, DEFAULT_BOT_CONFIG["stop_loss_usd"])
        cfg["hard_stop_usd"] = cfg["stop_loss_usd"]
    if trailing_profit_usd is not None:
        cfg["trailing_profit_usd"] = _safe_float_form(
            trailing_profit_usd,
            DEFAULT_BOT_CONFIG["trailing_profit_usd"],
        )
    if stop_loss_pct is not None:
        cfg["stop_loss_pct"] = _safe_float_form(stop_loss_pct, DEFAULT_BOT_CONFIG["stop_loss_pct"])
        cfg["per_share_stop_pct"] = cfg["stop_loss_pct"]
    if trailing_profit_pct is not None:
        cfg["trailing_profit_pct"] = _safe_float_form(
            trailing_profit_pct,
            DEFAULT_BOT_CONFIG["trailing_profit_pct"],
        )
        cfg["per_share_trailing_profit_pct"] = cfg["trailing_profit_pct"]
    if prob_trail_drop is not None:
        cfg["prob_trail_drop"] = _safe_float_form(
            prob_trail_drop,
            DEFAULT_BOT_CONFIG["prob_trail_drop"],
        )
    if prob_exit_mode is not None:
        prob_exit_mode = str(prob_exit_mode or DEFAULT_BOT_CONFIG["prob_exit_mode"]).strip().lower()
        cfg["prob_exit_mode"] = prob_exit_mode if prob_exit_mode in {"trailing", "fixed"} else DEFAULT_BOT_CONFIG["prob_exit_mode"]
    if long_fixed_exit_prob is not None:
        cfg["long_fixed_exit_prob"] = _safe_float_form(
            long_fixed_exit_prob,
            DEFAULT_BOT_CONFIG["long_fixed_exit_prob"],
        )
    if short_fixed_exit_prob is not None:
        cfg["short_fixed_exit_prob"] = _safe_float_form(
            short_fixed_exit_prob,
            DEFAULT_BOT_CONFIG["short_fixed_exit_prob"],
        )
    if long_entry_prob is not None:
        cfg["long_entry_prob"] = _safe_float_form(
            long_entry_prob,
            DEFAULT_BOT_CONFIG["long_entry_prob"],
        )
    if short_entry_prob is not None:
        cfg["short_entry_prob"] = _safe_float_form(
            short_entry_prob,
            DEFAULT_BOT_CONFIG["short_entry_prob"],
        )
    if cfg:
        _write_bot_config(bot, cfg)

    # Old code paths may still use quantity.
    if hasattr(bot, "quantity"):
        bot.quantity = int(trade_size)

    bot.is_active = True
    bot.status = "RUNNING"
    bot.updated_at = datetime.utcnow()

    db.commit()
    logging.info(f"💾▶️ Bot #{bot.id} size changed to {trade_size} and restarted for {user.email}")

    return RedirectResponse(url=prefixed_url(request, "/auth/papertradebot"), status_code=302)

# === AlgoMM probability log helpers =========================================

LOG_TS_RE = re.compile(r"\[(.*?)\]")
LOG_PROB_RE = re.compile(
    r"Probabilities:\s*UP:\s*([0-9.]+)\s*\|\s*DOWN:\s*([0-9.]+)",
    re.IGNORECASE,
)
MM_SIMPLE_HEADER_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+E[DS]T\s+\|\s+"
    r"(\S+)\s+(\S+)\s+\|\s+(\S+)\s+\|\s*(.*)$"
)
MM_SIMPLE_PROB_RE = re.compile(
    r"prob_up=([0-9.]+)\s+prob_down=([0-9.]+)"
    r"(?:\s+prob_up_avg=([0-9.]+)\s+prev_avg=([0-9.]+))?",
    re.IGNORECASE,
)
MM_SIMPLE_PRICE_RE = re.compile(
    r"close=([0-9.]+)\s+open=([0-9.]+)\s+rows=([0-9]+)",
    re.IGNORECASE,
)


def _et_epoch(ts_text: str) -> int | None:
    try:
        dt = datetime.strptime(ts_text, "%Y-%m-%d %H:%M:%S")
        if hasattr(ET, "localize"):
            dt = ET.localize(dt)
        else:
            dt = dt.replace(tzinfo=ET)
        return int(dt.timestamp())
    except Exception:
        return None


def _parse_current_mm_log(log_path: Path, max_candles: int = 500) -> dict:
    """
    Parse current Algo1_MM / Algo2_MM / Algo3_MM pretty logs, for example:
      2026-05-26 09:35:18 EDT | MU 5min | OPEN_SHORT | ...
      prob_up=0.30 prob_down=0.70 prob_up_avg=0.40 prev_avg=0.47
      close=835.00 open=835.14 rows=2264 position=SHORT @ 835.00

    The log does not include high/low, so chart candles use high/low from
    open/close. That is still enough to show the live bot timeline and
    probability series instead of returning an empty chart.
    """
    if not log_path.exists():
        return {"candles": [], "trades": [], "probs": []}

    candles_by_time = {}
    last_close = None
    trades = []
    probs = []
    block = None

    def flush():
        nonlocal block, last_close
        if not block:
            return

        ts_epoch = block.get("time")
        if ts_epoch and block.get("prob_up") is not None:
            probs.append(
                {
                    "time": ts_epoch,
                    "ts": block.get("ts"),
                    "up": float(block["prob_up"]),
                    "down": float(block.get("prob_down", 1.0 - float(block["prob_up"]))),
                    "avg": block.get("prob_up_avg"),
                    "prev_avg": block.get("prev_avg"),
                }
            )

        if ts_epoch and block.get("open") is not None and block.get("close") is not None:
            o = float(block["open"])
            c = float(block["close"])
            h = max(o, c, float(last_close)) if last_close is not None else max(o, c)
            l = min(o, c, float(last_close)) if last_close is not None else min(o, c)
            candles_by_time[ts_epoch] = {
                "time": ts_epoch,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": 0,
            }
            last_close = c

        decision = str(block.get("decision") or "").upper()
        price = block.get("close")
        if ts_epoch and price is not None:
            if decision.startswith("OPEN_LONG"):
                trades.append(
                    {
                        "time": ts_epoch,
                        "type": "ENTRY",
                        "side": "long",
                        "price": float(price),
                        "reason": block.get("reason", ""),
                    }
                )
            elif decision.startswith("OPEN_SHORT"):
                trades.append(
                    {
                        "time": ts_epoch,
                        "type": "ENTRY",
                        "side": "short",
                        "price": float(price),
                        "reason": block.get("reason", ""),
                    }
                )
            elif decision.startswith("EXIT"):
                trades.append(
                    {
                        "time": ts_epoch,
                        "type": "EXIT",
                        "side": "",
                        "price": float(price),
                        "reason": block.get("reason", ""),
                    }
                )

        block = None

    with log_path.open("r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("="):
                continue

            m = MM_SIMPLE_HEADER_RE.search(line)
            if m:
                flush()
                ts_text = m.group(1)
                block = {
                    "ts": ts_text,
                    "time": _et_epoch(ts_text),
                    "symbol": m.group(2),
                    "interval": m.group(3),
                    "decision": m.group(4),
                    "reason": m.group(5).strip(),
                }
                continue

            if not block:
                continue

            m = MM_SIMPLE_PROB_RE.search(line)
            if m:
                block["prob_up"] = float(m.group(1))
                block["prob_down"] = float(m.group(2))
                if m.group(3) is not None:
                    block["prob_up_avg"] = float(m.group(3))
                if m.group(4) is not None:
                    block["prev_avg"] = float(m.group(4))
                continue

            m = MM_SIMPLE_PRICE_RE.search(line)
            if m:
                block["close"] = float(m.group(1))
                block["open"] = float(m.group(2))
                block["rows"] = int(m.group(3))
                continue

    flush()

    candles = sorted(candles_by_time.values(), key=lambda c: c["time"])
    if len(candles) > max_candles:
        candles = candles[-max_candles:]
    return {
        "candles": candles,
        "trades": sorted(trades, key=lambda t: t["time"]),
        "probs": sorted(probs, key=lambda p: p["time"]),
    }


def _bot_log_path(user_id: int, bot_id: int, symbol: str, algo_name: str) -> Path:
    """
    Compose the expected log path, e.g.
    /var/www/stockwicks/data/116/bot_393_AVGO_AlgoMM.log
    """
    base = Path(DATA_DIR) / str(user_id)
    fname = f"bot_{bot_id}_{symbol}_{algo_name}.log"
    return base / fname


def _parse_bot_probabilities(log_path: Path, max_points: int = 300) -> list[dict]:
    """
    Parse the AlgoMM log for time-series of probabilities.

    Returns: [{"ts": "...", "up": 0.73, "down": 0.21}, ...] (last N points)
    """
    if not log_path.exists():
        return []

    points: list[dict] = []
    current_ts: str | None = None

    with log_path.open("r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.rstrip("\n")

            m_simple_ts = MM_SIMPLE_HEADER_RE.search(line.strip())
            if m_simple_ts:
                current_ts = m_simple_ts.group(1)
                continue

            # Timestamp line: [2025-11-23 16:05:08 EST] STALE_BAR
            m_ts = LOG_TS_RE.search(line)
            if m_ts:
                current_ts = m_ts.group(1)

            m_simple_prob = MM_SIMPLE_PROB_RE.search(line)
            if m_simple_prob:
                try:
                    points.append(
                        {
                            "ts": current_ts,
                            "up": float(m_simple_prob.group(1)),
                            "down": float(m_simple_prob.group(2)),
                        }
                    )
                except ValueError:
                    pass
                continue

            # Probability line: Probabilities:  UP:  0.000  |  DOWN:  0.000
            m_prob = LOG_PROB_RE.search(line)
            if m_prob:
                try:
                    up = float(m_prob.group(1))
                    down = float(m_prob.group(2))
                except ValueError:
                    continue

                points.append(
                    {
                        "ts": current_ts,  # raw text, fine for labels
                        "up": up,
                        "down": down,
                    }
                )

    if len(points) > max_points:
        points = points[-max_points:]
    return points
@router.get("/auth/papertradebot/bot_probs_json")
def bot_probs_json(
    bot_id: int = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Returns AlgoMM probability timeline + thresholds + entry/exit events.
    Used by dashboard interactive chart.
    """
    bot = (
        db.query(PaperStockTradeBot)
        .filter_by(id=bot_id, user_id=user.id)
        .first()
    )
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    # --- Parse probability points from bot log ---
    log_path = _bot_log_path(user.id, bot.id, bot.symbol, bot.algo_name)
    raw_points = _parse_bot_probabilities(log_path)

    # Ensure we have timestamps — fallback to index if missing
    points = []
    for i, p in enumerate(raw_points):
        ts = p.get("ts") or p.get("time") or p.get("datetime")
        if not ts:
            # fallback: bot runner logs have timestamp in each line
            ts = p.get("log_ts")  # you likely have this
        points.append({
            "ts": ts,
            "up": float(p.get("up", 0)),
            "down": float(p.get("down", 0)),
        })

    # --- Thresholds (from bot config JSON) ---
    try:
        cfg = json.loads(bot.config_json) if bot.config_json else {}
    except:
        cfg = {}

    thresholds = {
        "long_entry_prob": float(cfg.get("long_entry_prob", cfg.get("entry_prob_long", 0.60))),
            "short_entry_prob": float(cfg.get("short_entry_prob", cfg.get("entry_prob_short", 0.40))),
        "prob_smoothing_bars": int(cfg.get("prob_smoothing_bars", 3)),
        "prob_exit_mode": str(cfg.get("prob_exit_mode", DEFAULT_BOT_CONFIG["prob_exit_mode"])),
        "prob_trail_drop": float(cfg.get("prob_trail_drop", 0.05)),
        "long_fixed_exit_prob": float(
            cfg.get("long_fixed_exit_prob", cfg.get("prob_fixed_exit_prob", DEFAULT_BOT_CONFIG["long_fixed_exit_prob"]))
        ),
        "short_fixed_exit_prob": float(
            cfg.get("short_fixed_exit_prob", cfg.get("prob_fixed_exit_prob", DEFAULT_BOT_CONFIG["short_fixed_exit_prob"]))
        ),
        "stop_loss_usd": float(cfg.get("stop_loss_usd", cfg.get("hard_stop_usd", DEFAULT_BOT_CONFIG["stop_loss_usd"]))),
        "hard_stop_usd": float(cfg.get("stop_loss_usd", cfg.get("hard_stop_usd", DEFAULT_BOT_CONFIG["stop_loss_usd"]))),
        "trailing_profit_usd": float(
            cfg.get(
                "trailing_profit_usd",
                cfg.get("trailing_stop_distance", cfg.get("trailing_stop_activation", DEFAULT_BOT_CONFIG["trailing_profit_usd"])),
            )
        ),
        "stop_loss_pct": float(cfg.get("stop_loss_pct", cfg.get("per_share_stop_pct", DEFAULT_BOT_CONFIG["stop_loss_pct"]))),
        "trailing_profit_pct": float(
            cfg.get("trailing_profit_pct", cfg.get("per_share_trailing_profit_pct", DEFAULT_BOT_CONFIG["trailing_profit_pct"]))
        ),
    }

    # --- Entry/Exit events for vertical markers ---
    events = []

    # Open trade (ENTRY)
    open_trade = (
        db.query(PaperStockBotOpenTrade)
        .filter_by(bot_id=bot.id)
        .first()
    )
    if open_trade:
        events.append({
            "ts": open_trade.entry_time.isoformat(),
            "type": "ENTRY",
            "side": open_trade.position_side,
        })

    # Closed trades (EXIT)
    closed_trades = (
        db.query(PaperStockBotTradeHistory)
        .filter_by(bot_id=bot.id)
        .order_by(PaperStockBotTradeHistory.exit_time.asc())
        .all()
    )
    for t in closed_trades:
        if t.exit_time:
            events.append({
                "ts": t.exit_time.isoformat(),
                "type": "EXIT",
                "side": t.position_side,
            })

    return {
        "bot_id": bot.id,
        "symbol": bot.symbol,
        "interval": bot.interval,
        "algo": bot.algo_name,
        "points": points,
        "thresholds": thresholds,
        "events": events,
    }

# =============================================================================
# ADD THIS to the bottom of paper_trade_bot.py (before the final line)
# =============================================================================
# === AlgoMM Candle Chart endpoint (replaces/supplements probability chart) ====

import json
from app.utils.stock.bot_log_parser import parse_pretty_bot_log_candles

# New regex patterns for the updated log format
# ║ 2026-04-24 15:35:21 EST | OPEN_SHORT
LOG_HEADER_RE = re.compile(
    r"║\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+EST\s+\|\s+(\S+)"
)
# ║ Symbol: MSTR | Price: $171.40
LOG_PRICE_RE = re.compile(
    r"║\s+Symbol:\s+(\S+)\s+\|\s+Price:\s+\$([0-9.]+)"
)
# ║ Prob UP: 0.454 | Prob DOWN: 0.546
LOG_PROB_NEW_RE = re.compile(
    r"║\s+Prob\s+UP:\s+([0-9.]+)\s+\|\s+Prob\s+DOWN:\s+([0-9.]+)"
)
# ║ Last Candle: 2026-04-24 15:35:00 | O:171.50 H:171.50 L:171.35 C:171.40 V:14,062
LOG_CANDLE_RE = re.compile(
    r"║\s+Last\s+Candle:\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+\|\s+"
    r"O:([0-9.]+)\s+H:([0-9.]+)\s+L:([0-9.]+)\s+C:([0-9.]+)\s+V:([0-9,]+)"
)
# ║ ACTION: OPEN_LONG / EXIT / NO_ENTRY / HOLD etc
LOG_ACTION_RE = re.compile(r"║\s+ACTION:\s+(\S+)")
# ║ REASON: SHORT_CONDITIONS_MET
LOG_REASON_RE = re.compile(r"║\s+REASON:\s+(.+)")


def _parse_bot_candles(log_path: Path, max_candles: int = 500) -> dict:
    """
    Parse the AlgoMM log and extract:
      - candles: OHLCV bars (deduplicated by candle timestamp)
      - trades: entry/exit events with timestamps and prices
      - probabilities: UP/DOWN per tick

    Returns: {
        "candles": [{"time": epoch, "open": .., "high": .., "low": .., "close": .., "volume": ..}, ...],
        "trades": [{"time": epoch, "type": "ENTRY"|"EXIT", "side": "long"|"short", "price": .., "reason": ..}, ...],
        "probs": [{"time": epoch, "up": .., "down": ..}, ...],
    }
    """
    if not log_path.exists():
        return {"candles": [], "trades": [], "probs": []}

    # State machine: parse each log block (between ╔ and ╚)
    candle_map = {}  # candle_ts_str -> {time, o, h, l, c, v}
    trades = []
    probs = []

    # Current block state
    block_ts = None
    block_action = None
    block_reason = None
    block_price = None
    block_prob_up = None
    block_prob_down = None
    block_candle = None
    block_symbol = None

    def flush_block():
        nonlocal block_ts, block_action, block_reason, block_price
        nonlocal block_prob_up, block_prob_down, block_candle, block_symbol

        if block_candle:
            candle_key = block_candle["ts_str"]
            if candle_key not in candle_map:
                candle_map[candle_key] = block_candle

        if block_ts and block_prob_up is not None:
            try:
                ts_epoch = int(datetime.strptime(block_ts, "%Y-%m-%d %H:%M:%S")
                               .replace(tzinfo=ET).timestamp())
            except Exception:
                ts_epoch = None

            if ts_epoch:
                probs.append({
                    "time": ts_epoch,
                    "up": block_prob_up,
                    "down": block_prob_down,
                })

        if block_action and block_ts and block_price:
            try:
                ts_epoch = int(datetime.strptime(block_ts, "%Y-%m-%d %H:%M:%S")
                               .replace(tzinfo=ET).timestamp())
            except Exception:
                ts_epoch = None

            if ts_epoch:
                action = block_action.upper()
                if action.startswith("OPEN_"):
                    direction = action.replace("OPEN_", "").lower()
                    trades.append({
                        "time": ts_epoch,
                        "type": "ENTRY",
                        "side": direction,
                        "price": block_price,
                        "reason": block_reason or "",
                    })
                elif action == "EXIT":
                    trades.append({
                        "time": ts_epoch,
                        "type": "EXIT",
                        "side": "",
                        "price": block_price,
                        "reason": block_reason or "",
                    })

        # Reset
        block_ts = None
        block_action = None
        block_reason = None
        block_price = None
        block_prob_up = None
        block_prob_down = None
        block_candle = None
        block_symbol = None

    with log_path.open("r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.rstrip("\n")

            # New block start
            if line.startswith("╔"):
                flush_block()
                continue

            # Block end
            if line.startswith("╚"):
                flush_block()
                continue

            # Header: timestamp + decision
            m = LOG_HEADER_RE.search(line)
            if m:
                block_ts = m.group(1)
                block_action = m.group(2)
                continue

            # Price
            m = LOG_PRICE_RE.search(line)
            if m:
                block_symbol = m.group(1)
                try:
                    block_price = float(m.group(2))
                except ValueError:
                    pass
                continue

            # Probabilities
            m = LOG_PROB_NEW_RE.search(line)
            if m:
                try:
                    block_prob_up = float(m.group(1))
                    block_prob_down = float(m.group(2))
                except ValueError:
                    pass
                continue

            # Candle
            m = LOG_CANDLE_RE.search(line)
            if m:
                try:
                    candle_ts_str = m.group(1)
                    ts_epoch = int(datetime.strptime(candle_ts_str, "%Y-%m-%d %H:%M:%S")
                                   .replace(tzinfo=ET).timestamp())
                    block_candle = {
                        "ts_str": candle_ts_str,
                        "time": ts_epoch,
                        "open": float(m.group(2)),
                        "high": float(m.group(3)),
                        "low": float(m.group(4)),
                        "close": float(m.group(5)),
                        "volume": int(m.group(6).replace(",", "")),
                    }
                except (ValueError, IndexError):
                    pass
                continue

            # Action (might override header action)
            m = LOG_ACTION_RE.search(line)
            if m:
                block_action = m.group(1)
                continue

            # Reason
            m = LOG_REASON_RE.search(line)
            if m:
                block_reason = m.group(1).strip()
                continue

    # Final flush
    flush_block()

    # Sort candles by time and limit
    candles = sorted(candle_map.values(), key=lambda c: c["time"])
    filtered = []
    for c in candles:
        try:
            dt = datetime.fromtimestamp(c["time"], tz=ET)
            h, m = dt.hour, dt.minute
            if (h > 9 or (h == 9 and m >= 30)) and h < 16:
                filtered.append(c)
        except Exception:
            filtered.append(c)
    candles = filtered    
    # Remove internal key
    for c in candles:
        c.pop("ts_str", None)

    if len(candles) > max_candles:
        candles = candles[-max_candles:]

    return {
        "candles": candles,
        "trades": sorted(trades, key=lambda t: t["time"]),
        "probs": sorted(probs, key=lambda p: p["time"]),
    }


def _history_trade_markers(db: Session, bot: PaperStockTradeBot) -> list[dict]:
    markers = []
    rows = (
        db.query(PaperStockBotTradeHistory)
        .filter_by(user_id=bot.user_id, bot_id=bot.id)
        .order_by(PaperStockBotTradeHistory.entry_time.asc())
        .limit(200)
        .all()
    )
    for t in rows:
        side = (t.position_side or "").lower()
        if t.entry_time:
            markers.append({
                "time": int(t.entry_time.timestamp()),
                "type": "ENTRY",
                "side": side,
                "price": float(t.entry_price),
                "reason": "history",
            })
        if t.exit_time:
            markers.append({
                "time": int(t.exit_time.timestamp()),
                "type": "EXIT",
                "side": side,
                "price": float(t.exit_price),
                "reason": "history",
            })
    open_trade = (
        db.query(PaperStockBotOpenTrade)
        .filter_by(user_id=bot.user_id, bot_id=bot.id)
        .order_by(PaperStockBotOpenTrade.entry_time.desc())
        .first()
    )
    if open_trade and open_trade.entry_time:
        markers.append({
            "time": int(open_trade.entry_time.timestamp()),
            "type": "ENTRY",
            "side": (open_trade.position_side or "").lower(),
            "price": float(open_trade.entry_price),
            "reason": "open",
        })
    return sorted(markers, key=lambda t: t["time"])


def _fetch_market_candles_for_chart(symbol: str, interval: str, max_candles: int = 500) -> list[dict]:
    try:
        from app.utils.stock.schwab_price_history import get_schwab_intraday_multi_day, get_schwab_daily

        if str(interval).lower() == "1d":
            df = get_schwab_daily(symbol, period=12)
        else:
            df = get_schwab_intraday_multi_day(symbol, interval, num_days=5)
        if df is None or df.empty:
            return []
        df = df.tail(max_candles)
        candles = []
        for idx, row in df.iterrows():
            ts = idx
            if hasattr(ts, "timestamp"):
                epoch = int(ts.timestamp())
            else:
                epoch = int(datetime.fromisoformat(str(ts)).timestamp())
            candles.append({
                "time": epoch,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row.get("volume", 0) or 0),
            })
        return candles
    except Exception as exc:
        logging.warning("[CHART] market candle fallback failed for %s %s: %s", symbol, interval, exc)
        return []


@router.get("/auth/papertradebot/bot_candles_json")
def bot_candles_json(
    bot_id: int = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Returns candle OHLCV data + trade entry/exit markers + probabilities
    parsed from the bot's log file. Used by the live candlestick chart.
    """
    bot = (
        db.query(PaperStockTradeBot)
        .filter_by(id=bot_id, user_id=user.id)
        .first()
    )
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    log_path = _bot_log_path(user.id, bot.id, bot.symbol, bot.algo_name)
    data = _parse_bot_candles(log_path)

    if not data.get("candles") or not data.get("probs"):
        current_log_data = _parse_current_mm_log(log_path)
        if current_log_data.get("candles"):
            data["candles"] = current_log_data["candles"]
        if current_log_data.get("probs"):
            data["probs"] = current_log_data["probs"]
        if current_log_data.get("trades"):
            data["trades"] = current_log_data["trades"]

    # Commercial fallback:
    # Some AlgoMM bot logs are human-readable pretty logs, not JSONL.
    # Example:
    # Last Candle: 2026-05-18 15:50:00-04:00 | O: 410.85 H: 410.90 L: 410.20 C: 410.25 V:41292
    # If the legacy parser returns no candles, parse that format.
    if not data.get("candles"):
        pretty_candles = parse_pretty_bot_log_candles(log_path)
        if pretty_candles:
            data["candles"] = pretty_candles

    if not data.get("candles"):
        data["candles"] = _fetch_market_candles_for_chart(bot.symbol, bot.interval)

    history_markers = _history_trade_markers(db, bot)
    if history_markers:
        existing = {(t.get("time"), t.get("type"), t.get("price")) for t in data.get("trades", [])}
        for marker in history_markers:
            key = (marker.get("time"), marker.get("type"), marker.get("price"))
            if key not in existing:
                data.setdefault("trades", []).append(marker)
        data["trades"] = sorted(data.get("trades", []), key=lambda t: t["time"])

    # Also get thresholds from config
    try:
        cfg = json.loads(bot.config_json) if bot.config_json else {}
    except Exception:
        cfg = {}

    thresholds = {
        "long_entry_prob": float(cfg.get("long_entry_prob", cfg.get("entry_prob_long", 0.60))),
            "short_entry_prob": float(cfg.get("short_entry_prob", cfg.get("entry_prob_short", 0.40))),
        "prob_smoothing_bars": int(cfg.get("prob_smoothing_bars", 3)),
        "prob_exit_mode": str(cfg.get("prob_exit_mode", DEFAULT_BOT_CONFIG["prob_exit_mode"])),
        "prob_trail_drop": float(cfg.get("prob_trail_drop", 0.05)),
        "long_fixed_exit_prob": float(
            cfg.get("long_fixed_exit_prob", cfg.get("prob_fixed_exit_prob", DEFAULT_BOT_CONFIG["long_fixed_exit_prob"]))
        ),
        "short_fixed_exit_prob": float(
            cfg.get("short_fixed_exit_prob", cfg.get("prob_fixed_exit_prob", DEFAULT_BOT_CONFIG["short_fixed_exit_prob"]))
        ),
        "stop_loss_usd": float(cfg.get("stop_loss_usd", cfg.get("hard_stop_usd", DEFAULT_BOT_CONFIG["stop_loss_usd"]))),
        "hard_stop_usd": float(cfg.get("stop_loss_usd", cfg.get("hard_stop_usd", DEFAULT_BOT_CONFIG["stop_loss_usd"]))),
        "trailing_profit_usd": float(
            cfg.get(
                "trailing_profit_usd",
                cfg.get("trailing_stop_distance", cfg.get("trailing_stop_activation", DEFAULT_BOT_CONFIG["trailing_profit_usd"])),
            )
        ),
        "stop_loss_pct": float(cfg.get("stop_loss_pct", cfg.get("per_share_stop_pct", DEFAULT_BOT_CONFIG["stop_loss_pct"]))),
        "trailing_profit_pct": float(
            cfg.get("trailing_profit_pct", cfg.get("per_share_trailing_profit_pct", DEFAULT_BOT_CONFIG["trailing_profit_pct"]))
        ),
    }

    return JSONResponse({
        "bot_id": bot.id,
        "symbol": bot.symbol,
        "interval": bot.interval,
        "candles": data["candles"],
        "trades": data["trades"],
        "probs": data["probs"],
        "thresholds": thresholds,
    })
