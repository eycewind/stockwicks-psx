import logging
from datetime import datetime
from decimal import Decimal
from sqlalchemy.orm import Session
from app.database.connection import SessionLocal
from app.models.paper_trading import PaperAccount
from app.models.paper_trading_bot import PaperStockBotOpenTrade, PaperStockBotTradeHistory
from app.models.user import User
from app.services.email_service import EmailService, notification_already_sent, log_notification
from app.utils.stock.market_price import get_live_price
from app.utils.stock.algo_names import get_algo_name_from_file

def place_paper_trade(user_id, symbol, side, price, qty, bot_id=None):
    """Handle paper trades (open/close), balance, history, and notifications."""
    db: Session = SessionLocal()
    try:
        # normalize price/qty
        live_price = get_live_price(symbol)
        price = Decimal(str(live_price)) if live_price else Decimal(str(price))
        qty = Decimal(str(qty))

        # ensure account
        account = db.query(PaperAccount).filter_by(user_id=user_id).first()
        if not account:
            account = PaperAccount(user_id=user_id, current_balance=Decimal('10000000.0'))
            db.add(account)
            db.commit()
            db.refresh(account)

        # email + user
        email_service = EmailService()
        user = db.query(User).filter_by(id=user_id).first()
        user_email = user.email if user else None

        TRADE_TYPE = "stock"
        NOTIF_ENTRY, NOTIF_EXIT = "entry", "exit"

        # open trade check
        open_trade = (
            db.query(PaperStockBotOpenTrade)
              .filter_by(bot_id=bot_id, user_id=user_id, symbol=symbol)
              .first()
        )
        now_utc = datetime.utcnow()
        algo_name = get_algo_name_from_file(__file__)

        # BUY branch
        if side == "BUY":
            if open_trade and open_trade.position_side == "short":
                # close short
                pnl = (Decimal(str(open_trade.entry_price)) - price) * Decimal(str(open_trade.quantity))
                hist = PaperStockBotTradeHistory(
                    bot_id=open_trade.bot_id, user_id=open_trade.user_id, symbol=open_trade.symbol,
                    trade_type="BUY", position_side="short", quantity=open_trade.quantity,
                    entry_price=open_trade.entry_price, entry_time=open_trade.entry_time,
                    exit_price=float(price), exit_time=now_utc, profit_loss=float(pnl),
                    algo_name=algo_name,
                )
                db.add(hist)
                account.current_balance -= qty * price
                db.delete(open_trade)
                db.commit()

                if user_email and not notification_already_sent(db, user_id, NOTIF_EXIT, hist.id, trade_type=TRADE_TYPE):
                    email_service.send_stock_trade_notification(
                        email=user_email, symbol=symbol, side="BUY", price=float(price),
                        qty=int(qty), interval="N/A", algo_name=algo_name
                    )
                    log_notification(
                        db, user_id, NOTIF_EXIT, hist.id, user_email,
                        {"symbol": symbol, "side": "BUY", "qty": int(qty), "price": float(price)},
                        trade_type=TRADE_TYPE
                    )
                return True

            if open_trade and open_trade.position_side == "long":
                logging.info(f"[TRADE] Bot {bot_id} already long {symbol}; skip.")
                return True

            # open long
            cost = qty * price
            if account.current_balance < cost:
                logging.warning(f"[TRADE] Insufficient cash: need {cost}, have {account.current_balance}")
                db.rollback()
                return False

            account.current_balance -= cost
            new_open = PaperStockBotOpenTrade(
                bot_id=bot_id, user_id=user_id, symbol=symbol,
                trade_type="BUY", position_side="long", quantity=float(qty),
                entry_price=float(price), entry_time=now_utc, algo_name=algo_name,
            )
            db.add(new_open)
            db.commit()
            db.refresh(new_open)

            if user_email and not notification_already_sent(db, user_id, NOTIF_ENTRY, new_open.id, trade_type=TRADE_TYPE):
                email_service.send_stock_trade_notification(
                    email=user_email, symbol=symbol, side="BUY", price=float(price),
                    qty=int(qty), interval="N/A", algo_name=algo_name
                )
                log_notification(
                    db, user_id, NOTIF_ENTRY, new_open.id, user_email,
                    {"symbol": symbol, "side": "BUY", "qty": int(qty), "price": float(price)},
                    trade_type=TRADE_TYPE
                )
            return True

        # SELL branch
        elif side == "SELL":
            if open_trade and open_trade.position_side == "long":
                pnl = (price - Decimal(str(open_trade.entry_price))) * Decimal(str(open_trade.quantity))
                hist = PaperStockBotTradeHistory(
                    bot_id=open_trade.bot_id, user_id=open_trade.user_id, symbol=open_trade.symbol,
                    trade_type="SELL", position_side="long", quantity=open_trade.quantity,
                    entry_price=open_trade.entry_price, entry_time=open_trade.entry_time,
                    exit_price=float(price), exit_time=now_utc, profit_loss=float(pnl),
                    algo_name=algo_name,
                )
                db.add(hist)
                account.current_balance += qty * price
                db.delete(open_trade)
                db.commit()

                if user_email and not notification_already_sent(db, user_id, NOTIF_EXIT, hist.id, trade_type=TRADE_TYPE):
                    email_service.send_stock_trade_notification(
                        email=user_email, symbol=symbol, side="SELL", price=float(price),
                        qty=int(qty), interval="N/A", algo_name=algo_name
                    )
                    log_notification(
                        db, user_id, NOTIF_EXIT, hist.id, user_email,
                        {"symbol": symbol, "side": "SELL", "qty": int(qty), "price": float(price)},
                        trade_type=TRADE_TYPE
                    )
                return True

            if open_trade and open_trade.position_side == "short":
                logging.info(f"[TRADE] Bot {bot_id} already short {symbol}; skip.")
                return True

            # open short
            account.current_balance += qty * price
            new_open = PaperStockBotOpenTrade(
                bot_id=bot_id, user_id=user_id, symbol=symbol,
                trade_type="SELL", position_side="short", quantity=float(qty),
                entry_price=float(price), entry_time=now_utc, algo_name=algo_name,
            )
            db.add(new_open)
            db.commit()
            db.refresh(new_open)

            if user_email and not notification_already_sent(db, user_id, NOTIF_ENTRY, new_open.id, trade_type=TRADE_TYPE):
                email_service.send_stock_trade_notification(
                    email=user_email, symbol=symbol, side="SELL", price=float(price),
                    qty=int(qty), interval="N/A", algo_name=algo_name
                )
                log_notification(
                    db, user_id, NOTIF_ENTRY, new_open.id, user_email,
                    {"symbol": symbol, "side": "SELL", "qty": int(qty), "price": float(price)},
                    trade_type=TRADE_TYPE
                )
            return True

        logging.warning(f"[TRADE] Unknown side {side} for {symbol}")
        return False

    except Exception as e:
        db.rollback()
        logging.error(f"[TRADE] ❌ DB Error: {e}")
        return False
    finally:
        db.close()
