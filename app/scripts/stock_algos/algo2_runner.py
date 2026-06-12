#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/stock_algos/algo2_runner.py
"""
Algo2 Runner (LIVE) — MACD Reversal (REALISTIC mode)

Core behavior:
- Signals are generated from completed candles only.
- Entry: NEXT candle's open after a signal.
- Exit: NEXT candle's open after an opposite signal.
- Fixed-dollar stop-loss:
    hard_stop_usd (on bot) is treated like fixed_stop_loss_amount in backtest.
    Per-share stop = hard_stop_usd / qty, checked against current bar high/low.
- EOD close behavior unchanged.

MACD reversal definition used:
- Buy_Signal: MACD crosses ABOVE Signal (bullish crossover) on the previous completed bar.
- Sell_Signal: MACD crosses BELOW Signal (bearish crossover) on the previous completed bar.

ToS Matching:
- MACD is computed using SMA-seeded EMA (Thinkorswim-style) on CLOSE:
    fast=12, slow=26, signal=9, average type=EXPONENTIAL.

Compatibility:
- Accepts anchor_dt=... from the shared dispatcher to avoid:
    TypeError: got an unexpected keyword argument 'anchor_dt'

Logging:
- Writes per-bot log file under:
    DATA_ROOT/<user_id>/bot_<botid>_<symbol>_<algo_name>.log
- Futures symbols like /ESH26 are sanitized to _ESH26 in filenames.
"""

import os
import logging
from datetime import datetime

import numpy as np
import pandas as pd
import pandas_ta as ta
from dotenv import load_dotenv
from sqlalchemy.orm import Session

from app.database.connection import SessionLocal
from app.models.paper_trading_bot import (
    PaperStockTradeBot,
    PaperStockBotOpenTrade,
    PaperStockBotTradeHistory,  # noqa: F401 (kept for parity/import compatibility)
)
from app.services.paper_trade_service import open_position, close_position
from app.services.trade_service import place_paper_and_maybe_live_order

# Reuse the same wiring as AlgoMM for data & bar guards
from app.scripts.stock_algos.base_wiring import (
    StockBaseRunner,
    _ET,
)
from app.scripts.stocks.bots.algo5_logic import compute_macd

# --- Config ---
load_dotenv()
DATA_ROOT = os.getenv("DATA_DIR", "/var/www/stockwicks/data")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s (ALGO2) %(message)s")
logger = logging.getLogger("ALGO2")


# -----------------------------------------------------------------------------
# MACD Calculation + REALISTIC Signal Generation
# -----------------------------------------------------------------------------
def algo2_signals_live(df: pd.DataFrame) -> pd.DataFrame:
    """
    MACD reversal signal generation for Algo2 LIVE trading.

    REALISTIC (no lookahead):
    - We will ACT on signals from the PREVIOUS completed bar.
    - Buy_Signal / Sell_Signal columns are computed on each row, but the bot
      uses the previous row's signal to decide what to do at the current open.
    """
    df = df.copy()

    # Diagnostics (kept)
    df["ATR14"] = ta.atr(df["high"], df["low"], df["close"], length=14)

    # MACD (ToS-matching)
    df = compute_macd(df, fast=12, slow=26, signal=9)

    # Cross detection:
    # Bullish crossover at bar i when:
    #   macd[i] > signal[i] AND macd[i-1] <= signal[i-1]
    # Bearish crossover at bar i when:
    #   macd[i] < signal[i] AND macd[i-1] >= signal[i-1]
    df["macd_prev"] = df["macd"].shift(1)
    df["signal_prev"] = df["macd_signal"].shift(1)

    df["Buy_Signal"] = (df["macd"] > df["macd_signal"]) & (df["macd_prev"] <= df["signal_prev"])
    df["Sell_Signal"] = (df["macd"] < df["macd_signal"]) & (df["macd_prev"] >= df["signal_prev"])

    return df


# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------
def _getattr_safe(obj, name, default=None):
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _fmt_num(x):
    return f"{x:.4f}" if isinstance(x, (int, float, np.floating)) else str(x)


