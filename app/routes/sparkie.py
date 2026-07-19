import json
import re
from datetime import datetime

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
        "sessions": queued,
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
            }
        )

    return {
        "ok": True,
        "job_id": job_id,
        "status": "completed" if completed == len(rows) else "running",
        "completed_sessions": completed,
        "total_sessions": len(rows),
        "total_profit_loss": round(total_profit, 2),
        "total_trades": total_trades,
        "sessions": sessions,
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
