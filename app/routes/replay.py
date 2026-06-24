# app/routes/replay.py
"""
Replay Simulator Routes
=======================

Commercial fixed version.

Important changes:
- UI aliases support both:
    /auth/replay/...
    /replay-simulator/...
- Start route creates a ReplaySession row, then queues Celery task:
    app.tasks.replay_tasks.start_replay_session_task
- Route does NOT directly spawn the replay process anymore.
- Replay lifecycle is isolated from stock_tasks.py.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app.database.connection import get_db
from app.models.replay import ReplayOpenTrade, ReplaySession, ReplayTradeHistory
from app.models.user import User
from app.routes.auth import get_current_user
from app.routes import auth as auth_routes
from app.scripts.replay.data_ingest import fetch_and_save, get_data_paths
from app.scripts.replay.replay_data_provider import ReplayDataProvider
from app.scripts.ml.model_refresh_policy import (
    DEFAULT_MIN_NEW_BARS_BEFORE_RETRAIN,
    DEFAULT_MODEL_MAX_AGE_MINUTES,
    DEFAULT_MODEL_REFRESH_MODE,
)
from app.services.replay_process import (
    pid_is_alive,
    purge_old_sessions,
    reap_stale_sessions,
    stop_session,
)
from app.utils.client_context import public_prefix

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

log = logging.getLogger("ReplayRoutes")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [ReplayRoutes] %(message)s"))
    log.addHandler(_h)
log.setLevel(logging.INFO)


# Commercial MM replay config: keep aligned with paper_trade_bot.py and
# app/scripts/stock_algos/Algo1_MM.py through Algo5_MM.py.
ALLOWED_MM_ALGOS = {
    "Algo1_MM": "Featureset_1",
    "Algo2_MM": "Featureset_2",
    "Algo3_MM": "Featureset_3",
    "Algo4_MM": "Featureset_4",
    "Algo5_MM": "Featureset_5",
    "Algo_SMI": "SMI",
    "Algo_MACD": "MACD",
}

DEFAULT_REPLAY_MM_CONFIG = {
    "long_entry_prob": 0.60,
    "short_entry_prob": 0.40,
    "prob_smoothing_bars": 3,
    "prob_trail_drop": 0.05,
    "prob_exit_mode": "trailing",
    "long_fixed_exit_prob": 0.40,
    "short_fixed_exit_prob": 0.60,
    "stop_loss_usd": 300.0,
    "hard_stop_usd": 300.0,
    "trailing_profit_usd": 75.0,
    "stop_loss_pct": 0.02,
    "trailing_profit_pct": 0.005,
    "builder_days": 10,
    "k_forward": 3,
    "model_refresh_mode": DEFAULT_MODEL_REFRESH_MODE,
    "model_max_age_minutes": DEFAULT_MODEL_MAX_AGE_MINUTES,
    "model_max_age_hours": DEFAULT_MODEL_MAX_AGE_MINUTES / 60.0,
    "min_new_bars_before_retrain": DEFAULT_MIN_NEW_BARS_BEFORE_RETRAIN,
    "daily_loss_limit_usd": 5000.0,
}
REPLAY_TRAIN_MIN_ROWS = 30


def _safe_float_form(value, default: float, min_value: float = 0.0) -> float:
    try:
        f = float(value)
    except Exception:
        f = float(default)
    if f < min_value:
        f = float(default)
    return f


def _checkbox_on(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _build_mm_replay_config(
    *,
    algo_name: str,
    eod_auto_close: str | None,
    allow_short_selling: str | None,
    stop_loss_usd: float | None,
    trailing_profit_usd: float | None,
    stop_loss_pct: float | None,
    trailing_profit_pct: float | None,
    prob_trail_drop: float | None,
    prob_exit_mode: str | None,
    long_fixed_exit_prob: float | None,
    short_fixed_exit_prob: float | None,
    long_entry_prob: float | None,
    short_entry_prob: float | None,
) -> dict:
    algo_name = (algo_name or "Algo1_MM").strip()
    if algo_name == "AlgoMM":
        algo_name = "Algo1_MM"
    if algo_name not in ALLOWED_MM_ALGOS:
        raise HTTPException(
            status_code=400,
            detail="Invalid algo selected. Choose an MM algo, Algo_SMI, or Algo_MACD.",
        )
    prob_exit_mode = str(prob_exit_mode or DEFAULT_REPLAY_MM_CONFIG["prob_exit_mode"]).strip().lower()
    if prob_exit_mode not in {"trailing", "fixed"}:
        prob_exit_mode = DEFAULT_REPLAY_MM_CONFIG["prob_exit_mode"]

    return {
        "algo_name": algo_name,
        "feature_set": ALLOWED_MM_ALGOS[algo_name],

        # Backward-compatible aliases for older Algo4 config readers.
        "long_threshold": _safe_float_form(
            long_entry_prob,
            DEFAULT_REPLAY_MM_CONFIG["long_entry_prob"],
        ),
        "short_threshold": _safe_float_form(
            short_entry_prob,
            DEFAULT_REPLAY_MM_CONFIG["short_entry_prob"],
        ),
        "long_exit_threshold": 0.55,
        "short_exit_threshold": 0.45,
        "min_prob_advantage": 0.0,
        "min_volume_multiplier": 0.0,
        "cooldown_sec": 0,
        "obv_slope_threshold": 0.0,

        # Exact production model-only probability engine.
        "prediction_strategy": "model_only",
        "long_entry_prob": _safe_float_form(
            long_entry_prob,
            DEFAULT_REPLAY_MM_CONFIG["long_entry_prob"],
        ),
        "short_entry_prob": _safe_float_form(
            short_entry_prob,
            DEFAULT_REPLAY_MM_CONFIG["short_entry_prob"],
        ),
        "prob_smoothing_bars": DEFAULT_REPLAY_MM_CONFIG["prob_smoothing_bars"],
        "prob_trail_drop": _safe_float_form(
            prob_trail_drop,
            DEFAULT_REPLAY_MM_CONFIG["prob_trail_drop"],
        ),
        "prob_exit_mode": prob_exit_mode,
        "long_fixed_exit_prob": _safe_float_form(
            long_fixed_exit_prob,
            DEFAULT_REPLAY_MM_CONFIG["long_fixed_exit_prob"],
        ),
        "short_fixed_exit_prob": _safe_float_form(
            short_fixed_exit_prob,
            DEFAULT_REPLAY_MM_CONFIG["short_fixed_exit_prob"],
        ),
        "builder_days": DEFAULT_REPLAY_MM_CONFIG["builder_days"],
        "k_forward": DEFAULT_REPLAY_MM_CONFIG["k_forward"],
        "model_refresh_mode": DEFAULT_REPLAY_MM_CONFIG["model_refresh_mode"],
        "model_max_age_minutes": DEFAULT_REPLAY_MM_CONFIG["model_max_age_minutes"],
        "model_max_age_hours": DEFAULT_REPLAY_MM_CONFIG["model_max_age_hours"],
        "min_new_bars_before_retrain": DEFAULT_REPLAY_MM_CONFIG["min_new_bars_before_retrain"],

        # Same user-set guardrails as production paper bot.
        "stop_loss_usd": _safe_float_form(
            stop_loss_usd,
            DEFAULT_REPLAY_MM_CONFIG["stop_loss_usd"],
        ),
        "hard_stop_usd": _safe_float_form(
            stop_loss_usd,
            DEFAULT_REPLAY_MM_CONFIG["stop_loss_usd"],
        ),
        "trailing_profit_usd": _safe_float_form(
            trailing_profit_usd,
            DEFAULT_REPLAY_MM_CONFIG["trailing_profit_usd"],
        ),
        "stop_loss_pct": _safe_float_form(
            stop_loss_pct,
            DEFAULT_REPLAY_MM_CONFIG["stop_loss_pct"],
        ),
        "per_share_stop_pct": _safe_float_form(
            stop_loss_pct,
            DEFAULT_REPLAY_MM_CONFIG["stop_loss_pct"],
        ),
        "trailing_profit_pct": _safe_float_form(
            trailing_profit_pct,
            DEFAULT_REPLAY_MM_CONFIG["trailing_profit_pct"],
        ),
        "per_share_trailing_profit_pct": _safe_float_form(
            trailing_profit_pct,
            DEFAULT_REPLAY_MM_CONFIG["trailing_profit_pct"],
        ),
        "eod_close": _checkbox_on(eod_auto_close),
        "allow_short": _checkbox_on(allow_short_selling),
        "allow_short_selling": _checkbox_on(allow_short_selling),

        # Replay controls only.
        "replay_train_min_rows": REPLAY_TRAIN_MIN_ROWS,
        "replay_training_warmup_days": DEFAULT_REPLAY_MM_CONFIG["builder_days"],
        "replay_force_retrain_each_bar": False,
        "daily_loss_limit_usd": DEFAULT_REPLAY_MM_CONFIG["daily_loss_limit_usd"],
        "once_per_bar": True,

        # Algo1-3 stay model-only; Algo4 keeps legacy production blockers above.
    }


# =============================================================================
# Helpers
# =============================================================================
def _pick_template(user: User) -> str:
    """Use TD variant for Schwab-allowed users (same pattern as paper_trade_bot)."""
    if getattr(user, "schwab_allowed", "N") == "Y":
        return "td_replay.html"
    return "replay.html"


MAX_RUNNING_REPLAY_SESSIONS = 10


def _user_running_sessions(db: Session, user_id: int) -> list[ReplaySession]:
    return (
        db.query(ReplaySession)
        .filter_by(user_id=user_id, status="RUNNING")
        .order_by(ReplaySession.id.desc())
        .all()
    )


def _user_has_running_session(db: Session, user_id: int) -> Optional[ReplaySession]:
    rows = _user_running_sessions(db, user_id)
    return rows[0] if rows else None


def _user_running_session_count(db: Session, user_id: int) -> int:
    return (
        db.query(ReplaySession)
        .filter_by(user_id=user_id, status="RUNNING")
        .count()
    )


def _serialize_session(s: ReplaySession) -> dict:
    return {
        "id": s.id,
        "symbol": s.symbol,
        "start_date": s.start_date,
        "end_date": s.end_date,
        "interval": s.interval,
        "algo_name": s.algo_name,
        "speed": float(s.speed or 1.0),
        "trade_size": float(s.trade_size or 0.0),
        "status": s.status,
        "current_bar_idx": s.current_bar_idx or 0,
        "total_bars": s.total_bars or 0,
        "current_bar_time": s.current_bar_time.isoformat() if s.current_bar_time else None,
        "pid": s.pid,
        "error_message": s.error_message,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "stopped_at": s.stopped_at.isoformat() if s.stopped_at else None,
    }


def _serialize_open_trade(t: ReplayOpenTrade) -> dict:
    return {
        "id": t.id,
        "session_id": t.session_id,
        "symbol": t.symbol,
        "position_side": t.position_side,
        "quantity": float(t.quantity or 0.0),
        "entry_price": float(t.entry_price or 0.0),
        "entry_time": t.entry_time.isoformat() if t.entry_time else None,
        "current_price": float(t.current_price or 0.0) if t.current_price else None,
        "unrealized_pl": float(t.unrealized_pl or 0.0) if t.unrealized_pl else None,
    }


def _serialize_hist(h: ReplayTradeHistory) -> dict:
    return {
        "id": h.id,
        "session_id": h.session_id,
        "symbol": h.symbol,
        "position_side": h.position_side,
        "quantity": float(h.quantity or 0.0),
        "entry_price": float(h.entry_price or 0.0),
        "entry_time": h.entry_time.isoformat() if h.entry_time else None,
        "exit_price": float(h.exit_price or 0.0),
        "exit_time": h.exit_time.isoformat() if h.exit_time else None,
        "profit_loss": float(h.profit_loss or 0.0),
        "exit_reason": h.exit_reason,
    }


def _session_belongs_to_user(sess: ReplaySession | None, user: User) -> bool:
    return bool(sess and sess.user_id == user.id)


def _safe_replay_housekeeping(db: Session) -> None:
    try:
        reap_stale_sessions(db)
        purge_old_sessions(db, days=7)
    except Exception as e:
        db.rollback()
        log.warning("Replay housekeeping failed: %s", e)


# =============================================================================
# Page
# =============================================================================
@router.get("/replay-simulator")
@router.get("/auth/replay", name="replay_simulator")
def replay_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Housekeeping on every page load
    _safe_replay_housekeeping(db)

    user_sessions = (
        db.query(ReplaySession)
        .filter_by(user_id=user.id)
        .order_by(ReplaySession.id.desc())
        .limit(20)
        .all()
    )

    running_sessions = (
        db.query(ReplaySession)
        .filter_by(user_id=user.id, status="RUNNING")
        .order_by(ReplaySession.id.desc())
        .all()
    )
    running = running_sessions[0] if running_sessions else None
    running_count = len(running_sessions)

    return templates.TemplateResponse(
        _pick_template(user),
        {
            "request": request,
            "user": user,
            "sessions": user_sessions,
            "has_running": running_count >= MAX_RUNNING_REPLAY_SESSIONS,
            "running_session": running,
            "running_sessions": running_sessions,
            "running_session_count": running_count,
            "max_running_replay_sessions": MAX_RUNNING_REPLAY_SESSIONS,
            "url_prefix": public_prefix(),
        },
    )


# =============================================================================
# Start / stop / delete
# =============================================================================
@router.post("/replay-simulator/start")
@router.post("/auth/replay/start")
def start_replay(
    symbol: str = Form(...),
    start_date: str = Form(...),     # YYYY-MM-DD
    end_date: str = Form(...),       # YYYY-MM-DD
    interval: str = Form(...),
    algo_name: str = Form("Algo1_MM"),
    speed: float = Form(1.0),
    trade_size: float = Form(100.0),
    stop_loss_usd: float = Form(DEFAULT_REPLAY_MM_CONFIG["stop_loss_usd"]),
    trailing_profit_usd: float = Form(DEFAULT_REPLAY_MM_CONFIG["trailing_profit_usd"]),
    stop_loss_pct: float = Form(DEFAULT_REPLAY_MM_CONFIG["stop_loss_pct"]),
    trailing_profit_pct: float = Form(DEFAULT_REPLAY_MM_CONFIG["trailing_profit_pct"]),
    prob_trail_drop: float = Form(DEFAULT_REPLAY_MM_CONFIG["prob_trail_drop"]),
    prob_exit_mode: str = Form(DEFAULT_REPLAY_MM_CONFIG["prob_exit_mode"]),
    long_fixed_exit_prob: float = Form(DEFAULT_REPLAY_MM_CONFIG["long_fixed_exit_prob"]),
    short_fixed_exit_prob: float = Form(DEFAULT_REPLAY_MM_CONFIG["short_fixed_exit_prob"]),
    long_entry_prob: float = Form(DEFAULT_REPLAY_MM_CONFIG["long_entry_prob"]),
    short_entry_prob: float = Form(DEFAULT_REPLAY_MM_CONFIG["short_entry_prob"]),
    allow_short_selling: str = Form(None),
    eod_auto_close: str = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    log.info(
        "[REPLAY_ROUTE] start_replay HIT user_id=%s symbol=%s interval=%s",
        getattr(user, "id", None),
        symbol,
        interval,
    )

    # 1) Normalize + validate
    symbol = (symbol or "").upper().strip()
    interval = (interval or "5min").strip().lower()
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")
    if interval not in ("1min", "5min", "10min", "15min", "30min", "1d"):
        raise HTTPException(status_code=400, detail=f"Unsupported interval: {interval}")

    algo_name = (algo_name or "Algo1_MM").strip()
    if algo_name == "AlgoMM":
        algo_name = "Algo1_MM"
    if algo_name not in ALLOWED_MM_ALGOS:
        raise HTTPException(status_code=400, detail="Choose an MM algo, Algo_SMI, or Algo_MACD")

    try:
        sd = datetime.strptime(start_date, "%Y-%m-%d").date()
        ed = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
    if ed < sd:
        raise HTTPException(status_code=400, detail="end_date must be >= start_date")

    # 2) Reap stale sessions. One-session enforcement is intentionally disabled.
    _safe_replay_housekeeping(db)

    # 3) Ensure data is available — auto-ingest if missing
    csv_path, _ = get_data_paths(user.id, symbol)
    if not csv_path.exists():
        log.info("[%s] no local replay data for user %s; ingesting", symbol, user.id)
        try:
            fetch_and_save(user_id=user.id, symbol=symbol, days=30, force=False)
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to fetch data for {symbol}: {e}",
            )
    else:
        # Ensure coverage of requested range; re-fetch if insufficient.
        try:
            ReplayDataProvider(
                user_id=user.id,
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                interval=interval,
            )
        except Exception as e:
            log.info("[%s] range check failed (%s); forcing re-ingest", symbol, e)
            try:
                fetch_and_save(user_id=user.id, symbol=symbol, days=30, force=True)
            except Exception as ee:
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to fetch data for {symbol}: {ee}",
                )

    # 4) Create session row
    session = ReplaySession(
        user_id=user.id,
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        interval=interval,
        algo_name=algo_name,
        speed=float(speed),
        trade_size=float(trade_size),
        status="PENDING",
        config_json=json.dumps(
            _build_mm_replay_config(
                algo_name=algo_name,
                eod_auto_close=eod_auto_close,
                allow_short_selling=allow_short_selling,
                stop_loss_usd=stop_loss_usd,
                trailing_profit_usd=trailing_profit_usd,
                stop_loss_pct=stop_loss_pct,
                trailing_profit_pct=trailing_profit_pct,
                prob_trail_drop=prob_trail_drop,
                prob_exit_mode=prob_exit_mode,
                long_fixed_exit_prob=long_fixed_exit_prob,
                short_fixed_exit_prob=short_fixed_exit_prob,
                long_entry_prob=long_entry_prob,
                short_entry_prob=short_entry_prob,
            ),
            separators=(",", ":"),
            sort_keys=True,
        ),
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    # 5) Queue replay worker task through Celery
    try:
        from app.tasks.replay_tasks import start_replay_session_task

        log.info("[REPLAY_ROUTE] queueing Celery replay session_id=%s", session.id)

        async_result = start_replay_session_task.apply_async(
            args=(session.id,),
            queue="replay",
        )

        log.info(
            "[REPLAY_ROUTE] queued Celery replay session_id=%s task_id=%s",
            session.id,
            async_result.id,
        )

        session.status = "QUEUED"
        session.pid = None
        session.error_message = None
        db.commit()
        db.refresh(session)

    except Exception as e:
        log.exception("[REPLAY_ROUTE] failed queueing replay session_id=%s", session.id)
        session.status = "ERROR"
        session.error_message = f"Failed to queue replay task: {str(e)[:500]}"
        db.commit()
        raise HTTPException(status_code=500, detail=f"Replay queue failed: {e}")

    return JSONResponse(
        {
            "ok": True,
            "session_id": session.id,
            "task_id": async_result.id,
            "status": session.status,
        }
    )


@router.post("/replay-simulator/stop/{session_id}")
@router.post("/auth/replay/stop/{session_id}")
def stop_replay(
    session_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sess = db.query(ReplaySession).filter_by(id=session_id).first()
    if not _session_belongs_to_user(sess, user):
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        from app.tasks.replay_tasks import stop_replay_session_task

        stop_replay_session_task.apply_async(args=(session_id,), queue="replay")
    except Exception:
        log.exception("[REPLAY_ROUTE] failed queueing stop task session_id=%s", session_id)

    # Also stop old PID-based runtime immediately for backward compatibility.
    try:
        stop_session(db, session_id)
    except Exception:
        log.exception("[REPLAY_ROUTE] stop_session failed session_id=%s", session_id)

    sess.status = "STOPPED"
    if hasattr(sess, "stopped_at"):
        sess.stopped_at = datetime.utcnow()
    db.commit()

    return JSONResponse({"ok": True})


@router.post("/replay-simulator/delete/{session_id}")
@router.post("/auth/replay/delete/{session_id}")
def delete_replay(
    session_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sess = db.query(ReplaySession).filter_by(id=session_id).first()
    if not _session_belongs_to_user(sess, user):
        raise HTTPException(status_code=404, detail="Session not found")
    if str(sess.status or "").upper() == "RUNNING":
        raise HTTPException(
            status_code=409,
            detail="Stop the session before deleting it",
        )

    try:
        db.query(ReplayOpenTrade).filter_by(session_id=session_id).delete(
            synchronize_session=False
        )
        db.query(ReplayTradeHistory).filter_by(session_id=session_id).delete(
            synchronize_session=False
        )
        db.query(ReplaySession).filter_by(id=session_id).delete(
            synchronize_session=False
        )
        db.commit()
        return JSONResponse({"ok": True})

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to delete replay session: {e}")


# =============================================================================
# JSON polling endpoints
# =============================================================================
@router.get("/replay-simulator/api/sessions")
@router.get("/auth/replay/api/sessions")
def api_list_sessions(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _safe_replay_housekeeping(db)
    rows = (
        db.query(ReplaySession)
        .filter_by(user_id=user.id)
        .order_by(ReplaySession.id.desc())
        .limit(20)
        .all()
    )
    return JSONResponse({"sessions": [_serialize_session(s) for s in rows]})


@router.get("/replay-simulator/api/state/{session_id}")
@router.get("/auth/replay/api/state/{session_id}")
def api_state(
    session_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sess = db.query(ReplaySession).filter_by(id=session_id).first()
    if not _session_belongs_to_user(sess, user):
        raise HTTPException(status_code=404, detail="Session not found")

    # Liveness sanity — only reap PID-based sessions.
    if sess.status == "RUNNING" and sess.pid and not pid_is_alive(sess.pid):
        _safe_replay_housekeeping(db)
        db.refresh(sess)

    closed_count = (
        db.query(ReplayTradeHistory)
        .filter_by(session_id=session_id)
        .count()
    )
    realized = (
        db.query(ReplayTradeHistory.profit_loss)
        .filter_by(session_id=session_id)
        .all()
    )
    total_pnl = float(sum((r[0] or 0.0) for r in realized))
    open_count = (
        db.query(ReplayOpenTrade)
        .filter_by(session_id=session_id)
        .count()
    )

    return JSONResponse(
        {
            "session": _serialize_session(sess),
            "closed_trades_count": closed_count,
            "open_trades_count": open_count,
            "realized_pnl": total_pnl,
        }
    )


@router.get("/replay-simulator/api/bars/{session_id}")
@router.get("/auth/replay/api/bars/{session_id}")
def api_bars(
    session_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Return OHLCV bars up to the current replay cursor — the visible bars.
    For a completed session, returns all bars.
    """
    sess = db.query(ReplaySession).filter_by(id=session_id).first()
    if not _session_belongs_to_user(sess, user):
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        provider = ReplayDataProvider(
            user_id=user.id,
            symbol=sess.symbol,
            start_date=sess.start_date,
            end_date=sess.end_date,
            interval=sess.interval,
        )
    except FileNotFoundError:
        return JSONResponse({"bars": [], "cursor": 0, "total": 0})
    except Exception as e:
        log.error("Provider failed for session %s: %s", session_id, e)
        return JSONResponse({"bars": [], "cursor": 0, "total": 0, "error": str(e)})

    cursor = sess.current_bar_idx or 0
    total = provider.total_bars

    # If session completed/stopped, show all bars through the last one processed.
    if sess.status in ("COMPLETED", "STOPPED", "ERROR"):
        cursor = min(cursor + 1, total - 1) if total else 0

    end_idx = min(cursor, total - 1) if total else -1
    if end_idx < 0:
        return JSONResponse({"bars": [], "cursor": 0, "total": total})

    sliced = provider.bars_up_to(end_idx)
    bars = [
        {
            "time": int(ts.timestamp()),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        }
        for ts, row in sliced.iterrows()
    ]
    return JSONResponse({"bars": bars, "cursor": end_idx, "total": total})


