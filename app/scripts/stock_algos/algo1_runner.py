#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/stock_algos/algo1_runner.py

"""
Algo1 Runner - STANDALONE VERSION (NOT using AlgoMM)
Original Algo1 logic with basic probability-based trading
"""

import warnings
warnings.filterwarnings("ignore", category=RuntimeWarning)

import os
os.environ["LOKY_MAX_CPU_COUNT"] = "4"

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Any, Dict, Tuple

import numpy as np
import pandas as pd

from sqlalchemy.orm import Session

from app.database.connection import SessionLocal
from app.models.paper_trading_bot import (
    PaperStockTradeBot,
    PaperStockBotOpenTrade,
    PaperStockBotTradeHistory,
)
from app.scripts.stock_algos.base_wiring import StockBaseRunner, _ET
from app.services.paper_trade_service import open_position, close_position

logger = logging.getLogger("Algo1_Standalone")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [Algo1] %(message)s"
)

# ---------- DEFAULTS ----------
DEFAULTS = {
    "long_threshold": 0.60,
    "short_threshold": 0.40,
    "exit_threshold": 0.50,
    "hard_stop_usd": 300.0,
    "eod_close": True,
    "cooldown_sec": 60,
    "daily_loss_limit_usd": 500.0,
    "min_volume_multiplier": 1.2,
}


@dataclass
class BotConfig:
    long_threshold: float = DEFAULTS["long_threshold"]
    short_threshold: float = DEFAULTS["short_threshold"]
    exit_threshold: float = DEFAULTS["exit_threshold"]
    hard_stop_usd: float = DEFAULTS["hard_stop_usd"]
    eod_close: bool = DEFAULTS["eod_close"]
    cooldown_sec: int = DEFAULTS["cooldown_sec"]
    daily_loss_limit_usd: float = DEFAULTS["daily_loss_limit_usd"]
    min_volume_multiplier: float = DEFAULTS["min_volume_multiplier"]


# ---------------- VOLUME CHECK ----------------

def check_volume_requirement(
    df: pd.DataFrame,
    current_index: int,
    min_volume_multiplier: float = 1.2
) -> bool:
    """Simple volume check."""
    try:
        if current_index < 20:
            return True

        current_volume = df["volume"].iloc[current_index]
        avg_volume = df["volume"].iloc[max(0, current_index - 20):current_index].mean()
        return current_volume >= (avg_volume * min_volume_multiplier)
    except Exception:
        return True


# ---------------- ENTRY/EXIT LOGIC ----------------

def should_enter_trade(
    prob_up: float,
    prob_down: float,
    df: pd.DataFrame,
    current_index: int,
    cfg: BotConfig,
    allow_short: bool = True
) -> Tuple[bool, str, str]:
    """
    Simple Algo1 entry logic:
    - Long if prob_up >= long_threshold
    - Short if prob_down >= short_threshold
    """
    # 1. Volume requirement
    if not check_volume_requirement(df, current_index, cfg.min_volume_multiplier):
        logger.info("ENTRY_CHECK: rejected due to LOW_VOLUME")
        return False, "", "LOW_VOLUME"

    # 2. Simple threshold check
    if prob_up >= cfg.long_threshold:
        logger.info(f"LONG_TRIGGER: UP={prob_up:.3f} >= {cfg.long_threshold:.3f}")
        return True, "LONG", "LONG_THRESHOLD_MET"

    if prob_down >= cfg.short_threshold and allow_short:
        logger.info(f"SHORT_TRIGGER: DOWN={prob_down:.3f} >= {cfg.short_threshold:.3f}")
        return True, "SHORT", "SHORT_THRESHOLD_MET"

    return False, "", "THRESHOLD_NOT_MET"


