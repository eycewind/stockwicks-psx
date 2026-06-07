# /var/www/stockwicks/app/utils/options/option_trade_utils.py

import logging
from datetime import datetime
from typing import List, Union, Optional, Any, Dict
import json
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

# --- Core Imports ---
from app.database.connection import SessionLocal
from app.models.paper_option_trading_bot import (
    PaperOptionTradeBot, PaperOptionBotOpenTrade, PaperOptionBotTradeHistory
)
from app.models.user import User
from app.services.email_service import EmailService, notification_already_sent, log_notification
from app.utils.time_utils import get_now_et

log = logging.getLogger(__name__)

# ------------------------ helpers ------------------------

def _parse_expiry(expiry_str: str) -> datetime:
    """
    Accepts 'YYYY-MM-DD' (preferred) or a full ISO timestamp string.
    Returns naive datetime (your schema uses 'timestamp without time zone').
    """
    if not expiry_str:
        raise ValueError("expiry_str is required")
    # Try common formats
    fmts = ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]
    last_err = None
    for fmt in fmts:
        try:
            return datetime.strptime(expiry_str, fmt)
        except Exception as e:
            last_err = e
    # Fallback: try fromisoformat
    try:
        return datetime.fromisoformat(expiry_str.replace("Z", ""))
    except Exception:
        pass
    raise ValueError(f"Unrecognized expiry format: {expiry_str!r}; last error: {last_err}")

def _coerce_float(x, default=None) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return default

# =========================================================================
# ======================== EMAIL HELPER (RESILIENT) =======================
# =========================================================================

def _send_trade_notification(db: Session, trade: Union[PaperOptionBotOpenTrade, PaperOptionBotTradeHistory]):
    """Internal helper to send email notifications for open/closed trades."""
    try:
        is_open_trade = isinstance(trade, PaperOptionBotOpenTrade)
        bot = db.get(PaperOptionTradeBot, trade.bot_id)
        if not bot or not bot.notify_email:
            return

        user = db.get(User, bot.user_id)
        if not user or not user.email:
            return

        if is_open_trade:
            if notification_already_sent(
                db, user_id=user.id,
                notification_type="option_trade",
                trade_id=getattr(trade, "id", None),
                trade_type='option'
            ):
                return

        subject_action = "Opened" if is_open_trade else "Closed"
        price = float(getattr(trade, 'entry_price', 0.0)) if is_open_trade else float(getattr(trade, 'exit_price', 0.0))
        pnl = None if is_open_trade else float(getattr(trade, 'pnl', 0.0))

        underlying_symbol = getattr(trade, 'underlying_symbol', None)
        quantity = getattr(trade, 'quantity', None)
        strike_price = getattr(trade, 'strike_price', None)
        expiry_date = getattr(trade, 'expiry_date', None)
        position_side = getattr(trade, 'position_side', None)
        side = getattr(trade, 'side', None)  # BUY/SELL

        side_for_email = f"{subject_action.upper()} {side or 'TRADE'}"

        if not all([underlying_symbol, quantity, bot.interval, bot.algo_name]):
            log.warning("Skipping email for trade %s: missing data.", getattr(trade, 'id', 'N/A'))
            return

        EmailService().send_option_trade_notification(
            email=user.email,
            symbol=underlying_symbol,
            side=side_for_email,
            price=price,
            qty=quantity,
            interval=bot.interval,
            algo_name=bot.algo_name,
            strike=strike_price,
            expiry=expiry_date,
            position_side=position_side,
            pnl=pnl
        )

        if is_open_trade:
            log_notification(
                db,
                user_id=user.id,
                notification_type="option_trade",
                trade_id=getattr(trade, 'id', None),
                email_to=user.email,
                payload={"trade_id": getattr(trade, 'id', None)},
                trade_type='option'
            )

    except Exception as e:
        log.error("Failed to send trade notification for trade #%s: %s",
                  getattr(trade, 'id', 'N/A'), e, exc_info=True)

# =========================================================================
# ======================== TRADE EXECUTION (SCHEMA-MATCH) =================
# =========================================================================

