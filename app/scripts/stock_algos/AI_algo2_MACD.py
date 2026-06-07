
# /var/www/stockwicks/app/scripts/stock_algos/algo3_trades_paper_runner.py
# /var/www/stockwicks/app/scripts/stock_algos/algo3_trades_paper_runner.py

import logging
import os
from datetime import datetime
import pytz

from app.database.connection import SessionLocal
from app.models.paper_trading_bot import PaperStockTradeBot, PaperStockBotOpenTrade
from app.utils.stock.market_time import max_bar_age_for_interval
from app.utils.stock.data_fetch import get_schwab_1min_history, process_interval
from app.utils.stock.indicators import compute_smi
from app.services.trade_service import execute_trade_signal

# --- CONFIG ---
_ET = pytz.timezone("US/Eastern")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("algo3")

VALID_INTERVALS = {"1min", "5min", "15min", "1d"}

# Env toggles (mirror behavior)
_ENV_FORCE_MIRROR = os.getenv("MIRROR_TO_SCHWAB", "").strip().lower() in {"1", "true", "yes", "on"}
_ENV_ALLOW_NO_PAPER = os.getenv("ALLOW_MIRROR_WITHOUT_PAPER", "").strip().lower() in {"1", "true", "yes", "on"}

# In-process guard to avoid duplicate submits on the same finished bar
_LAST_BAR_TS = {}  # bot_id -> last_bar_iso_utc


def _is_new_bar(bot_id: int, latest_bar_dt) -> bool:
    """Return True only once per bar per process (guards repeated runner invocations on same candle)."""
    try:
        if getattr(latest_bar_dt, "tzinfo", None) is not None:
            # normalize to naive UTC iso (seconds) for stable comparison
            latest_bar_dt = latest_bar_dt.astimezone(pytz.UTC).replace(tzinfo=None)
        iso = latest_bar_dt.isoformat(timespec="seconds")
    except Exception:
        # If anything odd with the timestamp, allow once.
        return True

    if _LAST_BAR_TS.get(bot_id) == iso:
        return False
    _LAST_BAR_TS[bot_id] = iso
    return True


def _place_order_via_service(
    *,
    bot_id: int,
    symbol: str,
    side: str,                 # "BUY" | "SELL"
    qty: float,
    order_type: str = "MARKET",
    limit_price: float | None = None,
    time_in_force: str = "DAY",
    extended_hours: bool = False,
    mirror_live_override: bool | None = None,  # None = follow bot flag/env; True forces mirror
    mirror_even_if_no_paper: bool = False,
    actor: str | None = None,
):
    """Unified gateway -> executes paper + optional live mirror to Schwab via trade_service."""
    return execute_trade_signal(
        bot_id=bot_id,
        side=side,
        order_type=order_type,
        qty=qty,
        limit_price=limit_price,
        time_in_force=time_in_force,
        extended_hours=extended_hours,
        mirror_live_override=mirror_live_override,          # None -> follow bot/env; True forces mirror
        symbol_override=symbol,
        actor=actor or f"algo3:{bot_id}",
        mirror_even_if_no_paper=bool(mirror_even_if_no_paper),
    )


def _cross_up(series, level: float, lookback: int = 3) -> tuple[bool, int | None]:
    """
    Did SMI cross up through 'level' within the last `lookback` bar-to-bar transitions?
    Returns (True, bars_ago) if found, else (False, None).
    bars_ago = 1 means the cross happened between the previous bar and the current bar.
    """
    if series is None or len(series) < 2:
        return False, None
    lb = min(lookback, len(series) - 1)
    # k = 1..lb : compare [-(k+1)] -> [-k]
    for k in range(1, lb + 1):
        prev_v = float(series.iloc[-(k + 1)])
        curr_v = float(series.iloc[-k])
        if prev_v < level and curr_v > level:
            return True, k
    return False, None


