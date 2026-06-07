#/var/www/stockwicks/app/scripts/nearest_friday_option_bot.py
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
import logging
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import requests
import pytz
from dotenv import load_dotenv
import fcntl
from contextlib import contextmanager
from hashlib import sha256
from decimal import Decimal
from sqlalchemy import text

from app.models.paper_option_trading_bot import (
    PaperOptionTradeBot,
    PaperOptionBotOpenTrade,
    PaperOptionBotTradeHistory,
)
from app.database.connection import SessionLocal
from app.services.email_service import (
    EmailService, notification_already_sent, log_notification
)
from app.models.user import User
from app.models.paper_trading import PaperAccount
import logging
from datetime import datetime
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.database.connection import SessionLocal

# === ENVIRONMENT & LOGGING ===================================
load_dotenv()
BASE_DIR = "/var/www/stockwicks"
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "nearest_friday_results.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

# === CONFIG ================================================
MIN_VOLUME = 100
MIN_OI = 100
MIN_BID = 0.05
SCHWAB_API_URL = "https://api.schwabapi.com/marketdata/v1"
TRADE_SIZE = 1  # Default quantity for options
STOP_LOSS_BUFFER = Decimal('0.1')  # Buffer for stop-loss
PROFIT_TARGET_BUFFER = Decimal('0.1')  # Buffer for profit target


# === LOCK ===================================================
@contextmanager
def bot_lock(user_id: int, symbol: str, timeout_sec: int = 2):
    lock_path = f"/tmp/stockwicks_optionbot_{user_id}_{symbol}.lock"
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError:
        logger.warning(f"[OPTION-BOT] Lock busy for user={user_id} symbol={symbol}; skipping tick")
        return
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()

#/var/www/stockwicks/app/scripts/nearest_friday_option_bot.py
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
import logging
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import requests
import pytz
from dotenv import load_dotenv
import fcntl
from contextlib import contextmanager
from hashlib import sha256
from decimal import Decimal
from sqlalchemy import text

from app.models.paper_option_trading_bot import (
    PaperOptionTradeBot,
    PaperOptionBotOpenTrade,
    PaperOptionBotTradeHistory,
)
from app.database.connection import SessionLocal
from app.services.email_service import (
    EmailService, notification_already_sent, log_notification
)
from app.models.user import User
from app.models.paper_trading import PaperAccount
import logging
from datetime import datetime
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.database.connection import SessionLocal

# === ENVIRONMENT & LOGGING ===================================
load_dotenv()
BASE_DIR = "/var/www/stockwicks"
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "nearest_friday_results.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)


# >>>>>> INSERT THESE HELPERS RIGHT HERE <<<<<<
def _order_by_history_ts(q, Model):
    if hasattr(Model, "closed_at"):
        return q.order_by(Model.closed_at.desc())
    if hasattr(Model, "executed_at"):
        return q.order_by(Model.executed_at.desc())
    if hasattr(Model, "created_at"):
        return q.order_by(Model.created_at.desc())
    return q.order_by(Model.id.desc())

def _history_ts_kwargs(Model, dt):
    if hasattr(Model, "executed_at"):
        return {"executed_at": dt}
    if hasattr(Model, "closed_at"):
        return {"closed_at": dt}
    if hasattr(Model, "created_at"):
        return {"created_at": dt}
    return {}

# === CONFIG ================================================
MIN_VOLUME = 100
MIN_OI = 100
MIN_BID = 0.05
SCHWAB_API_URL = "https://api.schwabapi.com/marketdata/v1"
TRADE_SIZE = 1  # Default quantity for options
STOP_LOSS_BUFFER = Decimal('0.1')  # Buffer for stop-loss
PROFIT_TARGET_BUFFER = Decimal('0.1')  # Buffer for profit target


# === LOCK ===================================================
@contextmanager
def bot_lock(user_id: int, symbol: str, timeout_sec: int = 2):
    lock_path = f"/tmp/stockwicks_optionbot_{user_id}_{symbol}.lock"
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError:
        logger.warning(f"[OPTION-BOT] Lock busy for user={user_id} symbol={symbol}; skipping tick")
        return
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()


# === HELPERS ===============================================
def get_schwab_headers():
    from app.utils.stock.schwab_token import get_valid_access_token
    access_token = get_valid_access_token()
    logger.info(f"[SCHWAB TOKEN] {access_token}")
    if not access_token:
        raise ValueError("SCHWAB_ACCESS_TOKEN missing or could not refresh.")
    return {"Authorization": f"Bearer {access_token}"}

def get_option_expirations(symbol):
    url = f"{SCHWAB_API_URL}/expirationchain"
    params = {"symbol": symbol}
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params)
    logger.info(f"ExpirationChain API [{symbol}] status: {resp.status_code}, {resp.text[:200]}")
    data = resp.json() if resp.status_code == 200 else None
    expirations = []
    if data and "expirationList" in data:
        expirations = [e["expirationDate"] for e in data["expirationList"]]
    return expirations

def get_next_friday(expirations):
    tz = pytz.timezone('US/Eastern')
    now = datetime.now(tz).date()
    fridays = []
    for exp in expirations:
        try:
            dt = datetime.strptime(exp, "%Y-%m-%d").date()
            if dt >= now and dt.weekday() == 4:
                fridays.append(dt)
        except Exception:
            continue
    return min(fridays) if fridays else None

def get_quote_price(symbol, for_closing_side="SELL"):
    url = f"{SCHWAB_API_URL}/quotes"
    params = {"symbols": symbol}
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params)
    logger.info(f"Quotes API [{symbol}] status: {resp.status_code}, {resp.text[:200]}")
    if resp.status_code != 200:
        return None
    data = resp.json()
    q = data.get(symbol, {})
    if for_closing_side == "SELL":
        return q.get("bidPrice") or q.get("mark") or q.get("lastPrice") or q.get("askPrice")
    else:
        return q.get("askPrice") or q.get("mark") or q.get("lastPrice") or q.get("bidPrice")