def open_option_trade(
    db: Session, bot: PaperOptionTradeBot, option_symbol: str, underlying: str,
    trade_type: str, position_side: str, qty: int, entry_price: float,
    strike: float, expiry: str,
    stop_loss: float, take_profit: float
) -> Optional[PaperOptionBotOpenTrade]:
    """
    SINGLE-LEG open trade for paper_option_bot_open_trades.
    Maps to columns present in your schema:
      - side (BUY/SELL)  -> from trade_type.upper()
      - position_side    -> 'call' / 'put' (lowercase stored)
      - expiry_date      -> parsed datetime
      - planned_take_profit / planned_stop_loss
      - algo_name / interval copied from bot for convenience
    """
    trade: Optional[PaperOptionBotOpenTrade] = None
    try:
        # Basic validation
        if qty <= 0: raise ValueError("Quantity must be positive.")
        if entry_price <= 0: raise ValueError("Entry price must be positive.")
        if stop_loss <= 0: raise ValueError("Stop loss must be positive.")
        if take_profit <= 0: raise ValueError("Take profit must be positive.")
        if not expiry: raise ValueError("Expiry date is required.")
        expiry_dt = _parse_expiry(expiry)

        log.info(
            "[PAPER OPEN Attempt] Bot #%s: %s %s %sx %s @ $%.4f, TP=$%.4f, SL=$%.4f",
            bot.id, trade_type, position_side, qty, option_symbol, entry_price, take_profit, stop_loss
        )

        # Constructor ONLY with columns that exist in table
        trade = PaperOptionBotOpenTrade(
            bot_id=bot.id,
            user_id=bot.user_id,
            option_symbol=option_symbol,
            underlying_symbol=underlying,
            position_side=(position_side or "").lower(),
            quantity=qty,
            entry_price=entry_price,
            strike_price=strike,
            entry_time=get_now_et(),
            planned_stop_loss=stop_loss,
            planned_take_profit=take_profit,
            side=(trade_type or "").upper(),  # BUY/SELL
            expiry_date=expiry_dt,
            algo_name=bot.algo_name,
            interval=bot.interval,
        )

        db.add(trade)
        db.flush()
        db.refresh(trade)
        log.info("[PAPER OPEN Success] Bot #%s: Opened single-leg Trade ID #%s", bot.id, trade.id)

        _send_trade_notification(db, trade)
        db.commit()
        return trade

    except IntegrityError:
        log.warning("[PAPER OPEN IntegrityError] Bot #%s: Duplicate trade attempt. Rolling back.", bot.id)
        db.rollback()
        return None
    except Exception as e:
        log.error("[PAPER OPEN Exception] Single Leg for Bot #%s, Trade ID #%s: %s",
                  bot.id, getattr(trade, 'id', 'N/A'), e, exc_info=True)
        db.rollback()
        return None


