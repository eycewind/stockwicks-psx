import json
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from typing import Literal

from app.database.connection import get_db
from app.models.replay import ReplaySession
from app.routes.auth import get_current_user
from app.modules.replay.routes import (
    ALLOWED_MM_ALGOS,
    DEFAULT_REPLAY_MM_CONFIG,
    _build_mm_replay_config,
)
from app.scripts.replay.data_ingest import fetch_and_save
from app.scripts.replay.replay_data_provider import ReplayDataProvider
from app.services.replay_process import stop_session
from app.trading.sparkie import (
    AgentLaunchRequest,
    GoalRequest,
    SparkiePerformanceRequest,
    assess_goal_feasibility,
    build_agent_launch_plan,
    build_performance_preview,
)
from sqlalchemy.orm import Session


router = APIRouter(prefix="/api/sparkie", tags=["sparkie"])
page_router = APIRouter(tags=["sparkie"])
templates = Jinja2Templates(directory="app/templates")
SPARKIE_STALE_MINUTES = 12


class GoalFeasibilityRequest(BaseModel):
    account_equity: float = Field(..., gt=0)
    target_profit: float = Field(..., gt=0)
    target_period: Literal["daily", "weekly", "monthly"] = "daily"
    confidence_level: float = Field(0.60, ge=0.50, le=0.95)


class SparkieLaunchRequest(GoalFeasibilityRequest):
    requested_mode: Literal["paper", "live_mirror"] = "paper"
    acknowledged_live_risk: bool = False


class SparkiePerformanceApiRequest(GoalFeasibilityRequest):
    start_date: str | None = None
    end_date: str | None = None
    lookback_days: int | None = Field(None, gt=0)
    symbols: list[str] = Field(default_factory=list)
    intervals: list[str] = Field(default_factory=list)
    algos: list[str] = Field(default_factory=list)


class SparkieEvaluationRequest(SparkiePerformanceApiRequest):
    max_sessions: int = Field(12, ge=1, le=30)


@page_router.get("/auth/sparkie", response_class=HTMLResponse)
def sparkie_page(
    request: Request,
    user=Depends(get_current_user),
):
    return templates.TemplateResponse(
        request,
        "sparkie/index.html",
        {
            "request": request,
            "user": user,
            "title": "Sparkie Agent",
        },
    )


