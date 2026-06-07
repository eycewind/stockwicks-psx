
# app/scripts/stock_algos/algo3_trades_paper_runner.py
from __future__ import annotations

import logging
import os
from datetime import datetime
import pytz
import pandas as pd

from app.database.connection import SessionLocal
from app.models.paper_trading_bot import PaperStockTradeBot, PaperStockBotOpenTrade
from app.utils.stock.market_time import max_bar_age_for_interval
from app.services.trade_service import execute_trade_signal
from app.utils.stock import schwab_price_history as sph

# --- CONFIG ---
_ET = pytz.timezone("US/Eastern")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("algo3")

VALID_INTERVALS = {"1min", "5min", "15min", "30min", "1d"}

# Env toggles (mirror behavior)
_ENV_FORCE_MIRROR = os.getenv("MIRROR_TO_SCHWAB", "").strip().lower() in {"1", "true", "yes", "on"}
_ENV_ALLOW_NO_PAPER = os.getenv("ALLOW_MIRROR_WITHOUT_PAPER", "").strip().lower() in {"1", "true", "yes", "on"}

# In-process guard to avoid duplicate submits on the same finished bar
_LAST_BAR_TS: dict[int, str] = {}  # bot_id -> last_bar_iso_utc


def _is_new_bar(bot_id: int, latest_bar_dt) -> bool:
    """
    Return True only once per bar per process (guards repeated runner invocations on same candle).
    We treat the Schwab bar timestamp as the identity.
    """
    try:
        # normalize to naive UTC ISO (seconds)
        if getattr(latest_bar_dt, "tzinfo", None) is not None:
            latest_bar_dt = latest_bar_dt.astimezone(pytz.UTC).replace(tzinfo=None)
        iso = latest_bar_dt.isoformat(timespec="seconds")
    except Exception:
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
    mirror_live_override: bool | None = None,
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
        mirror_live_override=mirror_live_override,
        symbol_override=symbol,
        actor=actor or f"algo3:{bot_id}",
        mirror_even_if_no_paper=bool(mirror_even_if_no_paper),
    )


# ===== EXACT LOGIC FROM trades_working.py =====
def _compute_smi_like_trades_working(df: pd.DataFrame, period: int = 14, smooth_k: int = 3, smooth_d: int = 3):
    """
    Same SMI calculation used in trades_working.py:
      - rolling highs/lows over 'period'
      - 'smi_raw' = (close - midpoint) / (range/2) * 100
      - SMI  = rolling mean over smooth_k
      - SMI_Signal = rolling mean over smooth_d of SMI
    """
    df = df.copy()

    df["max_high"] = df["high"].rolling(window=period).max()
    df["min_low"] = df["low"].rolling(window=period).min()
    df["midpoint"] = (df["max_high"] + df["min_low"]) / 2
    df["diff"] = df["max_high"] - df["min_low"]

    # Avoid division by zero
    half_range = df["diff"] / 2
    half_range = half_range.replace(0, pd.NA).fillna(method="ffill")

    df["smi_raw"] = (df["close"] - df["midpoint"]) / half_range * 100
    df["SMI"] = df["smi_raw"].rolling(window=smooth_k).mean()
    df["SMI_Signal"] = df["SMI"].rolling(window=smooth_d).mean()
    return df["SMI"], df["SMI_Signal"]


def _determine_signals_like_trades_working(df: pd.DataFrame) -> pd.DataFrame:
    """
    EXACT Buy/Sell signal rules from trades_working.determine_signals:

    Buy_Signal  when:
        (SMI.shift(2) < -70) &
        (SMI.diff().shift(2) > 0) &
        (SMI.shift(1) > SMI.shift(2)) &
        (SMI > SMI.shift(1))

    Sell_Signal when:
        (SMI.shift(2) > 70) &
        (SMI.diff().shift(2) < 0) &
        (SMI.shift(1) < SMI.shift(2)) &
        (SMI < SMI.shift(1))
    """
    df = df.copy()
    df["SMI_Change"] = df["SMI"].diff()

    df["Buy_Signal"] = (
        (df["SMI"].shift(2) < -70)
        & (df["SMI_Change"].shift(2) > 0)
        & (df["SMI"].shift(1) > df["SMI"].shift(2))
        & (df["SMI"] > df["SMI"].shift(1))
    )

    df["Sell_Signal"] = (
        (df["SMI"].shift(2) > 70)
        & (df["SMI_Change"].shift(2) < 0)
        & (df["SMI"].shift(1) < df["SMI"].shift(2))
        & (df["SMI"] < df["SMI"].shift(1))
    )
    return df
# =============================================