def open_spread_trade(
    db: Session, bot: PaperOptionTradeBot, legs: List[dict], trade_type: str, position_side: str,
    qty: int, entry_price: float,
    stop_loss: float, take_profit: float
) -> Optional[PaperOptionBotOpenTrade]:
    """
    SPREAD open trade.
    Stores long/short legs into:
      option_symbol (we store the LONG leg’s symbol),
      side (BUY/SELL of the overall spread),
      strike_price (long strike),
      option_symbol_short / side_short / strike_price_short (the SHORT leg),
      expiry_date (same for both legs),
      planned_take_profit / planned_stop_loss.
    """
    trade: Optional[PaperOptionBotOpenTrade] = None
    try:
        if not legs or len(legs) < 2: raise ValueError("Spread requires at least two legs.")
        if qty <= 0: raise ValueError("Quantity must be positive.")
        if stop_loss <= 0: raise ValueError("Stop loss value must be positive.")
        if take_profit <= 0: raise ValueError("Take profit value must be positive.")

        # Identify legs by side
        short_leg = next((leg for leg in legs if (leg.get('side', '').lower() in ['sell', 'short'])), None)
        long_leg  = next((leg for leg in legs if (leg.get('side', '').lower() in ['buy', 'long'])), None)
        if not short_leg or not long_leg:
            raise ValueError("Could not identify short and long legs from input.")

        exp_short = short_leg.get('expiration')
        exp_long  = long_leg.get('expiration')
        if not exp_short or not exp_long:
            raise ValueError("Missing expiration on spread legs.")
        if exp_short != exp_long:
            log.warning("[open_spread_trade] Bot #%s: Different expiries (%s vs %s). Using short leg expiry.",
                        bot.id, exp_short, exp_long)
        expiry_dt = _parse_expiry(exp_short)

        short_symbol = short_leg.get("occ") or short_leg.get("symbol")
        long_symbol  = long_leg.get("occ")  or long_leg.get("symbol")
        short_strike = _coerce_float(short_leg.get("strike"))
        long_strike  = _coerce_float(long_leg.get("strike"))
        short_type   = (short_leg.get("type") or short_leg.get("putCall","")).lower()
        long_type    = (long_leg.get("type")  or long_leg.get("putCall","")).lower()

        if not all([short_symbol, long_symbol, short_strike is not None, long_strike is not None]):
            raise ValueError("Missing symbols/strikes for spread legs.")

        spread_desc = f"{position_side.upper()} SPREAD {long_strike}/{short_strike}"

        log.info("[PAPER OPEN Attempt] Bot #%s: %s %s %sx @ $%.4f, TP=$%.4f, SL=$%.4f",
                 bot.id, trade_type, spread_desc, qty, entry_price, take_profit, stop_loss)

        # By convention here:
        # - option_symbol / strike_price = LONG leg
        # - option_symbol_short / side_short / strike_price_short = SHORT leg
        trade = PaperOptionBotOpenTrade(
            bot_id=bot.id,
            user_id=bot.user_id,
            option_symbol=long_symbol,
            underlying_symbol=bot.symbol,
            position_side=(position_side or "").lower(),
            quantity=qty,
            entry_price=entry_price,
            strike_price=long_strike,
            entry_time=get_now_et(),
            planned_stop_loss=stop_loss,
            planned_take_profit=take_profit,
            side=(trade_type or "").upper(),        # overall trade side (BUY for debit spread, SELL for credit spread)
            expiry_date=expiry_dt,
            option_symbol_short=short_symbol,
            # side_short="SELL",                      # short leg is a sell
            side_short=(short_type or "").lower(),
            strike_price_short=short_strike,
            algo_name=bot.algo_name,
            interval=bot.interval,
        )

        db.add(trade)
        db.flush()
        db.refresh(trade)
        log.info("[PAPER OPEN Success] Bot #%s: Opened SPREAD Trade ID #%s", bot.id, trade.id)

        _send_trade_notification(db, trade)
        db.commit()
        return trade

    except Exception as e:
        log.error("[PAPER OPEN Exception] SPREAD for Bot #%s, Trade ID #%s: %s",
                  bot.id, getattr(trade, 'id', 'N/A'), e, exc_info=True)
        db.rollback()
        return None


# =========================================================================
# ======================== CLOSE TRADE (SCHEMA-MATCH) =====================
# =========================================================================

