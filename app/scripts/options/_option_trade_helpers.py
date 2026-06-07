#/var/www/stockwicks/app/scripts/_option_trade_helpers.py
from datetime import datetime
from sqlalchemy.exc import IntegrityError
from app.models.paper_option_trading_bot import (
    PaperOptionTradeBot,
    PaperOptionBotOpenTrade,
    PaperOptionBotTradeHistory,
)

def has_open_option(db, bot_id, user_id):
    return db.query(PaperOptionBotOpenTrade.id)\
             .filter_by(bot_id=bot_id, user_id=user_id)\
             .first() is not None

def open_option_position(db, bot: PaperOptionTradeBot, option_symbol: str, underlying: str,
                         trade_type: str, position_side: str, qty: int, price: float,
                         strike: float | None, expiry: str | None,
                         when: datetime | None = None):
    row = PaperOptionBotOpenTrade(
        bot_id=bot.id, user_id=bot.user_id,
        option_symbol=option_symbol, underlying_symbol=underlying,
        trade_type=trade_type, position_side=position_side,
        quantity=qty, entry_price=price, strike_price=strike, expiry=expiry,
        entry_time=when or datetime.utcnow(),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # UC hit: duplicate open on same contract+direction
        return None
    return row

def close_option_position(db, open_row: PaperOptionBotOpenTrade,
                          exit_price: float, when: datetime | None = None, note: str = ""):
    when = when or datetime.utcnow()
    # Options prices are per-contract premium; qty is contracts
    # Direction agnostic P&L depends on position side only if you’re shorting options.
    if open_row.trade_type.upper() == "BUY":
        pnl = (exit_price - open_row.entry_price) * open_row.quantity
    else:
        pnl = (open_row.entry_price - exit_price) * open_row.quantity

    hist = PaperOptionBotTradeHistory(
        bot_id=open_row.bot_id, user_id=open_row.user_id,
        option_symbol=open_row.option_symbol, underlying_symbol=open_row.underlying_symbol,
        trade_type=open_row.trade_type, position_side=open_row.position_side,
        quantity=open_row.quantity,
        entry_price=open_row.entry_price, entry_time=open_row.entry_time,
        exit_price=exit_price, exit_time=when,
        pnl=pnl,
        strike_price=open_row.strike_price, expiry=open_row.expiry,
        planned_stop_loss=open_row.planned_stop_loss,
        planned_exit_price=open_row.planned_exit_price,
    )
    db.add(hist)
    db.delete(open_row)
    db.commit()
    return hist