# -----------------------------------------------------------------------------
# Logging (same structure; filename sanitized for futures symbols)
# -----------------------------------------------------------------------------
def log_bot_decision(bot, data_len, last_bar, decision, now_et=None, latest_bar_time=None):
    """Append a detailed log of the bot's decision-making context."""
    try:
        user_log_dir = os.path.join(DATA_ROOT, str(bot.user_id))
        os.makedirs(user_log_dir, exist_ok=True)

        # ✅ sanitize futures symbols like /ESH26 -> _ESH26 so filename is valid
        safe_symbol = (bot.symbol or "").replace("/", "_")
        safe_algo = str(bot.algo_name or "Algo2").replace("/", "_")
        log_file_name = f"bot_{bot.id}_{safe_symbol}_{safe_algo}.log"
        log_file_path = os.path.join(user_log_dir, log_file_name)

        if now_et is None:
            now_et = datetime.now(_ET)

        timestamp_str = now_et.strftime("%Y-%m-%d %H:%M:%S %Z")
        log_message = f"[{timestamp_str}] ACTION: {decision}\n"
        log_message += f"\tData Fetched: {data_len} candles\n"

        if latest_bar_time is not None:
            if latest_bar_time.tzinfo is None:
                latest_bar_time = _ET.localize(latest_bar_time)
            log_message += f"\tLatest Bar Time: {latest_bar_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            age = now_et - latest_bar_time
            log_message += f"\tBar Age: {age}\n"

        close_val = last_bar.get("close", "N/A")
        macd_val = last_bar.get("macd", "N/A")
        sig_val = last_bar.get("macd_signal", "N/A")
        hist_val = last_bar.get("macd_hist", "N/A")
        atr_val = last_bar.get("ATR14", "N/A")

        log_message += (
            f"\tMACD State: Close={_fmt_num(close_val)}, "
            f"MACD={_fmt_num(macd_val)}, "
            f"Signal={_fmt_num(sig_val)}, "
            f"Hist={_fmt_num(hist_val)}, "
            f"ATR14={_fmt_num(atr_val)}\n"
        )
        log_message += (
            f"\tSignals: Buy={last_bar.get('Buy_Signal', 'N/A')}, "
            f"Sell={last_bar.get('Sell_Signal', 'N/A')}\n"
        )
        log_message += "-" * 40 + "\n"

        with open(log_file_path, "a") as f:
            f.write(log_message)
    except Exception as e:
        logging.error(f"CRITICAL: Failed to write bot log file: {e}")