def analyze_options(df, price):
    df = df.copy()
    needed = ["ask", "bid", "openInterest", "totalVolume", "strikePrice", "delta", "gamma", "putCall", "symbol"]
    missing = [col for col in needed if col not in df.columns]
    if missing:
        logger.error(f"Options DataFrame missing columns: {missing}")
        return pd.DataFrame()

    df = df.dropna(subset=needed)

    # Core liquidity
    df = df[df["ask"] > MIN_BID]
    df = df[df["openInterest"] > MIN_OI]

    # Only enforce intraday volume during RTH; after-hours it’s often 0
    if is_us_rth():
        df = df[df["totalVolume"] > MIN_VOLUME]

    # Widen moneyness band slightly to increase candidates
    df = df[df["strikePrice"].between(price * 0.88, price * 1.12)]
    if df.empty:
        return df

    before_spread = len(df)
    df["spread"] = df["ask"] - df["bid"]
    df["mid"] = (df["ask"] + df["bid"]) / 2
    df["spread_pct"] = df["spread"] / (df["mid"].replace(0, np.nan))

    # Be a bit more permissive after-hours
    max_spread = 0.30 if is_us_rth() else 0.50
    df = df[df["spread_pct"] < max_spread]
    logger.info(f"[FILTER] spread survivors: {len(df)}/{before_spread}")

    df["score"] = (df["openInterest"] * df["delta"].abs() * df["gamma"].abs()) / (df["spread"] + 1e-6)
    top = df.sort_values("score", ascending=False).head(1)
    if top.empty:
        return top

    top["Stop_Loss"] = (top["ask"] * 0.75).round(2)
    top["Exit_Price"] = (top["ask"] * 1.25).round(2)
    top["Action"] = np.where(top["putCall"].str.upper() == "CALL", "Buy Call", "Buy Put")
    est_now = datetime.now(pytz.timezone("US/Eastern"))
    top["date"] = est_now.strftime("%Y-%m-%d")
    top["time"] = est_now.strftime("%I:%M:%S %p")
    return top

def flatten_options_map(exp_map, side):
    records = []
    for exp, strikes in exp_map.items():
        for strike, contracts in strikes.items():
            for c in contracts:
                rec = c.copy()
                rec["side"] = side
                rec["expiration"] = exp.split(":")[0]
                records.append(rec)
    return records

def fetch_options_chain(symbol, expiration):
    url = f"{SCHWAB_API_URL}/chains"
    params = {
        "symbol": symbol,
        "fromDate": expiration.strftime("%Y-%m-%d"),
        "toDate": expiration.strftime("%Y-%m-%d"),
        "includeUnderlyingQuote": "true",
        "contractType": "ALL"
    }
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params)
    logger.info(f"Chains API [{symbol}] status: {resp.status_code}, {resp.text[:200]}")
    if resp.status_code != 200:
        return pd.DataFrame(), None
    chain = resp.json()
    call_map = chain.get("callExpDateMap", {})
    put_map = chain.get("putExpDateMap", {})
    calls = flatten_options_map(call_map, "call")
    puts = flatten_options_map(put_map, "put")
    df = pd.DataFrame(calls + puts)
    synth_count = 0
    if 'symbol' not in df.columns or df['symbol'].isna().all():
        df['symbol'] = [
            f"{symbol}{expiration.strftime('%y%m%d')}{c.get('putCall', '').upper()[0]}{int(c.get('strikePrice', 0)):06d}"
            for c in calls + puts
        ]
        synth_count = len(df)
    logger.info(f"[CHAINS] rows={len(df)} synthesized_symbols={synth_count}")
    return df, chain.get("underlying", {})

