# /var/stockwicks/clients/ashakil/app/services/paper_trade_service.py
# app/services/paper_trade_service.py
from __future__ import annotations
import json
import math
from sqlalchemy import inspect as sa_inspect
import logging
import os
from datetime import datetime
from typing import Optional
from sqlalchemy import text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from app.models.paper_trading_bot import (
    PaperStockTradeBot,
    PaperStockBotOpenTrade,
    PaperStockBotTradeHistory,
)
from app.models.user import User
from app.services.email_service import EmailService
from app.utils.time_utils import get_now_et
# NEW: we use this to submit live orders (it already loads token from user dir)
from app.utils.schwab_trade import submit_equity_order
# NEW: we resolve the user's account id from DB
from app.models.schwab_accounts import SchwabAccount

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s (TRADE_SERVICE) %(message)s")
log = logging.getLogger(__name__)

email_service = EmailService()


def _entry_quantity_for_bot(bot: PaperStockTradeBot, price: float) -> float:
    """Return the quantity for an entry, recalculating Sparkie full-cash bots.

    A Replay session is priced in the past, so its historical share count must
    never be copied unchanged into a live order.  Sparkie specifies a dollar
    allocation and derives whole shares from the live entry price instead.
    """
    default_qty = float(getattr(bot, "trade_size", 0.0) or 0.0)
    raw_config = getattr(bot, "config_json", None) or "{}"
    try:
        config = json.loads(raw_config)
    except (TypeError, ValueError):
        config = {}

    if config.get("sparkie_cash_deployment_policy") != "full_cash_v1":
        return default_qty

    allocation = float(config.get("sparkie_allocation_usd") or 0.0)
    entry_price = float(price or 0.0)
    if not math.isfinite(allocation) or allocation <= 0:
        raise ValueError("Sparkie bot has no valid cash allocation.")
    if not math.isfinite(entry_price) or entry_price <= 0:
        raise ValueError("Sparkie bot received an invalid live entry price.")

    quantity = int(allocation // entry_price)
    if quantity < 1:
        raise ValueError(
            f"Sparkie allocation ${allocation:,.2f} cannot buy one share at ${entry_price:,.2f}."
        )

    # Persist the actual sizing decision for auditability and make the bot UI
    # show the quantity used on the most recent live/paper entry.
    config.update(
        {
            "sparkie_sizing_mode": "full_cash_at_entry_price",
            "sparkie_last_entry_allocation_usd": round(allocation, 2),
            "sparkie_last_entry_price": round(entry_price, 4),
            "sparkie_last_entry_quantity": quantity,
        }
    )
    bot.trade_size = float(quantity)
    bot.quantity = int(quantity)
    bot.config_json = json.dumps(config, separators=(",", ":"), sort_keys=True)
    return float(quantity)


# -----------------------------------------------------------------------------
# Account helpers
# -----------------------------------------------------------------------------
def _resolve_default_account_id(db: Session, user_id: int) -> Optional[str]:
    """
    Returns an account identifier usable by submit_equity_order(... account_id=...).
    Prefers account_hash (Schwab Trader API format). Falls back to account_number.
    Selection rules:
      1) is_default == True if present
      2) otherwise, the most recently created/updated row
    """
    q = db.query(SchwabAccount).filter(SchwabAccount.user_id == user_id)

    # Prefer explicit default if column exists
    try:
        default = q.filter(getattr(SchwabAccount, "is_default", False) == True).first()
        if default:
            return default.account_hash or default.account_number
    except Exception:
        pass

    # Fallback: last row (you can change to created_at/updated_at if present)
    row = q.order_by(SchwabAccount.id.desc()).first()
    if row:
        return row.account_hash or row.account_number

    log.error("[SCHWAB] No SchwabAccount rows found for user_id=%s", user_id)
    return None

def _model_columns(model) -> set[str]:
    try:
        return {c.key for c in model.__table__.columns}
    except Exception:
        return {a.key for a in sa_inspect(model).mapper.attrs}

def _fit_str(model, field: str, val: str | None) -> str | None:
    """
    If model.field is a VARCHAR with length, truncate val to fit.
    Safe no-op for None or non-string values.
    """
    if val is None or not isinstance(val, str):
        return val
    try:
        col = model.__table__.columns.get(field)
        if col is not None and hasattr(col.type, "length") and col.type.length:
            maxlen = int(col.type.length)
            if len(val) > maxlen:
                return val[:maxlen]
    except Exception:
        pass
    return val
# -----------------------------------------------------------------------------
# Live Trading (mirroring)
# -----------------------------------------------------------------------------

def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _commercial_live_trading_allowed() -> bool:
    """
    Commercial MVP safety gate.

    Live Schwab mirroring is blocked unless both env flags are explicitly true:
    - TRADING_ENABLED=true
    - LIVE_TRADING_ENABLED=true
    """
    return _env_bool("TRADING_ENABLED", False) and _env_bool("LIVE_TRADING_ENABLED", False)


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


def _mirror_live_equity_order(
    db: Session,
    *,
    user_id: int,
    symbol: str,
    qty: float,
    instruction: str,      # BUY / SELL / SELL_SHORT / BUY_TO_COVER
    order_type: str = "MARKET",
    tif: str = "DAY",
    extended_hours: bool = False,
) -> Optional[str]:
    """
    Place a live equity order via Schwab Trader API. This:
      - resolves the account id from DB for the user,
      - calls submit_equity_order(),
      - which will load the user's token from /var/stockwicks/clients/ashakil/data/<user_id>/schwab_trade_token.json.
    """
    if not _commercial_live_trading_allowed():
        log.warning("[LIVE] Blocked by commercial safety gate: TRADING_ENABLED and LIVE_TRADING_ENABLED must both be true.")
        return None

    acct_id = _resolve_default_account_id(db, user_id)
    if not acct_id:
        log.error("[LIVE] No account id available for user_id=%s; skipping mirror.", user_id)
        return None

    try:
        order_id = submit_equity_order(
            account_id=acct_id,            # number or hash; resolver accepts both
            symbol=symbol,
            side=instruction,              # BUY / SELL / SELL_SHORT / BUY_TO_COVER
            qty=float(qty),
            order_type=order_type,
            limit_price=None,
            time_in_force=tif,
            extended_hours=extended_hours,
            user_id=user_id,               # commercial token: DATA_DIR/<user_id>/schwab_trade_token.json
        )
        log.info("[LIVE] OK user_id=%s order_id=%s %s %s x%s", user_id, order_id, instruction, symbol, qty)
        return order_id
    except Exception as e:
        log.error("[LIVE] submit_equity_order failed: %s", e, exc_info=True)
        return None


# -----------------------------------------------------------------------------
# Email helper
# -----------------------------------------------------------------------------
def _send_notification(bot: Optional[PaperStockTradeBot], subject: str, body_html: str):
    try:
        if not bot or not getattr(bot, "notify_email", False):
            return
        u: Optional[User] = getattr(bot, "user", None)
        to_email = getattr(u, "email", None) if u else None
        if not to_email:
            return
        log.info("[EMAIL] -> %s | %s", to_email, subject)
        email_service._send_email(to_email=to_email, subject=subject, body_html=body_html)
    except Exception as e:
        log.error("[EMAIL] Failed: %s", e, exc_info=True)


# -----------------------------------------------------------------------------
# Core Trading Functions (Paper)
# -----------------------------------------------------------------------------
def open_position(db: Session, bot: PaperStockTradeBot, side: str, price: float) -> Optional[PaperStockBotOpenTrade]:
    """
    Creates a new PaperStockBotOpenTrade record.
    side: "long" or "short"
    """
    try:
        if bot is None:
            raise ValueError("open_position: bot is required")

        qty = _entry_quantity_for_bot(bot, float(price))
        if qty <= 0:
            raise ValueError("open_position: trade_size must be > 0")

        ot = PaperStockBotOpenTrade(
            bot_id=bot.id,
            user_id=bot.user_id,
            symbol=bot.symbol,
            position_side=side.lower(),
            quantity=qty,
            entry_price=float(price),
            current_price=float(price),
            entry_time=get_now_et(),
        )
        db.add(ot)
        db.commit()
        log.info("[OPEN] Bot#%s %s %s qty=%s @ %.4f", bot.id, side.upper(), bot.symbol, qty, price)

        # Notify
        _send_notification(
            bot,
            subject=f"Trade Opened: {bot.symbol}",
            body_html=f"A new {side.upper()} trade was opened for {bot.symbol} at ${price:.2f} by Bot #{bot.id} ({bot.algo_name}).",
        )

        # Mirror (optional)
        if getattr(bot, "mirror_live", False):
            instr = "BUY" if side.lower() == "long" else "SELL_SHORT"
            _mirror_live_equity_order(db, user_id=bot.user_id, symbol=bot.symbol, qty=qty, instruction=instr)

        return ot

    except Exception as e:
        db.rollback()
        log.error("[OPEN] Failed for bot #%s: %s", getattr(bot, "id", "?"), e, exc_info=True)
        return None

# add near imports (top of file)
from sqlalchemy import inspect as sa_inspect

def _model_columns(model) -> set[str]:
    try:
        return {c.key for c in model.__table__.columns}
    except Exception:
        return {a.key for a in sa_inspect(model).mapper.attrs}

# replace your close_position with this version
# def close_position(db: Session, trade: PaperStockBotOpenTrade, price: float) -> Optional[PaperStockBotTradeHistory]:
#     """
#     Close an open trade, write to history using only fields that exist on the model,
#     delete the open row, and (optionally) mirror the live order.
#     """
#     try:
#         if trade is None:
#             raise ValueError("close_position: trade is required")

#         # ensure bot for notifications/mirroring
#         bot: Optional[PaperStockTradeBot] = getattr(trade, "bot", None)
#         if bot is None:
#             bot = db.query(PaperStockTradeBot).get(trade.bot_id)

#         side = (trade.position_side or "").lower()  # "long" or "short"
#         qty = float(trade.quantity or 0.0)
#         ep = float(trade.entry_price)
#         xp = float(price)
#         if side not in {"long", "short"}:
#             raise ValueError(f"Unknown position_side: {trade.position_side}")

#         pnl_val = (xp - ep) * qty if side == "long" else (ep - xp) * qty

#         cols = _model_columns(PaperStockBotTradeHistory)
#         now = get_now_et()

#         # Build create kwargs safely
#         kw = {}

#         # always-safe core fields (guard with column check)
#         for key, val in {
#             "bot_id": trade.bot_id,
#             "user_id": trade.user_id,
#             "symbol": trade.symbol,
#             "quantity": qty,
#             "entry_price": ep,
#             "exit_price": xp,
#         }.items():
#             if key in cols:
#                 kw[key] = val

#         # side field variants
#         if "position_side" in cols:
#             kw["position_side"] = side
#         elif "side" in cols:
#             kw["side"] = side

#         # pnl variants
#         if "pnl" in cols:
#             kw["pnl"] = pnl_val
#         elif "pl" in cols:
#             kw["pl"] = pnl_val
#         elif "profit_loss" in cols:
#             kw["profit_loss"] = pnl_val
#         elif "realized_pl" in cols:
#             kw["realized_pl"] = pnl_val

#         # algo name if present
#         algo_name = getattr(bot, "algo_name", getattr(trade, "algo_name", None))
#         if algo_name is not None and "algo_name" in cols:
#             kw["algo_name"] = algo_name

#         # timestamps (support multiple schemas)
#         if "entry_time" in cols:
#             kw["entry_time"] = trade.entry_time
#         if "exit_time" in cols:
#             kw["exit_time"] = now
#         if "opened_at" in cols and "entry_time" not in cols:
#             kw["opened_at"] = trade.entry_time
#         if "closed_at" in cols and "exit_time" not in cols:
#             kw["closed_at"] = now
#         if "created_at" in cols and "entry_time" not in cols:
#             kw["created_at"] = trade.entry_time
#         if "updated_at" in cols:
#             kw["updated_at"] = now

#         # trade type / action
#         exit_instr = "SELL" if side == "long" else "BUY_COVER"  # <= 10 chars (fix)
#         if "trade_type" in cols:
#             kw["trade_type"] = exit_instr
#         elif "action" in cols:
#             kw["action"] = exit_instr

#         # create history row
#         hist = PaperStockBotTradeHistory(**kw)
#         db.add(hist)
#         db.delete(trade)
#         db.commit()

#         bot_desc = f"Bot#{getattr(bot,'id',trade.bot_id)} ({getattr(bot,'algo_name','?')})" if bot else f"bot_id={trade.bot_id}"
#         log.info("[CLOSE] %s %s qty=%s @ %.4f pnl=%.2f", bot_desc, hist.symbol, qty, xp, pnl_val)

#         # notify
#         _send_notification(
#             bot,
#             subject=f"Trade Closed: {hist.symbol}",
#             body_html=f"The {side.upper()} trade for {hist.symbol} was closed at ${xp:.2f} for a P/L of ${pnl_val:.2f}.",
#         )

#         # live mirror (account id resolved in _mirror_live_equity_order via submit_equity_order path)
#         if bot and getattr(bot, "mirror_live", False):
#             _mirror_live_equity_order(
#                 db,
#                 user_id=bot.user_id,
#                 symbol=hist.symbol,
#                 qty=qty,
#                 instruction=exit_instr,
#             )

#         return hist

#     except Exception as e:
#         db.rollback()
#         log.error(f"[CLOSE] Failed to close trade #{getattr(trade,'id','?')}: {e}", exc_info=True)
#         return None

def close_position(db: Session, trade: PaperStockBotOpenTrade, price: float) -> Optional[PaperStockBotTradeHistory]:
    """
    Close an open trade, write to history using only fields that exist on the model,
    delete the open row, and (optionally) mirror the live order.
    """
    try:
        if trade is None:
            raise ValueError("close_position: trade is required")

        # ensure bot for notifications/mirroring
        bot: Optional[PaperStockTradeBot] = getattr(trade, "bot", None)
        if bot is None:
            bot = db.get(PaperStockTradeBot, trade.bot_id)  # modern API (no deprecated .query(...).get())

        side = (trade.position_side or "").lower()  # "long" or "short"
        qty = float(trade.quantity or 0.0)
        ep = float(trade.entry_price)
        xp = float(price)
        if side not in {"long", "short"}:
            raise ValueError(f"Unknown position_side: {trade.position_side}")

        pnl_val = (xp - ep) * qty if side == "long" else (ep - xp) * qty

        cols = _model_columns(PaperStockBotTradeHistory)
        now = get_now_et()

        # Build create kwargs safely
        kw = {}

        # always-safe core fields (guard with column check)
        for key, val in {
            "bot_id": trade.bot_id,
            "user_id": trade.user_id,
            "symbol": trade.symbol,
            "quantity": qty,
            "entry_price": ep,
            "exit_price": xp,
        }.items():
            if key in cols:
                kw[key] = val

        # side field variants
        if "position_side" in cols:
            kw["position_side"] = side
        elif "side" in cols:
            kw["side"] = side

        # pnl variants
        if "pnl" in cols:
            kw["pnl"] = pnl_val
        elif "pl" in cols:
            kw["pl"] = pnl_val
        elif "profit_loss" in cols:
            kw["profit_loss"] = pnl_val
        elif "realized_pl" in cols:
            kw["realized_pl"] = pnl_val

        # algo name if present
        algo_name = getattr(bot, "algo_name", getattr(trade, "algo_name", None))
        if algo_name is not None and "algo_name" in cols:
            kw["algo_name"] = algo_name

        # timestamps (support multiple schemas)
        if "entry_time" in cols:
            kw["entry_time"] = trade.entry_time
        if "exit_time" in cols:
            kw["exit_time"] = now
        if "opened_at" in cols and "entry_time" not in cols:
            kw["opened_at"] = trade.entry_time
        if "closed_at" in cols and "exit_time" not in cols:
            kw["closed_at"] = now
        if "created_at" in cols and "entry_time" not in cols:
            kw["created_at"] = trade.entry_time
        if "updated_at" in cols:
            kw["updated_at"] = now

        # trade type / action (MATCH your DB allowed values)
        exit_instr = "SELL" if side == "long" else "BUY_TO_COVER"
        if "trade_type" in cols:
            kw["trade_type"] = exit_instr
        elif "action" in cols:
            kw["action"] = exit_instr

        # create history row
        hist = PaperStockBotTradeHistory(**kw)
        db.add(hist)
        db.delete(trade)
        db.commit()

        bot_desc = f"Bot#{getattr(bot,'id',trade.bot_id)} ({getattr(bot,'algo_name','?')})" if bot else f"bot_id={trade.bot_id}"
        log.info("[CLOSE] %s %s qty=%s @ %.4f pnl=%.2f", bot_desc, hist.symbol, qty, xp, pnl_val)

        # notify
        _send_notification(
            bot,
            subject=f"Trade Closed: {hist.symbol}",
            body_html=f"The {side.upper()} trade for {hist.symbol} was closed at ${xp:.2f} for a P/L of ${pnl_val:.2f}.",
        )

        # live mirror (account id resolved in _mirror_live_equity_order via submit_equity_order path)
        if bot and getattr(bot, "mirror_live", False):
            schwab_order_id = _mirror_live_equity_order(
                db,
                user_id=bot.user_id,
                symbol=hist.symbol,
                qty=qty,
                instruction=exit_instr,
            )
            try:
                _ensure_live_mirror_history_table(db)
                db.execute(
                    text("""
                        INSERT INTO paper_stock_bot_live_mirror_history (
                            user_id, bot_id, history_trade_id, symbol, side, quantity,
                            entry_price, exit_price, profit_loss, entry_time, exit_time,
                            schwab_order_id, mirror_status
                        )
                        VALUES (
                            :user_id, :bot_id, :history_trade_id, :symbol, :side, :quantity,
                            :entry_price, :exit_price, :profit_loss, :entry_time, :exit_time,
                            :schwab_order_id, :mirror_status
                        )
                    """),
                    {
                        "user_id": hist.user_id,
                        "bot_id": hist.bot_id,
                        "history_trade_id": hist.id,
                        "symbol": hist.symbol,
                        "side": side,
                        "quantity": qty,
                        "entry_price": ep,
                        "exit_price": xp,
                        "profit_loss": pnl_val,
                        "entry_time": getattr(hist, "entry_time", None),
                        "exit_time": getattr(hist, "exit_time", None),
                        "schwab_order_id": schwab_order_id,
                        "mirror_status": "submitted" if schwab_order_id else "requested",
                    },
                )
                db.commit()
            except Exception as mirror_exc:
                db.rollback()
                log.error("[LIVE] Failed to record mirrored history for hist_id=%s: %s", getattr(hist, "id", "?"), mirror_exc, exc_info=True)

        return hist

    except Exception as e:
        db.rollback()
        log.error(f"[CLOSE] Failed to close trade #{getattr(trade,'id','?')}: {e}", exc_info=True)
        return None
