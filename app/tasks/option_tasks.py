# /var/www/stockwicks/app/tasks/option_tasks.py
"""
Celery tasks for Options paper bots.

Mirrors stock paper bot architecture:
- One task to run bot ticks (open new trades)
- One task to manage open trades (mark pricing, TP/SL / time exits)
- Per-minute mark/PnL updater (concurrency-safe)
- Per-minute exit enforcement (concurrency-safe, idempotent)
- EOD auto-close (runs every minute but only acts at 3:58 PM ET)

Notes:
- PnL for options is in USD: (points PnL) * 100 * qty
- Long profit when mark > entry
- Short profit when mark < entry
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime
from typing import Optional

import pytz
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm.exc import StaleDataError, ObjectDeletedError

from app.celery_worker import celery
from app.database.connection import SessionLocal
from app.models.paper_option_trading_bot import PaperOptionTradeBot, PaperOptionBotOpenTrade

from app.scripts.options.option_bots_runner import run_option_bots_tick, manage_open_trades

# ✅ IMPORTANT: import mark_open_trade once, and call it WITHOUT db=
from app.utils.options.options_pricing import mark_open_trade

log = logging.getLogger(__name__)
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

_ET = pytz.timezone("US/Eastern")


def _now_et() -> datetime:
    return datetime.now(_ET)


def _as_float(x, default=None):
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


#If want to disable Market hours for bot, enable following finction disable above
def _is_regular_market_session(now_et: datetime | None = None) -> bool:
    """
    BYPASS VERSION — Always returns True.
    """
    return True

# def _is_regular_market_session(now_et: datetime | None = None) -> bool:
#     """
#     Return True only during regular US stock hours:
#       - Weekdays (Mon–Fri)
#       - Between 10 AM and 3:40 PM ET (inclusive)
#     """
#     now_et = now_et or _now_et()

#     # Saturday=5, Sunday=6
#     if now_et.weekday() >= 5:
#         return False

#     t = now_et.time()
#     # ✅ Start only after 9:30am ET
#     # ✅ Stop trading after 3:55pm ET (no entries at 3:56+)
#     return dtime(9, 40) <= t <= dtime(15, 50)



def _is_eod_exact_close(now_et: datetime | None = None) -> bool:
    """
    Run every minute; only act at exactly 3:58 PM ET on weekdays.
    """
    now_et = now_et or _now_et()
    if now_et.weekday() >= 5:
        return False
    t = now_et.time()
    return t.hour == 15 and t.minute == 55


def _infer_is_short(position_side: str | None) -> bool:
    side = (position_side or "").strip().lower()
    return side in ("short", "credit", "sell", "single sell", "sell_to_open", "sto")


def _compute_unrealized_pl_usd(entry: float, mark: float, qty: int, is_short: bool) -> float:
    # option points -> USD multiplier
    mult = 100.0 * float(qty)
    points = (entry - mark) if is_short else (mark - entry)
    return float(points) * mult


# -----------------------------
# Main runners
# -----------------------------
@celery.task(name="options.run_all_bots")
def run_all_bots(user_id: Optional[int] = None):
    """
    Runs one tick for all active option bots (or a single user).
    ✅ Market-hours gate here prevents after-hours re-opening loops while you test manual exits.
    """
    now_et = _now_et()
    if not _is_regular_market_session(now_et):
        log.info("[options.run_all_bots] ⏩ Market closed. Skipping entries. now_et=%s", now_et.isoformat())
        return {"status": "SKIPPED_OUTSIDE_SESSION", "now_et": now_et.isoformat()}

    return run_option_bots_tick(user_id=user_id)


@celery.task(name="options.manage_open_trades")
def manage_all_open_trades(user_id: Optional[int] = None):
    """
    Applies algo exit logic via runner helper.
    (We still keep explicit tasks below for stock-like behavior.)
    """
    db = SessionLocal()
    try:
        manage_open_trades(db=db, user_id=user_id)
        return "ok"
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass


# -----------------------------
# Per-minute mark + PnL updater (like stock_tasks.update_open_trades_prices)
# -----------------------------
@celery.task(name="options.update_open_trades_prices")
def update_open_option_trades_prices(user_id: int | None = None):
    """
    Update current_mark_price & unrealized_pl for all open option trades.
    Concurrency-safe + per-row savepoints (same pattern as stock).
    """
    db = SessionLocal()
    try:
        q = db.query(PaperOptionBotOpenTrade.id)
        if user_id:
            q = q.filter(PaperOptionBotOpenTrade.user_id == user_id)
        trade_ids = [row[0] for row in q.all()]

        updated = 0
        skipped = 0

        for tid in trade_ids:
            try:
                # lock row if possible
                try:
                    ot = (
                        db.query(PaperOptionBotOpenTrade)
                        .filter(PaperOptionBotOpenTrade.id == tid)
                        .with_for_update(of=PaperOptionBotOpenTrade, skip_locked=True)
                        .one_or_none()
                    )
                except (OperationalError, TypeError):
                    ot = db.get(PaperOptionBotOpenTrade, tid)

                if not ot:
                    skipped += 1
                    continue

                # ✅ DO NOT assume a single mark_open_trade signature
                mark = None
                last_type_error = None
                for attempt in (
                    lambda: mark_open_trade(open_trade=ot),
                    lambda: mark_open_trade(ot),
                    lambda: mark_open_trade(open_trade=ot, logger=log),
                    lambda: mark_open_trade(ot, logger=log),
                    lambda: mark_open_trade(open_trade=ot, db=db),
                    lambda: mark_open_trade(ot, db=db),
                ):
                    try:
                        mark = attempt()
                        break
                    except TypeError as e:
                        last_type_error = e
                        continue

                if mark is None and last_type_error is not None:
                    log.warning("[options.update_open_trades_prices] mark_open_trade signature mismatch tid=%s: %s", tid, last_type_error)

                # If it returned a number, store it; if it updated model internally, keep existing
                if mark is not None:
                    ot.current_mark_price = float(mark)

                entry = float(getattr(ot, "entry_price", 0.0) or 0.0)
                cur = float(getattr(ot, "current_mark_price", 0.0) or 0.0)
                qty = int(getattr(ot, "quantity", 1) or 1)

                is_short = _infer_is_short(getattr(ot, "position_side", None))
                ot.unrealized_pl = _compute_unrealized_pl_usd(entry, cur, qty, is_short)

                with db.begin_nested():
                    db.flush()

                updated += 1

            except (StaleDataError, ObjectDeletedError):
                db.rollback()
                skipped += 1
            except Exception:
                db.rollback()
                skipped += 1
                log.exception("[options.update_open_trades_prices] Failed tid=%s", tid)

        db.commit()
        return {"status": "OK", "updated": updated, "skipped": skipped, "open_trades": len(trade_ids)}

    finally:
        try:
            db.rollback()
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass


# -----------------------------
# Per-minute exit enforcement (like stock pnl_exit_open_trades)
# -----------------------------
@celery.task(name="options.pnl_exit_open_trades")
def pnl_exit_open_option_trades(user_id: Optional[int] = None):
    db = SessionLocal()
    try:
        q = db.query(PaperOptionBotOpenTrade.id)
        if user_id:
            q = q.filter(PaperOptionBotOpenTrade.user_id == user_id)
        trade_ids = [row[0] for row in q.all()]

        if not trade_ids:
            return {"status": "NO_OPEN_TRADES", "exited": 0, "skipped": 0}

        from app.utils.options.option_trade_utils import close_option_trade
        from app.utils.options.options_pricing import mark_open_trade as _mark_open_trade_local

        exited = 0
        skipped = 0

        for tid in trade_ids:
            try:
                # lock row
                try:
                    ot = (
                        db.query(PaperOptionBotOpenTrade)
                        .filter(PaperOptionBotOpenTrade.id == tid)
                        .with_for_update(of=PaperOptionBotOpenTrade, skip_locked=True)
                        .one_or_none()
                    )
                except (OperationalError, TypeError):
                    ot = db.get(PaperOptionBotOpenTrade, tid)

                if not ot:
                    skipped += 1
                    continue

                # -----------------------------------------
                # refresh mark (support multiple signatures)
                # -----------------------------------------
                m = None
                last_type_error = None

                for attempt in (
                    lambda: _mark_open_trade_local(open_trade=ot, logger=log),
                    lambda: _mark_open_trade_local(ot, logger=log),
                    lambda: _mark_open_trade_local(open_trade=ot),
                    lambda: _mark_open_trade_local(ot),
                    lambda: _mark_open_trade_local(open_trade=ot, db=db),
                    lambda: _mark_open_trade_local(ot, db=db),
                    lambda: _mark_open_trade_local(open_trade=ot, db=db, logger=log),
                    lambda: _mark_open_trade_local(ot, db=db, logger=log),
                ):
                    try:
                        m = attempt()
                        break
                    except TypeError as e:
                        last_type_error = e
                        continue

                if m is None and last_type_error is not None:
                    log.warning("[options.pnl_exit] mark_open_trade signature mismatch tid=%s: %s", tid, last_type_error)

                if m is not None:
                    try:
                        ot.current_mark_price = float(m)
                    except Exception:
                        log.warning("[options.pnl_exit] Could not cast mark to float tid=%s mark=%r", tid, m)

                # read values
                m = float(getattr(ot, "current_mark_price", None) or 0.0)
                entry = float(getattr(ot, "entry_price", None) or 0.0)
                qty = int(getattr(ot, "quantity", None) or 1)

                if m <= 0 or entry <= 0:
                    skipped += 1
                    continue

                # planned exits
                sl = _as_float(getattr(ot, "planned_stop_loss", None))
                tp1 = _as_float(getattr(ot, "planned_take_profit_1", None))
                tp2 = _as_float(getattr(ot, "planned_take_profit_2", None))
                tp = _as_float(getattr(ot, "planned_take_profit", None))

                # pick first available TP in priority order
                target = tp1 if tp1 is not None else (tp2 if tp2 is not None else tp)

                # direction (BUY/SELL) comes from position_side
                side = (getattr(ot, "position_side", "") or "").lower()
                is_short = side in ("sell", "short", "credit", "sto", "sell_to_open")
                is_long = not is_short

                # update unrealized_pl (USD)
                points = (m - entry) if is_long else (entry - m)
                ot.unrealized_pl = float(points) * 100.0 * qty

                # hit checks
                sl_hit = False
                tp_hit = False
                if sl is not None:
                    sl_hit = (m <= sl) if is_long else (m >= sl)
                if target is not None:
                    tp_hit = (m >= target) if is_long else (m <= target)

                log.info(
                    "[options.pnl_exit] id=%s sym=%s pos=%s entry=%.4f mark=%.4f sl=%s tp=%s sl_hit=%s tp_hit=%s pnl=$%.2f",
                    getattr(ot, "id", None),
                    getattr(ot, "underlying_symbol", None),
                    side,
                    entry,
                    m,
                    sl,
                    target,
                    sl_hit,
                    tp_hit,
                    float(ot.unrealized_pl or 0.0),
                )

                if not (sl_hit or tp_hit):
                    with db.begin_nested():
                        db.flush()
                    continue

                reason = "SL_HIT" if sl_hit else "TP_HIT"
                # Prefer service-layer close (supports Schwab mirroring) if available
                try:
                    from app.services.paper_option_trade_service import close_option_position  # type: ignore
                    close_option_position(db, ot, float(getattr(ot, "current_mark_price", None) or 0.0), reason=reason)
                except Exception:
                    close_option_trade(db=db, open_trade=ot, reason=reason, logger=log)


                with db.begin_nested():
                    db.flush()

                exited += 1

            except (StaleDataError, ObjectDeletedError):
                db.rollback()
                skipped += 1
            except Exception as e:
                db.rollback()
                skipped += 1
                log.exception("[options.pnl_exit] Failed tid=%s err=%s", tid, e)

        db.commit()
        return {"status": "OK", "exited": exited, "skipped": skipped, "open_trades": len(trade_ids)}

    finally:
        try:
            db.rollback()
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass


# -----------------------------
# EOD close (3:58 PM ET weekdays)
# -----------------------------
@celery.task(name="options.eod_close_open_trades")
def eod_close_open_option_trades(user_id: Optional[int] = None):
    now_et = _now_et()
    if not _is_eod_exact_close(now_et):
        return {"status": "SKIPPED_WRONG_TIME", "now_et": now_et.isoformat()}

    db = SessionLocal()
    try:
        q = db.query(PaperOptionBotOpenTrade)
        if user_id:
            q = q.filter(PaperOptionBotOpenTrade.user_id == user_id)
        open_trades = q.all()

        if not open_trades:
            return {"status": "NO_OPEN_TRADES", "closed": 0}

        from app.utils.options.option_trade_utils import close_option_trade

        closed = 0
        skipped = 0

        for ot in open_trades:
            try:
                # Prefer service-layer close (supports Schwab mirroring) if available
                try:
                    from app.services.paper_option_trade_service import close_option_position  # type: ignore
                    close_option_position(db, ot, float(getattr(ot, "current_mark_price", None) or 0.0), reason="EOD")
                except Exception:
                    close_option_trade(db=db, open_trade=ot, reason="EOD", logger=log)

                with db.begin_nested():
                    db.flush()
                closed += 1
            except Exception:
                db.rollback()
                skipped += 1
                log.exception("[options.eod_close_open_trades] Failed open_trade_id=%s", getattr(ot, "id", None))

        db.commit()
        return {"status": "OK", "closed": closed, "skipped": skipped}

    finally:
        try:
            db.rollback()
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass


# -----------------------------
# Start/Stop Bot tasks
# -----------------------------
@celery.task(name="options.start_option_paper_bot_task")
def start_option_paper_bot_task(bot_id: int):
    db = SessionLocal()
    try:
        bot = db.query(PaperOptionTradeBot).get(bot_id)
        if not bot:
            return f"Bot {bot_id} not found"
        bot.is_active = True
        bot.status = "RUNNING"
        db.commit()
        return "ok"
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass


@celery.task(name="options.stop_option_paper_bot_task")
def stop_option_paper_bot_task(bot_id: int):
    db = SessionLocal()
    try:
        bot = db.query(PaperOptionTradeBot).get(bot_id)
        if not bot:
            return f"Bot {bot_id} not found"
        bot.is_active = False
        bot.status = "STOPPED"
        db.commit()
        return "ok"
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass


@celery.task(name="options.test_algo_imports")
def test_algo_imports():
    """Sanity check that option algos import cleanly."""
    try:
        from app.scripts.options.algos.algo_scalper import run_scalper_bots  # noqa
        from app.scripts.options.algos.algo_guru import run_guru_bots  # noqa
        from app.scripts.options.algos.algo_debit_spread import run_debit_spread_bots  # noqa
        from app.scripts.options.algos.algo_credit_spread import run_credit_spread_bots  # noqa
        from app.scripts.options.algos import guru_pick_4exp  # noqa
        return "ok"
    except Exception as e:
        log.error(f"Algo import failed: {e}", exc_info=True)
        return f"failed: {e}"