def build_idem_key(user_id: int, underlying_symbol: str, expiry: str, option_symbol: str, position_side: str, minute_bucket: bool = True) -> str:
    bucket = int(datetime.utcnow().timestamp() // 60) if minute_bucket else 0
    raw = f"{user_id}|{underlying_symbol}|{expiry}|{option_symbol}|{position_side}|{bucket}"
    return sha256(raw.encode()).hexdigest()


# === BOT ENTRY =============================================
def run_option_paper_bot_tick(user_id, symbol, notify_email=None):
    """Public entrypoint (Celery/CLI). Ensures single-run via file lock."""
    with bot_lock(user_id, symbol):
        _run_option_paper_bot_tick(user_id, symbol, notify_email)



def _run_option_paper_bot_tick(user_id, symbol, notify_email=None):
    db = SessionLocal()
    email_service = EmailService()
    try:
        logger.info(f"🧪 OptionBot Tick for {symbol} (User {user_id})")
        user = db.query(User).filter_by(id=user_id).first()
        user_email = user.email if user else None

        # === PROCESS OPEN TRADES (exits) ===
        open_trades = db.query(PaperOptionBotOpenTrade).filter_by(
            user_id=user_id, underlying_symbol=symbol, status="OPEN"
        ).all()
        now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)

        for trade in open_trades:
            expiry_date = datetime.strptime(trade.expiry, "%Y-%m-%d").replace(tzinfo=pytz.UTC)
            should_close = False
            close_reason = None
            exit_price = None
            pnl = None

            # Expiry check
            if now_utc > expiry_date:
                exit_price = 0.0
                should_close = True
                close_reason = "expiry"
            else:
                live_price = get_quote_price(trade.option_symbol)
                if live_price is not None:
                    live_price = Decimal(str(live_price))
                    entry_price = Decimal(str(trade.entry_price))
                    if trade.planned_stop_loss and live_price <= Decimal(str(trade.planned_stop_loss)):
                        exit_price = float(live_price)
                        should_close = True
                        close_reason = "stop-loss"
                    elif trade.planned_exit_price and live_price >= Decimal(str(trade.planned_exit_price)):
                        exit_price = float(live_price)
                        should_close = True
                        close_reason = "profit"

            if should_close:
                entry_price = Decimal(str(trade.entry_price))
                exit_val = Decimal(str(exit_price))
                pnl = float((exit_val - entry_price) * trade.quantity)

                ts_now = now_utc
                closed_trade = PaperOptionBotTradeHistory(
                    bot_id=trade.bot_id,
                    user_id=trade.user_id,
                    option_symbol=trade.option_symbol,
                    underlying_symbol=trade.underlying_symbol,
                    trade_type=trade.trade_type,
                    position_side=trade.position_side,
                    quantity=trade.quantity,
                    entry_price=trade.entry_price,
                    exit_price=exit_price,
                    status="CLOSED",
                    # if history has created_at, propagate original entry time into it
                    **({"created_at": trade.executed_at} if hasattr(PaperOptionBotTradeHistory, "created_at") else {}),
                    # choose the right "close/executed" timestamp field for history
                    **_history_ts_kwargs(PaperOptionBotTradeHistory, ts_now),
                    strike_price=trade.strike_price,
                    expiry=trade.expiry,
                    pnl=pnl,
                    planned_stop_loss=trade.planned_stop_loss,
                    planned_exit_price=trade.planned_exit_price,
                )
                db.add(closed_trade)
                db.delete(trade)

                account = db.query(PaperAccount).filter_by(user_id=user_id).first()
                if account:
                    account.current_balance += (trade.quantity * Decimal(str(exit_price))) - (trade.quantity * Decimal(str(trade.entry_price)))
                db.commit()
                logger.info(f"[OPTION-BOT] ✅ Closed {trade.option_symbol} due to {close_reason} @ {exit_price}")

                # Email idempotency: use a deterministic key for the close
                exit_key = build_idem_key(
                    user_id, trade.underlying_symbol, trade.expiry, trade.option_symbol, trade.position_side, minute_bucket=False
                )
                if user_email and not notification_already_sent(db, user_id, "exit", exit_key, trade_type="option"):
                    email_service.send_option_trade_notification(
                        email=user_email,
                        symbol=trade.option_symbol,
                        side="SELL",
                        price=float(exit_price),
                        qty=int(trade.quantity),
                        interval="N/A",
                        algo_name="Option Bot",
                        strike=trade.strike_price,
                        expiry=trade.expiry,
                        position_side=trade.position_side
                    )
                    log_notification(
                        db, user_id, "exit", exit_key, user_email,
                        {
                            "symbol": trade.option_symbol, "side": "SELL", "qty": int(trade.quantity),
                            "price": float(exit_price), "strike": trade.strike_price, "expiry": trade.expiry
                        },
                        trade_type="option"
                    )

        # === ENTRY LOGIC ===
        open_trade = db.query(PaperOptionBotOpenTrade).filter_by(
            user_id=user_id, underlying_symbol=symbol, status="OPEN"
        ).first()

        if not open_trade:
            expirations = get_option_expirations(symbol)
            if not expirations:
                logger.warning(f"[OPTION-BOT] No expiration chain data for {symbol}")
                return

            nearest_expiry_date = get_next_friday(expirations)
            if not nearest_expiry_date:
                logger.warning(f"[OPTION-BOT] No weekly Friday expiration for {symbol}")
                return

            nearest_expiry = nearest_expiry_date.strftime("%Y-%m-%d")
            options_df, underlying = fetch_options_chain(symbol, nearest_expiry_date)
            if options_df.empty or not underlying:
                logger.warning(f"[OPTION-BOT] No options data or underlying for {symbol}")
                return

            underlying_price = Decimal(str(underlying.get("lastPrice") or underlying.get("mark") or 0))
            if not underlying_price or underlying_price == 0:
                logger.warning(f"[OPTION-BOT] No valid underlying price for {symbol}; underlying payload: {underlying}")
                return

            # Visibility on where filters kill the set
            df = options_df.copy()
            raw_count = len(df)
            df = df.dropna(subset=["ask","bid","openInterest","totalVolume","strikePrice"])
            after_basic = len(df)
            df = df[df["ask"] > MIN_BID]; after_minbid = len(df)
            df = df[df["openInterest"] > MIN_OI]; after_oi = len(df)
            df = df[df["totalVolume"] > MIN_VOLUME]; after_vol = len(df)
            df = df[df["strikePrice"].between(float(underlying_price) * 0.9, float(underlying_price) * 1.1)]
            after_moneyness = len(df)
            logger.info(f"[FILTER] raw={raw_count} basic={after_basic} minbid={after_minbid} oi={after_oi} vol={after_vol} money={after_moneyness}")

            top_option = analyze_options(df, float(underlying_price))
            if top_option.empty:
                logger.warning(f"[OPTION-BOT] No suitable options for {symbol}")
                return

            option_symbol = str(top_option["symbol"].iloc[0])
            ask_price = Decimal(str(top_option["ask"].iloc[0]))
            stop_loss = Decimal(str(top_option["Stop_Loss"].iloc[0]))
            profit_target = Decimal(str(top_option["Exit_Price"].iloc[0]))
            action = top_option["Action"].iloc[0]
            strike_price = float(top_option["strikePrice"].iloc[0])
            position_side = "call" if "Call" in action else "put"

            # Prevent immediate re-entry (same contract already CLOSED earlier)
            q = db.query(PaperOptionBotTradeHistory).filter_by(
                user_id=user_id,
                option_symbol=option_symbol,
                expiry=nearest_expiry,
                status="CLOSED"
            )
            latest_closed = _order_by_history_ts(q, PaperOptionBotTradeHistory).first()
            if latest_closed:
                logger.info(f"[OPTION-BOT] Already traded {option_symbol} for {symbol} with expiry {nearest_expiry}. Skipping.")
                return

            # Guard: if an OPEN of the same contract slipped in via a race, bail
            dupe_open = db.query(PaperOptionBotOpenTrade).filter_by(
                user_id=user_id,
                option_symbol=option_symbol,
                expiry=nearest_expiry,
                status="OPEN"
            ).first()
            if dupe_open:
                logger.info(f"[OPTION-BOT] Duplicate open detected for {option_symbol}; skipping.")
                return

            logger.info(
                f"[PICKED OPTION] user={user_id} symbol={symbol} option_symbol={option_symbol} "
                f"expiry={nearest_expiry} strike={strike_price} side={position_side} action={action} "
                f"ask={ask_price} stop_loss={stop_loss} exit={profit_target} "
                f"date={top_option['date'].iloc[0]} time={top_option['time'].iloc[0]} "
                f"gamma={top_option.get('gamma', 0.0).iloc[0]} delta={top_option.get('delta', 0.0).iloc[0]}"
            )

            account = db.query(PaperAccount).filter_by(user_id=user_id).first()
            if not account or account.current_balance < (TRADE_SIZE * ask_price):
                logger.warning(f"[OPTION-BOT] Insufficient balance for {option_symbol}")
                return

            # Spend, create open trade
            account.current_balance -= TRADE_SIZE * ask_price
            new_trade = PaperOptionBotOpenTrade(
                user_id=user_id,
                option_symbol=option_symbol,
                underlying_symbol=symbol,
                trade_type="BUY" if "Buy" in action else "SELL",
                position_side=position_side,
                quantity=TRADE_SIZE,
                entry_price=float(ask_price),
                strike_price=float(strike_price),
                expiry=nearest_expiry,
                status="OPEN",
                planned_stop_loss=float(stop_loss),
                planned_exit_price=float(profit_target),
                executed_at=datetime.utcnow(),
            )
            db.add(new_trade)
            db.commit()
            logger.info("📩 Option trade notification sent to %s for %s", user_email, option_symbol)

            # Idempotent entry email
            entry_key = build_idem_key(user_id, symbol, nearest_expiry, option_symbol, position_side, minute_bucket=False)
            if user_email and not notification_already_sent(db, user_id, "entry", entry_key, trade_type="option"):
                email_service.send_option_trade_notification(
                    email=user_email,
                    symbol=option_symbol,
                    side="BUY" if "Buy" in action else "SELL",
                    price=float(ask_price),
                    qty=TRADE_SIZE,
                    interval="N/A",
                    algo_name="Option Bot",
                    strike=strike_price,
                    expiry=nearest_expiry,
                    position_side=position_side
                )
                log_notification(
                    db, user_id, "entry", entry_key, user_email,
                    {
                        "symbol": option_symbol,
                        "side": "BUY" if "Buy" in action else "SELL",
                        "qty": TRADE_SIZE,
                        "price": float(ask_price),
                        "strike": strike_price,
                        "expiry": nearest_expiry
                    },
                    trade_type="option"
                )

    except Exception as e:
        db.rollback()
        logger.error(f"[OPTION-BOT] Error: {e}")
    finally:
        db.close()
    logger.warning(f"[OPTION-BOT] Finished tick for {symbol} user_id={user_id}")


