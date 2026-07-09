# /var/www/stockwicks/app/scripts/stock_algos/algo3_runner.py

import os
import json
import logging
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy.orm import Session

from app.database.connection import SessionLocal
from app.models.paper_trading_bot import (
    PaperStockTradeBot,
    PaperStockBotOpenTrade,
    PaperStockBotTradeHistory,
)

# --- IMPORT ACTUAL ALGO LOGIC ---
from app.scripts.stocks.bots.algo3_logic import determine_signals as algo3_signals

from app.scripts.stock_algos.base_wiring import (
    StockBaseRunner,
    is_market_open_now,
    _ET,
)

from app.services.paper_trade_service import open_position, close_position


# --- Config ---
COOLDOWN_SEC = 90
DATA_ROOT = os.getenv("DATA_DIR", "/var/www/stockwicks/data")


# --- Logging Function ---
def log_bot_decision(bot, data_len, last_bar, decision):
    """Appends a detailed log of the bot's decision-making context."""
    try:
        user_log_dir = os.path.join(DATA_ROOT, str(bot.user_id))
        os.makedirs(user_log_dir, exist_ok=True)

        safe_symbol = str(bot.symbol or "").replace("/", "_").replace(" ", "_").upper()
        log_file_name = f"bot_{bot.id}_{safe_symbol}_{bot.algo_name}.log"
        log_file_path = os.path.join(user_log_dir, log_file_name)

        timestamp_str = datetime.now(_ET).strftime("%Y-%m-%d %H:%M:%S %Z")

        smi_val = last_bar.get("SMI", "N/A") if hasattr(last_bar, "get") else "N/A"
        smi_str = f"{smi_val:.2f}" if isinstance(smi_val, (int, float)) else str(smi_val)

        buy_signal = last_bar.get("Buy_Signal", "N/A") if hasattr(last_bar, "get") else "N/A"
        sell_signal = last_bar.get("Sell_Signal", "N/A") if hasattr(last_bar, "get") else "N/A"
        close_val = last_bar.get("close", "N/A") if hasattr(last_bar, "get") else "N/A"
        open_val = last_bar.get("open", "N/A") if hasattr(last_bar, "get") else "N/A"
        interval = getattr(bot, "interval", "") or ""
        symbol = str(getattr(bot, "symbol", "") or "").upper()
        feature_snapshot = {
            "smi": smi_val,
            "buy_signal": 1.0 if buy_signal is True else 0.0,
            "sell_signal": 1.0 if sell_signal is True else 0.0,
        }

        log_message = "\n" + "=" * 88 + "\n"
        log_message += f"{timestamp_str} | {symbol} {interval} | {decision} | INDICATOR_STATE\n"
        log_message += f"algo={bot.algo_name} feature_set=SMI\n"
        log_message += f"close={close_val} open={open_val} rows={data_len} position=UNKNOWN\n"
        log_message += "features=" + json.dumps(feature_snapshot, default=str) + "\n"
        log_message += f"[{timestamp_str}] ACTION: {decision}\n"
        log_message += f"\tData Fetched: {data_len} candles\n"
        log_message += f"\tAlgo3 State: SMI={smi_str}\n"

        log_message += f"\tSignals: Buy={buy_signal}, Sell={sell_signal}\n"
        log_message += "-" * 40 + "\n"

        with open(log_file_path, "a") as f:
            f.write(log_message)

    except Exception as e:
        logging.error(f"CRITICAL: Failed to write Algo3 bot log file: {e}", exc_info=True)


def _normalize_now_et(anchor_dt=None):
    """
    Uses scheduler-provided anchor_dt when Celery passes it.
    Falls back to current ET time when run manually.
    """
    now_et = anchor_dt if anchor_dt is not None else datetime.now(_ET)

    if getattr(now_et, "tzinfo", None) is None:
        now_et = _ET.localize(now_et)
    else:
        now_et = now_et.astimezone(_ET)

    return now_et