@router.post("/goal-feasibility")
def goal_feasibility(
    payload: GoalFeasibilityRequest,
    _user=Depends(get_current_user),
):
    try:
        result = assess_goal_feasibility(
            GoalRequest(
                account_equity=payload.account_equity,
                target_profit=payload.target_profit,
                target_period=payload.target_period,
                confidence_level=payload.confidence_level,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return result.to_dict()


@router.post("/performance-preview")
def performance_preview(
    payload: SparkiePerformanceApiRequest,
    _user=Depends(get_current_user),
):
    try:
        result = build_performance_preview(
            SparkiePerformanceRequest(
                account_equity=payload.account_equity,
                target_profit=payload.target_profit,
                target_period=payload.target_period,
                start_date=_parse_date(payload.start_date, "start_date"),
                end_date=_parse_date(payload.end_date, "end_date"),
                lookback_days=payload.lookback_days,
                symbols=tuple(payload.symbols or ()),
                intervals=tuple(payload.intervals or ()),
                algos=tuple(payload.algos or ()),
                confidence_level=payload.confidence_level,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return result.to_dict()


@router.post("/run-evaluation")
def run_evaluation(
    payload: SparkieEvaluationRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        preview = build_performance_preview(
            SparkiePerformanceRequest(
                account_equity=payload.account_equity,
                target_profit=payload.target_profit,
                target_period=payload.target_period,
                start_date=_parse_date(payload.start_date, "start_date"),
                end_date=_parse_date(payload.end_date, "end_date"),
                lookback_days=payload.lookback_days,
                symbols=tuple(payload.symbols or ()),
                intervals=tuple(payload.intervals or ()),
                algos=tuple(payload.algos or ()),
                confidence_level=payload.confidence_level,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not preview.replay_ready:
        return {
            "ok": False,
            "status": "blocked",
            "message": preview.message,
            "preview": preview.to_dict(),
            "sessions": [],
        }

    symbols = _clean_symbols(payload.symbols)
    intervals = _clean_intervals(payload.intervals)
    algos = _clean_algos(payload.algos)
    combos = [
        (symbol, interval, algo)
        for symbol in symbols
        for interval in intervals
        for algo in algos
    ][: payload.max_sessions]

    if not combos:
        raise HTTPException(status_code=400, detail="Enter at least one symbol, interval, and algo.")

    data_errors = _prepare_replay_data(
        user_id=user.id,
        symbols=symbols,
        intervals=intervals,
        start_date=preview.start_date,
        end_date=preview.end_date,
    )
    runnable_symbols = [symbol for symbol in symbols if symbol not in data_errors]
    combos = [
        (symbol, interval, algo)
        for symbol, interval, algo in combos
        if symbol in runnable_symbols
    ]
    skipped_sessions = [
        {
            "session_id": None,
            "symbol": symbol,
            "interval": ",".join(intervals),
            "algo_name": ",".join(algos),
            "status": "DATA_ERROR",
            "error": error,
        }
        for symbol, error in data_errors.items()
    ]

    if not combos:
        return {
            "ok": False,
            "status": "blocked",
            "job_id": None,
            "message": "Sparkie could not prepare replay data for the selected symbols.",
            "preview": preview.to_dict(),
            "sessions": skipped_sessions,
        }

    trade_size = _sparkie_trade_size(payload.account_equity)
    sessions = []
    job_id = f"sparkie-{user.id}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

    for symbol, interval, algo_name in combos:
        cfg = _sparkie_replay_config(
            algo_name=algo_name,
            account_equity=payload.account_equity,
            target_profit=payload.target_profit,
            target_period=payload.target_period,
        )
        cfg["sparkie_job_id"] = job_id
        cfg["sparkie_target_profit"] = float(payload.target_profit)
        cfg["sparkie_target_period"] = payload.target_period
        cfg["sparkie_account_equity"] = float(payload.account_equity)
        cfg["sparkie_confidence_level"] = float(payload.confidence_level)

        session = ReplaySession(
            user_id=user.id,
            symbol=symbol,
            start_date=preview.start_date,
            end_date=preview.end_date,
            interval=interval,
            algo_name=algo_name,
            speed=20.0,
            trade_size=trade_size,
            status="PENDING",
            config_json=json.dumps(cfg, separators=(",", ":"), sort_keys=True),
        )
        db.add(session)
        db.flush()

        sessions.append(
            {
                "session_id": session.id,
                "symbol": symbol,
                "interval": interval,
                "algo_name": algo_name,
                "status": "PENDING",
            }
        )

    db.commit()

    queued = []
    for session in sessions:
        try:
            from app.tasks.replay_tasks import start_replay_session_task

            async_result = start_replay_session_task.apply_async(
                args=(session["session_id"],),
                queue="replay",
            )
            row = db.query(ReplaySession).filter_by(id=session["session_id"]).first()
            if row:
                row.status = "QUEUED"
                db.commit()
            session["status"] = "QUEUED"
            session["task_id"] = async_result.id
            queued.append(session)
        except Exception as exc:
            row = db.query(ReplaySession).filter_by(id=session["session_id"]).first()
            if row:
                row.status = "ERROR"
                row.error_message = f"Sparkie queue failed: {str(exc)[:400]}"
                db.commit()
            session["status"] = "ERROR"
            session["error"] = str(exc)
            queued.append(session)

    return {
        "ok": True,
        "status": "queued",
        "job_id": job_id,
        "message": "Sparkie queued replay sessions. It will compare results after the replay workers finish.",
        "preview": preview.to_dict(),
        "sessions": skipped_sessions + queued,
        "next_step": "Wait for sessions to complete, then review Sparkie results before considering Live Mirror.",
    }


@router.get("/evaluation-status/{job_id}")
def evaluation_status(
    job_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not re.fullmatch(r"sparkie-\d+-\d{14}", job_id or ""):
        raise HTTPException(status_code=400, detail="Invalid Sparkie job id.")

    rows = (
        db.query(ReplaySession)
        .filter(ReplaySession.user_id == user.id)
        .filter(ReplaySession.config_json.like(f'%"sparkie_job_id":"{job_id}"%'))
        .order_by(ReplaySession.id.desc())
        .all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Sparkie evaluation job not found.")

    _mark_stale_sparkie_sessions(db, rows)
    for row in rows:
        db.refresh(row)

    preview = _sparkie_preview_from_sessions(rows)
    sessions = []
    completed = 0
    total_profit = 0.0
    total_trades = 0
    for row in rows:
        session_profit, trade_count = _replay_session_pnl(db, row.id)
        total_profit += session_profit
        total_trades += trade_count
        if str(row.status or "").upper() in {"COMPLETED", "STOPPED", "ERROR"}:
            completed += 1
        sessions.append(
            {
                "session_id": row.id,
                "symbol": row.symbol,
                "interval": row.interval,
                "algo_name": row.algo_name,
                "status": row.status,
                "profit_loss": round(session_profit, 2),
                "trade_count": trade_count,
                "error_message": row.error_message,
                "current_bar_idx": row.current_bar_idx or 0,
                "total_bars": row.total_bars or 0,
                "age_minutes": _session_age_minutes(row),
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        )

    status = "completed" if completed == len(rows) else "running"
    best_session = _best_sparkie_session(sessions)
    percent_complete = round((completed / len(rows)) * 100.0, 1) if rows else 0.0
    return {
        "ok": True,
        "job_id": job_id,
        "status": status,
        "completed_sessions": completed,
        "total_sessions": len(rows),
        "percent_complete": percent_complete,
        "total_profit_loss": round(total_profit, 2),
        "total_trades": total_trades,
        "best_session": best_session,
        "preview": preview,
        "sessions": sessions,
        "next_step": _sparkie_status_next_step(status, total_profit, total_trades),
    }


@router.post("/stop-evaluation/{job_id}")
def stop_evaluation(
    job_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not re.fullmatch(r"sparkie-\d+-\d{14}", job_id or ""):
        raise HTTPException(status_code=400, detail="Invalid Sparkie job id.")

    rows = (
        db.query(ReplaySession)
        .filter(ReplaySession.user_id == user.id)
        .filter(ReplaySession.config_json.like(f'%"sparkie_job_id":"{job_id}"%'))
        .all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Sparkie evaluation job not found.")

    stopped = 0
    for row in rows:
        if str(row.status or "").upper() in {"COMPLETED", "STOPPED", "ERROR"}:
            continue
        try:
            stop_session(db, int(row.id))
        except Exception:
            row.status = "STOPPED"
            row.error_message = "Sparkie stop requested."
            row.updated_at = datetime.utcnow()
            db.commit()
        stopped += 1

    return {
        "ok": True,
        "job_id": job_id,
        "stopped_sessions": stopped,
        "message": "Sparkie stop requested for active replay sessions.",
    }


def _parse_date(value: str | None, field_name: str):
    if not value:
        return None
    try:
        from datetime import date

        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be YYYY-MM-DD") from exc


def _clean_symbols(values: list[str]) -> list[str]:
    symbols = []
    for value in values or []:
        symbol = str(value or "").upper().strip()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols[:20]


def _clean_intervals(values: list[str]) -> list[str]:
    allowed = {"1min", "5min", "10min", "15min", "30min", "1d"}
    intervals = []
    for value in values or []:
        interval = str(value or "").strip().lower()
        if interval in allowed and interval not in intervals:
            intervals.append(interval)
    return intervals or ["5min"]


def _clean_algos(values: list[str]) -> list[str]:
    algos = []
    for value in values or []:
        algo = str(value or "").strip()
        if algo in ALLOWED_MM_ALGOS and algo not in algos:
            algos.append(algo)
    return algos or ["Algo1_MM"]


def _prepare_replay_data(
    *,
    user_id: int,
    symbols: list[str],
    intervals: list[str],
    start_date: str,
    end_date: str,
) -> dict[str, str]:
    errors: dict[str, str] = {}
    for symbol in symbols:
        try:
            fetch_and_save(user_id=user_id, symbol=symbol, days=30, force=False)
            _validate_replay_symbol_range(
                user_id=user_id,
                symbol=symbol,
                intervals=intervals,
                start_date=start_date,
                end_date=end_date,
            )
        except Exception:
            try:
                fetch_and_save(user_id=user_id, symbol=symbol, days=30, force=True)
                _validate_replay_symbol_range(
                    user_id=user_id,
                    symbol=symbol,
                    intervals=intervals,
                    start_date=start_date,
                    end_date=end_date,
                )
            except Exception as exc:
                errors[symbol] = str(exc)[:500]
    return errors


def _validate_replay_symbol_range(
    *,
    user_id: int,
    symbol: str,
    intervals: list[str],
    start_date: str,
    end_date: str,
) -> None:
    for interval in intervals:
        ReplayDataProvider(
            user_id=user_id,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            interval=interval,
        )


def _sparkie_trade_size(account_equity: float) -> float:
    return float(max(1, int(float(account_equity) * 0.10)))


def _sparkie_replay_config(
    *,
    algo_name: str,
    account_equity: float,
    target_profit: float,
    target_period: str,
) -> dict:
    daily_stop = max(25.0, float(account_equity) * 0.01)
    trade_size = _sparkie_trade_size(account_equity)
    return _build_mm_replay_config(
        algo_name=algo_name,
        eod_auto_close="on",
        allow_short_selling="on",
        stop_loss_usd=min(daily_stop, trade_size * 0.25),
        trailing_profit_usd=max(10.0, float(target_profit) * 0.5),
        stop_loss_pct=DEFAULT_REPLAY_MM_CONFIG["stop_loss_pct"],
        trailing_profit_pct=DEFAULT_REPLAY_MM_CONFIG["trailing_profit_pct"],
        prob_trail_drop=DEFAULT_REPLAY_MM_CONFIG["prob_trail_drop"],
        prob_exit_mode=DEFAULT_REPLAY_MM_CONFIG["prob_exit_mode"],
        long_fixed_exit_prob=DEFAULT_REPLAY_MM_CONFIG["long_fixed_exit_prob"],
        short_fixed_exit_prob=DEFAULT_REPLAY_MM_CONFIG["short_fixed_exit_prob"],
        long_entry_prob=DEFAULT_REPLAY_MM_CONFIG["long_entry_prob"],
        short_entry_prob=DEFAULT_REPLAY_MM_CONFIG["short_entry_prob"],
        entry_confirmation_bars=DEFAULT_REPLAY_MM_CONFIG["entry_confirmation_bars"],
    )


def _replay_session_pnl(db: Session, session_id: int) -> tuple[float, int]:
    from app.models.replay import ReplayTradeHistory

    rows = db.query(ReplayTradeHistory.profit_loss).filter_by(session_id=session_id).all()
    values = [float(row[0] or 0.0) for row in rows]
    return sum(values), len(values)


def _mark_stale_sparkie_sessions(db: Session, rows: list[ReplaySession]) -> None:
    now = datetime.utcnow()
    changed = False
    for row in rows:
        status = str(row.status or "").upper()
        if status not in {"RUNNING", "QUEUED", "STARTING", "PENDING"}:
            continue
        age_start = row.updated_at or row.started_at or row.created_at or now
        if age_start.tzinfo is not None:
            age_start = age_start.replace(tzinfo=None)
        current_bar = int(row.current_bar_idx or 0)
        total_bars = int(row.total_bars or 0)
        stalled = now - age_start > timedelta(minutes=SPARKIE_STALE_MINUTES)
        if stalled:
            detail = "with no replay progress" if current_bar <= 0 or total_bars <= 0 else "with no replay update"
            row.status = "ERROR"
            row.error_message = (
                f"Sparkie marked session stale after {SPARKIE_STALE_MINUTES} minutes "
                f"{detail}."
            )
            row.updated_at = now
            changed = True
    if changed:
        db.commit()


def _session_age_minutes(row: ReplaySession) -> float:
    now = datetime.utcnow()
    age_start = row.updated_at or row.started_at or row.created_at
    if not age_start:
        return 0.0
    if age_start.tzinfo is not None:
        age_start = age_start.replace(tzinfo=None)
    return round(max((now - age_start).total_seconds(), 0.0) / 60.0, 1)


def _best_sparkie_session(sessions: list[dict]) -> dict | None:
    completed = [
        session
        for session in sessions
        if str(session.get("status") or "").upper() in {"COMPLETED", "STOPPED"}
    ]
    if not completed:
        return None
    return max(completed, key=lambda session: float(session.get("profit_loss") or 0.0))


def _sparkie_preview_from_sessions(rows: list[ReplaySession]) -> dict:
    first = rows[0]
    cfg = _first_sparkie_config(rows)
    account_equity = float(cfg.get("sparkie_account_equity") or 0.0)
    target_profit = float(cfg.get("sparkie_target_profit") or 0.0)
    target_period = str(cfg.get("sparkie_target_period") or "daily")
    confidence_level = float(cfg.get("sparkie_confidence_level") or 0.60)

    if account_equity <= 0 or target_profit <= 0:
        return {
            "account_equity": account_equity,
            "target_profit_for_window": 0.0,
            "meets_minimum_equity": False,
            "feasibility": {},
            "start_date": first.start_date,
            "end_date": first.end_date,
        }

    symbols = tuple(sorted({row.symbol for row in rows if row.symbol}))
    intervals = tuple(sorted({row.interval for row in rows if row.interval}))
    algos = tuple(sorted({row.algo_name for row in rows if row.algo_name}))
    try:
        preview = build_performance_preview(
            SparkiePerformanceRequest(
                account_equity=account_equity,
                target_profit=target_profit,
                target_period=target_period,  # type: ignore[arg-type]
                start_date=_parse_date(first.start_date, "start_date"),
                end_date=_parse_date(first.end_date, "end_date"),
                symbols=symbols,
                intervals=intervals,
                algos=algos,
                confidence_level=confidence_level,
            )
        )
        return preview.to_dict()
    except Exception:
        return {
            "account_equity": account_equity,
            "target_profit_for_window": 0.0,
            "meets_minimum_equity": account_equity >= 5000,
            "feasibility": {},
            "start_date": first.start_date,
            "end_date": first.end_date,
        }


def _parse_config_json(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _first_sparkie_config(rows: list[ReplaySession]) -> dict:
    for row in rows:
        cfg = _parse_config_json(row.config_json)
        if cfg.get("sparkie_account_equity") and cfg.get("sparkie_target_profit"):
            return cfg
    return _parse_config_json(rows[0].config_json) if rows else {}


def _sparkie_status_next_step(status: str, total_profit: float, total_trades: int) -> str:
    if status != "completed":
        return "Sparkie is still running replay sessions. Keep this page open or check back shortly."
    if total_trades <= 0:
        return "Sparkie finished but found no completed trades. Try a wider date range or different symbols."
    if total_profit > 0:
        return "Sparkie found positive replay P/L. Review drawdown and individual sessions before Live Mirror."
    return "Sparkie did not find positive replay P/L. Stay in paper mode and adjust symbols, algos, or target."


@router.post("/launch-plan")
def launch_plan(
    payload: SparkieLaunchRequest,
    _user=Depends(get_current_user),
):
    try:
        result = build_agent_launch_plan(
            AgentLaunchRequest(
                account_equity=payload.account_equity,
                target_profit=payload.target_profit,
                target_period=payload.target_period,
                requested_mode=payload.requested_mode,
                confidence_level=payload.confidence_level,
                acknowledged_live_risk=payload.acknowledged_live_risk,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return result.to_dict()