def check_and_close_open_option_trades():
    # Intentionally keeps the same logic, adds idempotent email key
    db = SessionLocal()
    email_service = EmailService()
    try:
        open_trades = db.query(PaperOptionBotOpenTrade).filter_by(status="OPEN").all()
        now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
        for trade in open_trades:
            expiry_date = datetime.strptime(trade.expiry, "%Y-%m-%d").replace(tzinfo=pytz.UTC)
            user = db.query(User).filter_by(id=trade.user_id).first()
            user_email = user.email if user else None

            should_close = False
            close_reason = None
            exit_price = None

            if now_utc > expiry_date:
                exit_price = 0.0
                should_close = True
                close_reason = "expiry"
            else:
                live_price = get_quote_price(trade.option_symbol)
                if live_price is not None:
                    live_price = Decimal(str(live_price))
                    if trade.planned_stop_loss and live_price <= Decimal(str(trade.planned_stop_loss)):
                        exit_price = float(live_price)
                        should_close = True
                        close_reason = "stop-loss"
                    elif trade.planned_exit_price and live_price >= Decimal(str(trade.planned_exit_price)):
                        exit_price = float(live_price)
                        should_close = True
                        close_reason = "profit"

            if should_close:
                entry_price = Decimal(str(trade.entry_price))
                exit_val = Decimal(str(exit_price))
                pnl = float((exit_val - entry_price) * trade.quantity)
                closed_trade = PaperOptionBotTradeHistory(
                    bot_id=trade.bot_id,
                    user_id=trade.user_id,
                    option_symbol=trade.option_symbol,
                    underlying_symbol=trade.underlying_symbol,
                    trade_type=trade.trade_type,
                    position_side=trade.position_side,
                    quantity=trade.quantity,
                    entry_price=trade.entry_price,
                    exit_price=exit_price,
                    status="CLOSED",
                    created_at=trade.executed_at,
                    closed_at=now_utc,
                    strike_price=trade.strike_price,
                    expiry=trade.expiry,
                    pnl=pnl,
                    planned_stop_loss=trade.planned_stop_loss,
                    planned_exit_price=trade.planned_exit_price,
                )
                db.add(closed_trade)
                db.delete(trade)

                account = db.query(PaperAccount).filter_by(user_id=trade.user_id).first()
                if account:
                    account.current_balance += (trade.quantity * Decimal(str(exit_price))) - (trade.quantity * Decimal(str(trade.entry_price)))
                db.commit()
                reason_str = f" due to {close_reason}" if close_reason else ""
                logging.info(f"[OPTION-BOT] ✅ Closed {trade.option_symbol}{reason_str} @ {exit_price}")

                exit_key = build_idem_key(
                    trade.user_id, trade.underlying_symbol, trade.expiry, trade.option_symbol, trade.position_side, minute_bucket=False
                )
                if user_email and not notification_already_sent(db, trade.user_id, "exit", exit_key, trade_type="option"):
                    email_service.send_option_trade_notification(
                        email=user_email,
                        symbol=trade.option_symbol,
                        side="SELL",
                        price=float(exit_price),
                        qty=int(trade.quantity),
                        interval="N/A",
                        algo_name="Option Bot",
                        strike=trade.strike_price,
                        expiry=trade.expiry,
                        position_side=trade.position_side
                    )
                    log_notification(
                        db, trade.user_id, "exit", exit_key, user_email,
                        {
                            "symbol": trade.option_symbol, "side": "SELL", "qty": int(trade.quantity),
                            "price": float(exit_price), "strike": trade.strike_price, "expiry": trade.expiry
                        },
                        trade_type="option"
                    )

        db.commit()
    except Exception as e:
        db.rollback()
        logging.error(f"[OPTION-BOT] Error in close-checker: {e}")
    finally:
        db.close()