def _cross_down(series, level: float, lookback: int = 3) -> tuple[bool, int | None]:
    """Mirror of _cross_up: cross down through 'level' within last `lookback` transitions."""
    if series is None or len(series) < 2:
        return False, None
    lb = min(lookback, len(series) - 1)
    for k in range(1, lb + 1):
        prev_v = float(series.iloc[-(k + 1)])
        curr_v = float(series.iloc[-k])
        if prev_v > level and curr_v < level:
            return True, k
    return False, None


def run_algo3_bot_tick(bot_id: int):
    """Run one tick of Algo3 (SMI momentum strategy) for 1min/5min/15min/1d."""
    db = SessionLocal()
    try:
        bot = db.query(PaperStockTradeBot).filter_by(id=bot_id).first()
        if not bot:
            log.info(f"[ALGO3] Bot {bot_id} not found; returning.")
            return
        if not bot.is_active:
            log.info(f"[ALGO3] Bot {bot_id} inactive; returning.")
            return

        symbol = (bot.symbol or "").upper().strip()
        user_id = bot.user_id
        trade_size = float(bot.trade_size or 0.0)
        interval = (bot.interval or "1min").strip().lower()
        if interval not in VALID_INTERVALS:
            log.warning(f"[ALGO3] Unsupported interval '{interval}' for bot {bot_id}; supported: {sorted(VALID_INTERVALS)}")
            return

        now_et = datetime.now(_ET)
        log.info(
            f"[ALGO3-TASK] ▶ bot_id={bot_id} user={user_id} "
            f"{symbol}@{interval} size={trade_size} mirror_live={getattr(bot,'mirror_live',None)}"
        )
        if trade_size <= 0:
            log.info(f"[ALGO3] Bot {bot_id} has invalid trade_size={trade_size}; returning.")
            return

        # --- Fetch 1min history and resample to target interval ---
        df = get_schwab_1min_history(symbol)
        if df is None or df.empty:
            log.warning(f"[ALGO3] No data for {symbol}@{interval}")
            bot.updated_at, bot.status = datetime.utcnow(), "IDLE"
            db.commit()
            return
        log.info(
            f"[ALGO3] Raw 1min df len={len(df)} "
            f"first={df.index[0] if len(df) else 'NA'} last={df.index[-1] if len(df) else 'NA'}"
        )

        latest_time, _, resampled = process_interval(df, interval, symbol)
        if not latest_time or resampled is None or resampled.empty:
            log.info(f"[ALGO3] Resample failed/empty for {symbol}@{interval}")
            return

        # --- Bar age check + duplicate guard ---
        log.info(f"[ALGO3] Resampled len={len(resampled)} latest_time={latest_time}")
        age = now_et - (
            latest_time.tz_convert(_ET) if getattr(latest_time, "tzinfo", None) else _ET.localize(latest_time)
        )
        max_age = max_bar_age_for_interval(interval)
        log.info(f"[ALGO3] Bar age={age}, max_age={max_age}")
        if age > max_age:
            log.info(f"[ALGO3] [STALE] {symbol}@{interval} bar too old; skip.")
            return

        if not _is_new_bar(bot_id, latest_time):
            log.info(f"[ALGO3] Already processed latest bar for bot {bot_id}; skipping duplicate in same bar.")
            return

        # --- Compute SMI and form signals ---
        smi = compute_smi(resampled)
        if smi is None or len(smi) < 2:
            log.info("[ALGO3] Not enough SMI points to make a decision.")
            return

        last_px = float(resampled["close"].iloc[-1])
        prev_smi = float(smi.iloc[-2]) if len(smi) >= 2 else float("nan")
        curr_smi = float(smi.iloc[-1])
        log.info(f"[ALGO3] SMI check: prev={prev_smi:.2f}, curr={curr_smi:.2f}, last_px={last_px}")

        # --- Cross checks with 3-bar lookback ---
        long_entry, le_k = _cross_up(smi, -60.0, lookback=3)
        long_exit, lx_k = _cross_down(smi, +60.0, lookback=3)
        short_entry, se_k = _cross_down(smi, +60.0, lookback=3)
        short_exit, sx_k = _cross_up(smi, -60.0, lookback=3)

        # --- Open trade state ---
        open_trade = (
            db.query(PaperStockBotOpenTrade)
            .filter_by(bot_id=bot.id, user_id=user_id, symbol=symbol)
            .first()
        )
        if open_trade:
            log.info(
                f"[ALGO3] Open trade: side={open_trade.position_side} "
                f"qty={open_trade.quantity} entry={open_trade.entry_price} "
                f"at={open_trade.entry_time}"
            )
        else:
            log.info("[ALGO3] Open trade: NONE")

        # --- Mirroring policy ---
        # Force live mirror if either env MIRROR_TO_SCHWAB is on OR bot.mirror_live is True
        should_force_mirror = bool(_ENV_FORCE_MIRROR or getattr(bot, "mirror_live", False))
        mirror_live_override = True if should_force_mirror else None
        mirror_even_if_no_paper = _ENV_ALLOW_NO_PAPER

        # --- Trading Decisions + Execution ---
        result = None

        if not open_trade:
            # Entries
            if long_entry:
                log.info(f"[ALGO3] 🔔 Entry LONG (SMI crossed up -60, {le_k} bar(s) ago) @ {last_px} (qty={trade_size})")
                result = _place_order_via_service(
                    bot_id=bot.id,
                    symbol=symbol,
                    side="BUY",
                    qty=trade_size,
                    order_type="MARKET",
                    mirror_live_override=mirror_live_override,
                    mirror_even_if_no_paper=mirror_even_if_no_paper,
                    actor=f"algo3@{interval}",
                )
            elif short_entry:
                log.info(f"[ALGO3] 🔔 Entry SHORT (SMI crossed down +60, {se_k} bar(s) ago) @ {last_px} (qty={trade_size})")
                result = _place_order_via_service(
                    bot_id=bot.id,
                    symbol=symbol,
                    side="SELL",
                    qty=trade_size,
                    order_type="MARKET",
                    mirror_live_override=mirror_live_override,
                    mirror_even_if_no_paper=mirror_even_if_no_paper,
                    actor=f"algo3@{interval}",
                )
            else:
                log.info("[ALGO3] No entry signal within last 3 bars.")
        else:
            # Exits (mirror the entry thresholds)
            if open_trade.position_side == "long" and long_exit:
                log.info(f"[ALGO3] 🔔 Exit LONG (SMI crossed down +60, {lx_k} bar(s) ago) @ {last_px} (qty={open_trade.quantity})")
                result = _place_order_via_service(
                    bot_id=bot.id,
                    symbol=symbol,
                    side="SELL",
                    qty=float(open_trade.quantity),
                    order_type="MARKET",
                    mirror_live_override=mirror_live_override,
                    mirror_even_if_no_paper=mirror_even_if_no_paper,
                    actor=f"algo3@{interval}",
                )
            elif open_trade.position_side == "short" and short_exit:
                log.info(f"[ALGO3] 🔔 Cover SHORT (SMI crossed up -60, {sx_k} bar(s) ago) @ {last_px} (qty={open_trade.quantity})")
                result = _place_order_via_service(
                    bot_id=bot.id,
                    symbol=symbol,
                    side="BUY",
                    qty=float(open_trade.quantity),
                    order_type="MARKET",
                    mirror_live_override=mirror_live_override,
                    mirror_even_if_no_paper=mirror_even_if_no_paper,
                    actor=f"algo3@{interval}",
                )
            else:
                log.info("[ALGO3] No exit signal within last 3 bars.")

        if result:
            log.info(f"[ALGO3] ✅ Trade executed result: {result}")

        bot.updated_at, bot.status = datetime.utcnow(), "RUNNING"
        db.commit()

    except Exception as e:
        db.rollback()
        log.error(f"[ALGO3] ❌ Error: {e}", exc_info=True)
    finally:
        db.close()