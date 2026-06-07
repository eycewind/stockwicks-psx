# /var/www/stockwicks/app/scripts/stock_algos/algo2_trades_paper_runner.py
from __future__ import annotations

import logging
import os
from datetime import datetime
import pytz
import numpy as np
import pandas as pd

from app.database.connection import SessionLocal
from app.models.paper_trading_bot import PaperStockTradeBot, PaperStockBotOpenTrade
from app.utils.stock.market_time import (
    max_bar_age_for_interval,
    is_market_open_now_or_premarket,
    is_regular_trading_hours,
)
from app.utils.stock.indicators import compute_atr
from app.services.trade_service import place_paper_and_maybe_live_order
from app.utils.stock import schwab_price_history as sph

# --- CONFIG ---
_ET = pytz.timezone("US/Eastern")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("algo2")

VALID_INTERVALS = {"1min", "5min", "15min", "30min", "1d"}

# Env toggles (mirroring)
_ENV_FORCE_MIRROR = os.getenv("MIRROR_TO_SCHWAB", "").strip().lower() in {"1", "true", "yes", "on"}
_ENV_ALLOW_NO_PAPER = os.getenv("ALLOW_MIRROR_WITHOUT_PAPER", "").strip().lower() in {"1", "true", "yes", "on"}

# Staleness policy (after-hours testing helpers)
ALLOW_AH_EVAL = os.getenv("ALGO2_ALLOW_AFTER_HOURS_EVAL", "1").strip().lower() in {"1","true","yes","on"}
SKIP_STALE_CHECK = os.getenv("ALGO2_SKIP_STALE_CHECK", "0").strip().lower() in {"1","true","yes","on"}

# ATR Trailing Stop params (defaults match your screenshot)
ATR_PERIOD = int(os.getenv("ALGO2_ATR_PERIOD", "21"))
ATR_MULT   = float(os.getenv("ALGO2_ATR_MULT", "3.0"))
LOOKBACK   = int(os.getenv("ALGO2_LOOKBACK", "3"))  # bars to look back for a cross

# In-memory guard to avoid duplicate submits on the same finished bar
_LAST_BAR_TS: dict[int, str] = {}  # bot_id -> last_bar_iso_utc


def _is_new_bar(bot_id: int, latest_bar_dt) -> bool:
    """True only once per bar per process."""
    try:
        if getattr(latest_bar_dt, "tzinfo", None) is not None:
            latest_bar_dt = latest_bar_dt.astimezone(pytz.UTC).replace(tzinfo=None)
        iso = latest_bar_dt.isoformat(timespec="seconds")
    except Exception:
        return True
    if _LAST_BAR_TS.get(bot_id) == iso:
        return False
    _LAST_BAR_TS[bot_id] = iso
    return True


# ------------------------
# ATR Trailing Stops (Supertrend-like)
# ------------------------
def compute_atr_trailing_stops(
    df: pd.DataFrame,
    period: int = ATR_PERIOD,
    mult: float = ATR_MULT,
    use_highlow: bool = False,
) -> tuple[pd.Series, pd.Series]:
    """
    Returns (buy_stop, sell_stop) series:
      - buy_stop (green) trails below price in uptrends
      - sell_stop (red) trails above price in downtrends

    Implementation:
      basic_upper = base + mult * ATR
      basic_lower = base - mult * ATR
      in uptrend:  lower = max(basic_lower, prev_lower), switch to downtrend if close < lower
      in downtrend: upper = min(basic_upper, prev_upper), switch to uptrend if close > upper

    If use_highlow=True, 'base' = (high+low)/2, else 'base' = close.
    """
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low  = df["low"].astype(float)
    atr  = compute_atr(df, period=period).astype(float)

    if use_highlow:
        base = (high + low) / 2.0
    else:
        base = close

    basic_upper = base + mult * atr
    basic_lower = base - mult * atr

    upper = pd.Series(index=df.index, dtype=float)  # trailing sell stop (red)
    lower = pd.Series(index=df.index, dtype=float)  # trailing buy stop (green)
    trend = pd.Series(index=df.index, dtype=int)    # +1 uptrend, -1 downtrend

    # initialize with no trend; decide on first full bar we can
    prev_upper = np.nan
    prev_lower = np.nan
    prev_trend = 0

    for i, ts in enumerate(df.index):
        bu = float(basic_upper.iloc[i])
        bl = float(basic_lower.iloc[i])
        px = float(close.iloc[i])

        if i == 0:
            # start neutral; set initial bounds
            upper.iloc[i] = bu
            lower.iloc[i] = bl
            trend.iloc[i] = 0
            prev_upper, prev_lower, prev_trend = bu, bl, 0
            continue

        # carry forward previous trailing stops
        u = bu
        l = bl
        t = prev_trend

        if prev_trend >= 0:  # was uptrend or neutral -> maintain/begin uptrend
            # trail the lower stop upward
            l = max(bl, prev_lower if not np.isnan(prev_lower) else -np.inf)
            # condition to flip to downtrend
            if px < l:
                t = -1
                u = bu  # start downtrend with current upper
                l = np.nan
            else:
                t = +1
                u = np.nan
        else:  # was downtrend
            # trail the upper stop downward
            u = min(bu, prev_upper if not np.isnan(prev_upper) else np.inf)
            # condition to flip to uptrend
            if px > u:
                t = +1
                l = bl  # start uptrend with current lower
                u = np.nan
            else:
                t = -1
                l = np.nan

        upper.iloc[i] = u
        lower.iloc[i] = l
        trend.iloc[i] = t

        prev_upper, prev_lower, prev_trend = u, l, t

    buy_stop  = lower  # green dots below price in uptrend
    sell_stop = upper  # red dots above price in downtrend
    return buy_stop, sell_stop