# ============ Email BOT =================#

def _as_str_id(x) -> str:
    """Always store/compare trade_id as a string."""
    return "" if x is None else str(x)

def _notification_already_sent(db, *, user_id: int, trade_type: str, notification_type: str, trade_id) -> bool:
    trade_id_str = _as_str_id(trade_id)
    sql = text("""
        SELECT 1
        FROM trade_notification_log
        WHERE user_id = :user_id
          AND trade_type = :trade_type
          AND notification_type = :notification_type
          AND trade_id = :trade_id
        LIMIT 1
    """)
    row = db.execute(sql, {
        "user_id": user_id,
        "trade_type": trade_type,
        "notification_type": notification_type,
        "trade_id": trade_id_str,
    }).first()
    return row is not None

def _record_notification(db, *, user_id: int, trade_type: str, notification_type: str, trade_id):
    trade_id_str = _as_str_id(trade_id)
    sql = text("""
        INSERT INTO trade_notification_log (user_id, trade_id, trade_type, notification_type, sent_at)
        VALUES (:user_id, :trade_id, :trade_type, :notification_type, :sent_at)
        ON CONFLICT DO NOTHING
    """)
    try:
        db.execute(sql, {
            "user_id": user_id,
            "trade_id": trade_id_str,
            "trade_type": trade_type,
            "notification_type": notification_type,
            "sent_at": datetime.utcnow(),
        })
        db.commit()
    except IntegrityError:
        db.rollback()
        
# === CLI Entry ===
if __name__ == "__main__":
    symbol = sys.argv[1].upper() if len(sys.argv) > 1 else "AAPL"
    user_id = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    run_option_paper_bot_tick(user_id, symbol)

# === HELPERS ===============================================
def get_schwab_headers():
    from app.utils.stock.schwab_token import get_valid_access_token
    access_token = get_valid_access_token()
    logger.info(f"[SCHWAB TOKEN] {access_token}")
    if not access_token:
        raise ValueError("SCHWAB_ACCESS_TOKEN missing or could not refresh.")
    return {"Authorization": f"Bearer {access_token}"}

def get_option_expirations(symbol):
    url = f"{SCHWAB_API_URL}/expirationchain"
    params = {"symbol": symbol}
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params)
    logger.info(f"ExpirationChain API [{symbol}] status: {resp.status_code}, {resp.text[:200]}")
    data = resp.json() if resp.status_code == 200 else None
    expirations = []
    if data and "expirationList" in data:
        expirations = [e["expirationDate"] for e in data["expirationList"]]
    return expirations

def get_next_friday(expirations):
    tz = pytz.timezone('US/Eastern')
    now = datetime.now(tz).date()
    fridays = []
    for exp in expirations:
        try:
            dt = datetime.strptime(exp, "%Y-%m-%d").date()
            if dt >= now and dt.weekday() == 4:
                fridays.append(dt)
        except Exception:
            continue
    return min(fridays) if fridays else None

def get_quote_price(symbol, for_closing_side="SELL"):
    url = f"{SCHWAB_API_URL}/quotes"
    params = {"symbols": symbol}
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params)
    logger.info(f"Quotes API [{symbol}] status: {resp.status_code}, {resp.text[:200]}")
    if resp.status_code != 200:
        return None
    data = resp.json()
    q = data.get(symbol, {})
    if for_closing_side == "SELL":
        return q.get("bidPrice") or q.get("mark") or q.get("lastPrice") or q.get("askPrice")
    else:
        return q.get("askPrice") or q.get("mark") or q.get("lastPrice") or q.get("bidPrice")

def analyze_options(df, price):
    df = df.copy()
    needed = ["ask", "bid", "openInterest", "totalVolume", "strikePrice", "delta", "gamma", "putCall", "symbol"]
    missing = [col for col in needed if col not in df.columns]
    if missing:
        logger.error(f"Options DataFrame missing columns: {missing}")
        print("DF COLUMNS:", df.columns.tolist())
        return pd.DataFrame()
    df = df.dropna(subset=needed)
    df = df[df["ask"] > MIN_BID]
    df = df[df["openInterest"] > MIN_OI]
    df = df[df["totalVolume"] > MIN_VOLUME]
    df = df[df["strikePrice"].between(price * 0.9, price * 1.1)]
    if df.empty:
        return df

    before_spread = len(df)
    df["spread"] = df["ask"] - df["bid"]
    df["mid"] = (df["ask"] + df["bid"]) / 2
    df["spread_pct"] = df["spread"] / (df["mid"].replace(0, np.nan))
    df = df[df["spread_pct"] < 0.30]
    logger.info(f"[FILTER] spread survivors: {len(df)}/{before_spread}")

    df["score"] = (df["openInterest"] * df["delta"].abs() * df["gamma"].abs()) / (df["spread"] + 1e-6)
    top = df.sort_values("score", ascending=False).head(1)
    if top.empty:
        return top
    top["Stop_Loss"] = (top["ask"] * 0.75).round(2)
    top["Exit_Price"] = (top["ask"] * 1.25).round(2)
    top["Action"] = np.where(top["putCall"].str.upper() == "CALL", "Buy Call", "Buy Put")
    est_now = datetime.now(pytz.timezone("US/Eastern"))
    top["date"] = est_now.strftime("%Y-%m-%d")
    top["time"] = est_now.strftime("%I:%M:%S %p")
    return top

def flatten_options_map(exp_map, side):
    records = []
    for exp, strikes in exp_map.items():
        for strike, contracts in strikes.items():
            for c in contracts:
                rec = c.copy()
                rec["side"] = side
                rec["expiration"] = exp.split(":")[0]
                records.append(rec)
    return records

