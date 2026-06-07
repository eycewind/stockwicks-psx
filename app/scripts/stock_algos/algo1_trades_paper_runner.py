# /var/www/stockwicks/app/scripts/stock_algos/algo1_trades_paper_runner.py
import logging
import os
from datetime import datetime
import pytz

from app.database.connection import SessionLocal
from app.models.paper_trading_bot import PaperStockTradeBot, PaperStockBotOpenTrade
from app.utils.stock.market_time import max_bar_age_for_interval
from app.services.trade_service import place_paper_and_maybe_live_order
from app.utils.stock import schwab_price_history as sph

# --- CONFIG ---
_ET = pytz.timezone("US/Eastern")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("algo1")

# Supported intervals
VALID_INTERVALS = {"1min", "5min", "15min", "30min", "1d"}

# Env toggles
_ENV_FORCE_MIRROR = os.getenv("MIRROR_TO_SCHWAB", "").strip().lower() in {"1", "true", "yes", "on"}
_ENV_ALLOW_NO_PAPER = os.getenv("ALLOW_MIRROR_WITHOUT_PAPER", "").strip().lower() in {"1", "true", "yes", "on"}

# In-memory guard so we don’t mirror multiple times on the same bar
_LAST_BAR_TS = {}  # bot_id -> last_bar_timestamp (UTC ISO string)


def _is_new_bar(bot_id: int, latest_bar_dt) -> bool:
    """Returns True the first time we see a given bar, False on repeats."""
    try:
        if getattr(latest_bar_dt, "tzinfo", None) is not None:
            latest_bar_dt = latest_bar_dt.astimezone(pytz.UTC).replace(tzinfo=None)
        iso = latest_bar_dt.isoformat(timespec="seconds")
    except Exception:
        return True

    last_seen = _LAST_BAR_TS.get(bot_id)
    if last_seen == iso:
        return False
    _LAST_BAR_TS[bot_id] = iso
    return True