# -----------------------------------------------------------------------------
# Main Bot Tick - ALGO2 MACD REVERSAL (REALISTIC semantics)
# -----------------------------------------------------------------------------
def run_algo2_bot_tick(bot_id: int, anchor_dt=None, **kwargs):
    db: Session = SessionLocal()
    runner = StockBaseRunner()

    decision_made = "NONE"
    last_bar = {}
    data_len = 0
    bot = None
    now_et = None
    latest_bar_time = None

    try:
        bot = db.query(PaperStockTradeBot).filter(PaperStockTradeBot.id == bot_id).first()
        if not bot:
            logging.error(f"[ALGO2] Bot ID {bot_id} not found.")
            return

        # ✅ anchor_dt compatibility
        now_et = anchor_dt if anchor_dt is not None else datetime.now(_ET)
        if now_et.tzinfo is None:
            now_et = _ET.localize(now_et)

        # ------------------------------------------------------------------
        # 1) Fetch + resample bars using the same machinery as AlgoMM_runner
        # ------------------------------------------------------------------
        df_raw = runner.fetch_source_bars(bot.symbol)
        if df_raw is None or df_raw.empty:
            decision_made = "NO_DATA"
            logging.warning(f"[ALGO2] No raw data for {bot.symbol}")
            return

        latest_time, _, resampled_df = runner.resample_interval(
            df_raw,
            (bot.interval or "1min"),
            bot.symbol,
        )
        if latest_time is None or resampled_df is None or resampled_df.empty:
            decision_made = "RESAMPLE_FAILED"
            logging.warning(f"[ALGO2] Resample failed for {bot.symbol}@{bot.interval}")
            return

        data_len = len(resampled_df)

        # ------------------------------------------------------------------
        # 2) Generate signals (realistic MACD reversal)
        # ------------------------------------------------------------------
        signals_df = algo2_signals_live(resampled_df)
        if signals_df is None or signals_df.empty or "Buy_Signal" not in signals_df.columns:
            decision_made = "SIGNAL_GEN_FAILED"
            logging.warning(f"[ALGO2] Signal generation failed for {bot.symbol}")
            return

        latest_bar_time = latest_time if getattr(latest_time, "tzinfo", None) else _ET.localize(latest_time)

        # Populate last_bar for logging (latest bar)
        last_row = signals_df.iloc[-1]
        last_bar = {
            "close": last_row.get("close", "N/A"),
            "macd": last_row.get("macd", "N/A"),
            "macd_signal": last_row.get("macd_signal", "N/A"),
            "macd_hist": last_row.get("macd_hist", "N/A"),
            "ATR14": last_row.get("ATR14", "N/A"),
            "Buy_Signal": last_row.get("Buy_Signal", "N/A"),
            "Sell_Signal": last_row.get("Sell_Signal", "N/A"),
        }

        # ------------------------------------------------------------------
        # 3) Stale bar guard (same semantics as AlgoMM_runner)
        # ------------------------------------------------------------------
        age = now_et - latest_bar_time
        max_age = runner.max_age_for_interval(bot.interval)
        if age > max_age:
            logging.info(
                f"[ALGO2] Stale bar for {bot.symbol}@{bot.interval}: "
                f"bar_time={latest_bar_time}, now={now_et}, age={age}, max_age={max_age}"
            )
            decision_made = "STALE_BAR"
            return

        # ------------------------------------------------------------------
        # 4) Once-per-bar execution guard (shared with AlgoMM_runner)
        # ------------------------------------------------------------------
        if runner.once_per_bar_guard(bot.id, latest_bar_time):
            decision_made = "ALREADY_PROCESSED_BAR"
            return

        # Need at least 2 bars for realistic (prev + current)
        if len(signals_df) < 2:
            decision_made = "INSUFFICIENT_DATA_FOR_SIGNALS"
            return

        # Align with realistic semantics:
        # act on prev bar signals; execute on current bar (open).
        prev_bar = signals_df.iloc[-2]
        curr_bar = signals_df.iloc[-1]

        # For logging, show prev_bar context (more meaningful)
        last_bar = {
            "close": prev_bar.get("close", "N/A"),
            "macd": prev_bar.get("macd", "N/A"),
            "macd_signal": prev_bar.get("macd_signal", "N/A"),
            "macd_hist": prev_bar.get("macd_hist", "N/A"),
            "ATR14": prev_bar.get("ATR14", "N/A"),
            "Buy_Signal": prev_bar.get("Buy_Signal", "N/A"),
            "Sell_Signal": prev_bar.get("Sell_Signal", "N/A"),
        }

        prev_buy_signal = bool(prev_bar["Buy_Signal"])
        prev_sell_signal = bool(prev_bar["Sell_Signal"])

        current_open = float(curr_bar["open"])
        current_high = float(curr_bar["high"])
        current_low = float(curr_bar["low"])
        current_close = float(curr_bar["close"])

        # Open position record (if any)
        open_trade = db.query(PaperStockBotOpenTrade).filter_by(bot_id=bot.id).first()

        # ------------------------------------------------------------------
        # 5) Fixed $ Stop-Loss (kept)
        # ------------------------------------------------------------------
        if open_trade:
            qty = float(_getattr_safe(open_trade, "quantity", 0) or 0)
            fixed_usd = float(_getattr_safe(bot, "hard_stop_usd", 0) or 0)

            if qty > 0 and fixed_usd > 0:
                stop_per_share = fixed_usd / qty
                entry_price = float(_getattr_safe(open_trade, "entry_price", current_open))
                side = (_getattr_safe(open_trade, "position_side", "") or "").lower()

                if side == "long":
                    stop_price = entry_price - stop_per_share
                    if current_low <= stop_price:
                        exit_price = max(stop_price, current_low)
                        close_position(db, open_trade, exit_price)
                        decision_made = "CLOSE_LONG_STOP"
                        db.commit()
                        return

                elif side == "short":
                    stop_price = entry_price + stop_per_share
                    if current_high >= stop_price:
                        exit_price = min(stop_price, current_high)
                        close_position(db, open_trade, exit_price)
                        decision_made = "CLOSE_SHORT_STOP"
                        db.commit()
                        return

        # ------------------------------------------------------------------
        # 6) EOD Close (kept)
        # ------------------------------------------------------------------
        eod_close_enabled = bool(_getattr_safe(bot, "eod_close", True))
        if (
            eod_close_enabled
            and bot.interval in {"1min", "5min", "10min", "15min", "30min"}
            and now_et.hour >= 15
            and (now_et.hour > 15 or now_et.minute >= 58)
        ):
            if open_trade:
                try:
                    side = "SELL" if (_getattr_safe(open_trade, "position_side", "") or "").lower() == "long" else "BUY"
                    qty = float(_getattr_safe(open_trade, "quantity", 0) or 0)
                    if qty > 0:
                        place_paper_and_maybe_live_order(
                            db=db,
                            bot=bot,
                            side=side,
                            order_type="MARKET",
                            qty=qty,
                            limit_price=None,
                            time_in_force="DAY",
                            extended_hours=False,
                            mirror_live_override=None,
                            symbol_override=bot.symbol,
                            actor=f"EOD:{bot.id}",
                        )
                        decision_made = "END_OF_DAY_CLOSE"
                        db.commit()
                        return
                except Exception as mirror_err:
                    logging.warning(
                        f"[ALGO2] EOD mirror failed, falling back to paper close: {mirror_err}",
                        exc_info=True,
                    )
                    close_position(db, open_trade, current_close)
                    decision_made = "END_OF_DAY_CLOSE_FALLBACK"
                    db.commit()
                    return

            decision_made = "EOD_NO_POSITION"
            return

        # ------------------------------------------------------------------
        # 7) Signal-Based Exits (MACD reversal)
        # ------------------------------------------------------------------
        if open_trade:
            side = (_getattr_safe(open_trade, "position_side", "") or "").lower()

            if side == "long" and prev_sell_signal:
                close_position(db, open_trade, current_open)
                decision_made = "CLOSE_LONG_SIGNAL"
                db.commit()
                return

            if side == "short" and prev_buy_signal:
                close_position(db, open_trade, current_open)
                decision_made = "CLOSE_SHORT_SIGNAL"
                db.commit()
                return

            decision_made = "HOLD_POSITION"
            db.commit()
            return

        # ------------------------------------------------------------------
        # 8) Entries (MACD reversal)
        # ------------------------------------------------------------------
        if open_trade is None:
            if prev_buy_signal:
                open_position(db, bot, "long", current_open)
                decision_made = "OPEN_LONG"
                db.commit()
                return

            if prev_sell_signal and bool(_getattr_safe(bot, "allow_short_selling", False)):
                open_position(db, bot, "short", current_open)
                decision_made = "OPEN_SHORT"
                db.commit()
                return

            decision_made = "NO_SIGNAL"
            db.commit()
            return

    except Exception as e:
        db.rollback()
        logging.error(f"[ALGO2] tick error for bot {bot_id}: {e}", exc_info=True)
        decision_made = f"ERROR: {e}"

    finally:
        if bot:
            if not isinstance(last_bar, dict):
                if hasattr(last_bar, "to_dict"):
                    try:
                        last_bar = last_bar.to_dict()
                    except Exception:
                        last_bar = {}
                else:
                    last_bar = {}
            log_bot_decision(bot, data_len, last_bar, decision_made, now_et, latest_bar_time)
        if db:
            db.close()


# -----------------------------------------------------------------------------
# Main Execution
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python algo2_runner.py <bot_id>")
        sys.exit(1)

    try:
        bot_id = int(sys.argv[1])
        run_algo2_bot_tick(bot_id)
    except ValueError:
        print("Error: bot_id must be an integer")
        sys.exit(1)
    except Exception as e:
        logging.error(f"Fatal error running Algo2 bot: {e}")
        sys.exit(1)
