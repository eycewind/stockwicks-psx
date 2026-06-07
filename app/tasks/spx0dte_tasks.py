# app/tasks/spx0dte_tasks.py
"""
Celery tasks for the SPX 0DTE bot.

Beat schedule references four task names (see app/celery_worker.py):
  • options.run_spx0dte_tick
  • options.spx0dte_update_open_trades_prices
  • options.spx0dte_pnl_exit_open_trades
  • options.spx0dte_eod_close_open_trades

In v4 the bot has a SINGLE unified manage loop that handles:
  - mark/bid updates
  - TP1 hits
  - lock-in stop raises
  - trailing stop arming + updates
  - bid-based stop loss exits
  - EOD exits

So the three "manage-style" tasks all delegate to the same function.
That function is idempotent — running it multiple times in the same minute
is safe.
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from datetime import datetime, time as dtime
from typing import Any, Dict, Optional

import pytz
from celery import shared_task

# Ensure project root is on sys.path so imports from `app.*` work in workers.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "../.."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Default user_id (the shared paper bot account)
DEFAULT_USER_ID = int(os.getenv("SPX0DTE_BOT_USER_ID", "116"))
DEFAULT_MAX_RISK = float(os.getenv("SPX0DTE_MAX_RISK", "100"))
ET = pytz.timezone("US/Eastern")

log = logging.getLogger("SPX0DTE.tasks")
if not log.handlers:
    log.setLevel(logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
    log.addHandler(sh)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _now_et() -> datetime:
    return datetime.now(ET)


def _is_market_hours_et() -> bool:
    """Returns True if currently within regular SPX option market hours (Mon-Fri, 9:30-16:00 ET)."""
    now = _now_et()
    if now.weekday() >= 5:  # Sat=5, Sun=6
        return False
    t = now.time()
    return dtime(9, 30) <= t <= dtime(16, 0)


def _safe_call(fn_name: str, fn, *args, **kwargs) -> Dict[str, Any]:
    """Wrap a callable so any exception is caught and returned as JSON-safe dict."""
    try:
        result = fn(*args, **kwargs)
        if isinstance(result, dict):
            return result
        return {"ok": True, "result": str(result)}
    except Exception as e:
        log.exception("[SPX0DTE.tasks] %s failed: %s", fn_name, e)
        return {
            "ok": False,
            "error": str(e),
            "task": fn_name,
            "traceback": traceback.format_exc(limit=5),
        }


# ─── Task 1: Generate signals (entry side) ────────────────────────────────────

@shared_task(name="options.run_spx0dte_tick")
def run_spx0dte_tick(user_id: Optional[int] = None,
                     max_risk: Optional[float] = None,
                     interval_minutes: Optional[int] = None) -> Dict[str, Any]:
    """
    Fires on the beat schedule (every minute).
    Looks for a trade entry signal; if found, opens a paper trade.
    """
    if not _is_market_hours_et():
        return {"ok": True, "skipped": True, "reason": "outside_market_hours_et"}

    uid = int(user_id if user_id is not None else DEFAULT_USER_ID)
    risk = float(max_risk if max_risk is not None else DEFAULT_MAX_RISK)

    from app.scripts.options.spx_0dte_bot_runner import run_spx0dte_tick as _run

    return _safe_call(
        "run_spx0dte_tick",
        _run,
        user_id=uid,
        max_risk=risk,
        interval_minutes=interval_minutes,
    )


# ─── Task 2: Update marks + manage trades (the v4 unified loop) ───────────────

@shared_task(name="options.spx0dte_update_open_trades_prices")
def spx0dte_update_open_trades_prices(user_id: Optional[int] = None) -> Dict[str, Any]:
    """
    v4: this is the unified manage loop. Updates marks, checks TP/SL,
    arms trail, applies lock-in. Idempotent — safe to call as often as desired.
    """
    if not _is_market_hours_et():
        return {"ok": True, "skipped": True, "reason": "outside_market_hours_et"}

    uid = int(user_id if user_id is not None else DEFAULT_USER_ID)

    from app.scripts.options.spx_0dte_bot_runner import manage_spx0dte_open_trades

    return _safe_call(
        "spx0dte_update_open_trades_prices",
        manage_spx0dte_open_trades,
        user_id=uid,
    )


# ─── Task 3: P/L exit enforcement (same v4 loop, defensive duplicate) ─────────

@shared_task(name="options.spx0dte_pnl_exit_open_trades")
def spx0dte_pnl_exit_open_trades(user_id: Optional[int] = None) -> Dict[str, Any]:
    """
    Defensive duplicate of the manage loop. v4 doesn't separate "update marks"
    from "exit enforcement" — both are a single idempotent pass — so this just
    calls the same function. If a tick was missed in task 2 due to a worker
    hiccup, this gives us a safety net.
    """
    if not _is_market_hours_et():
        return {"ok": True, "skipped": True, "reason": "outside_market_hours_et"}

    uid = int(user_id if user_id is not None else DEFAULT_USER_ID)

    from app.scripts.options.spx_0dte_bot_runner import manage_spx0dte_open_trades

    return _safe_call(
        "spx0dte_pnl_exit_open_trades",
        manage_spx0dte_open_trades,
        user_id=uid,
    )


# ─── Task 4: EOD close (only fires once at 15:35 ET) ──────────────────────────

@shared_task(name="options.spx0dte_eod_close_open_trades")
def spx0dte_eod_close_open_trades(user_id: Optional[int] = None) -> Dict[str, Any]:
    """
    Fires once near end of day. Calls the same manage loop — when the time
    is past EOD_EXIT_TIME (15:30 in v4), it'll close any remaining open
    trades with reason='EOD'.
    """
    uid = int(user_id if user_id is not None else DEFAULT_USER_ID)

    from app.scripts.options.spx_0dte_bot_runner import manage_spx0dte_open_trades

    return _safe_call(
        "spx0dte_eod_close_open_trades",
        manage_spx0dte_open_trades,
        user_id=uid,
    )