def fetch_options_chain(symbol, expiration):
    url = f"{SCHWAB_API_URL}/chains"
    params = {
        "symbol": symbol,
        "fromDate": expiration.strftime("%Y-%m-%d"),
        "toDate": expiration.strftime("%Y-%m-%d"),
        "includeUnderlyingQuote": "true",
        "contractType": "ALL"
    }
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params)
    logger.info(f"Chains API [{symbol}] status: {resp.status_code}, {resp.text[:200]}")
    if resp.status_code != 200:
        return pd.DataFrame(), None
    chain = resp.json()
    call_map = chain.get("callExpDateMap", {})
    put_map = chain.get("putExpDateMap", {})
    calls = flatten_options_map(call_map, "call")
    puts = flatten_options_map(put_map, "put")
    df = pd.DataFrame(calls + puts)
    synth_count = 0
    if 'symbol' not in df.columns or df['symbol'].isna().all():
        df['symbol'] = [
            f"{symbol}{expiration.strftime('%y%m%d')}{c.get('putCall', '').upper()[0]}{int(c.get('strikePrice', 0)):06d}"
            for c in calls + puts
        ]
        synth_count = len(df)
    logger.info(f"[CHAINS] rows={len(df)} synthesized_symbols={synth_count}")
    return df, chain.get("underlying", {})

