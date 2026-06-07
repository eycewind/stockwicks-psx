#/var/www/stockwicks/app/utils/paper_trade_engine.py
"""
paper_trade_engine.py

This module provides the core logic for simulating paper trades in a stock trading bot system.
It includes the following functionalities:

- Checks if the market is currently open (custom rule: open from 00:00 to 23:55 ET).
- Retrieves recent minute-level stock price history using the Schwab API.
- Resamples raw price data to higher intervals (1min, 5min, etc.).
- Executes simulated buy/sell trades:
    - Opens new long or short positions.
    - Closes existing positions and calculates profit/loss.
    - Updates a paper trading account's virtual balance.
- Sends optional email notifications for trade entry/exit.
- Handles missing data with optional CSV fallback and robust error handling.

Used primarily by Celery tasks or bot runner scripts to manage algorithm-based stock trading strategies.
"""

import os
import pandas as pd
import numpy as np
import requests
import logging
from datetime import datetime, timedelta, time as dtime
import pytz
import time
from dotenv import load_dotenv

from decimal import Decimal
from sqlalchemy.orm import Session

from app.database.connection import SessionLocal
from app.models.paper_trading import PaperAccount
from app.models.paper_trading_bot import (
    PaperStockTradeBot,
    PaperStockBotOpenTrade,
    PaperStockBotTradeHistory,
)
from app.models.user import User
from app.utils.stock.market_price import get_live_price
from app.services.email_service import (
    EmailService, notification_already_sent, log_notification
)
from app.utils.stock.schwab_token import get_valid_access_token

load_dotenv()
os.environ['PYTHONIOENCODING'] = 'utf-8'
logging.basicConfig(level=logging.INFO)
np.random.seed(42)

_ET = pytz.timezone("US/Eastern")

# def is_market_open_now(now_et: datetime | None = None) -> bool:
#     now_et = now_et or datetime.now(_ET)
#     t = now_et.time()
#     return dtime(0, 0) <= t < dtime(23, 55)

# WITH:
from app.utils.common.trading_window import is_within_trading_window

def is_market_open_now(now_et: datetime | None = None) -> bool:
    return is_within_trading_window(now_et)

def max_bar_age_for_interval(interval: str) -> timedelta:
    interval = (interval or "1min").lower()
    return {
        "1min": timedelta(minutes=3),
        "5min": timedelta(minutes=12),
        "10min": timedelta(minutes=20),
        "15min": timedelta(minutes=35),
        "30min": timedelta(minutes=80),
        "1h": timedelta(minutes=140),
        "1d": timedelta(days=2),
        "1wk": timedelta(days=14),
    }.get(interval, timedelta(minutes=10))

def get_schwab_1min_history(symbol, num_days=7, interval: str = "1min"):
    """
    Fetch Schwab intraday history for the given interval.
    Despite the name, supports more than 1min (5min, 15min, etc.).
    """

    interval = (interval or "1min").lower()
    freq_map = {
        "1min": 1,
        "5min": 5,
        "10min": 10,
        "15min": 15,
        "30min": 30,
        "1h": 60,  # Schwab may cap this differently
    }
    freq = freq_map.get(interval, 1)  # default back to 1min if unknown

    eastern = pytz.timezone('US/Eastern')
    all_dfs = []
    today = pd.Timestamp.now(tz=eastern).date()

    for days_back in range(num_days):
        day = today - pd.Timedelta(days=days_back)
        if day.weekday() >= 5:
            continue
        start_dt = pd.Timestamp(day, tz=eastern).replace(hour=9, minute=30)
        end_dt   = pd.Timestamp(day, tz=eastern).replace(hour=16, minute=0)
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms   = int(end_dt.timestamp() * 1000)

        access_token = get_valid_access_token()
        if not access_token:
            continue

        url = "https://api.schwabapi.com/marketdata/v1/pricehistory"
        params = {
            "symbol": symbol.upper(),
            "frequencyType": "minute",
            "frequency": freq,   # ✅ dynamic frequency
            "startDate": start_ms,
            "endDate": end_ms,
            "needExtendedHoursData": "false"
        }
        headers = {"Authorization": f"Bearer {access_token}"}

        try:
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            if not data.get("candles"):
                continue

            day_df = pd.DataFrame(data["candles"])
            if "datetime" in day_df.columns:
                day_df["timestamp"] = pd.to_datetime(
                    day_df["datetime"], unit='ms', utc=True
                ).dt.tz_convert(eastern)
                day_df = day_df.set_index("timestamp")

            all_dfs.append(day_df[["open", "high", "low", "close", "volume"]])
            time.sleep(0.2)
        except Exception as e:
            logging.warning(f"[SCHWAB] History fetch failed for {symbol}@{interval}: {e}")
            continue

    if not all_dfs:
        return pd.DataFrame()

    df_all = pd.concat(all_dfs).sort_index()
    df_all = df_all[~df_all.index.duplicated(keep='first')]
    df_all = df_all.between_time('09:30', '16:00')
    return df_all


