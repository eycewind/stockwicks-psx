#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/bots/stock_bot_worker.py

import sys, os, time, logging
from datetime import datetime
import pandas as pd
from sqlalchemy.orm import Session

# --- Setup Project Path ---
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
if REPO_ROOT not in sys.path: sys.path.insert(0, REPO_ROOT)

# --- Database and Model Imports ---
from app.database.connection import get_db
from app.models.paper_trading_bot import PaperStockTradeBot, PaperStockBotOpenTrade, PaperStockBotTradeHistory

# --- Algorithm and Data Fetching Imports ---
from app.scripts.stocks.backtest_algos.Algo1_eval import get_data_for_fixed_period, ET
from app.scripts.stocks.bots.algo1_logic import determine_signals as algo1_signals
from app.scripts.stocks.bots.algo2_logic import determine_signals as algo2_signals
from app.scripts.stocks.bots.algo3_logic import determine_signals as algo3_signals
from app.services.notification_service import send_email_notification # For email alerts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s (BOT_WORKER) %(message)s")

# --- Mapping Algorithm Names to Functions ---
ALGO_FUNCTIONS = {
    "Algo1": algo1_signals,
    "Algo2": algo2_signals,
    "Algo3": algo3_signals,
}

# /var/www/stockwicks/app/scripts/bots/stock_bot_worker.py
... (after your imports) ...

# --- Config ---
DATA_ROOT = os.getenv("DATA_DIR", "/var/www/stockwicks/data")

def log_bot_decision(bot, data_len, last_bar, decision):
    """
    Appends a detailed log of the bot's decision-making context to a
    user-specific file.
    """
    try:
        # 1. Ensure the user's data directory exists
        user_log_dir = os.path.join(DATA_ROOT, str(bot.user_id))
        os.makedirs(user_log_dir, exist_ok=True)
        
        # 2. Define the specific log file for this bot
        log_file_name = f"bot_{bot.id}_{bot.symbol}_{bot.algo_name}.log"
        log_file_path = os.path.join(user_log_dir, log_file_name)
        
        # 3. Create the detailed log message
        timestamp_str = datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S %Z')
        log_message = f"[{timestamp_str}] ACTION: {decision}\n"
        
        # --- THIS IS THE SMOKING GUN ---
        log_message += f"\tData Fetched: {data_len} candles\n"
        # ---
        
        # 4. Add indicator-specific data
        if bot.algo_name == "Algo1":
            log_message += (
                f"\tAlgo1 State: Close={last_bar.get('close', 'N/A'):.2f}, "
                f"EMA200={last_bar.get('EMA200', 'N/A'):.2f}, "
                f"VWAP_Lower={last_bar.get('VWAP_LowerBand', 'N/A'):.2f}\n"
            )
        elif bot.algo_name == "Algo2":
            log_message += (
                f"\tAlgo2 State: Close={last_bar.get('close', 'N/A'):.2f}, "
                f"ATR_Stop={last_bar.get('atr_stop', 'N/A'):.2f}\n"
            )
        elif bot.algo_name == "Algo3":
            log_message += (
                f"\tAlgo3 State: SMI={last_bar.get('SMI', 'N/A'):.2f}\n"
            )
            
        log_message += f"\tSignals: Buy={last_bar.get('Buy_Signal', 'N/A')}, Sell={last_bar.get('Sell_Signal', 'N/A')}\n"
        log_message += "-"*40 + "\n"
        
        # 5. Append to the file (using 'a' mode)
        with open(log_file_path, 'a') as f:
            f.write(log_message)
            
    except Exception as e:
        # Log this specific error to the main (console) log
        logging.error(f"CRITICAL: Failed to write to bot log file {log_file_path}: {e}")

# --- Live Trading & Email Notification Placeholders ---
def send_trade_to_schwab(symbol, quantity, instruction):
    """
    Placeholder for your live Schwab trading logic.
    Instruction will be "BUY", "SELL", "SELL_SHORT", or "BUY_TO_COVER".
    """
    logging.info(f"--- LIVE TRADE (MIRROR) --- Symbol: {symbol}, Qty: {quantity}, Action: {instruction}")
    # Here you would add your code to format and send an order to the Schwab API.
    # Example: schwab.place_order(...)
    pass

def send_trade_notification_email(user_email, subject, body):
    """
    Sends a trade notification email via the notification service.
    """
    if not user_email: return
    try:
        logging.info(f"Sending trade notification email to {user_email}")
        send_email_notification(user_email, subject, body)
    except Exception as e:
        logging.error(f"Failed to send notification email to {user_email}: {e}")

# --- Core Bot Trading Logic ---
def open_position(db: Session, bot: PaperStockTradeBot, side: str, price: float):
    new_trade = PaperStockBotOpenTrade(
        bot_id=bot.id, user_id=bot.user_id, symbol=bot.symbol,
        position_side=side, quantity=bot.trade_size, entry_price=price,
        entry_time=datetime.utcnow()
    )
    db.add(new_trade)
    db.commit()
    logging.info(f"PAPER TRADE: Opened {side} position for bot #{bot.id} at ${price:.2f}")

    if bot.notify_email and bot.user.email:
        subject = f"Trade Opened: {bot.symbol}"
        body = f"A new {side} trade was opened for {bot.symbol} at ${price:.2f} by Bot #{bot.id} ({bot.algo_name})."
        send_trade_notification_email(bot.user.email, subject, body)

    if bot.mirror_live:
        instruction = "BUY" if side == "long" else "SELL_SHORT"
        send_trade_to_schwab(bot.symbol, bot.trade_size, instruction)

