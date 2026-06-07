#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/replay/orchestrator.py
"""
Replay Orchestrator
===================

Walks every bar in a replay session, calls the replay runner for each one,
paces at the requested speed, updates DB cursor fields so the UI can track
progress, and responds to a STOPPED signal.

Run modes
---------
    # Foreground (CLI dev mode)
    python -m app.scripts.replay.orchestrator --session-id 1

    # Foreground, but skip sleep between bars (fastest possible replay)
    python -m app.scripts.replay.orchestrator --session-id 1 --no-sleep

    # Detached (later called by the web route in Step 5)
    nohup python -m app.scripts.replay.orchestrator --session-id 1 &

Lifecycle
---------
    1. Loads ReplaySession, validates it's startable.
    2. Transitions status PENDING → RUNNING, writes pid + started_at.
    3. Initializes ReplayDataProvider (1-min CSV → resample).
    4. For each bar_idx in 0..total_bars:
         - Re-reads session row (cheap) to check for 'STOPPED' status from UI.
         - If stopped: break cleanly.
         - Calls run_algoMM_replay_tick(session_id, bar_idx, provider).
         - Updates cursor (current_bar_idx, current_bar_time) every N bars.
         - Sleeps provider.tick_seconds(speed) unless --no-sleep.
    5. On exit (natural / stopped / error): flushes cursor, sets final status,
       sets stopped_at, clears model cache, closes dangling open trade.

Signals
-------
    SIGTERM / SIGINT → treated as user stop. Flushes state, writes STOPPED,
    exits code 0. This is how the web's Stop button will kill the process.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
import traceback
from datetime import datetime
from typing import Optional

from app.database.connection import SessionLocal
from app.models.replay import ReplaySession, ReplayOpenTrade
from app.scripts.replay.replay_data_provider import ReplayDataProvider
from app.scripts.stock_algos.algoMM_replay_runner import (
    run_algoMM_replay_tick,
    clear_model_cache,
)


# =============================================================================
# Logging
# =============================================================================
logger = logging.getLogger("ReplayOrchestrator")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [Orch] %(message)s"
    ))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# =============================================================================
# Constants
# =============================================================================
CURSOR_FLUSH_EVERY = 1       # update session.current_bar_idx in DB every N bars
STOP_POLL_EVERY = 10         # check for status='STOPPED' every N bars


# =============================================================================
# Module-level state for signal handlers
# =============================================================================
_STOP_REQUESTED = False


def _install_signal_handlers():
    """Make SIGTERM/SIGINT set a flag instead of killing us mid-tick."""
    def _handler(signum, frame):
        global _STOP_REQUESTED
        logger.warning(f"Received signal {signum}; will stop after current bar")
        _STOP_REQUESTED = True
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


# =============================================================================
# Session lifecycle helpers
# =============================================================================
def _load_session(session_id: int) -> Optional[ReplaySession]:
    db = SessionLocal()
    try:
        return db.query(ReplaySession).filter_by(id=session_id).first()
    finally:
        db.close()


def _update_session(session_id: int, **fields) -> None:
    """Best-effort atomic update of a subset of columns."""
    db = SessionLocal()
    try:
        s = db.query(ReplaySession).filter_by(id=session_id).first()
        if not s:
            return
        for k, v in fields.items():
            setattr(s, k, v)
        s.updated_at = datetime.utcnow()
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update session {session_id}: {e}")
    finally:
        db.close()


def _read_session_status(session_id: int) -> Optional[str]:
    """Cheap read — just the status column, for the stop poll."""
    db = SessionLocal()
    try:
        s = db.query(ReplaySession.status).filter_by(id=session_id).first()
        return s[0] if s else None
    finally:
        db.close()


def _close_dangling_open_trade(session_id: int) -> None:
    """
    If the session ends with an open trade, delete it — the UI should only
    show trades that completed within the replay. Leaving them around
    pollutes the open positions table across restarts.
    """
    db = SessionLocal()
    try:
        dangling = db.query(ReplayOpenTrade).filter_by(session_id=session_id).all()
        for ot in dangling:
            logger.info(
                f"Discarding dangling open trade: {ot.position_side} {ot.symbol} "
                f"qty={ot.quantity} entry={ot.entry_price} at bar_time={ot.entry_time}"
            )
            db.delete(ot)
        if dangling:
            db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Cleanup failed for session {session_id}: {e}")
    finally:
        db.close()


# =============================================================================
# Main loop
# =============================================================================
def run_session(session_id: int, *, no_sleep: bool = False) -> int:
    """
    Run a replay session to completion (or until stopped).
    Returns process exit code (0 = success).
    """
    _install_signal_handlers()

    # ---- 1. Load session and validate ----
    session = _load_session(session_id)
    if session is None:
        logger.error(f"Session {session_id} not found")
        return 2

    if session.status not in ("PENDING", "QUEUED", "STARTING", "STOPPED", "COMPLETED", "ERROR"):
        logger.error(
            f"Session {session_id} is not startable (status={session.status}); "
            "it may already be running."
        )
        return 3

    logger.info(
        f"Starting session {session_id}: "
        f"{session.symbol} {session.interval} "
        f"{session.start_date} → {session.end_date} "
        f"algo={session.algo_name} speed={session.speed}x"
    )

    # ---- 2. Initialize provider ----
    try:
        provider = ReplayDataProvider(
            user_id=session.user_id,
            symbol=session.symbol,
            start_date=session.start_date,
            end_date=session.end_date,
            interval=session.interval,
        )
    except Exception as e:
        logger.error(f"Provider init failed: {e}", exc_info=True)
        _update_session(
            session_id,
            status="ERROR",
            error_message=f"Provider init: {str(e)[:500]}",
            stopped_at=datetime.utcnow(),
        )
        return 4

    total_bars = provider.total_bars
    if total_bars == 0:
        logger.error("No bars in replay range")
        _update_session(
            session_id,
            status="ERROR",
            error_message="No bars in replay range",
            stopped_at=datetime.utcnow(),
        )
        return 5

    # ---- 3. Transition to RUNNING ----
    start_time = datetime.utcnow()
    _update_session(
        session_id,
        status="RUNNING",
        pid=os.getpid(),
        total_bars=total_bars,
        current_bar_idx=0,
        current_bar_time=provider.bar_time(0).to_pydatetime(),
        started_at=start_time,
        stopped_at=None,
        error_message=None,
    )

    speed = float(session.speed or 1.0)
    if speed not in {1.0, 5.0, 10.0, 20.0}:
        speed = 1.0

    # 1x = 1 candle/sec, 5x = 5 candles/sec, 10x = 10 candles/sec, 20x = 20 candles/sec.
    tick_sleep = 0.0 if no_sleep else max(0.05, 1.0 / speed)
    logger.info(
        f"Walking {total_bars} bars, tick_sleep={tick_sleep:.2f}s "
        f"(speed={session.speed}x{', no-sleep' if no_sleep else ''})"
    )

    # ---- 4. Main loop ----
    exit_status = "COMPLETED"
    error_msg: Optional[str] = None
    last_decision_log = datetime.utcnow()

    try:
        for bar_idx in range(total_bars):
            # --- Check for stop signal (from UI or POSIX signal) ---
            if _STOP_REQUESTED:
                logger.info("Stop signal received; exiting main loop")
                exit_status = "STOPPED"
                break

            if bar_idx % STOP_POLL_EVERY == 0:
                db_status = _read_session_status(session_id)
                if db_status == "STOPPED":
                    logger.info("Session marked STOPPED in DB; exiting")
                    exit_status = "STOPPED"
                    break

            # --- Run one tick ---
            try:
                result = run_algoMM_replay_tick(
                    session_id=session_id,
                    bar_idx=bar_idx,
                    provider=provider,
                )
            except Exception as e:
                logger.error(
                    f"Tick failed at bar {bar_idx}: {e}",
                    exc_info=True,
                )
                # Don't abort the whole session on a single-tick failure —
                # algo-land flakiness shouldn't kill a 6-hour replay.
                # But do log it so we notice.
                time.sleep(tick_sleep)
                continue

            # --- Log interesting decisions ---
            decision = result.get("decision", "NONE")
            if decision not in ("NONE", "NO_ENTRY", "HOLD", "COOLDOWN"):
                logger.info(
                    f"bar={bar_idx}/{total_bars} "
                    f"time={result.get('bar_time')} "
                    f"px={result.get('price', 0):.2f} "
                    f"UP={result.get('prob_up', 0):.3f} "
                    f"{decision} ({result.get('reason', '')})"
                )
            elif (datetime.utcnow() - last_decision_log).total_seconds() > 30:
                # Periodic heartbeat so you can see the replay is alive
                logger.info(
                    f"bar={bar_idx}/{total_bars} "
                    f"time={result.get('bar_time')} "
                    f"px={result.get('price', 0):.2f} "
                    f"UP={result.get('prob_up', 0):.3f} "
                    f"{decision}"
                )
                last_decision_log = datetime.utcnow()

            # --- Flush cursor periodically (cheap but not every bar) ---
            if bar_idx % CURSOR_FLUSH_EVERY == 0 or bar_idx == total_bars - 1:
                bt = provider.bar_time(bar_idx)
                _update_session(
                    session_id,
                    current_bar_idx=bar_idx,
                    current_bar_time=bt.to_pydatetime()
                    if hasattr(bt, "to_pydatetime") else bt,
                )

            # --- Pace ---
            if tick_sleep > 0:
                # Split sleep so we can react to stop signals responsively
                remaining = tick_sleep
                while remaining > 0 and not _STOP_REQUESTED:
                    chunk = min(0.5, remaining)
                    time.sleep(chunk)
                    remaining -= chunk

    except Exception as e:
        logger.error(f"Main loop crashed: {e}", exc_info=True)
        exit_status = "ERROR"
        error_msg = f"{type(e).__name__}: {str(e)[:400]}"

    # ---- 5. Shutdown ----
    logger.info(f"Main loop ended with status={exit_status}")

    try:
        _close_dangling_open_trade(session_id)
    except Exception as e:
        logger.warning(f"Cleanup error (non-fatal): {e}")

    try:
        clear_model_cache(session_id)
    except Exception:
        pass

    # Final flush — cursor at last processed bar
    try:
        last_idx = min(bar_idx if 'bar_idx' in locals() else 0, total_bars - 1)
        last_bt = provider.bar_time(last_idx)
        _update_session(
            session_id,
            status=exit_status,
            current_bar_idx=last_idx,
            current_bar_time=last_bt.to_pydatetime()
            if hasattr(last_bt, "to_pydatetime") else last_bt,
            stopped_at=datetime.utcnow(),
            error_message=error_msg,
            pid=None,
        )
    except Exception as e:
        logger.error(f"Final update failed: {e}")

    dur = (datetime.utcnow() - start_time).total_seconds()
    logger.info(
        f"Session {session_id} finished: status={exit_status} "
        f"bars={last_idx + 1 if 'last_idx' in locals() else 0}/{total_bars} "
        f"elapsed={dur:.1f}s"
    )

    return 0 if exit_status in ("COMPLETED", "STOPPED") else 1


# =============================================================================
# CLI
# =============================================================================
def main():
    p = argparse.ArgumentParser(description="Run a replay session to completion.")
    p.add_argument("--session-id", type=int, required=True)
    p.add_argument(
        "--no-sleep",
        action="store_true",
        help="Skip tick sleep — run as fast as possible (for debugging).",
    )
    args = p.parse_args()

    try:
        sys.exit(run_session(args.session_id, no_sleep=args.no_sleep))
    except KeyboardInterrupt:
        # Shouldn't reach here because of signal handler, but just in case
        logger.warning("KeyboardInterrupt")
        sys.exit(130)


if __name__ == "__main__":
    main()