def process_interval(df, interval, symbol):
    tz = pytz.timezone('US/Eastern')
    if df.empty:
        logging.warning(f"[BOT-TICK] Empty DF for {symbol}")
        return None, None, None

    try:
        if not pd.api.types.is_datetime64_any_dtype(df.index):
            df.index = pd.to_datetime(df.index, utc=True)
        elif df.index.tz is None:
            df.index = df.index.tz_localize('UTC')
        df.index = df.index.tz_convert(tz)
    except Exception as e:
        logging.warning(f"[BOT-TICK] {symbol} tz/parse error: {e}")
        return None, None, None

    rule_map = {
        "1min": "1min",
        "5min": "5min",
        "10min": "10min",
        "15min": "15min",
        "30min": "30min",
        "1d": "1D",
        "1wk": "1W",
    }
    rule = rule_map.get(interval)
    if not rule:
        logging.warning(f"[BOT-TICK] Unsupported interval '{interval}' for {symbol}")
        return None, None, None

    resampled = df.resample(rule).agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
    }).dropna()

    if resampled.empty:
        logging.warning(f"[BOT-TICK] Resampled empty for {symbol}@{interval}")
        return None, None, None

    latest_time = resampled.index[-1]
    return latest_time, None, resampled


from app.models.paper_trading_bot import PaperStockTradeBot