def close_position(db: Session, trade: PaperStockBotOpenTrade, price: float):
    bot = trade.bot # Access the bot relationship
    pnl = (price - trade.entry_price) * trade.quantity if trade.position_side == "long" else (trade.entry_price - price) * trade.quantity
    
    history_trade = PaperStockBotTradeHistory(
        bot_id=trade.bot_id, user_id=trade.user_id, symbol=trade.symbol,
        position_side=trade.position_side, quantity=trade.quantity,
        entry_price=trade.entry_price, exit_price=price, pnl=pnl,
        entry_time=trade.entry_time, exit_time=datetime.utcnow()
    )
    db.add(history_trade)
    db.delete(trade)
    db.commit()
    logging.info(f"PAPER TRADE: Closed {trade.position_side} position for bot #{trade.bot_id} for a P/L of ${pnl:.2f}")

    if bot and bot.notify_email and bot.user.email:
        subject = f"Trade Closed: {bot.symbol}"
        body = f"The {trade.position_side} trade for {bot.symbol} was closed at ${price:.2f} for a P/L of ${pnl:.2f}."
        send_trade_notification_email(bot.user.email, subject, body)

    if bot and bot.mirror_live:
        instruction = "SELL" if trade.position_side == "long" else "BUY_TO_COVER"
        send_trade_to_schwab(trade.symbol, trade.quantity, instruction)

# /var/www/stockwicks/app/scripts/bots/stock_bot_worker.py

def process_active_bots():
    db: Session = next(get_db())
    try:
        active_bots = db.query(PaperStockTradeBot).filter_by(is_active=True).all()
        logging.info(f"Found {len(active_bots)} active bot(s) to process.")

        for bot in active_bots:
            try:
                logging.info(f"Processing bot #{bot.id}: {bot.symbol} | {bot.algo_name} | {bot.interval}")
                data_df = get_data_for_fixed_period(bot.symbol, bot.interval)
                
                # --- Get the total candle count ---
                data_len = len(data_df)
                
                if data_df.empty or data_len < 2:
                    logging.warning(f"Not enough data for {bot.symbol} on {bot.interval}, skipping.")
                    continue

                signal_function = ALGO_FUNCTIONS.get(bot.algo_name)
                if not signal_function:
                    logging.error(f"Algorithm '{bot.algo_name}' not found for bot #{bot.id}.")
                    continue
                    
                signals_df = signal_function(data_df)
                if 'Buy_Signal' not in signals_df.columns:
                    logging.error(f"Signal generation failed for bot #{bot.id}")
                    continue

                last_bar = signals_df.iloc[-2]
                execution_price = last_bar['close'] 

                open_trade = db.query(PaperStockBotOpenTrade).filter_by(bot_id=bot.id).first()

                now_et = datetime.now(ET)
                day_trade_intervals = {'1min', '5min', '10min', '15min'}
                
                decision_made = "NONE" # Default decision for logging

                if bot.interval in day_trade_intervals and now_et.hour == 15 and now_et.minute >= 58:
                    if open_trade:
                        logging.info(f"END OF DAY CLOSE for bot #{bot.id} ({bot.symbol})")
                        close_position(db, open_trade, signals_df.iloc[-1]['close'])
                        decision_made = "END_OF_DAY_CLOSE"
                    
                    # --- CALL THE LOGGER ---
                    log_bot_decision(bot, data_len, last_bar, decision_made)
                    continue 

                if open_trade:
                    if (open_trade.position_side == "long" and last_bar['Sell_Signal']):
                        logging.info(f"Exit signal found for bot #{bot.id} ({bot.symbol})")
                        close_position(db, open_trade, execution_price)
                        decision_made = "CLOSE_LONG"
                    elif (open_trade.position_side == "short" and last_bar['Buy_Signal']):
                        logging.info(f"Exit signal found for bot #{bot.id} ({bot.symbol})")
                        close_position(db, open_trade, execution_price)
                        decision_made = "CLOSE_SHORT"
                else:
                    if last_bar['Buy_Signal']:
                        logging.info(f"Enter Long signal found for bot #{bot.id} ({bot.symbol})")
                        open_position(db, bot, "long", execution_price)
                        decision_made = "OPEN_LONG"
                    elif last_bar['Sell_Signal'] and bot.allow_short_selling:
                        logging.info(f"Enter Short signal found for bot #{bot.id} ({bot.symbol})")
                        open_position(db, bot, "short", execution_price)
                        decision_made = "OPEN_SHORT"

                # --- CALL THE LOGGER (main decision call) ---
                log_bot_decision(bot, data_len, last_bar, decision_made)
                
            except Exception as e:
                logging.error(f"Error processing bot #{bot.id}: {e}", exc_info=True)
    finally:
        db.close()

# In /var/www/stockwicks/app/scripts/bots/stock_bot_worker.py
if __name__ == "__main__":
    logging.info("--- Starting Stock Bot Worker (15-Second Synchronized Clock) ---")
    while True:
        try:
            # Run the main processing function first
            process_active_bots()

            # --- SMART 15-SECOND SYNCHRONIZATION LOGIC ---
            now = datetime.now(ET)
            # Calculate how many seconds to sleep to wake up 2 seconds past the next 15-second interval
            # The modulo operator (%) finds the remainder of the current second within a 15s block.
            seconds_to_sleep = (15 - (now.second % 15)) + 2  # Wake up at :02, :17, :32, :47

            logging.info(f"Cycle finished. Synchronizing... Sleeping for {seconds_to_sleep} seconds.")
            time.sleep(seconds_to_sleep)

        except Exception as e:
            logging.error(f"An unhandled error occurred in the main loop: {e}", exc_info=True)
            # In case of a major error, fall back to a longer sleep
            logging.info("Error detected. Sleeping for 15 seconds before retrying...")
            time.sleep(15)