def build_idem_key(user_id: int, underlying_symbol: str, expiry: str, option_symbol: str, position_side: str, minute_bucket: bool = True) -> str:
    bucket = int(datetime.utcnow().timestamp() // 60) if minute_bucket else 0
    raw = f"{user_id}|{underlying_symbol}|{expiry}|{option_symbol}|{position_side}|{bucket}"
    return sha256(raw.encode()).hexdigest()


# === BOT ENTRY =============================================
def run_option_paper_bot_tick(user_id, symbol, notify_email=None):
    """Public entrypoint (Celery/CLI). Ensures single-run via file lock."""
    with bot_lock(user_id, symbol):
        _run_option_paper_bot_tick(user_id, symbol, notify_email)


def _run_option_paper_bot_tick(user_id, symbol, notify_email=None):
    db = SessionLocal()
    email_service = EmailService()
    try:
        logger.info(f"🧪 OptionBot Tick for {symbol} (User {user_id})")
        user = db.query(User).filter_by(id=user_id).first()
        user_email = user.email if user else None

        # === PROCESS OPEN TRADES (exits) ===
        open_trades = db.query(PaperOptionBotOpenTrade).filter_by(
            user_id=user_id, underlying_symbol=symbol, status="OPEN"
        ).all()
        now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)

        for trade in open_trades:
            expiry_date = datetime.strptime(trade.expiry, "%Y-%m-%d").replace(tzinfo=pytz.UTC)
            should_close = False
            close_reason = None
            exit_price = None
            pnl = None

            # Expiry check
            if now_utc > expiry_date:
                exit_price = 0.0
                should_close = True
                close_reason = "expiry"
            else:
                live_price = get_quote_price(trade.option_symbol)
                if live_price is not None:
                    live_price = Decimal(str(live_price))
                    entry_price = Decimal(str(trade.entry_price))
                    if trade.planned_stop_loss and live_price <= Decimal(str(trade.planned_stop_loss)):
                        exit_price = float(live_price)
                        should_close = True
                        close_reason = "stop-loss"
                    elif trade.planned_exit_price and live_price >= Decimal(str(trade.planned_exit_price)):
                        exit_price = float(live_price)
                        should_close = True
                        close_reason = "profit"

            if should_close:
                entry_price = Decimal(str(trade.entry_price))
                exit_val = Decimal(str(exit_price))
                pnl = float((exit_val - entry_price) * trade.quantity)
                closed_trade = PaperOptionBotTradeHistory(
                    bot_id=trade.bot_id,
                    user_id=trade.user_id,
                    option_symbol=trade.option_symbol,
                    underlying_symbol=trade.underlying_symbol,
                    trade_type=trade.trade_type,
                    position_side=trade.position_side,
                    quantity=trade.quantity,
                    entry_price=trade.entry_price,
                    exit_price=exit_price,
                    status="CLOSED",
                    created_at=trade.executed_at,
                    closed_at=now_utc,
                    strike_price=trade.strike_price,
                    expiry=trade.expiry,
                    pnl=pnl,
                    planned_stop_loss=trade.planned_stop_loss,
                    planned_exit_price=trade.planned_exit_price,
                )
                db.add(closed_trade)
                db.delete(trade)

                account = db.query(PaperAccount).filter_by(user_id=user_id).first()
                if account:
                    account.current_balance += (trade.quantity * Decimal(str(exit_price))) - (trade.quantity * Decimal(str(trade.entry_price)))
                db.commit()
                logger.info(f"[OPTION-BOT] ✅ Closed {trade.option_symbol} due to {close_reason} @ {exit_price}")

                # Email idempotency: use a deterministic key for the close
                exit_key = build_idem_key(
                    user_id, trade.underlying_symbol, trade.expiry, trade.option_symbol, trade.position_side, minute_bucket=False
                )
                if user_email and not notification_already_sent(db, user_id, "exit", exit_key, trade_type="option"):
                    email_service.send_option_trade_notification(
                        email=user_email,
                        symbol=trade.option_symbol,
                        side="SELL",
                        price=float(exit_price),
                        qty=int(trade.quantity),
                        interval="N/A",
                        algo_name="Option Bot",
                        strike=trade.strike_price,
                        expiry=trade.expiry,
                        position_side=trade.position_side
                    )
                    log_notification(
                        db, user_id, "exit", exit_key, user_email,
                        {
                            "symbol": trade.option_symbol, "side": "SELL", "qty": int(trade.quantity),
                            "price": float(exit_price), "strike": trade.strike_price, "expiry": trade.expiry
                        },
                        trade_type="option"
                    )

        # === ENTRY LOGIC ===
        open_trade = db.query(PaperOptionBotOpenTrade).filter_by(
            user_id=user_id, underlying_symbol=symbol, status="OPEN"
        ).first()

        if not open_trade:
            expirations = get_option_expirations(symbol)
            if not expirations:
                logger.warning(f"[OPTION-BOT] No expiration chain data for {symbol}")
                return

            nearest_expiry_date = get_next_friday(expirations)
            if not nearest_expiry_date:
                logger.warning(f"[OPTION-BOT] No weekly Friday expiration for {symbol}")
                return

            nearest_expiry = nearest_expiry_date.strftime("%Y-%m-%d")
            options_df, underlying = fetch_options_chain(symbol, nearest_expiry_date)
            if options_df.empty or not underlying:
                logger.warning(f"[OPTION-BOT] No options data or underlying for {symbol}")
                return

            underlying_price = Decimal(str(underlying.get("lastPrice") or underlying.get("mark") or 0))
            if not underlying_price or underlying_price == 0:
                logger.warning(f"[OPTION-BOT] No valid underlying price for {symbol}; underlying payload: {underlying}")
                return

            # Visibility on where filters kill the set
            df = options_df.copy()
            raw_count = len(df)
            df = df.dropna(subset=["ask","bid","openInterest","totalVolume","strikePrice"])
            after_basic = len(df)
            df = df[df["ask"] > MIN_BID]; after_minbid = len(df)
            df = df[df["openInterest"] > MIN_OI]; after_oi = len(df)
            df = df[df["totalVolume"] > MIN_VOLUME]; after_vol = len(df)
            df = df[df["strikePrice"].between(float(underlying_price) * 0.9, float(underlying_price) * 1.1)]
            after_moneyness = len(df)
            logger.info(f"[FILTER] raw={raw_count} basic={after_basic} minbid={after_minbid} oi={after_oi} vol={after_vol} money={after_moneyness}")

            top_option = analyze_options(df, float(underlying_price))
            if top_option.empty:
                logger.warning(f"[OPTION-BOT] No suitable options for {symbol}")
                return

            option_symbol = str(top_option["symbol"].iloc[0])
            ask_price = Decimal(str(top_option["ask"].iloc[0]))
            stop_loss = Decimal(str(top_option["Stop_Loss"].iloc[0]))
            profit_target = Decimal(str(top_option["Exit_Price"].iloc[0]))
            action = top_option["Action"].iloc[0]
            strike_price = float(top_option["strikePrice"].iloc[0])
            position_side = "call" if "Call" in action else "put"

            # Prevent immediate re-entry (same contract already CLOSED earlier)
            q = db.query(PaperOptionBotTradeHistory).filter_by(
            user_id=user_id,
            option_symbol=option_symbol,
            expiry=nearest_expiry,
            status="CLOSED"
            )
            latest_closed = _order_by_history_ts(q, PaperOptionBotTradeHistory).first()

            latest_closed = db.query(PaperOptionBotTradeHistory).filter_by(
                user_id=user_id,
                option_symbol=option_symbol,
                expiry=nearest_expiry,
                status="CLOSED"
            ).order_by(PaperOptionBotTradeHistory.closed_at.desc()).first()
            if latest_closed:
                logger.info(f"[OPTION-BOT] Already traded {option_symbol} for {symbol} with expiry {nearest_expiry}. Skipping.")
                return

            # Guard: if an OPEN of the same contract slipped in via a race, bail
            dupe_open = db.query(PaperOptionBotOpenTrade).filter_by(
                user_id=user_id,
                option_symbol=option_symbol,
                expiry=nearest_expiry,
                status="OPEN"
            ).first()
            if dupe_open:
                logger.info(f"[OPTION-BOT] Duplicate open detected for {option_symbol}; skipping.")
                return

            logger.info(
                f"[PICKED OPTION] user={user_id} symbol={symbol} option_symbol={option_symbol} "
                f"expiry={nearest_expiry} strike={strike_price} side={position_side} action={action} "
                f"ask={ask_price} stop_loss={stop_loss} exit={profit_target} "
                f"date={top_option['date'].iloc[0]} time={top_option['time'].iloc[0]} "
                f"gamma={top_option.get('gamma', 0.0).iloc[0]} delta={top_option.get('delta', 0.0).iloc[0]}"
            )

            account = db.query(PaperAccount).filter_by(user_id=user_id).first()
            if not account or account.current_balance < (TRADE_SIZE * ask_price):
                logger.warning(f"[OPTION-BOT] Insufficient balance for {option_symbol}")
                return

            # Spend, create open trade
            account.current_balance -= TRADE_SIZE * ask_price
            new_trade = PaperOptionBotOpenTrade(
                user_id=user_id,
                option_symbol=option_symbol,
                underlying_symbol=symbol,
                trade_type="BUY" if "Buy" in action else "SELL",
                position_side=position_side,
                quantity=TRADE_SIZE,
                entry_price=float(ask_price),
                strike_price=float(strike_price),
                expiry=nearest_expiry,
                status="OPEN",
                planned_stop_loss=float(stop_loss),
                planned_exit_price=float(profit_target),
                executed_at=datetime.utcnow(),
            )
            db.add(new_trade)
            db.commit()
            logger.info("📩 Option trade notification sent to %s for %s", user_email, option_symbol)
        # except Exception as e:
        #     logger.exception("[OPTION-BOT] Email send failed but trade opened: %s", e)

            # Idempotent email: same key for the same (user, underlying, expiry, contract)
            entry_key = build_idem_key(user_id, symbol, nearest_expiry, option_symbol, position_side, minute_bucket=False)
            if user_email and not notification_already_sent(db, user_id, "entry", entry_key, trade_type="option"):
                email_service.send_option_trade_notification(
                    email=user_email,
                    symbol=option_symbol,
                    side="BUY" if "Buy" in action else "SELL",
                    price=float(ask_price),
                    qty=TRADE_SIZE,
                    interval="N/A",
                    algo_name="Option Bot",
                    strike=strike_price,
                    expiry=nearest_expiry,
                    position_side=position_side
                )
                log_notification(
                    db, user_id, "entry", entry_key, user_email,
                    {
                        "symbol": option_symbol,
                        "side": "BUY" if "Buy" in action else "SELL",
                        "qty": TRADE_SIZE,
                        "price": float(ask_price),
                        "strike": strike_price,
                        "expiry": nearest_expiry
                    },
                    trade_type="option"
                )

    except Exception as e:
        db.rollback()
        logger.error(f"[OPTION-BOT] Error: {e}")
    finally:
        db.close()
    logger.warning(f"[OPTION-BOT] Finished tick for {symbol} user_id={user_id}")