def place_paper_trade(user_id, symbol, side, price, qty, bot_id=None):
    """
    Execute a paper trade for a given bot & user.
    - Opens/closes long/short positions.
    - Updates PaperAccount balance.
    - Records PaperStockBotTradeHistory with correct algo_name.
    - Sends email notifications (if enabled).
    """
    db: Session = SessionLocal()
    try:
        live_price = get_live_price(symbol)
        price = Decimal(str(live_price)) if live_price else Decimal(str(price))
        qty = Decimal(str(qty))

        # ensure account exists
        account = db.query(PaperAccount).filter_by(user_id=user_id).first()
        if not account:
            account = PaperAccount(user_id=user_id, current_balance=Decimal('100000.0'))
            db.add(account)
            db.commit()
            db.refresh(account)

        # ✅ fetch bot info for algo_name
        bot = None
        algo_name = "unknown"
        if bot_id:
            bot = db.query(PaperStockTradeBot).filter_by(id=bot_id, user_id=user_id).first()
            if bot:
                algo_name = bot.algo_name or "unknown"

        email_service = EmailService()
        user = db.query(User).filter_by(id=user_id).first()
        user_email = user.email if user else None

        TRADE_TYPE = "stock"
        NOTIF_ENTRY = "entry"
        NOTIF_EXIT  = "exit"

        open_trade = (
            db.query(PaperStockBotOpenTrade)
              .filter_by(bot_id=bot_id, user_id=user_id, symbol=symbol)
              .first()
        )

        now_utc = datetime.utcnow()

        # --- BUY logic ---
        if side == "BUY":
            # close short
            if open_trade and open_trade.position_side == "short":
                pnl = (Decimal(str(open_trade.entry_price)) - price) * Decimal(str(open_trade.quantity))
                hist = PaperStockBotTradeHistory(
                    bot_id=open_trade.bot_id,
                    user_id=open_trade.user_id,
                    symbol=open_trade.symbol,
                    position_side="short",
                    quantity=open_trade.quantity,
                    entry_price=open_trade.entry_price,
                    entry_time=open_trade.entry_time,
                    exit_price=float(price),
                    exit_time=now_utc,
                    profit_loss=float(pnl),
                )
                db.add(hist)
                account.current_balance = float(account.current_balance or 0.0) - (int(qty) * float(price))
                db.delete(open_trade)
                db.commit()

                if user_email and not notification_already_sent(db, user_id, NOTIF_EXIT, hist.id, trade_type=TRADE_TYPE):
                    email_service.send_stock_trade_notification(
                        email=user_email, symbol=symbol, side="BUY",
                        price=float(price), qty=int(qty),
                        interval="N/A", algo_name=algo_name
                    )
                    log_notification(db, user_id, NOTIF_EXIT, hist.id, user_email,
                                     {"symbol": symbol, "side": "BUY", "qty": int(qty), "price": float(price)},
                                     trade_type=TRADE_TYPE)
                return {"paper_trade_id": hist.id, "action": "closed_short", "message": "closed short"}

            # already long → skip
            if open_trade and open_trade.position_side == "long":
                logging.info(f"[PAPER ALGO] Bot {bot_id} already long {symbol}; skip duplicate open.")
                return {"paper_trade_id": None, "action": "already_long", "message": "already long; no new trade"}

            # open long
            cost = qty * price
            if account.current_balance < cost:
                logging.warning(f"[PAPER ALGO] Insufficient cash: need {cost}, have {account.current_balance}")
                db.rollback()
                return {"paper_trade_id": None, "action": "insufficient_cash", "message": "insufficient cash"}

            account.current_balance = float(account.current_balance or 0.0) - float(cost)
            new_open = PaperStockBotOpenTrade(
            bot_id=bot_id,
            user_id=user_id,
            symbol=symbol,
            position_side="long",
            quantity=float(qty),
            entry_price=float(price),
            entry_time=now_utc,
        )
            db.add(new_open)
            db.commit()
            db.refresh(new_open)

            if user_email and not notification_already_sent(db, user_id, NOTIF_ENTRY, new_open.id, trade_type=TRADE_TYPE):
                email_service.send_stock_trade_notification(
                    email=user_email, symbol=symbol, side="BUY",
                    price=float(price), qty=int(qty),
                    interval="N/A", algo_name=algo_name
                )
                log_notification(db, user_id, NOTIF_ENTRY, new_open.id, user_email,
                                 {"symbol": symbol, "side": "BUY", "qty": int(qty), "price": float(price)},
                                 trade_type=TRADE_TYPE)
            return {"paper_trade_id": new_open.id, "action": "opened_long", "message": "opened long"}

        # --- SELL logic ---
        elif side == "SELL":
            # close long
            if open_trade and open_trade.position_side == "long":
                pnl = (price - Decimal(str(open_trade.entry_price))) * Decimal(str(open_trade.quantity))
                hist = PaperStockBotTradeHistory(
                    bot_id=open_trade.bot_id,
                    user_id=open_trade.user_id,
                    symbol=open_trade.symbol,
                    position_side="long",
                    quantity=open_trade.quantity,
                    entry_price=open_trade.entry_price,
                    entry_time=open_trade.entry_time,
                    exit_price=float(price),
                    exit_time=now_utc,
                    profit_loss=float(pnl),
                )
                db.add(hist)
                account.current_balance = float(account.current_balance or 0.0) + (int(qty) * float(price))
                db.delete(open_trade)
                db.commit()

                if user_email and not notification_already_sent(db, user_id, NOTIF_EXIT, hist.id, trade_type=TRADE_TYPE):
                    email_service.send_stock_trade_notification(
                        email=user_email, symbol=symbol, side="SELL",
                        price=float(price), qty=int(qty),
                        interval="N/A", algo_name=algo_name
                    )
                    log_notification(db, user_id, NOTIF_EXIT, hist.id, user_email,
                                     {"symbol": symbol, "side": "SELL", "qty": int(qty), "price": float(price)},
                                     trade_type=TRADE_TYPE)
                return {"paper_trade_id": hist.id, "action": "closed_long", "message": "closed long"}

            # already short → skip
            if open_trade and open_trade.position_side == "short":
                logging.info(f"[PAPER ALGO] Bot {bot_id} already short {symbol}; skip duplicate open.")
                return {"paper_trade_id": None, "action": "already_short", "message": "already short; no new trade"}

            # open short
            credit = qty * price
            account.current_balance += credit
            new_open = PaperStockBotOpenTrade(
            bot_id=bot_id,
            user_id=user_id,
            symbol=symbol,
            position_side="short",
            quantity=float(qty),
            entry_price=float(price),
            entry_time=now_utc,
        )

            db.add(new_open)
            db.commit()
            db.refresh(new_open)

            if user_email and not notification_already_sent(db, user_id, NOTIF_ENTRY, new_open.id, trade_type=TRADE_TYPE):
                email_service.send_stock_trade_notification(
                    email=user_email, symbol=symbol, side="SELL",
                    price=float(price), qty=int(qty),
                    interval="N/A", algo_name=algo_name
                )
                log_notification(db, user_id, NOTIF_ENTRY, new_open.id, user_email,
                                 {"symbol": symbol, "side": "SELL", "qty": int(qty), "price": float(price)},
                                 trade_type=TRADE_TYPE)
            return {"paper_trade_id": new_open.id, "action": "opened_short", "message": "opened short"}

        # invalid side
        logging.warning(f"[PAPER ALGO] Unknown side '{side}' for {symbol}")
        return {"paper_trade_id": None, "action": "invalid_side", "message": f"unknown side {side}"}

    except Exception as e:
        db.rollback()
        logging.error(f"[PAPER ALGO] ❌ DB Error: {e}", exc_info=True)
        return {"paper_trade_id": None, "action": "db_error", "message": str(e)}
    finally:
        db.close()