def should_exit_trade(
    open_trade: PaperStockBotOpenTrade,
    current_price: float,
    prob_up: float,
    prob_down: float,
    cfg: BotConfig
) -> Tuple[bool, str]:
    """Simple exit logic."""
    if not open_trade:
        return False, ""

    position_side = open_trade.position_side

    if position_side == "long" and prob_up < cfg.exit_threshold:
        return True, "PROBABILITY_DROP"
    elif position_side == "short" and prob_down < cfg.exit_threshold:
        return True, "PROBABILITY_DROP"

    return False, ""


# ---------------- CONFIG LOADERS ----------------

def _load_bot_config(bot: PaperStockTradeBot) -> BotConfig:
    """Load config from bot settings."""
    cfg = BotConfig()

    # Try JSON fields first
    for json_field in ("config_json", "settings", "params", "note"):
        if hasattr(bot, json_field):
            js = _parse_json_field(getattr(bot, json_field))
            if not js:
                continue

            cfg.long_threshold = _safe_float(js.get("long_threshold", cfg.long_threshold), cfg.long_threshold)
            cfg.short_threshold = _safe_float(js.get("short_threshold", cfg.short_threshold), cfg.short_threshold)
            cfg.exit_threshold = _safe_float(js.get("exit_threshold", cfg.exit_threshold), cfg.exit_threshold)
            cfg.min_volume_multiplier = _safe_float(js.get("min_volume_multiplier", cfg.min_volume_multiplier), cfg.min_volume_multiplier)
            cfg.hard_stop_usd = _safe_float(js.get("hard_stop_usd", cfg.hard_stop_usd), cfg.hard_stop_usd)
            cfg.daily_loss_limit_usd = _safe_float(js.get("daily_loss_limit_usd", cfg.daily_loss_limit_usd), cfg.daily_loss_limit_usd)
            cfg.eod_close = _safe_bool(js.get("eod_close", cfg.eod_close), cfg.eod_close)
            cfg.cooldown_sec = _safe_int(js.get("cooldown_sec", cfg.cooldown_sec), cfg.cooldown_sec)

    return cfg


def _safe_float(v: Any, default: float) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _safe_int(v: Any, default: int) -> int:
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default


def _safe_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off"):
            return False
    return default


def _parse_json_field(s: Any) -> Dict[str, Any]:
    if not isinstance(s, str) or not s.strip():
        return {}
    try:
        return json.loads(s)
    except Exception:
        try:
            return json.loads(s.replace("'", '"'))
        except Exception:
            return {}


# ---------------- SIMPLE PROBABILITY CALCULATION ----------------

def calculate_simple_probability(df: pd.DataFrame) -> float:
    """
    Simple probability calculation for Algo1.
    Uses basic price momentum and volume indicators.
    """
    if len(df) < 10:
        return 0.5  # Neutral if not enough data
    
    try:
        # Simple moving average crossover
        close = df['close'].values
        sma_short = np.mean(close[-5:]) if len(close) >= 5 else close[-1]
        sma_long = np.mean(close[-10:]) if len(close) >= 10 else close[-1]
        
        # Volume trend
        volume = df['volume'].values
        volume_avg = np.mean(volume[-5:]) if len(volume) >= 5 else volume[-1]
        current_volume = volume[-1]
        
        # Price momentum
        price_change = (close[-1] - close[-2]) / close[-2] if len(close) >= 2 else 0
        
        # Calculate simple probability
        prob = 0.5  # Start neutral
        
        # MA crossover factor
        if sma_short > sma_long:
            prob += 0.15
        else:
            prob -= 0.15
            
        # Volume factor
        if current_volume > volume_avg * 1.2:
            prob += 0.1
            
        # Momentum factor
        if price_change > 0.001:  # 0.1% up
            prob += 0.1
        elif price_change < -0.001:  # 0.1% down
            prob -= 0.1
            
        # Bound between 0 and 1
        return max(0.0, min(1.0, prob))
        
    except Exception:
        return 0.5


# ---------------- COOL DOWN HELPER ----------------

