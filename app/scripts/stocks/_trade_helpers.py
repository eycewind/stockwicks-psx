from datetime import datetime
from sqlalchemy.exc import IntegrityError
from app.models.paper_trading_bot import (
    PaperStockTradeBot,
    PaperStockBotOpenTrade,
    PaperStockBotTradeHistory,
)

def has_open_stock(db, bot_id, user_id):
    return db.query(PaperStockBotOpenTrade.id)\
             .filter_by(bot_id=bot_id, user_id=user_id)\
             .first() is not None

def open_stock_position(db, bot: PaperStockTradeBot, trade_type: str, position_side: str,
                        qty: float, price: float, when: datetime | None = None):
    row = PaperStockBotOpenTrade(
        bot_id=bot.id, user_id=bot.user_id, symbol=bot.symbol,
        trade_type=trade_type, position_side=position_side,
        quantity=qty, entry_price=price, entry_time=when or datetime.utcnow(),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # unique-constraint: already open on this side
        return None
    return row

def close_stock_position(db, open_row: PaperStockBotOpenTrade,
                         exit_price: float, when: datetime | None = None, note: str = ""):
    when = when or datetime.utcnow()
    # P&L: long -> (exit - entry) * qty ; short -> (entry - exit) * qty
    if open_row.position_side.lower() == "long":
        pl = (exit_price - open_row.entry_price) * open_row.quantity
    else:
        pl = (open_row.entry_price - exit_price) * open_row.quantity

    hist = PaperStockBotTradeHistory(
        bot_id=open_row.bot_id, user_id=open_row.user_id, symbol=open_row.symbol,
        trade_type=open_row.trade_type, position_side=open_row.position_side,
        quantity=open_row.quantity,
        entry_price=open_row.entry_price, entry_time=open_row.entry_time,
        exit_price=exit_price,  exit_time=when,
        profit_loss=pl, note=note[:255] if note else None,
        algo_name=bot.algo_name or "unknown",
)
    db.add(hist)
    db.delete(open_row)
    db.commit()
    return hist