@router.get("/replay-simulator/api/trades/{session_id}")
@router.get("/auth/replay/api/trades/{session_id}")
def api_trades(
    session_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sess = db.query(ReplaySession).filter_by(id=session_id).first()
    if not _session_belongs_to_user(sess, user):
        raise HTTPException(status_code=404, detail="Session not found")

    opens = (
        db.query(ReplayOpenTrade)
        .filter_by(session_id=session_id)
        .order_by(ReplayOpenTrade.id.desc())
        .all()
    )
    hist = (
        db.query(ReplayTradeHistory)
        .filter_by(session_id=session_id)
        .order_by(ReplayTradeHistory.id.desc())
        .limit(200)
        .all()
    )

    return JSONResponse(
        {
            "open_trades": [_serialize_open_trade(t) for t in opens],
            "trade_history": [_serialize_hist(h) for h in hist],
        }
    )


# =============================================================================
# Manual ingest
# =============================================================================
@router.post("/replay-simulator/api/ingest")
@router.post("/auth/replay/api/ingest")
def api_ingest(
    symbol: str = Form(...),
    days: int = Form(30),
    force: bool = Form(False),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    symbol = (symbol or "").upper().strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol required")
    try:
        csv_path, meta = fetch_and_save(
            user_id=user.id,
            symbol=symbol,
            days=int(days),
            force=bool(force),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    return JSONResponse(
        {
            "ok": True,
            "csv_path": str(csv_path),
            "meta": {
                "total_rows": meta.total_rows,
                "first_bar": meta.first_bar,
                "last_bar": meta.last_bar,
                "unique_dates": meta.unique_dates,
                "downloaded_at": meta.downloaded_at,
            },
        }
    )