# --- Main Bot Logic ---
def run_algo3_bot_tick(bot_id: int, anchor_dt=None, **kwargs):
    """
    Algo3 live/paper runner.

    Accepts anchor_dt so it is compatible with:
        fn(bot.id, anchor_dt=anchor_dt)

    This fixes:
        run_algo3_bot_tick() got an unexpected keyword argument 'anchor_dt'
    """
    runner = StockBaseRunner()
    db: Session = SessionLocal()

    decision_made = "NONE"
    last_bar = {}
    data_len = 0
    bot = None

    try:
        bot = db.query(PaperStockTradeBot).filter(PaperStockTradeBot.id == bot_id).first()
        if not bot:
            logging.error(f"[ALGO3] Bot ID {bot_id} not found.")
            return

        now_et = _normalize_now_et(anchor_dt)

        # ------------------------------------------------------------
        # MARKET HOURS GATE
        # Currently disabled for testing, as in your original script.
        # Enable later if needed.
        # ------------------------------------------------------------
        # if not is_market_open_now(now_et):
        #     decision_made = "MARKET_CLOSED"
        #     return

        df = runner.fetch_source_bars(bot.symbol, user_id=bot.user_id)

        if df is None or df.empty:
            decision_made = "NO_DATA"
            return

        data_len = len(df)

        if data_len < 20:
            decision_made = "INSUFFICIENT_DATA"
            return

        latest_time, _, resampled_df = runner.resample_interval(df, bot.interval, bot.symbol)

        if latest_time is None or resampled_df is None or resampled_df.empty:
            decision_made = "RESAMPLE_FAILED"
            return

        # --- Use ACTUAL Algo3 logic ---
        signals_df = algo3_signals(resampled_df)

        if signals_df is None or signals_df.empty or "Buy_Signal" not in signals_df.columns:
            decision_made = "SIGNAL_GEN_FAILED"
            return

        lt_et = latest_time if getattr(latest_time, "tzinfo", None) else _ET.localize(latest_time)
        lt_et = lt_et.astimezone(_ET)
        age = now_et - lt_et

        # ------------------------------------------------------------
        # STALE BAR GATE
        # Currently disabled for testing, as in your original script.
        # Enable later if needed.
        # ------------------------------------------------------------
        # if age > runner.max_age_for_interval(bot.interval):
        #     logging.info(
        #         f"[ALGO3] stale bar {bot.symbol}@{bot.interval} "
        #         f"bar={lt_et} now={now_et} age={age}"
        #     )
        #     decision_made = "STALE_BAR"
        #     return

        if runner.once_per_bar_guard(bot_id, lt_et):
            decision_made = "ALREADY_PROCESSED_BAR"
            return

        if len(signals_df) < 2:
            decision_made = "NOT_ENOUGH_SIGNAL_ROWS"
            return

        # Use previous completed signal bar, execute at current bar open
        last_bar = signals_df.iloc[-2]
        current_price = float(signals_df.iloc[-1]["open"])

        open_trade = (
            db.query(PaperStockBotOpenTrade)
            .filter_by(bot_id=bot.id)
            .first()
        )

        # EOD close for intraday intervals
        if bot.interval in {"1min", "5min", "10min", "15min", "30min"}:
            if now_et.hour > 15 or (now_et.hour == 15 and now_et.minute >= 58):
                if open_trade:
                    close_position(db, open_trade, float(signals_df.iloc[-1]["close"]))
                    decision_made = "END_OF_DAY_CLOSE"
                else:
                    decision_made = "EOD_NO_OPEN_TRADE"
                return

        # Cooldown after last closed trade
        if not open_trade:
            last_hist = (
                db.query(PaperStockBotTradeHistory)
                .filter_by(bot_id=bot.id)
                .order_by(PaperStockBotTradeHistory.id.desc())
                .first()
            )

            if last_hist and getattr(last_hist, "exit_time", None):
                if (datetime.utcnow() - last_hist.exit_time) < timedelta(seconds=COOLDOWN_SEC):
                    decision_made = "COOLDOWN"
                    return

        buy_signal = bool(last_bar.get("Buy_Signal"))
        sell_signal = bool(last_bar.get("Sell_Signal"))

        if open_trade:
            if open_trade.position_side == "long" and sell_signal:
                close_position(db, open_trade, current_price)
                decision_made = "CLOSE_LONG"

            elif open_trade.position_side == "short" and buy_signal:
                close_position(db, open_trade, current_price)
                decision_made = "CLOSE_SHORT"

            else:
                decision_made = "HOLD_OPEN_TRADE"

        else:
            if buy_signal:
                open_position(db, bot, "long", current_price)
                decision_made = "OPEN_LONG"

            elif sell_signal and getattr(bot, "allow_short_selling", False):
                open_position(db, bot, "short", current_price)
                decision_made = "OPEN_SHORT"

            elif sell_signal and not getattr(bot, "allow_short_selling", False):
                decision_made = "SELL_SIGNAL_SHORT_DISABLED"

            else:
                decision_made = "NO_ENTRY_SIGNAL"

    except Exception as e:
        db.rollback()
        logging.error(f"[ALGO3] tick error for bot {bot_id}: {e}", exc_info=True)
        decision_made = f"ERROR: {e}"

    finally:
        if bot:
            log_bot_decision(bot, data_len, last_bar, decision_made)

        if db:
            db.close()
