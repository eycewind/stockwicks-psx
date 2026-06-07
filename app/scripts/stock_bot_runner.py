# app/scripts/stock_bot_runner.py
import logging, datetime as dt
from app.database.connection import SessionLocal
from app.models.paper_trading_bot import (
    PaperStockTradeBot,
    PaperStockBotOpenTrade,
    PaperStockBotTradeHistory
)
from app.scripts import algo1_SMI_runner, algo2_simple_runner, algo3_MACD_runner
from app.utils.notifications import send_trade_email

ALGOS = {
    "Algo1": algo1_SMI_runner.run_algo,
    "Algo2": algo2_simple_runner.run_algo,
    "Algo3": algo3_MACD_runner.run_algo,
}

def run_stock_bot_tick(bot_id: int):
    db = SessionLocal()
    try:
        bot = db.query(PaperStockTradeBot).filter_by(id=bot_id, is_active=True).first()
        if not bot:
            return

        logging.info(f"[BOT {bot.id}] Running {bot.algo_name} on {bot.symbol} ({bot.interval})")

        # Run the algo (it will update CSVs)
        algo_fn = ALGOS.get(bot.algo_name)
        if not algo_fn:
            logging.error(f"Unknown algo {bot.algo_name}")
            return
        algo_fn(bot.user_id, bot.symbol, bot.interval, bot.trade_size)

        # Check if we should open/close
        handle_positions(db, bot)

        bot.updated_at = dt.datetime.utcnow()
        db.commit()

    except Exception as e:
        logging.exception(f"Bot {bot_id} tick failed: {e}")
    finally:
        db.close()

def handle_positions(db, bot):
    # look for an open trade
    open_pos = db.query(PaperStockBotOpenTrade).filter_by(bot_id=bot.id).first()

    # 🔍 instead of CSV parsing, you may want a direct signal generator
    # For now we fake: if no open pos -> open long; if open pos -> close it.
    # Replace with logic reading notify CSVs.
    now = dt.datetime.utcnow()

    if not open_pos:
        new_trade = PaperStockBotOpenTrade(
            bot_id=bot.id,
            user_id=bot.user_id,
            symbol=bot.symbol,
            trade_type="BUY",
            position_side="long",
            quantity=bot.trade_size,
            entry_price=100.0,  # replace with real price
            entry_time=now
        )
        db.add(new_trade)
        db.flush()
        send_trade_email(f"Bot {bot.symbol} opened long", f"Entry at 100.0")

    else:
        hist = PaperStockBotTradeHistory(
            bot_id=bot.id,
            user_id=bot.user_id,
            symbol=bot.symbol,
            trade_type=open_pos.trade_type,
            position_side=open_pos.position_side,
            quantity=open_pos.quantity,
            entry_price=open_pos.entry_price,
            entry_time=open_pos.entry_time,
            exit_price=101.0,  # replace with real price
            exit_time=now,
            profit_loss=1.0
            algo_name=bot.algo_name or "unknown",
)
        db.add(hist)
        db.delete(open_pos)
        send_trade_email(f"Bot {bot.symbol} closed", f"Exit at 101.0 | PnL=+1.0")