def close_option_trade(trade: PaperOptionBotOpenTrade, exit_price: float) -> Optional[dict]:
    """
    Closes an open option trade (single or spread) and records it in paper_option_bot_trade_history.
    Returns {"hist_id": ..., "bot_id": ...} on success, else None.
    Uses its own DB session.
    """
    db = SessionLocal()
    hist: Optional[PaperOptionBotTradeHistory] = None
    returned_data: Optional[Dict[str, Any]] = None

    trade_id_log = getattr(trade, 'id', 'N/A')
    bot_id_log = getattr(trade, 'bot_id', 'N/A')

    try:
        log.debug("[PAPER CLOSE Debug] Trade ID #%s: Merging trade into fresh session.", trade_id_log)
        trade_in_session = db.merge(trade)
        if not trade_in_session:
            log.warning("[PAPER CLOSE] Trade ID #%s could not be found/merged.", trade_id_log)
            return None

        log.info("[PAPER CLOSE Attempt] Trade ID #%s (Bot #%s): Exit @ $%.4f",
                 trade_id_log, bot_id_log, exit_price)

        entry_price_fl = float(getattr(trade_in_session, 'entry_price', 0.0) or 0.0)
        quantity_fl    = float(getattr(trade_in_session, 'quantity', 0.0) or 0.0)
        side_upper     = (getattr(trade_in_session, 'side', '') or '').upper()  # BUY/SELL
        multiplier = 100.0 # Define multiplier

        if side_upper == "BUY":
            pnl = (exit_price - entry_price_fl) * quantity_fl * multiplier
        elif side_upper == "SELL":
            pnl = (entry_price_fl - exit_price) * quantity_fl * multiplier
        else:
            log.warning("[PAPER CLOSE] Trade ID #%s: Unknown side '%s'. Assuming BUY for PnL.",
                        trade_id_log, side_upper)
            pnl = (exit_price - entry_price_fl) * quantity_fl * multiplier

        log.info("[PAPER CLOSE PnL Calc] Trade ID #%s: Side=%s, Entry=$%.4f, Exit=$%.4f, Qty=%.0f -> PnL=$%.2f",
                 trade_id_log, side_upper or "UNKNOWN", entry_price_fl, exit_price, quantity_fl, pnl)

        # Build history record with schema columns
        hist = PaperOptionBotTradeHistory(
            bot_id=getattr(trade_in_session, 'bot_id', None),
            user_id=getattr(trade_in_session, 'user_id', None),
            option_symbol=getattr(trade_in_session, 'option_symbol', None),
            underlying_symbol=getattr(trade_in_session, 'underlying_symbol', None),
            position_side=getattr(trade_in_session, 'position_side', None),
            quantity=getattr(trade_in_session, 'quantity', None),
            entry_price=getattr(trade_in_session, 'entry_price', None),
            exit_price=exit_price,
            strike_price=getattr(trade_in_session, 'strike_price', None),
            pnl=pnl,
            planned_stop_loss=getattr(trade_in_session, 'planned_stop_loss', None),
            planned_take_profit=getattr(trade_in_session, 'planned_take_profit', None),
            entry_time=getattr(trade_in_session, 'entry_time', None),
            exit_time=get_now_et(),
            side=getattr(trade_in_session, 'side', None),
            reason=None,
            expiry_date=getattr(trade_in_session, 'expiry_date', None),
            option_symbol_short=getattr(trade_in_session, 'option_symbol_short', None),
            side_short=getattr(trade_in_session, 'side_short', None),
            strike_price_short=getattr(trade_in_session, 'strike_price_short', None),
            algo_name=getattr(trade_in_session, 'algo_name', None),
            interval=getattr(trade_in_session, 'interval', None),
        )

        db.add(hist)
        db.delete(trade_in_session)
        db.flush()
        hist_id = getattr(hist, 'id', None)
        log.info("[PAPER CLOSE Success] Trade ID #%s: Closed. History ID: %s, Bot #%s, P/L: $%.2f",
                 trade_id_log, hist_id, getattr(hist, 'bot_id', None), pnl)

        try:
            _send_trade_notification(db, hist)
        except Exception as email_err:
            log.error("[PAPER CLOSE Email Error] HistID %s: %s", hist_id, email_err, exc_info=True)

        db.commit()
        log.info("[PAPER CLOSE Commit] Trade ID #%s: Commit complete.", trade_id_log)
        returned_data = {"hist_id": hist_id, "bot_id": getattr(hist, 'bot_id', None)}

    except Exception as e:
        log.error("[PAPER CLOSE Exception] Trade ID #%s (Bot #%s). HistID=%s. Error: %s",
                  trade_id_log, bot_id_log, getattr(hist, 'id', 'N/A'), e, exc_info=True)
        db.rollback()
        returned_data = None
    finally:
        db.close()
        log.debug("[PAPER CLOSE Session Closed] DB session closed for trade ID %s.", trade_id_log)

    return returned_data
