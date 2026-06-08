# /var/stockwicks/clients/ashakil/app/routes/replay.py
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
from app.services.backtest_cheatsheet_service import CheatSheetRequest, run_cheatsheet
from app.services.replay_process import (
    pid_is_alive,
    purge_old_sessions,
    reap_stale_sessions,
    stop_session,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

log = logging.getLogger("ReplayRoutes")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [ReplayRoutes] %(message)s"))
    log.addHandler(_h)
log.setLevel(logging.INFO)


# Commercial MM replay config: keep aligned with paper_trade_bot.py and
# app/scripts/stock_algos/Algo1_MM.py / Algo2_MM.py / Algo3_MM.py / Algo4_MM.py / Algo5_MM.py.
ALLOWED_MM_ALGOS = {
    "Algo1_MM": "Featureset_1",
    "Algo2_MM": "Featureset_2",
    "Algo3_MM": "Featureset_3",
    "Algo4_MM": "Featureset_4",
    "Algo5_MM": "Featureset_5",
}

DEFAULT_REPLAY_MM_CONFIG = {
    "long_entry_prob": 0.60,
    "short_entry_prob": 0.40,
    "prob_smoothing_bars": 3,
    "prob_trail_drop": 0.05,
    "prob_exit_mode": "trailing",
    "long_fixed_exit_prob": 0.55,
    "short_fixed_exit_prob": 0.55,
    "hard_stop_usd": 300.0,
    "per_share_stop_pct": 0.01,
    "trailing_stop_activation": 75.0,
    "trailing_stop_distance": 35.0,
    "force_retrain_each_tick": True,
    "builder_days": 30,
    "k_forward": 3,
    "model_max_age_hours": 0.25,
    "daily_loss_limit_usd": 5000.0,
    "replay_train_min_rows": 30,
    "replay_training_warmup_days": 45,
}


def _safe_float_form(value, default: float, min_value: float = 0.0) -> float:
    try:
        f = float(value)
    except Exception:
        f = float(default)
    if f < min_value:
        f = float(default)
    return f


def _safe_int_form(value, default: int, min_value: int = 0) -> int:
    try:
        i = int(float(value))
    except Exception:
        i = int(default)
    if i < min_value:
        i = int(default)
    return i


def _checkbox_on(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _build_mm_replay_config(
    *,
    algo_name: str,
    eod_auto_close: str | None,
    allow_short_selling: str | None,
    hard_stop_usd: float | None,
    per_share_stop_pct: float | None,
    trailing_stop_activation: float | None,
    trailing_stop_distance: float | None,
    prob_trail_drop: float | None,
    prob_exit_mode: str | None,
    long_fixed_exit_prob: float | None,
    short_fixed_exit_prob: float | None,
    long_entry_prob: float | None,
    short_entry_prob: float | None,
    replay_train_min_rows: int | None,
) -> dict:
    algo_name = (algo_name or "Algo1_MM").strip()
    if algo_name == "AlgoMM":
        algo_name = "Algo1_MM"
    if algo_name not in ALLOWED_MM_ALGOS:
        raise HTTPException(
            status_code=400,
            detail="Invalid algo selected. Choose Algo1_MM, Algo2_MM, Algo3_MM, Algo4_MM, or Algo5_MM.",
        )
    is_algo4 = algo_name == "Algo4_MM"
    prob_exit_mode = str(prob_exit_mode or DEFAULT_REPLAY_MM_CONFIG["prob_exit_mode"]).strip().lower()
    if prob_exit_mode not in {"trailing", "fixed"}:
        prob_exit_mode = DEFAULT_REPLAY_MM_CONFIG["prob_exit_mode"]

    return {
        "algo_name": algo_name,
        "feature_set": ALLOWED_MM_ALGOS[algo_name],

        # Algo4_MM legacy production probability gates. Algo1-3 ignore these.
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
        "min_prob_advantage": 0.03 if is_algo4 else 0.0,
        "min_volume_multiplier": 0.1 if is_algo4 else 0.0,
        "cooldown_sec": 60 if is_algo4 else 0,
        "obv_slope_threshold": 0.1 if is_algo4 else 0.0,

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
        "model_max_age_hours": DEFAULT_REPLAY_MM_CONFIG["model_max_age_hours"],

        # Same user-set guardrails as production paper bot.
        "hard_stop_usd": _safe_float_form(
            hard_stop_usd,
            DEFAULT_REPLAY_MM_CONFIG["hard_stop_usd"],
        ),
        "per_share_stop_pct": _safe_float_form(
            per_share_stop_pct,
            DEFAULT_REPLAY_MM_CONFIG["per_share_stop_pct"],
        ),
        "trailing_stop_activation": _safe_float_form(
            trailing_stop_activation,
            DEFAULT_REPLAY_MM_CONFIG["trailing_stop_activation"],
        ),
        "trailing_stop_distance": _safe_float_form(
            trailing_stop_distance,
            DEFAULT_REPLAY_MM_CONFIG["trailing_stop_distance"],
        ),
        "eod_close": _checkbox_on(eod_auto_close),
        "allow_short": _checkbox_on(allow_short_selling),
        "allow_short_selling": _checkbox_on(allow_short_selling),

        # Replay controls only.
        "replay_train_min_rows": _safe_int_form(
            replay_train_min_rows,
            DEFAULT_REPLAY_MM_CONFIG["replay_train_min_rows"],
            min_value=1,
        ),
        "replay_training_warmup_days": DEFAULT_REPLAY_MM_CONFIG["replay_training_warmup_days"],
        "force_retrain_each_tick": True,
        "replay_force_retrain_each_bar": True,
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


def _session_config_dict(s: ReplaySession) -> dict:
    raw = getattr(s, "config_json", None)
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _serialize_session(s: ReplaySession) -> dict:
    cfg = _session_config_dict(s)
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
        "config": {
            "hard_stop_usd": float(cfg.get("hard_stop_usd", DEFAULT_REPLAY_MM_CONFIG["hard_stop_usd"])),
            "per_share_stop_pct": float(
                cfg.get("per_share_stop_pct", DEFAULT_REPLAY_MM_CONFIG["per_share_stop_pct"])
            ),
            "trailing_stop_activation": float(
                cfg.get("trailing_stop_activation", DEFAULT_REPLAY_MM_CONFIG["trailing_stop_activation"])
            ),
            "trailing_stop_distance": float(
                cfg.get("trailing_stop_distance", DEFAULT_REPLAY_MM_CONFIG["trailing_stop_distance"])
            ),
            "long_entry_prob": float(cfg.get("long_entry_prob", DEFAULT_REPLAY_MM_CONFIG["long_entry_prob"])),
            "short_entry_prob": float(cfg.get("short_entry_prob", DEFAULT_REPLAY_MM_CONFIG["short_entry_prob"])),
            "prob_exit_mode": str(cfg.get("prob_exit_mode", DEFAULT_REPLAY_MM_CONFIG["prob_exit_mode"])),
            "prob_trail_drop": float(cfg.get("prob_trail_drop", DEFAULT_REPLAY_MM_CONFIG["prob_trail_drop"])),
            "long_fixed_exit_prob": float(
                cfg.get(
                    "long_fixed_exit_prob",
                    cfg.get("prob_fixed_exit_prob", DEFAULT_REPLAY_MM_CONFIG["long_fixed_exit_prob"]),
                )
            ),
            "short_fixed_exit_prob": float(
                cfg.get(
                    "short_fixed_exit_prob",
                    cfg.get("prob_fixed_exit_prob", DEFAULT_REPLAY_MM_CONFIG["short_fixed_exit_prob"]),
                )
            ),
            "replay_train_min_rows": int(
                cfg.get("replay_train_min_rows", DEFAULT_REPLAY_MM_CONFIG["replay_train_min_rows"])
            ),
            "interval": s.interval,
            "algo_name": s.algo_name,
        },
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
    try:
        reap_stale_sessions(db)
        purge_old_sessions(db, days=7)
    except Exception as e:
        log.warning("Housekeeping failed: %s", e)

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
        request=request,
        name=_pick_template(user),
        context={
            "request": request,
            "user": user,
            "sessions": user_sessions,
            "has_running": running_count >= MAX_RUNNING_REPLAY_SESSIONS,
            "running_session": running,
            "running_sessions": running_sessions,
            "running_session_count": running_count,
            "max_running_replay_sessions": MAX_RUNNING_REPLAY_SESSIONS,
            "url_prefix": os.getenv("CLIENT_PUBLIC_PREFIX", "/clients/ashakil"),
        },
    )


@router.get("/analysis/cheatsheet")
@router.get("/auth/backtest-cheatsheet")
def backtest_cheatsheet_page(
    request: Request,
    user: User = Depends(get_current_user),
):
    return templates.TemplateResponse(
        request=request,
        name="backtest_cheatsheet.html",
        context={
            "request": request,
            "user": user,
            "url_prefix": os.getenv("CLIENT_PUBLIC_PREFIX", "/clients/ashakil"),
        },
    )


@router.post("/analysis/cheatsheet/api/run")
@router.post("/auth/backtest-cheatsheet/api/run")
def run_backtest_cheatsheet(
    symbol: str = Form(...),
    intervals: str = Form("5min"),
    trade_size: float = Form(100.0),
    builder_days: int = Form(DEFAULT_REPLAY_MM_CONFIG["builder_days"]),
    k_forward: int = Form(DEFAULT_REPLAY_MM_CONFIG["k_forward"]),
    profile: str = Form("quick"),
    allow_short_selling: str = Form("on"),
    eod_auto_close: str = Form("on"),
    user: User = Depends(get_current_user),
):
    symbol = (symbol or "").upper().strip()
    parsed_intervals = tuple(
        i.strip().lower()
        for i in str(intervals or "5min").replace(";", ",").split(",")
        if i.strip()
    )
    allowed_intervals = {"1min", "5min", "10min", "15min", "30min", "1d"}
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")
    if not parsed_intervals or any(i not in allowed_intervals for i in parsed_intervals):
        raise HTTPException(status_code=400, detail="Choose one or more supported intervals")

    try:
        result = run_cheatsheet(
            CheatSheetRequest(
                symbol=symbol,
                intervals=parsed_intervals,
                trade_size=max(float(trade_size or 1.0), 1.0),
                builder_days=max(int(builder_days or DEFAULT_REPLAY_MM_CONFIG["builder_days"]), 10),
                k_forward=max(int(k_forward or DEFAULT_REPLAY_MM_CONFIG["k_forward"]), 1),
                profile=str(profile or "quick").lower(),
                allow_short=_checkbox_on(allow_short_selling),
                eod_close=_checkbox_on(eod_auto_close),
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        log.exception("[CHEATSHEET] failed for user_id=%s symbol=%s", getattr(user, "id", None), symbol)
        raise HTTPException(status_code=500, detail=f"Cheat sheet failed: {exc}")

    return JSONResponse(result)


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
    hard_stop_usd: float = Form(DEFAULT_REPLAY_MM_CONFIG["hard_stop_usd"]),
    per_share_stop_pct: float = Form(DEFAULT_REPLAY_MM_CONFIG["per_share_stop_pct"]),
    trailing_stop_activation: float = Form(DEFAULT_REPLAY_MM_CONFIG["trailing_stop_activation"]),
    trailing_stop_distance: float = Form(DEFAULT_REPLAY_MM_CONFIG["trailing_stop_distance"]),
    prob_trail_drop: float = Form(DEFAULT_REPLAY_MM_CONFIG["prob_trail_drop"]),
    prob_exit_mode: str = Form(DEFAULT_REPLAY_MM_CONFIG["prob_exit_mode"]),
    long_fixed_exit_prob: float = Form(DEFAULT_REPLAY_MM_CONFIG["long_fixed_exit_prob"]),
    short_fixed_exit_prob: float = Form(DEFAULT_REPLAY_MM_CONFIG["short_fixed_exit_prob"]),
    long_entry_prob: float = Form(DEFAULT_REPLAY_MM_CONFIG["long_entry_prob"]),
    short_entry_prob: float = Form(DEFAULT_REPLAY_MM_CONFIG["short_entry_prob"]),
    replay_train_min_rows: int = Form(DEFAULT_REPLAY_MM_CONFIG["replay_train_min_rows"]),
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
        raise HTTPException(status_code=400, detail="Choose Algo1_MM, Algo2_MM, Algo3_MM, Algo4_MM, or Algo5_MM")

    try:
        sd = datetime.strptime(start_date, "%Y-%m-%d").date()
        ed = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
    if ed < sd:
        raise HTTPException(status_code=400, detail="end_date must be >= start_date")

    # 2) Reap stale sessions. One-session enforcement is intentionally disabled.
    reap_stale_sessions(db)

    # 3) Ensure data is available — auto-ingest if missing
    csv_path, _ = get_data_paths(user.id, symbol)
    try:
        fetch_and_save(user_id=user.id, symbol=symbol, days=30, force=False)
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch data for {symbol}: {e}",
        )

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
                hard_stop_usd=hard_stop_usd,
                per_share_stop_pct=per_share_stop_pct,
                trailing_stop_activation=trailing_stop_activation,
                trailing_stop_distance=trailing_stop_distance,
                prob_trail_drop=prob_trail_drop,
                prob_exit_mode=prob_exit_mode,
                long_fixed_exit_prob=long_fixed_exit_prob,
                short_fixed_exit_prob=short_fixed_exit_prob,
                long_entry_prob=long_entry_prob,
                short_entry_prob=short_entry_prob,
                replay_train_min_rows=replay_train_min_rows,
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


@router.post("/replay-simulator/run-live/{session_id}")
@router.post("/auth/replay/run-live/{session_id}")
def run_replay_as_live_bot(
    session_id: int,
    mirror_live: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sess = db.query(ReplaySession).filter_by(id=session_id).first()
    if not _session_belongs_to_user(sess, user):
        raise HTTPException(status_code=404, detail="Session not found")

    algo_name = (sess.algo_name or "").strip()
    if algo_name == "AlgoMM":
        algo_name = "Algo1_MM"
    if algo_name not in ALLOWED_MM_ALGOS:
        raise HTTPException(status_code=400, detail="Replay algo cannot be run as a live bot")

    from app.models.paper_trading_bot import PaperStockTradeBot

    existing = (
        db.query(PaperStockTradeBot)
        .filter_by(user_id=user.id, symbol=sess.symbol, is_active=True)
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Active live bot #{existing.id} already exists for {sess.symbol}. Stop it first.",
        )

    cfg = _session_config_dict(sess)
    cfg.update(
        {
            "algo_name": algo_name,
            "feature_set": ALLOWED_MM_ALGOS[algo_name],
            "force_retrain_each_tick": True,
        }
    )

    bot = PaperStockTradeBot(
        user_id=user.id,
        symbol=sess.symbol,
        interval=sess.interval,
        algo_name=algo_name,
        trade_size=float(sess.trade_size or 1.0),
        quantity=int(float(sess.trade_size or 1.0)),
        notify_email=False,
        allow_short_selling=bool(cfg.get("allow_short_selling", cfg.get("allow_short", True))),
        eod_auto_close=bool(cfg.get("eod_close", True)),
        mirror_live=_checkbox_on(mirror_live),
        is_active=True,
        status="RUNNING",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    bot.config_json = json.dumps(cfg, separators=(",", ":"), sort_keys=True)

    db.add(bot)
    db.commit()
    db.refresh(bot)

    log.info(
        "[REPLAY_ROUTE] replay session_id=%s copied to live bot_id=%s symbol=%s algo=%s",
        session_id,
        bot.id,
        bot.symbol,
        bot.algo_name,
    )

    return JSONResponse({"ok": True, "bot_id": bot.id})


# =============================================================================
# JSON polling endpoints
# =============================================================================
@router.get("/replay-simulator/api/sessions")
@router.get("/auth/replay/api/sessions")
def api_list_sessions(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    reap_stale_sessions(db)
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
        reap_stale_sessions(db)
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