# --- ADD: manual/bulk close helpers ---
def _get_now_utc():
    return datetime.utcnow()

def _last_tradeable_price(symbol: str) -> Decimal:
    live = get_live_price(symbol)
    return Decimal(str(live)) if live is not None else Decimal("0")

def close_open_trade_at_market(user_id: int, open_trade_id: int) -> bool:
    """
    Close a single open position NOW at market price.
    - Writes a row in PaperStockBotTradeHistory
    - Adjusts PaperAccount.current_balance (adds for closing long via SELL; subtracts for closing short via BUY)
    - Deletes the open row from PaperStockBotOpenTrade
    """
    db: Session = SessionLocal()
    try:
        account = db.query(PaperAccount).filter_by(user_id=user_id).first()
        if not account:
            logging.error("[CLOSE] No PaperAccount for user %s", user_id)
            return False

        open_trade = (
            db.query(PaperStockBotOpenTrade)
              .filter_by(id=open_trade_id, user_id=user_id).first()
        )
        if not open_trade:
            logging.warning("[CLOSE] Open trade %s not found for user %s", open_trade_id, user_id)
            return False

        now_utc = _get_now_utc()
        symbol = open_trade.symbol
        qty    = Decimal(str(open_trade.quantity))
        entry  = Decimal(str(open_trade.entry_price))
        last   = _last_tradeable_price(symbol)

   
        if open_trade.position_side == "long":
            # closing long -> SELL at market
            pnl = (last - entry) * qty
            hist = PaperStockBotTradeHistory(
                bot_id=open_trade.bot_id,
                user_id=user_id,
                symbol=symbol,
                    position_side="long",
                quantity=float(qty),
                entry_price=float(entry),
                entry_time=open_trade.entry_time,
                exit_price=float(last),
                exit_time=now_utc,
                profit_loss=float(pnl),
            )
            db.add(hist)
            account.current_balance += qty * last  # receive cash
            db.delete(open_trade)
            db.commit()
            return True

        elif open_trade.position_side == "short":
               # closing short -> BUY at market
            pnl = (entry - last) * qty
            hist = PaperStockBotTradeHistory(
                bot_id=open_trade.bot_id,
                user_id=user_id,
                symbol=symbol,
                    position_side="short",
                quantity=float(qty),
                entry_price=float(entry),
                entry_time=open_trade.entry_time,
                exit_price=float(last),
                exit_time=now_utc,
                profit_loss=float(pnl),
            )

            db.add(hist)
            account.current_balance -= qty * last  # pay to buy back
            db.delete(open_trade)
            db.commit()
            return True

        logging.error("[CLOSE] Unknown position_side %s", open_trade.position_side)
        return False

    except Exception as e:
        db.rollback()
        logging.error(f"[CLOSE] ❌ Error: {e}")
        return False
    finally:
        db.close()

def close_all_open_trades_for_bot(user_id: int, bot_id: int) -> int:
    """
    Close **all** open positions for a bot at market.
    Returns the number of closed positions.
    """
    db: Session = SessionLocal()
    try:
        opens = (db.query(PaperStockBotOpenTrade)
                   .filter_by(user_id=user_id, bot_id=bot_id).all())
        count = 0
        for ot in opens:
            # call the single close using the **id** to reuse consistent logic/accounting
            if close_open_trade_at_market(user_id=user_id, open_trade_id=ot.id):
                count += 1
        return count
    finally:
        db.close()
# --- END ADD ---