# ------------------------
# Cross helpers (with lookback)
# ------------------------
def _crossed_above(price: pd.Series, level: pd.Series, lookback: int) -> tuple[bool, int | None]:
    """price crosses above level within last 'lookback' bars"""
    if level is None or price is None or len(price) < 2:
        return False, None
    lb = min(lookback, len(price) - 1)
    for k in range(1, lb + 1):
        p_prev = float(price.iloc[-(k + 1)])
        p_curr = float(price.iloc[-k])
        l_prev = float(level.iloc[-(k + 1)]) if not np.isnan(level.iloc[-(k + 1)]) else np.nan
        l_curr = float(level.iloc[-k]) if not np.isnan(level.iloc[-k]) else np.nan
        if np.isnan(l_prev) or np.isnan(l_curr):
            continue
        if p_prev <= l_prev and p_curr > l_curr:
            return True, k
    return False, None

def _crossed_below(price: pd.Series, level: pd.Series, lookback: int) -> tuple[bool, int | None]:
    """price crosses below level within last 'lookback' bars"""
    if level is None or price is None or len(price) < 2:
        return False, None
    lb = min(lookback, len(price) - 1)
    for k in range(1, lb + 1):
        p_prev = float(price.iloc[-(k + 1)])
        p_curr = float(price.iloc[-k])
        l_prev = float(level.iloc[-(k + 1)]) if not np.isnan(level.iloc[-(k + 1)]) else np.nan
        l_curr = float(level.iloc[-k]) if not np.isnan(level.iloc[-k]) else np.nan
        if np.isnan(l_prev) or np.isnan(l_curr):
            continue
        if p_prev >= l_prev and p_curr < l_curr:
            return True, k
    return False, None