def get_last_trade_time(bot_id: int, db: Session) -> Optional[datetime]:
    """Return timestamp of latest trade."""
    try:
        last_hist = (
            db.query(PaperStockBotTradeHistory)
            .filter_by(bot_id=bot_id)
            .order_by(PaperStockBotTradeHistory.id.desc())
            .first()
        )
        if last_hist is None:
            return None

        ts = getattr(last_hist, "created_at", None) or getattr(last_hist, "opened_at", None)
        return ts
    except Exception:
        return None


# ---------------- MAIN TICK FUNCTION ----------------

def run_algo1_bot_tick(bot_id: int, anchor_dt: Optional[datetime] = None):
    """
    Standalone Algo1 tick function.
    Uses simple probability calculation (NOT AlgoMM).
    """
    db: Session = SessionLocal()
    runner = StockBaseRunner()

    try:
        bot = db.query(PaperStockTradeBot).filter_by(id=bot_id).first()
        if not bot:
            logger.warning(f"Bot {bot_id} not found")
            return

        cfg = _load_bot_config(bot)

        # Use anchor_dt if provided
        if anchor_dt is not None:
            if anchor_dt.tzinfo is None:
                now_et = _ET.localize(anchor_dt)
            else:
                now_et = anchor_dt.astimezone(_ET)
        else:
            now_et = datetime.now(_ET)

        # Fetch data
        df_raw = runner.fetch_source_bars(bot.symbol)
        if df_raw is None or df_raw.empty:
            logger.info("NO_DATA: Raw data empty")
            return

        latest_time, _, df = runner.resample_interval(
            df_raw,
            (bot.interval or "1min"),
            bot.symbol,
        )
        if latest_time is None or df is None or df.empty:
            logger.info("RESAMPLE_FAILED")
            return

        df = df.sort_index()
        
        # Validate prices
        bar_close_px = float(df["close"].iloc[-1])
        bar_open_px = float(df["open"].iloc[-1])
        
        if bar_close_px <= 0 or not np.isfinite(bar_close_px):
            logger.info("INVALID_PRICE")
            return

        # Calculate simple probability (NOT using AlgoMM model)
        prob_up = calculate_simple_probability(df)
        prob_down = 1.0 - prob_up
        
        logger.info(f"Algo1 Probabilities: UP={prob_up:.3f}, DOWN={prob_down:.3f}")

        # Check open trade
        open_trade = db.query(PaperStockBotOpenTrade).filter_by(bot_id=bot.id).first()

        # Cooldown check
        if cfg.cooldown_sec and cfg.cooldown_sec > 0:
            last_trade_time = get_last_trade_time(bot.id, db)
            if last_trade_time is not None:
                delta_sec = (now_et - last_trade_time).total_seconds()
                if delta_sec < cfg.cooldown_sec:
                    logger.info(f"COOLDOWN: Last trade {int(delta_sec)}s ago")
                    return

        # ENTRY/EXIT LOGIC
        if open_trade is None:
            should_enter, enter_direction, entry_reason = should_enter_trade(
                prob_up,
                prob_down,
                df,
                len(df) - 1,
                cfg,
            )

            if should_enter:
                trade_size = getattr(bot, "trade_size", 100.0)
                side = "BUY" if enter_direction == "LONG" else "SELL"
                
                # Open position using paper_trade_service
                position_side = "long" if side == "BUY" else "short"
                open_position(db, bot, position_side, float(bar_close_px))
                
                db.commit()
                logger.info(f"OPEN_{enter_direction}: {entry_reason}")

        elif open_trade is not None:
            should_exit, exit_reason = should_exit_trade(
                open_trade,
                bar_close_px,
                prob_up,
                prob_down,
                cfg,
            )
            if should_exit:
                close_position(db, open_trade, bar_close_px)
                db.commit()
                logger.info(f"EXIT: {exit_reason}")

    except Exception as e:
        db.rollback()
        logger.error(f"Algo1 error for bot {bot_id}: {e}", exc_info=True)
    finally:
        if db:
            db.close()


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 2:
        run_algo1_bot_tick(int(sys.argv[1]))
    else:
        print("Usage: python algo1_runner.py <BOT_ID>")