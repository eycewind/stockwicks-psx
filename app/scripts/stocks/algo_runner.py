# app/scripts/stock_algos/algo_runner.py
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
import os  # <-- ADDED IMPORT
import logging  # <-- ADDED IMPORT

from app.database.connection import SessionLocal
from app.models.paper_trading_bot import PaperStockTradeBot, PaperStockBotOpenTrade, PaperStockBotTradeHistory
from app.scripts.stocks.backtest_algos.Algo1_eval import get_data_for_fixed_period, ET
from app.scripts.stocks.bots.algo1_logic import determine_signals as algo1_signals
from app.scripts.stocks.bots.algo2_logic import determine_signals as algo2_signals
from app.scripts.stocks.bots.algo3_logic import determine_signals as algo3_signals
from app.services.paper_trade_service import open_position, close_position


# --- Config ---
ALGO_FUNCTIONS = {
    "Algo1": algo1_signals,
    "Algo2": algo2_signals,
    "Algo3": algo3_signals,
}
COOLDOWN_SEC = 90  # <- prevent immediate re-entry after a close
DATA_ROOT = os.getenv("DATA_DIR", "/var/www/stockwicks/data") # <-- ADDED CONFIG

# --- ADD A CONFIGURABLE STOP LOSS AMOUNT ---
FIXED_STOP_LOSS_DOLLAR_AMOUNT = 300.0

# --- NEW LOGGING FUNCTION ---
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
        safe_symbol = str(bot.symbol or "").replace("/", "_").replace(" ", "_").upper()
        log_file_name = f"bot_{bot.id}_{safe_symbol}_{bot.algo_name}.log"
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
        # Use 'logging' directly since 'logger' might not be in scope
        logging.error(f"CRITICAL: Failed to write to bot log file {log_file_path}: {e}")
# --- END OF LOGGING FUNCTION ---


def run_algo_bot_tick(bot: PaperStockTradeBot):
    db: Session = SessionLocal()
    decision_made = "NONE"; last_bar = {}; data_len = 0
    try:
        now_et = datetime.now(ET)
        # MARKET HOURS GUARD (ensure this is uncommented for live)
        # if not (9 <= now_et.hour < 16 and now_et.weekday() < 5):
        #     decision_made = "MARKET_CLOSED"; return

        # --- Fetch Data and Check ---
        data_df = get_data_for_fixed_period(bot.symbol, bot.interval)
        data_len = len(data_df)
        if data_df is None or data_df.empty or data_len < 20: # Ensure enough data for indicators
            decision_made = "NO_DATA"; return

        # --- Get Signals ---
        signal_function = ALGO_FUNCTIONS.get(bot.algo_name)
        if not signal_function:
            decision_made = "NO_ALGO_FUNCTION"; return
        signals_df = signal_function(data_df)
        if signals_df is None or signals_df.empty or 'Buy_Signal' not in signals_df.columns:
            decision_made = "SIGNAL_GEN_FAILED"; return

        # --- Prepare for Execution ---
        last_bar = signals_df.iloc[-2] # Signal bar
        current_bar = signals_df.iloc[-1] # Current (potentially incomplete) bar
        execution_price = float(current_bar['open']) # Execute at current bar's open

        # --- Get Open Trade ---
        # Refresh the open_trade object to get the latest unrealized_pl
        open_trade = db.query(PaperStockBotOpenTrade).filter_by(bot_id=bot.id).first()
        if open_trade:
            db.refresh(open_trade) # Get the latest P/L calculated by the updater task

        # --- STOP LOSS CHECK (using unrealized_pl) ---
        if open_trade and open_trade.unrealized_pl is not None:
            if open_trade.unrealized_pl <= -abs(FIXED_STOP_LOSS_DOLLAR_AMOUNT):
                log.warning(f"STOP LOSS HIT ($ Amount) for Bot {bot.id} {bot.symbol}. Unrealized P/L: ${open_trade.unrealized_pl:.2f} <= -${abs(FIXED_STOP_LOSS_DOLLAR_AMOUNT):.2f}")
                # Use the *current* market price for closing, fetched live if possible, otherwise last close
                live_close_price = get_live_price(bot.symbol) # Assumes you have this function
                if live_close_price is None:
                    log.warning(f"Could not fetch live price for stop loss exit on {bot.symbol}, using last bar close.")
                    live_close_price = float(current_bar['close'])

                close_position(db, open_trade, live_close_price, reason=f"Stop Loss Hit (${FIXED_STOP_LOSS_DOLLAR_AMOUNT})")
                decision_made = "STOP_LOSS_HIT"
                return # Exit after stop loss
        # --- END STOP LOSS CHECK ---

        # --- EOD Close Check ---
        if bot.interval in {'1min','5min','10min','15min','30min'} and (now_et.hour > 15 or (now_et.hour == 15 and now_et.minute >= 58)):
            if open_trade:
                close_position(db, open_trade, float(current_bar['close']), reason="End of Day Close")
                decision_made = "END_OF_DAY_CLOSE"
            return # EOD done

        # --- Cooldown Check ---
        if not open_trade:
            last_hist = db.query(PaperStockBotTradeHistory).filter_by(bot_id=bot.id).order_by(PaperStockBotTradeHistory.id.desc()).first()
            if last_hist and getattr(last_hist, "exit_time", None):
                if (datetime.utcnow() - last_hist.exit_time) < timedelta(seconds=COOLDOWN_SEC):
                    decision_made = "COOLDOWN"; return

        # --- Normal Signal Exit/Entry ---
        if open_trade: # Check exits only if stop wasn't hit
            if (open_trade.position_side == "long" and bool(last_bar.get('Sell_Signal'))):
                close_position(db, open_trade, execution_price, reason="Signal Exit")
                decision_made = "CLOSE_LONG"
            elif (open_trade.position_side == "short" and bool(last_bar.get('Buy_Signal'))):
                close_position(db, open_trade, execution_price, reason="Signal Exit")
                decision_made = "CLOSE_SHORT"
        else: # Check entries only if flat and not in cooldown
            if bool(last_bar.get('Buy_Signal')):
                open_position(db, bot, "long", execution_price) # open_position does NOT need stop param now
                decision_made = "OPEN_LONG"
            elif bool(last_bar.get('Sell_Signal')) and getattr(bot, "allow_short_selling", False):
                open_position(db, bot, "short", execution_price) # open_position does NOT need stop param now
                decision_made = "OPEN_SHORT"

    finally:
        log_bot_decision(bot, data_len, last_bar, decision_made)
        db.close()