def run_algo1_bot_tick(bot_id: int, schedule_exit_trade_func=None):
    """
    Run one tick of Algo1 (trend-following + MA cross fallback).
    Supports intervals: 1min, 5min, 15min, 30min, 1d.
    """
    db = SessionLocal()
    try:
        bot = db.query(PaperStockTradeBot).filter_by(id=bot_id).first()
        if not bot:
            log.info(f"[ALGO1] Bot {bot_id} not found; returning.")
            return
        if not bot.is_active:
            log.info(f"[ALGO1] Bot {bot_id} inactive; returning.")
            return

        symbol = (bot.symbol or "").upper().strip()
        user_id = bot.user_id
        trade_size = float(bot.trade_size or 0.0)
        interval = (bot.interval or "1min").strip().lower()

        if interval not in VALID_INTERVALS:
            log.warning(f"[ALGO1] Unsupported interval '{interval}' for bot {bot_id}; supported: {sorted(VALID_INTERVALS)}")
            return

        now_et = datetime.now(_ET)

        log.info(
            f"[ALGO1-TASK] ▶ bot_id={bot_id} user={user_id} "
            f"{symbol}@{interval} size={trade_size} mirror_live={getattr(bot,'mirror_live',None)}"
        )
        if trade_size <= 0:
            log.info(f"[ALGO1] Bot {bot_id} has invalid trade_size={trade_size}; returning.")
            return

        # --- Fetch Schwab interval data ---
        fetch_map = {
            "1min": sph.get_schwab_1min,
            "5min": sph.get_schwab_5min,
            "15min": sph.get_schwab_15min,
            "30min": sph.get_schwab_30min,
            "1d": sph.get_schwab_daily,
        }
        fetch_fn = fetch_map.get(interval)
        if not fetch_fn:
            log.warning(f"[ALGO1] No fetch function for {interval}")
            return

        df = fetch_fn(symbol)
        if df is None or df.empty:
            log.warning(f"[ALGO1] No Schwab data for {symbol}@{interval}")
            bot.updated_at, bot.status = datetime.utcnow(), "IDLE"
            db.commit()
            return

        latest_time = df.index[-1]
        resampled = df
        log.info(f"[ALGO1] Bars fetched len={len(resampled)} latest_time={latest_time}")

        # --- Bar freshness & guard ---
        age = now_et - (
            latest_time.tz_convert(_ET) if getattr(latest_time, "tzinfo", None) else _ET.localize(latest_time)
        )
        max_age = max_bar_age_for_interval(interval)
        log.info(f"[ALGO1] Bar age={age}, max_age={max_age}")
        if age > max_age:
            log.info(f"[ALGO1] [STALE] {symbol}@{interval} bar too old; skip.")
            return

        if not _is_new_bar(bot_id, latest_time):
            log.info(f"[ALGO1] Already processed latest bar for bot {bot_id}; skipping duplicate.")
            return

        # --- Signals (trend + MA cross fallback) ---
        closes = resampled["close"]
        if len(closes) < 3:
            log.info(f"[ALGO1] Not enough bars (len={len(closes)})")
            return

        last_3 = closes.tail(3).values
        buy_signal = last_3[0] < last_3[1] < last_3[2]
        sell_signal = last_3[0] > last_3[1] > last_3[2]

        # Fallback: MA cross
        if not buy_signal and not sell_signal and len(closes) >= 20:
            ma5, ma20 = closes.rolling(5).mean(), closes.rolling(20).mean()
            if ma5.iloc[-2] <= ma20.iloc[-2] and ma5.iloc[-1] > ma20.iloc[-1]:
                buy_signal = True
            elif ma5.iloc[-2] >= ma20.iloc[-2] and ma5.iloc[-1] < ma20.iloc[-1]:
                sell_signal = True

        log.info(f"[ALGO1] Signal check: closes={last_3}, buy_signal={buy_signal}, sell_signal={sell_signal}")

        # --- Open trade state ---
        open_trade = (
            db.query(PaperStockBotOpenTrade)
            .filter_by(bot_id=bot.id, user_id=user_id, symbol=symbol)
            .first()
        )
        if open_trade:
            log.info(
                f"[ALGO1] Open trade: side={open_trade.position_side} "
                f"qty={open_trade.quantity} entry={open_trade.entry_price} "
                f"at={open_trade.entry_time}"
            )
        else:
            log.info("[ALGO1] Open trade: NONE")

        last_px = float(closes.iloc[-1])

        # --- Mirroring behavior ---
        mirror_live_override = True if (_ENV_FORCE_MIRROR or getattr(bot, "mirror_live", False)) else None
        mirror_even_if_no_paper = _ENV_ALLOW_NO_PAPER

        # --- Trading Decisions ---
        result = None
        if buy_signal and not open_trade:
            log.info(f"[ALGO1] 🔔 Entry Long @ {last_px} (qty={trade_size})")
            result = place_paper_and_maybe_live_order(
                db=db, bot=bot, side="BUY", qty=trade_size,
                order_type="MARKET",
                mirror_live_override=mirror_live_override,
                mirror_even_if_no_paper=mirror_even_if_no_paper,
                actor=f"algo1@{interval}",
            )

        elif sell_signal and not open_trade:
            log.info(f"[ALGO1] 🔔 Entry Short @ {last_px} (qty={trade_size})")
            result = place_paper_and_maybe_live_order(
                db=db, bot=bot, side="SELL", qty=trade_size,
                order_type="MARKET",
                mirror_live_override=mirror_live_override,
                mirror_even_if_no_paper=mirror_even_if_no_paper,
                actor=f"algo1@{interval}",
            )

        elif buy_signal and open_trade and open_trade.position_side == "short":
            log.info(f"[ALGO1] 🔔 Exit Short → Enter Long @ {last_px} (qty={open_trade.quantity})")
            result = place_paper_and_maybe_live_order(
                db=db, bot=bot, side="BUY", qty=float(open_trade.quantity),
                order_type="MARKET",
                mirror_live_override=mirror_live_override,
                mirror_even_if_no_paper=mirror_even_if_no_paper,
                actor=f"algo1@{interval}",
            )

        elif sell_signal and open_trade and open_trade.position_side == "long":
            log.info(f"[ALGO1] 🔔 Exit Long → Enter Short @ {last_px} (qty={open_trade.quantity})")
            result = place_paper_and_maybe_live_order(
                db=db, bot=bot, side="SELL", qty=float(open_trade.quantity),
                order_type="MARKET",
                mirror_live_override=mirror_live_override,
                mirror_even_if_no_paper=mirror_even_if_no_paper,
                actor=f"algo1@{interval}",
            )
        else:
            log.info("[ALGO1] No trade triggered this tick.")

        if result:
            log.info(f"[ALGO1] ✅ Trade executed result: {result}")

        bot.updated_at, bot.status = datetime.utcnow(), "RUNNING"
        db.commit()

        # Optional custom exit scheduler hook
        if schedule_exit_trade_func:
            try:
                schedule_exit_trade_func(bot.id, symbol, open_trade)
            except Exception as e:
                log.error(f"[ALGO1] schedule_exit_trade_func error: {e}", exc_info=True)

    except Exception as e:
        db.rollback()
        log.error(f"[ALGO1] ❌ Error: {e}", exc_info=True)
    finally:
        db.close()