def run_algo2_bot_tick(bot_id: int):
    """ATR Trailing Stop strategy (entries/exits by price crossing stops; 3-bar lookback)."""
    db = SessionLocal()
    try:
        bot = db.query(PaperStockTradeBot).filter_by(id=bot_id).first()
        if not bot:
            log.info(f"[ALGO2] Bot {bot_id} not found; returning.")
            return
        if not bot.is_active:
            log.info(f"[ALGO2] Bot {bot_id} inactive; returning.")
            return

        symbol = (bot.symbol or "").upper().strip()
        user_id = bot.user_id
        trade_size = float(bot.trade_size or 0.0)
        interval = (bot.interval or "1min").strip().lower()
        if interval not in VALID_INTERVALS:
            log.warning(f"[ALGO2] Unsupported interval '{interval}' for bot {bot_id}; supported: {sorted(VALID_INTERVALS)}")
            return

        now_et = datetime.now(_ET)
        log.info(f"[ALGO2-TASK] ▶ bot_id={bot_id} user={user_id} {symbol}@{interval} size={trade_size} mirror_live={getattr(bot,'mirror_live',None)}")
        if trade_size <= 0:
            log.info(f"[ALGO2] Invalid trade_size={trade_size}; returning.")
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
        df = fetch_fn(symbol) if fetch_fn else None
        if df is None or df.empty:
            log.warning(f"[ALGO2] No Schwab data for {symbol}@{interval}")
            bot.updated_at, bot.status = datetime.utcnow(), "IDLE"
            db.commit()
            return

        latest_time = df.index[-1]
        resampled = df
        log.info(f"[ALGO2] Bars fetched len={len(resampled)} latest_time={latest_time}")

        # --- Bar staleness & duplicate guard ---
        age = now_et - (latest_time.tz_convert(_ET) if getattr(latest_time, "tzinfo", None) else _ET.localize(latest_time))
        max_age = max_bar_age_for_interval(interval)
        market_live = bool(is_market_open_now_or_premarket())
        if not SKIP_STALE_CHECK:
            if market_live:
                if age > max_age:
                    log.info(f"[ALGO2] [STALE] {symbol}@{interval} bar too old (live); skip.")
                    return
            else:
                if not ALLOW_AH_EVAL and age > max_age:
                    log.info(f"[ALGO2] [STALE] market closed and AH eval disabled; skip.")
                    return

        if not _is_new_bar(bot_id, latest_time):
            log.info(f"[ALGO2] Already processed latest bar for bot {bot_id}; skipping duplicate.")
            return

        # --- Compute ATR Trailing Stops ---
        buy_stop, sell_stop = compute_atr_trailing_stops(resampled, period=ATR_PERIOD, mult=ATR_MULT, use_highlow=False)

        close = resampled["close"]
        last_px = float(close.iloc[-1])

        # Signals (3-bar lookback by default)
        long_entry,  le_k = _crossed_above(close, sell_stop, LOOKBACK)   # cross above red stop → trend flip up
        short_entry, se_k = _crossed_below(close, buy_stop, LOOKBACK)    # cross below green stop → trend flip down
        long_exit,   lx_k = _crossed_below(close, buy_stop, LOOKBACK)    # drop below green stop exits long
        short_exit,  sx_k = _crossed_above(close, sell_stop, LOOKBACK)   # rise above red stop exits short

        log.info(
            f"[ALGO2] ATR p={ATR_PERIOD} x{ATR_MULT} lb={LOOKBACK} | "
            f"px={last_px:.4f} | LE={long_entry}({le_k}) SE={short_entry}({se_k}) "
            f"LX={long_exit}({lx_k}) SX={short_exit}({sx_k})"
        )

        # --- Open trade state ---
        open_trade = (
            db.query(PaperStockBotOpenTrade)
            .filter_by(bot_id=bot.id, user_id=user_id, symbol=symbol)
            .first()
        )

        # --- Mirroring behavior ---
        mirror_live_override = True if (_ENV_FORCE_MIRROR or getattr(bot, "mirror_live", False)) else None
        mirror_even_if_no_paper = _ENV_ALLOW_NO_PAPER
        extended = not is_regular_trading_hours()

        # --- Trading Logic ---
        result = None
        if not open_trade:
            if long_entry:
                log.info(f"[ALGO2] 🔔 Entry LONG (crossed above sell stop {se_k}–{le_k} bar(s) ago) @ {last_px} qty={trade_size}")
                result = place_paper_and_maybe_live_order(
                    db=db, bot=bot, side="BUY", qty=trade_size,
                    order_type="MARKET",
                    extended_hours=extended,
                    mirror_live_override=mirror_live_override,
                    mirror_even_if_no_paper=mirror_even_if_no_paper,
                    actor=f"algo2@{interval}",
                )
            elif short_entry:
                log.info(f"[ALGO2] 🔔 Entry SHORT (crossed below buy stop {se_k} bar(s) ago) @ {last_px} qty={trade_size}")
                result = place_paper_and_maybe_live_order(
                    db=db, bot=bot, side="SELL", qty=trade_size,
                    order_type="MARKET",
                    extended_hours=extended,
                    mirror_live_override=mirror_live_override,
                    mirror_even_if_no_paper=mirror_even_if_no_paper,
                    actor=f"algo2@{interval}",
                )
            else:
                log.info("[ALGO2] No entry signal within lookback window.")
        else:
            if open_trade.position_side == "long" and long_exit:
                log.info(f"[ALGO2] 🔔 Exit LONG (crossed below buy stop {lx_k} bar(s) ago) @ {last_px}")
                result = place_paper_and_maybe_live_order(
                    db=db, bot=bot, side="SELL", qty=float(open_trade.quantity),
                    order_type="MARKET",
                    extended_hours=extended,
                    mirror_live_override=mirror_live_override,
                    mirror_even_if_no_paper=mirror_even_if_no_paper,
                    actor=f"algo2@{interval}",
                )
            elif open_trade.position_side == "short" and short_exit:
                log.info(f"[ALGO2] 🔔 Cover SHORT (crossed above sell stop {sx_k} bar(s) ago) @ {last_px}")
                result = place_paper_and_maybe_live_order(
                    db=db, bot=bot, side="BUY", qty=float(open_trade.quantity),
                    order_type="MARKET",
                    extended_hours=extended,
                    mirror_live_override=mirror_live_override,
                    mirror_even_if_no_paper=mirror_even_if_no_paper,
                    actor=f"algo2@{interval}",
                )
            else:
                log.info("[ALGO2] No exit signal within lookback window.")

        if result:
            log.info(f"[ALGO2] ✅ Trade executed result: {result}")

        bot.updated_at, bot.status = datetime.utcnow(), "RUNNING"
        db.commit()

    except Exception as e:
        db.rollback()
        log.error(f"[ALGO2] ❌ Error: {e}", exc_info=True)
    finally:
        db.close()