def check_and_close_open_option_trades():
    # Intentionally keeps the same logic, adds idempotent email key
    db = SessionLocal()
    email_service = EmailService()
    try:
        open_trades = db.query(PaperOptionBotOpenTrade).filter_by(status="OPEN").all()
        now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
        for trade in open_trades:
            expiry_date = datetime.strptime(trade.expiry, "%Y-%m-%d").replace(tzinfo=pytz.UTC)
            user = db.query(User).filter_by(id=trade.user_id).first()
            user_email = user.email if user else None

            should_close = False
            close_reason = None
            exit_price = None

            if now_utc > expiry_date:
                exit_price = 0.0
                should_close = True
                close_reason = "expiry"
            else:
                live_price = get_quote_price(trade.option_symbol)
                if live_price is not None:
                    live_price = Decimal(str(live_price))
                    if trade.planned_stop_loss and live_price <= Decimal(str(trade.planned_stop_loss)):
                        exit_price = float(live_price)
                        should_close = True
                        close_reason = "stop-loss"
                    elif trade.planned_exit_price and live_price >= Decimal(str(trade.planned_exit_price)):
                        exit_price = float(live_price)
                        should_close = True
                        close_reason = "profit"

            if should_close:
                entry_price = Decimal(str(trade.entry_price))
                exit_val = Decimal(str(exit_price))
                pnl = float((exit_val - entry_price) * trade.quantity)
                closed_trade = PaperOptionBotTradeHistory(
                    bot_id=trade.bot_id,
                    user_id=trade.user_id,
                    option_symbol=trade.option_symbol,
                    underlying_symbol=trade.underlying_symbol,
                    trade_type=trade.trade_type,
                    position_side=trade.position_side,
                    quantity=trade.quantity,
                    entry_price=trade.entry_price,
                    exit_price=exit_price,
                    status="CLOSED",
                    created_at=trade.executed_at,
                    closed_at=now_utc,
                    strike_price=trade.strike_price,
                    expiry=trade.expiry,
                    pnl=pnl,
                    planned_stop_loss=trade.planned_stop_loss,
                    planned_exit_price=trade.planned_exit_price,
                )
                db.add(closed_trade)
                db.delete(trade)

                account = db.query(PaperAccount).filter_by(user_id=trade.user_id).first()
                if account:
                    account.current_balance += (trade.quantity * Decimal(str(exit_price))) - (trade.quantity * Decimal(str(trade.entry_price)))
                db.commit()
                reason_str = f" due to {close_reason}" if close_reason else ""
                logging.info(f"[OPTION-BOT] ✅ Closed {trade.option_symbol}{reason_str} @ {exit_price}")

                exit_key = build_idem_key(
                    trade.user_id, trade.underlying_symbol, trade.expiry, trade.option_symbol, trade.position_side, minute_bucket=False
                )
                if user_email and not notification_already_sent(db, trade.user_id, "exit", exit_key, trade_type="option"):
                    email_service.send_option_trade_notification(
                        email=user_email,
                        symbol=trade.option_symbol,
                        side="SELL",
                        price=float(exit_price),
                        qty=int(trade.quantity),
                        interval="N/A",
                        algo_name="Option Bot",
                        strike=trade.strike_price,
                        expiry=trade.expiry,
                        position_side=trade.position_side
                    )
                    log_notification(
                        db, trade.user_id, "exit", exit_key, user_email,
                        {
                            "symbol": trade.option_symbol, "side": "SELL", "qty": int(trade.quantity),
                            "price": float(exit_price), "strike": trade.strike_price, "expiry": trade.expiry
                        },
                        trade_type="option"
                    )

        db.commit()
    except Exception as e:
        db.rollback()
        logging.error(f"[OPTION-BOT] Error in close-checker: {e}")
    finally:
        db.close()

# ============ Email BOT =================#

def _as_str_id(x) -> str:
    """Always store/compare trade_id as a string."""
    return "" if x is None else str(x)

def _notification_already_sent(db, *, user_id: int, trade_type: str, notification_type: str, trade_id) -> bool:
    trade_id_str = _as_str_id(trade_id)
    sql = text("""
        SELECT 1
        FROM trade_notification_log
        WHERE user_id = :user_id
          AND trade_type = :trade_type
          AND notification_type = :notification_type
          AND trade_id = :trade_id
        LIMIT 1
    """)
    row = db.execute(sql, {
        "user_id": user_id,
        "trade_type": trade_type,
        "notification_type": notification_type,
        "trade_id": trade_id_str,
    }).first()
    return row is not None

def _record_notification(db, *, user_id: int, trade_type: str, notification_type: str, trade_id):
    trade_id_str = _as_str_id(trade_id)
    sql = text("""
        INSERT INTO trade_notification_log (user_id, trade_id, trade_type, notification_type, sent_at)
        VALUES (:user_id, :trade_id, :trade_type, :notification_type, :sent_at)
        ON CONFLICT DO NOTHING
    """)
    try:
        db.execute(sql, {
            "user_id": user_id,
            "trade_id": trade_id_str,
            "trade_type": trade_type,
            "notification_type": notification_type,
            "sent_at": datetime.utcnow(),
        })
        db.commit()
    except IntegrityError:
        db.rollback()
        
# === CLI Entry ===
if __name__ == "__main__":
    symbol = sys.argv[1].upper() if len(sys.argv) > 1 else "AAPL"
    user_id = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    run_option_paper_bot_tick(user_id, symbol)
