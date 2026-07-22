#/var/stockwicks/clients/ashakil/app/tasks/stock_tasks.py
"""
Celery tasks for Stock paper/live bots only.

Replay tasks do not belong in this file. Use app.tasks.replay_tasks.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, time as dtime
from typing import Any

import pytz
from celery import shared_task
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import ObjectDeletedError, StaleDataError

from app.database.connection import SessionLocal
from app.models.paper_trading_bot import PaperStockBotOpenTrade, PaperStockTradeBot
from app.services.paper_trade_service import close_position
from app.services.stock_daily_risk import evaluate_daily_state
from app.trading.runners.stock_bot_runner import run_stock_bot_tick
from app.utils.stock.market_price import get_live_price

logger = logging.getLogger(__name__)
_ET = pytz.timezone("US/Eastern")


# -----------------------------------------------------------------------------
# Runtime / lifecycle helpers
# -----------------------------------------------------------------------------
def _bypass_market_open_check() -> bool:
    """
    TEMP COMMERCIAL TEST ONLY.

    Use .env:
      BYPASS_MARKET_OPEN_CHECK=true

    For normal trading:
      BYPASS_MARKET_OPEN_CHECK=false
    """
    return os.getenv("BYPASS_MARKET_OPEN_CHECK", "false").strip().lower() == "true"


def _is_bot_runnable(bot: PaperStockTradeBot) -> bool:
    """
    Central runnable-bot rule.

    A bot should run only when explicitly active/running.
    This prevents deleted/stopped UI state from leaving periodic tasks active.
    """
    if not bot:
        return False

    status = str(getattr(bot, "status", "") or "").strip().lower()
    is_active = bool(getattr(bot, "is_active", False))

    if status in {"stopped", "deleted", "inactive", "paused", "error"}:
        return False

    return is_active or status in {"running", "active", "started"}


def _stop_bot_runtime_fields(bot: PaperStockTradeBot) -> None:
    for attr, value in [
        ("is_active", False),
        ("active", False),
        ("enabled", False),
        ("running", False),
        ("status", "STOPPED"),
    ]:
        if hasattr(bot, attr):
            try:
                setattr(bot, attr, value)
            except Exception:
                pass

    if hasattr(bot, "updated_at"):
        try:
            bot.updated_at = datetime.utcnow()
        except Exception:
            pass


def _redis_client():
    try:
        import redis
        from app.config import settings

        redis_url = getattr(settings, "REDIS_URL", None) or getattr(settings, "redis_url", None)
        redis_url = redis_url or os.getenv("REDIS_URL")
        if not redis_url:
            return None
        return redis.Redis.from_url(redis_url, decode_responses=True)
    except Exception:
        logger.debug("[TASK] Redis unavailable", exc_info=True)
        return None


def _set_redis_stop_flag(bot_id: int) -> None:
    r = _redis_client()
    if not r:
        return
    r.set(f"stockwicks:bot:{bot_id}:stop_requested", "1", ex=60 * 60 * 24)
    r.delete(f"stockwicks:bot:{bot_id}:running")
    r.delete(f"stockwicks:bot:{bot_id}:streaming")


def _clear_redis_stop_flag(bot_id: int) -> None:
    r = _redis_client()
    if not r:
        return
    r.delete(f"stockwicks:bot:{bot_id}:stop_requested")


# -----------------------------------------------------------------------------
# Asset/session helpers
# -----------------------------------------------------------------------------
def _is_futures_symbol(symbol: str | None) -> bool:
    return (symbol or "").strip().upper().startswith("/")


def _in_equity_tick_session(now_et: datetime | None = None) -> bool:
    now_et = now_et or datetime.now(_ET)
    if now_et.weekday() >= 5:
        return False
    return dtime(9, 30) <= now_et.time() <= dtime(15, 55)


def _in_equity_price_session(now_et: datetime | None = None) -> bool:
    now_et = now_et or datetime.now(_ET)
    if now_et.weekday() >= 5:
        return False
    return dtime(9, 30) <= now_et.time() <= dtime(16, 0)


def _in_futures_session(now_et: datetime | None = None) -> bool:
    """
    Generic CME Globex-style futures session in ET:
    - closed Saturday
    - Sunday opens 18:00 ET
    - Friday closes 17:00 ET
    - daily break 17:00-18:00 ET Mon-Thu
    """
    now_et = now_et or datetime.now(_ET)
    wd = now_et.weekday()
    t = now_et.time()

    if wd == 5:
        return False
    if wd == 6:
        return t >= dtime(18, 0)
    if wd == 4:
        return t < dtime(17, 0)
    if dtime(17, 0) <= t < dtime(18, 0):
        return False
    return True


def _should_run_tick_for_symbol(symbol: str | None, now_et: datetime | None = None) -> bool:
    now_et = now_et or datetime.now(_ET)
    if _is_futures_symbol(symbol):
        return _in_futures_session(now_et)
    return _in_equity_tick_session(now_et)


def _should_update_price_for_symbol(symbol: str | None, now_et: datetime | None = None) -> bool:
    now_et = now_et or datetime.now(_ET)
    if _is_futures_symbol(symbol):
        return _in_futures_session(now_et)
    return _in_equity_price_session(now_et)


# -----------------------------------------------------------------------------
# Interval anchoring
# -----------------------------------------------------------------------------
def _get_interval_anchor(interval: str, now_et: datetime | None = None) -> datetime:
    now_et = now_et or datetime.now(_ET)

    if interval == "1d":
        return now_et.replace(hour=16, minute=0, second=0, microsecond=0)

    minutes_map = {
        "1min": 1,
        "5min": 5,
        "10min": 10,
        "15min": 15,
        "30min": 30,
    }
    mins = minutes_map.get(interval)
    if mins is None:
        logger.warning("Unknown interval '%s' for anchor snapping; using now_et", interval)
        return now_et.replace(second=0, microsecond=0)

    minute_bucket = (now_et.minute // mins) * mins
    return now_et.replace(minute=minute_bucket, second=0, microsecond=0)


# -----------------------------------------------------------------------------
# Stock bot interval runner
# -----------------------------------------------------------------------------
@shared_task(name="app.tasks.stock_tasks.run_stock_bots_for_interval", queue="stock")
def run_stock_bots_for_interval(interval: str | None = None):
    """
    Runs active stock bots for a specific interval.

    Beat may call this forever. This task must no-op when no active bots exist.
    """
    now_et = datetime.now(_ET)
    interval_for_anchor = interval or "5min"
    anchor_dt = _get_interval_anchor(interval_for_anchor, now_et=now_et)

    logger.info(
        "[TASK] ▶ run_stock_bots_for_interval interval=%s now_et=%s anchor_dt=%s",
        interval,
        now_et.isoformat(),
        anchor_dt.isoformat(),
    )

    db: Session = SessionLocal()
    try:
        query = db.query(PaperStockTradeBot)
        if interval:
            query = query.filter(PaperStockTradeBot.interval == interval)

        all_bots = query.all()
        bots = [bot for bot in all_bots if _is_bot_runnable(bot)]

        if not bots:
            logger.info("[TASK] No active stock bots for interval=%s; skipping.", interval)
            return {
                "status": "SUCCESS",
                "interval_filter": interval,
                "total_bots": 0,
                "ran": 0,
                "skipped_session": 0,
                "failed": 0,
                "reason": "NO_ACTIVE_BOTS",
            }

        ran = 0
        skipped_session = 0
        failed = 0

        for bot in bots:
            try:
                db.refresh(bot)
                if not _is_bot_runnable(bot):
                    logger.info("[TASK] skipped inactive bot_id=%s", getattr(bot, "id", "?"))
                    continue

                if (
                    not _bypass_market_open_check()
                    and not _should_run_tick_for_symbol(getattr(bot, "symbol", None), now_et=now_et)
                ):
                    logger.info(
                        "[TASK] skipped_session bot_id=%s symbol=%s interval=%s now_et=%s",
                        getattr(bot, "id", "?"),
                        getattr(bot, "symbol", "?"),
                        getattr(bot, "interval", "?"),
                        now_et.isoformat(),
                    )
                    skipped_session += 1
                    continue

                if _bypass_market_open_check():
                    logger.warning(
                        "[COMMERCIAL TEST] BYPASS_MARKET_OPEN_CHECK=true; running bot_id=%s symbol=%s interval=%s outside normal session",
                        getattr(bot, "id", "?"),
                        getattr(bot, "symbol", "?"),
                        getattr(bot, "interval", "?"),
                    )

                run_stock_bot_tick(bot, anchor_dt=anchor_dt)
                ran += 1

            except Exception as inner_e:
                failed += 1
                logger.exception("[TASK] bot_id=%s failed: %s", getattr(bot, "id", "?"), inner_e)

        return {
            "status": "SUCCESS",
            "interval_filter": interval,
            "total_bots": len(bots),
            "ran": ran,
            "skipped_session": skipped_session,
            "failed": failed,
        }

    except Exception as e:
        logger.error("[TASK] ❌ Error in run_stock_bots_for_interval: %s", e, exc_info=True)
        return {"status": "FAILED", "error": str(e)}
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


# -----------------------------------------------------------------------------
# Start/Stop Bot Tasks
# -----------------------------------------------------------------------------
@shared_task(name="app.tasks.stock_tasks.start_paper_stock_bot_task", queue="stock")
def start_paper_stock_bot_task(bot_id: int):
    db: Session = SessionLocal()
    try:
        bot = db.query(PaperStockTradeBot).filter(PaperStockTradeBot.id == bot_id).first()
        if not bot:
            logger.warning("[TASK] Tried to start bot %s, but not found", bot_id)
            return False

        _clear_redis_stop_flag(bot_id)

        if hasattr(bot, "is_active"):
            bot.is_active = True
        if hasattr(bot, "status"):
            bot.status = "RUNNING"
        if hasattr(bot, "updated_at"):
            bot.updated_at = datetime.utcnow()

        db.commit()
        logger.info(
            "[TASK] ✅ Started bot %s (%s) %s@%s",
            bot_id,
            getattr(bot, "algo_name", "?"),
            getattr(bot, "symbol", "?"),
            getattr(bot, "interval", None),
        )
        return True

    except Exception as e:
        db.rollback()
        logger.error("[TASK] ❌ Error starting bot %s: %s", bot_id, e, exc_info=True)
        return False
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


@shared_task(name="app.tasks.stock_tasks.stop_paper_stock_bot_task", queue="stock")
def stop_paper_stock_bot_task(bot_id: int):
    db: Session = SessionLocal()
    try:
        bot = db.query(PaperStockTradeBot).filter(PaperStockTradeBot.id == bot_id).first()
        if not bot:
            logger.warning("[TASK] Tried to stop bot %s, but not found", bot_id)
            _set_redis_stop_flag(bot_id)
            return False

        _set_redis_stop_flag(bot_id)
        _stop_bot_runtime_fields(bot)

        db.commit()
        logger.info(
            "[TASK] ⏹️ Stopped bot %s (%s) %s@%s",
            bot_id,
            getattr(bot, "algo_name", "?"),
            getattr(bot, "symbol", "?"),
            getattr(bot, "interval", None),
        )
        return True

    except Exception as e:
        db.rollback()
        logger.error("[TASK] ❌ Error stopping bot %s: %s", bot_id, e, exc_info=True)
        return False
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


# -----------------------------------------------------------------------------
# Open-trade price updater
# -----------------------------------------------------------------------------
@shared_task(name="app.tasks.stock_tasks.update_open_trades_prices", queue="stock")
def update_open_trades_prices():
    db: Session = SessionLocal()
    now_et = datetime.now(_ET)
    try:
        trade_ids = [row[0] for row in db.query(PaperStockBotOpenTrade.id).all()]

        if not trade_ids:
            logger.info("[PRICE_UPDATE] No open trades; skipping.")
            return {"status": "OK", "open_trades": 0, "updated": 0, "skipped": 0, "reason": "NO_OPEN_TRADES"}

        updated = 0
        skipped = 0
        skipped_session = 0

        for tid in trade_ids:
            try:
                try:
                    trade = (
                        db.query(PaperStockBotOpenTrade)
                        .filter(PaperStockBotOpenTrade.id == tid)
                        .with_for_update(of=PaperStockBotOpenTrade, skip_locked=True)
                        .one_or_none()
                    )
                except (OperationalError, TypeError):
                    trade = db.get(PaperStockBotOpenTrade, tid)

                if not trade:
                    skipped += 1
                    continue

                if not _should_update_price_for_symbol(getattr(trade, "symbol", None), now_et=now_et):
                    skipped_session += 1
                    continue

                price = get_live_price(trade.symbol)
                if price is None:
                    skipped += 1
                    continue

                trade.current_price = float(price)
                side = (trade.position_side or "").lower()
                if side == "long":
                    trade.unrealized_pl = (trade.current_price - trade.entry_price) * trade.quantity
                else:
                    trade.unrealized_pl = (trade.entry_price - trade.current_price) * trade.quantity

                with db.begin_nested():
                    db.flush()

                updated += 1

            except (StaleDataError, ObjectDeletedError) as e:
                logger.warning("[TASK] Skipping stale/missing open trade id=%s: %s", tid, e)
                db.rollback()
                skipped += 1
            except Exception as inner_e:
                logger.warning("[TASK] Price update failed for open trade id=%s: %s", tid, inner_e, exc_info=True)
                db.rollback()
                skipped += 1

        db.commit()
        logger.info("[TASK] ✅ Updated %d open trades (skipped=%d, skipped_session=%d)", updated, skipped, skipped_session)
        return {
            "status": "OK",
            "open_trades": len(trade_ids),
            "updated": updated,
            "skipped": skipped,
            "skipped_session": skipped_session,
        }

    except Exception as e:
        db.rollback()
        logger.error("[TASK] ❌ Error updating open trades prices: %s", e, exc_info=True)
        return {"status": "ERROR", "error": str(e)}
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


# -----------------------------------------------------------------------------
# PnL Exit
# -----------------------------------------------------------------------------
@shared_task(name="app.tasks.stock_tasks.pnl_exit_open_trades", queue="stock")
def pnl_exit_open_trades(
    stop_loss_usd_default: float = 200.0,
    take_profit_usd_default: float = 500.0,
    price_source: str = "live",
    **kwargs: Any,
):
    if kwargs:
        logger.warning("[PnL EXIT] Ignoring unsupported legacy kwargs: %s", sorted(kwargs.keys()))

    db: Session = SessionLocal()
    now_et = datetime.now(_ET)
    try:
        trade_ids = [row[0] for row in db.query(PaperStockBotOpenTrade.id).all()]

        if not trade_ids:
            logger.info("[PnL EXIT] No open trades; skipping.")
            return {"status": "OK", "open_trades": 0, "exited": 0, "skipped": 0, "reason": "NO_OPEN_TRADES"}

        exited = 0
        skipped = 0
        skipped_session = 0

        for tid in trade_ids:
            try:
                try:
                    trade = (
                        db.query(PaperStockBotOpenTrade)
                        .filter(PaperStockBotOpenTrade.id == tid)
                        .with_for_update(of=PaperStockBotOpenTrade, skip_locked=True)
                        .one_or_none()
                    )
                except (OperationalError, TypeError):
                    trade = db.get(PaperStockBotOpenTrade, tid)

                if not trade:
                    skipped += 1
                    continue

                if not _should_update_price_for_symbol(getattr(trade, "symbol", None), now_et=now_et):
                    skipped_session += 1
                    continue

                bot = db.get(PaperStockTradeBot, trade.bot_id)
                stop_loss_usd = getattr(bot, "stop_loss_usd", None) if bot else None
                take_profit_usd = getattr(bot, "take_profit_usd", None) if bot else None

                stop_loss_usd = float(stop_loss_usd) if stop_loss_usd is not None else float(stop_loss_usd_default)
                take_profit_usd = float(take_profit_usd) if take_profit_usd is not None else float(take_profit_usd_default)

                if price_source == "live":
                    px = get_live_price(trade.symbol)
                    if px is None:
                        skipped += 1
                        continue
                    trade.current_price = float(px)
                elif getattr(trade, "current_price", None) is None:
                    skipped += 1
                    continue

                side = (trade.position_side or "").lower()
                if side == "long":
                    trade.unrealized_pl = (trade.current_price - trade.entry_price) * trade.quantity
                else:
                    trade.unrealized_pl = (trade.entry_price - trade.current_price) * trade.quantity

                pnl = float(trade.unrealized_pl or 0.0)
                daily_risk = evaluate_daily_state(db, bot, unrealized_pnl=pnl, now_et=now_et) if bot else {"locked": False}
                if daily_risk.get("locked"):
                    close_position(db, trade, float(trade.current_price))
                    with db.begin_nested():
                        db.flush()
                    exited += 1
                    logger.warning(
                        "[PnL EXIT] daily lock bot_id=%s reason=%s total_pnl=%s target=%s loss_limit=%s",
                        getattr(bot, "id", None),
                        daily_risk.get("reason"),
                        daily_risk.get("total_pnl"),
                        daily_risk.get("target"),
                        daily_risk.get("loss_limit"),
                    )
                    continue
                if pnl <= -stop_loss_usd or pnl >= take_profit_usd:
                    close_position(db, trade, float(trade.current_price))
                    with db.begin_nested():
                        db.flush()
                    exited += 1
                    continue

                with db.begin_nested():
                    db.flush()

            except (StaleDataError, ObjectDeletedError):
                db.rollback()
                skipped += 1
            except Exception:
                db.rollback()
                skipped += 1
                logger.exception("[PnL EXIT] Failed processing open trade id=%s", tid)

        db.commit()
        return {
            "status": "OK",
            "open_trades": len(trade_ids),
            "exited": exited,
            "skipped": skipped,
            "skipped_session": skipped_session,
        }

    except Exception as e:
        db.rollback()
        logger.exception("[PnL EXIT] Fatal error")
        return {"status": "ERROR", "error": str(e)}
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


# -----------------------------------------------------------------------------
# EOD auto-close
# -----------------------------------------------------------------------------
def _is_eod_exact_close(now_et: datetime | None = None) -> bool:
    now_et = now_et or datetime.now(_ET)
    if now_et.weekday() >= 5:
        return False
    return now_et.time().hour == 15 and now_et.time().minute == 58


@shared_task(name="app.tasks.stock_tasks.eod_close_open_trades", queue="stock")
def eod_close_open_trades(price_source: str = "live", dry_run: bool = False):
    now_et = datetime.now(_ET)

    if not _is_eod_exact_close(now_et):
        logger.info("[EOD] Skipped (not 3:58 PM ET) now=%s", now_et)
        return {"status": "SKIPPED_WRONG_TIME", "now_et": now_et.isoformat()}

    db: Session = SessionLocal()
    try:
        open_trades = db.query(PaperStockBotOpenTrade).all()
        if not open_trades:
            logger.info("[EOD] No open trades to close")
            return {"status": "NO_OPEN_TRADES", "closed": 0, "skipped": 0}

        closed = 0
        skipped = 0
        skipped_futures = 0

        for trade in open_trades:
            try:
                if _is_futures_symbol(getattr(trade, "symbol", None)):
                    skipped_futures += 1
                    continue

                price = None
                if price_source == "live":
                    price = get_live_price(trade.symbol)

                if price is None:
                    if getattr(trade, "current_price", None) is not None:
                        price = float(trade.current_price)
                    else:
                        price = float(trade.entry_price)

                if dry_run:
                    logger.info(
                        "[EOD] DRY_RUN would close trade id=%s bot_id=%s symbol=%s qty=%s @ %.4f",
                        trade.id,
                        trade.bot_id,
                        trade.symbol,
                        trade.quantity,
                        price,
                    )
                    skipped += 1
                    continue

                close_position(db, trade, price)
                closed += 1

            except Exception as inner_e:
                logger.error(
                    "[EOD] Failed to close trade id=%s bot_id=%s: %s",
                    getattr(trade, "id", "?"),
                    getattr(trade, "bot_id", "?"),
                    inner_e,
                    exc_info=True,
                )
                db.rollback()
                skipped += 1

        db.commit()
        logger.info("[EOD] ✅ EOD close done. Closed=%d, skipped=%d, skipped_futures=%d", closed, skipped, skipped_futures)
        return {
            "status": "SUCCESS",
            "closed": closed,
            "skipped": skipped,
            "skipped_futures": skipped_futures,
        }

    except Exception as e:
        logger.error("[EOD] ❌ Fatal error in eod_close_open_trades: %s", e, exc_info=True)
        db.rollback()
        return {"status": "ERROR", "error": str(e)}
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


# -----------------------------------------------------------------------------
# Backward-compat dispatcher
# -----------------------------------------------------------------------------
@shared_task(name="app.tasks.stock_tasks.run_all_active_bots_dispatcher", queue="stock")
def _compat_run_all_active_bots_dispatcher():
    logger.info("[TASK] (compat) run_all_active_bots_dispatcher -> run_stock_bots_for_interval()")
    return run_stock_bots_for_interval()