def run_algo3_bot_tick(bot_id: int):
    """
    Run one tick of Algo3 where entry/exit is IDENTICAL to trades_working.py:

      - SMI computed exactly as in trades_working (period=14, smooth_k=3, smooth_d=3)
      - Buy_Signal / Sell_Signal rules exactly the same
      - Entry Long  when Buy_Signal
      - Exit  Long  when Sell_Signal
      - Entry Short when Sell_Signal
      - Cover Short when Buy_Signal

    Orders are submitted via execute_trade_signal; open trade state is kept in PaperStockBotOpenTrade.
    """
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
            f"{symbol}@{interval} size={trade_size} mirror_live={getattr(bot, 'mirror_live', None)}"
        )
        if trade_size <= 0:
            log.info(f"[ALGO3] Bot {bot_id} has invalid trade_size={trade_size}; returning.")
            return

        # --- Fetch Schwab interval data (already at exact target interval) ---
        fetch_map = {
            "1min": sph.get_schwab_1min,
            "5min":  sph.get_schwab_5min,
            "15min": sph.get_schwab_15min,
            "30min": sph.get_schwab_30min,
            "1d":    sph.get_schwab_daily,
        }
        fetch_fn = fetch_map.get(interval)
        if not fetch_fn:
            log.warning(f"[ALGO3] No fetch function for {interval}")
            return

        df = fetch_fn(symbol)
        if df is None or df.empty:
            log.warning(f"[ALGO3] No Schwab data for {symbol}@{interval}")
            bot.updated_at, bot.status = datetime.utcnow(), "IDLE"
            db.commit()
            return

        latest_time = df.index[-1]
        resampled = df  # already correct interval
        log.info(f"[ALGO3] Bars fetched len={len(resampled)} latest_time={latest_time}")

        # --- Bar age check + duplicate guard ---
        age = now_et - (
            latest_time.tz_convert(_ET) if getattr(latest_time, "tzinfo", None) else _ET.localize(latest_time)
        )
        max_age = max_bar_age_for_interval(interval)
        log.info(f"[ALGO3] Bar age={age}, max_age={max_age}")
        if age > max_age:
            log.info(f"[ALGO3] [STALE] {symbol}@{interval} bar too old; skip.")
            return

        if not _is_new_bar(bot_id, latest_time):
            log.info(f"[ALGO3] Already processed latest bar for bot {bot_id}; skipping duplicate.")
            return

        # --- Compute SMI EXACTLY like trades_working ---
        smi_k, smi_d = _compute_smi_like_trades_working(resampled, period=14, smooth_k=3, smooth_d=3)

        # attach to a working frame for signals
        work = resampled.copy()
        work["SMI"] = smi_k
        work["SMI_Signal"] = smi_d

        # guard against insufficient history
        if work["SMI"].isna().sum() > len(work) - 10:
            log.info("[ALGO3] Not enough SMI points to make a decision.")
            return

        # Compute EXACT signal logic and act on the LATEST CLOSED BAR
        work = _determine_signals_like_trades_working(work)

        last_px = float(work["close"].iloc[-1])
        buy_now = bool(work["Buy_Signal"].iloc[-1])
        sell_now = bool(work["Sell_Signal"].iloc[-1])

        curr_smi = float(work["SMI"].iloc[-1]) if pd.notna(work["SMI"].iloc[-1]) else float("nan")
        prev_smi = float(work["SMI"].iloc[-2]) if len(work) >= 2 and pd.notna(work["SMI"].iloc[-2]) else float("nan")

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

        log.info(
            f"[ALGO3] SMI={curr_smi:.2f} (prev {prev_smi:.2f}) "
            f"| Buy_Signal={buy_now} Sell_Signal={sell_now} px={last_px}"
        )

        # --- Mirroring policy ---
        should_force_mirror = bool(_ENV_FORCE_MIRROR or getattr(bot, "mirror_live", False))
        mirror_live_override = True if should_force_mirror else None
        mirror_even_if_no_paper = _ENV_ALLOW_NO_PAPER

        # --- Trading Decisions + Execution (EXACT entry/exit semantics) ---
        result = None

        if not open_trade:
            if buy_now:
                log.info(f"[ALGO3] 🔔 Entry LONG (Buy_Signal) @ {last_px} (qty={trade_size})")
                result = _place_order_via_service(
                    bot_id=bot.id,
                    symbol=symbol,
                    side="BUY",
                    qty=trade_size,
                    mirror_live_override=mirror_live_override,
                    mirror_even_if_no_paper=mirror_even_if_no_paper,
                    actor=f"algo3@{interval}",
                )
            elif sell_now:
                log.info(f"[ALGO3] 🔔 Entry SHORT (Sell_Signal) @ {last_px} (qty={trade_size})")
                result = _place_order_via_service(
                    bot_id=bot.id,
                    symbol=symbol,
                    side="SELL",
                    qty=trade_size,
                    mirror_live_override=mirror_live_override,
                    mirror_even_if_no_paper=mirror_even_if_no_paper,
                    actor=f"algo3@{interval}",
                )
            else:
                log.info("[ALGO3] No entry signal on latest bar.")
        else:
            if open_trade.position_side == "long" and sell_now:
                log.info(f"[ALGO3] 🔔 Exit LONG (Sell_Signal) @ {last_px} (qty={open_trade.quantity})")
                result = _place_order_via_service(
                    bot_id=bot.id,
                    symbol=symbol,
                    side="SELL",
                    qty=float(open_trade.quantity),
                    mirror_live_override=mirror_live_override,
                    mirror_even_if_no_paper=mirror_even_if_no_paper,
                    actor=f"algo3@{interval}",
                )
            elif open_trade.position_side == "short" and buy_now:
                log.info(f"[ALGO3] 🔔 Cover SHORT (Buy_Signal) @ {last_px} (qty={open_trade.quantity})")
                result = _place_order_via_service(
                    bot_id=bot.id,
                    symbol=symbol,
                    side="BUY",
                    qty=float(open_trade.quantity),
                    mirror_live_override=mirror_live_override,
                    mirror_even_if_no_paper=mirror_even_if_no_paper,
                    actor=f"algo3@{interval}",
                )
            else:
                log.info("[ALGO3] No exit signal on latest bar.")

        if result:
            log.info(f"[ALGO3] ✅ Trade executed result: {result}")

        bot.updated_at, bot.status = datetime.utcnow(), "RUNNING"
        db.commit()

    except Exception as e:
        db.rollback()
        log.error(f"[ALGO3] ❌ Error: {e}", exc_info=True)
    finally:
        db.close()
