import json
import math
import os
import signal
import subprocess
import sys
import re
import time
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from typing import Any, Literal

from app.database.connection import get_db
from app.models.replay import ReplayOpenTrade, ReplaySession, ReplayTradeHistory
from app.models.sparkie import (
    SparkieCandidate,
    SparkieEvent,
    SparkieJob,
    SparkieWeeklyRun,
    SparkieWeeklySchedule,
)
from app.routes.auth import get_current_user
from app.modules.replay.routes import (
    ALLOWED_MM_ALGOS,
    DEFAULT_REPLAY_MM_CONFIG,
    _cheatsheet_jobs,
    _cheatsheet_jobs_lock,
    _cleanup_cheatsheet_jobs,
    _build_mm_replay_config,
    _create_live_bot_from_replay_session,
    _load_optimizer_run,
    _load_optimizer_cache,
    _queue_qqq_optimizer_job,
    _snapshot_cheatsheet_job,
    _store_cheatsheet_job,
)
from app.services.backtest_cheatsheet_service import CheatSheetRequest
from app.services.barchart_symbols import (
    BARCHART_TOP_100_PAGE,
    barchart_top_symbols,
    barchart_top_symbols_with_source,
)
from app.scripts.replay.data_ingest import fetch_and_save
from app.scripts.replay.replay_data_provider import ReplayDataProvider
from app.services.replay_process import stop_session
from app.services.stocktwits_symbols import STOCKTWITS_SOURCES, stocktwits_ranked_symbols
from app.services.sparkie_engine import (
    ACTIVE_STATUSES,
    CASH_DEPLOYMENT_POLICY,
    MIN_ACCOUNT_EQUITY,
    TERMINAL_STATUSES,
    add_event,
    create_sparkie_job,
    sparkie_symbol_bucket_with_source,
    stop_sparkie_job,
)
from app.services.sparkie_weekly_service import (
    DAILY_LOSS_MULTIPLIER,
    WEEKLY_ACTIVE_STATUSES,
    WEEKLY_PROCESS_PREFIX,
    create_weekly_run,
    latest_weekly_run,
    weekly_recommendation,
    weekly_run_payload,
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
SPARKIE_STALE_MINUTES = 12
SPARKIE_REPLAY_MAX_MINUTES = 30
SPARKIE_OPTIMIZER_MAX_MINUTES = 60
SPARKIE_JOB_PATTERN = re.compile(r"sparkie-\d+-\d{14}(?:-[a-f0-9]{8})?")
SPARKIE_PROCESS_PREFIX = "sparkie-process:"
# A live account must have enough independent historical days to evaluate a
# daily-target claim.  Six days can look good by chance and is paper-only.
SPARKIE_MIN_LIVE_EVIDENCE_DAYS = max(20, int(os.getenv("SPARKIE_MIN_LIVE_EVIDENCE_DAYS", "20")))
SPARKIE_RESEARCH_GATES = {
    "minimum_daily_evidence",
    "positive_average",
    "validation_positive",
    "trade_evidence",
    "daily_loss_limit_respected",
    "execution_cost_model_present",
    "liquidity_capacity",
    "profitable_day_confidence",
    "conservative_monthly_return_positive",
}
# These gates express client risk preference or market-capacity conservatism.
# Structural evidence/model gates remain non-overridable for live brokerage.
SPARKIE_LIVE_OVERRIDABLE_GATES = {
    "liquidity_capacity",
    "profitable_day_confidence",
}


class GoalFeasibilityRequest(BaseModel):
    account_equity: float = Field(..., gt=0)
    target_profit: float = Field(..., gt=0)
    target_period: Literal["daily", "weekly", "monthly"] = "daily"
    confidence_level: float = Field(0.60, ge=0.40, le=0.95)


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
    symbol_source: Literal["most_active", "trending", "watchers", "barchart_top", "own_list"] = "most_active"
    acknowledged_full_cash_risk: bool = False


class SparkieEvaluationRequest(BaseModel):
    account_equity: float = Field(..., ge=2000.0, le=100_000.0)
    risk_per_day_pct: float = Field(..., ge=0.10, le=1.0)
    confidence_level: float = Field(0.60, ge=0.40, le=0.95)
    symbols: list[str] = Field(default_factory=list)
    symbol_source: Literal["most_active", "trending", "watchers", "barchart_top", "own_list"] = "most_active"
    acknowledged_full_cash_risk: bool = False
    max_sessions: int = Field(12, ge=1, le=30)
    use_backtest_prefilter: bool = True
    replay_finalists: int = Field(3, ge=1, le=12)
    finalist_verification: bool = False
    replay_gate_override: bool = False
    acknowledged_gate_override: bool = False
    override_failed_gates: list[str] = Field(default_factory=list)
    forced_interval: str | None = None
    forced_algo_name: str | None = None


class SparkieClearHistoryRequest(BaseModel):
    confirm: bool = False


class SparkieLiveBotSetupRequest(BaseModel):
    mirror_live: bool = False
    acknowledged_live_risk: bool = False
    acknowledged_full_cash_risk: bool = False
    acknowledged_gate_override: bool = False
    override_failed_gates: list[str] = Field(default_factory=list)


class SparkieWeeklyRunRequest(BaseModel):
    baseline_cash: float = Field(10000.0, ge=2000.0, le=1_000_000.0)


class SparkieWeeklyRecommendationRequest(BaseModel):
    account_equity: float = Field(..., ge=2000.0)
    risk_per_day_pct: float = Field(..., ge=0.10, le=1.0)
    confidence_level: float = Field(0.60, ge=0.40, le=0.95)
    acknowledged_full_cash_risk: bool = False


class SparkieWeeklyScheduleRequest(BaseModel):
    enabled: bool


@page_router.get("/auth/sparkie", response_class=HTMLResponse)
def sparkie_page(
    request: Request,
    user=Depends(get_current_user),
):
    default_symbols, symbol_bucket_source = sparkie_symbol_bucket_with_source()
    return templates.TemplateResponse(
        request,
        "sparkie/index.html",
        {
            "request": request,
            "user": user,
            "title": "Sparkie Agent",
            "default_symbol_bucket": ",".join(default_symbols),
            "symbol_bucket_source": symbol_bucket_source,
        },
    )


@router.get("/symbol-source/{source}")
def symbol_source(
    source: Literal["most_active", "trending", "watchers", "barchart_top"],
    _user=Depends(get_current_user),
):
    if source == "barchart_top":
        try:
            symbols, data_mode = barchart_top_symbols_with_source(5)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Stock Bullish rankings were unavailable: {exc}") from exc
        return {
            "ok": True,
            "source": source,
            "provider": "Stock Bullish",
            "label": "Top 5 by Weighted Alpha" + (" (CSV snapshot)" if data_mode == "snapshot" else ""),
            "data_mode": data_mode,
            "url": BARCHART_TOP_100_PAGE,
            "symbols": symbols,
        }
    try:
        symbols = stocktwits_ranked_symbols(source)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    config = STOCKTWITS_SOURCES[source]
    return {
        "ok": True,
        "source": source,
        "provider": "Stocktwits",
        "label": config["label"],
        "url": config["page"],
        "symbols": symbols,
    }


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
    if not payload.acknowledged_full_cash_risk:
        raise HTTPException(
            status_code=400,
            detail="Acknowledge that Sparkie may deploy all available cash and that the entire allocation is at risk.",
        )
    active_job = _latest_active_sparkie_v2_job(db, user.id)
    if active_job:
        result = _sparkie_v2_job_payload(db, active_job, include_details=True)
        result["reused_existing_job"] = True
        result["message"] = "Sparkie already has an active evaluation. Showing that job instead of starting a duplicate."
        return result

    if float(payload.account_equity or 0.0) < MIN_ACCOUNT_EQUITY:
        raise HTTPException(
            status_code=400,
            detail=f"Sparkie requires at least ${MIN_ACCOUNT_EQUITY:,.0f} account equity.",
        )

    override_failed_gates = sorted(
        {str(gate).strip() for gate in payload.override_failed_gates if str(gate).strip()}
    )
    unknown_override_gates = sorted(set(override_failed_gates) - SPARKIE_RESEARCH_GATES)
    if unknown_override_gates:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown Sparkie gate override(s): {', '.join(unknown_override_gates)}.",
        )
    if payload.replay_gate_override and not payload.finalist_verification:
        raise HTTPException(status_code=400, detail="Replay gate override requires an exact finalist verification.")
    if payload.finalist_verification:
        if payload.replay_gate_override and not payload.acknowledged_gate_override:
            raise HTTPException(
                status_code=400,
                detail="Explicit failed-gate acknowledgement is required for a Replay override.",
            )
        if not payload.forced_interval or payload.forced_interval not in {"5min", "10min", "15min"}:
            raise HTTPException(status_code=400, detail="Select the exact 5min, 10min, or 15min finalist interval.")
        if not payload.forced_algo_name or payload.forced_algo_name not in ALLOWED_MM_ALGOS:
            raise HTTPException(status_code=400, detail="Select the exact supported finalist algorithm.")

    if payload.symbol_source == "own_list":
        selected_symbols = _clean_symbols(payload.symbols)
        if not selected_symbols:
            raise HTTPException(status_code=400, detail="Enter at least one ticker for Own List.")
        if payload.finalist_verification and len(selected_symbols) != 1:
            raise HTTPException(status_code=400, detail="Finalist verification must use exactly one symbol.")
    else:
        try:
            selected_symbols = (
                barchart_top_symbols(5)
                if payload.symbol_source == "barchart_top"
                else stocktwits_ranked_symbols(payload.symbol_source)
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    job_id = f"sparkie-{user.id}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    job = create_sparkie_job(
        db,
        job_id=job_id,
        user_id=user.id,
        account_equity=payload.account_equity,
        target_profit=float(payload.account_equity) * float(payload.risk_per_day_pct),
        target_period="risk_profile",
        confidence_level=payload.confidence_level,
        symbol_bucket=selected_symbols,
        symbol_source=payload.symbol_source,
        risk_per_day_pct=payload.risk_per_day_pct,
        interval_policy=[payload.forced_interval] if payload.finalist_verification else None,
        algo_policy=[payload.forced_algo_name] if payload.finalist_verification else None,
        finalist_verification=payload.finalist_verification,
        replay_gate_override=payload.replay_gate_override,
        override_failed_gates=override_failed_gates,
    )
    db.commit()

    try:
        proc = _start_sparkie_process(job.id)
        job.task_id = f"{SPARKIE_PROCESS_PREFIX}{proc.pid}"
        job.message = "Sparkie started a dedicated background runner."
        db.commit()
    except Exception as exc:
        job.status = "error"
        job.stage = "error"
        job.error_message = f"Sparkie runner start failed: {str(exc)[:500]}"
        job.message = job.error_message
        db.commit()

    return _sparkie_v2_job_payload(db, job, include_details=True)


def _start_sparkie_process(job_id: str) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "app.scripts.sparkie_runner", str(job_id)]
    kwargs: dict[str, Any] = {
        "cwd": os.getcwd(),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": os.name != "nt",
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def _start_sparkie_weekly_process(run_id: str) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "app.scripts.sparkie_weekly_runner", str(run_id)]
    kwargs: dict[str, Any] = {
        "cwd": os.getcwd(),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": os.name != "nt",
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def _weekly_process_pid(task_id: str | None) -> int | None:
    text = str(task_id or "")
    if not text.startswith(WEEKLY_PROCESS_PREFIX):
        return None
    try:
        pid = int(text.removeprefix(WEEKLY_PROCESS_PREFIX))
    except ValueError:
        return None
    return pid if pid > 0 else None


def _stop_sparkie_weekly_process(task_id: str | None) -> bool:
    pid = _weekly_process_pid(task_id)
    if not pid:
        return False
    try:
        if os.name == "nt":
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except Exception:
            return False


@router.get("/evaluation-status/{job_id}")
def evaluation_status(
    job_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    job = db.get(SparkieJob, job_id)
    if not job or int(job.user_id) != int(user.id):
        raise HTTPException(status_code=404, detail="Sparkie evaluation job not found.")

    return _sparkie_v2_job_payload(db, job, include_details=True)


@router.get("/history")
def evaluation_history(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    jobs = (
        db.query(SparkieJob)
        .filter(SparkieJob.user_id == user.id)
        .order_by(SparkieJob.created_at.desc())
        .limit(10)
        .all()
    )

    return {
        "ok": True,
        "jobs": [_sparkie_v2_job_payload(db, job, include_details=False) for job in jobs],
    }


@router.post("/stop-evaluation/{job_id}")
def stop_evaluation(
    job_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    job = db.get(SparkieJob, job_id)
    if not job or int(job.user_id) != int(user.id):
        raise HTTPException(status_code=404, detail="Sparkie evaluation job not found.")

    stop_sparkie_job(db, job, reason="Sparkie stop requested.")
    db.commit()

    return {
        "ok": True,
        "job_id": job_id,
        "message": "Sparkie stop requested.",
    }


@router.post("/stop-all")
def stop_all_evaluations(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    # Include already-stopped jobs that still have a Celery task id. A previous
    # stop may have changed the DB status while the worker process continued
    # burning CPU inside a long backtest unit.
    jobs = (
        db.query(SparkieJob)
        .filter(SparkieJob.user_id == user.id)
        .filter((SparkieJob.status.in_(tuple(ACTIVE_STATUSES))) | ((SparkieJob.status == "stopped") & (SparkieJob.task_id.isnot(None))))
        .all()
    )
    for job in jobs:
        stop_sparkie_job(db, job, reason="Sparkie stop-all requested.")
    db.commit()

    return {
        "ok": True,
        "stopped_sessions": len(jobs),
        "message": f"Sparkie stop-all requested for {len(jobs)} active job(s).",
    }


@router.post("/setup-live-bot/{job_id}")
def setup_live_bot(
    job_id: str,
    payload: SparkieLiveBotSetupRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not payload.acknowledged_full_cash_risk:
        raise HTTPException(status_code=400, detail="Full-cash risk acknowledgement is required before bot setup.")
    job = db.get(SparkieJob, job_id)
    if not job or int(job.user_id) != int(user.id):
        raise HTTPException(status_code=404, detail="Sparkie evaluation job not found.")
    if str(job.status or "").lower() != "completed" or not job.replay_session_id:
        raise HTTPException(status_code=409, detail="Complete Replay verification before setting up a bot.")

    replay = db.get(ReplaySession, int(job.replay_session_id))
    if not replay or int(replay.user_id) != int(user.id):
        raise HTTPException(status_code=404, detail="Sparkie Replay verification session was not found.")
    if str(replay.status or "").upper() != "COMPLETED":
        raise HTTPException(status_code=409, detail="Replay verification must be completed before setting up a bot.")

    if payload.mirror_live:
        if not payload.acknowledged_live_risk:
            raise HTTPException(status_code=400, detail="Explicit live-trading risk acknowledgement is required.")
        request_payload = _parse_json_value(job.request_json, {})
        job_override_gates = {
            str(gate).strip()
            for gate in (
                request_payload.get("override_failed_gates", [])
                if isinstance(request_payload, dict)
                else []
            )
            if str(gate).strip()
        }
        requested_override_gates = {
            str(gate).strip() for gate in payload.override_failed_gates if str(gate).strip()
        }
        non_overridable_gates = sorted(job_override_gates - SPARKIE_LIVE_OVERRIDABLE_GATES)
        if non_overridable_gates:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Live Mirror remains blocked because these structural gates cannot be overridden: "
                    f"{', '.join(non_overridable_gates)}. Start a paper bot instead."
                ),
            )
        if job_override_gates:
            if not payload.acknowledged_gate_override:
                raise HTTPException(
                    status_code=400,
                    detail="Explicit live failed-gate acknowledgement is required.",
                )
            if not job_override_gates.issubset(requested_override_gates):
                raise HTTPException(
                    status_code=400,
                    detail="Acknowledge every failed research gate before starting Live Mirror.",
                )
        candidate = (
            db.query(SparkieCandidate)
            .filter(SparkieCandidate.job_id == job.id)
            .filter(SparkieCandidate.symbol == job.best_symbol)
            .filter(SparkieCandidate.interval == job.best_interval)
            .filter(SparkieCandidate.algo_name == job.best_algo_name)
            .first()
        )
        best = _sparkie_candidate_payload(candidate, job.target_period, job.target_profit) if candidate else _sparkie_best_backtest_payload(job)
        readiness = _sparkie_live_readiness(best, float(job.confidence_level or 0.0))
        replay_pnl, _replay_trades = _replay_session_pnl(db, int(replay.id))
        confidence_overridden = bool(
            "profitable_day_confidence" in job_override_gates
            and "confidence" in str(readiness.get("reason") or "").lower()
        )
        readiness_eligible = bool(readiness["eligible"] or confidence_overridden)
        if not readiness_eligible:
            raise HTTPException(
                status_code=409,
                detail=f"Live Mirror is blocked: {readiness['reason']} Start a paper bot instead.",
            )
        if replay_pnl <= 0:
            raise HTTPException(
                status_code=409,
                detail="Live Mirror is blocked: Replay verification was not profitable. Start a paper bot instead.",
            )

    bot = _create_live_bot_from_replay_session(
        db,
        user_id=int(user.id),
        sess=replay,
        mirror_live=bool(payload.mirror_live),
    )
    add_event(
        db,
        job,
        "bot_setup",
        (
            f"Started bot #{bot.id} from Replay #{replay.id} "
            f"({'Schwab Live Mirror' if payload.mirror_live else 'paper-only'})."
        ),
        {
            "bot_id": bot.id,
            "replay_session_id": replay.id,
            "mirror_live": bool(payload.mirror_live),
            "acknowledged_gate_override": bool(payload.acknowledged_gate_override),
            "override_failed_gates": sorted(
                {str(gate).strip() for gate in payload.override_failed_gates if str(gate).strip()}
            ),
        },
    )
    db.commit()
    return {
        "ok": True,
        "bot_id": int(bot.id),
        "mirror_live": bool(payload.mirror_live),
        "symbol": bot.symbol,
        "interval": bot.interval,
        "algo_name": bot.algo_name,
    }


@router.post("/weekly/run")
def run_weekly_sparkie(
    payload: SparkieWeeklyRunRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    run = create_weekly_run(
        db,
        user_id=int(user.id),
        run_mode="manual",
        baseline_cash=float(payload.baseline_cash),
    )
    if not run.task_id:
        try:
            proc = _start_sparkie_weekly_process(run.id)
            run.task_id = f"{WEEKLY_PROCESS_PREFIX}{proc.pid}"
            run.message = "Weekly Sparkie started in a dedicated background process."
            db.commit()
        except Exception as exc:
            run.status = "error"
            run.stage = "error"
            run.error_message = f"Weekly Sparkie start failed: {str(exc)[:500]}"
            run.message = run.error_message
            db.commit()
    return {"ok": True, "weekly_run": weekly_run_payload(db, run)}


@router.get("/weekly/status")
def weekly_sparkie_status(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    return {"ok": True, "weekly_run": weekly_run_payload(db, latest_weekly_run(db, int(user.id)))}


@router.post("/weekly/stop")
def stop_weekly_sparkie(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    run = latest_weekly_run(db, int(user.id))
    if not run or str(run.status or "").lower() not in WEEKLY_ACTIVE_STATUSES:
        return {"ok": True, "message": "No active Weekly Sparkie run was found.", "weekly_run": weekly_run_payload(db, run)}
    run.status = "stopping"
    run.stage = "stopping"
    run.message = "Weekly Sparkie stop requested; completed symbols remain saved."
    run.stop_requested_at = datetime.utcnow()
    db.commit()
    _stop_sparkie_weekly_process(run.task_id)
    return {"ok": True, "message": run.message, "weekly_run": weekly_run_payload(db, run)}


@router.post("/weekly/resume")
def resume_weekly_sparkie(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    run = latest_weekly_run(db, int(user.id))
    if not run or str(run.status or "").lower() not in {"stopped", "error"}:
        raise HTTPException(status_code=409, detail="Only a stopped or failed Weekly Sparkie run can be resumed.")
    run.status = "queued"
    run.stage = "queued"
    run.message = "Weekly Sparkie resume queued from the last saved symbol."
    run.stop_requested_at = None
    run.error_message = None
    run.finished_at = None
    db.commit()
    try:
        proc = _start_sparkie_weekly_process(run.id)
        run.task_id = f"{WEEKLY_PROCESS_PREFIX}{proc.pid}"
        db.commit()
    except Exception as exc:
        run.status = "error"
        run.stage = "error"
        run.error_message = f"Weekly Sparkie resume failed: {str(exc)[:500]}"
        run.message = run.error_message
        db.commit()
    return {"ok": True, "weekly_run": weekly_run_payload(db, run)}


@router.post("/weekly/restart")
def restart_weekly_sparkie(
    payload: SparkieWeeklyRunRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    current = latest_weekly_run(db, int(user.id))
    if current and str(current.status or "").lower() in WEEKLY_ACTIVE_STATUSES:
        current.status = "stopped"
        current.stage = "stopped"
        current.message = "Weekly Sparkie was replaced by a fresh run."
        current.stop_requested_at = datetime.utcnow()
        current.finished_at = datetime.utcnow()
        db.commit()
        _stop_sparkie_weekly_process(current.task_id)
    run = create_weekly_run(
        db,
        user_id=int(user.id),
        run_mode="manual_restart",
        baseline_cash=float(payload.baseline_cash),
    )
    try:
        proc = _start_sparkie_weekly_process(run.id)
        run.task_id = f"{WEEKLY_PROCESS_PREFIX}{proc.pid}"
        run.message = "Fresh Weekly Sparkie research started."
        db.commit()
    except Exception as exc:
        run.status = "error"
        run.stage = "error"
        run.error_message = f"Fresh Weekly Sparkie start failed: {str(exc)[:500]}"
        run.message = run.error_message
        db.commit()
    return {"ok": True, "weekly_run": weekly_run_payload(db, run)}


@router.post("/weekly/recommendation")
def saved_weekly_recommendation(
    payload: SparkieWeeklyRecommendationRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not payload.acknowledged_full_cash_risk:
        raise HTTPException(
            status_code=400,
            detail="Acknowledge that the proposed bot may deploy all client cash and that the entire allocation is at risk.",
        )
    try:
        return weekly_recommendation(
            db,
            user_id=int(user.id),
            account_equity=float(payload.account_equity),
            risk_per_day_pct=float(payload.risk_per_day_pct),
            confidence_level=float(payload.confidence_level),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/weekly/schedule")
def get_weekly_schedule(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    schedule = db.query(SparkieWeeklySchedule).filter_by(user_id=int(user.id)).first()
    return {
        "ok": True,
        "enabled": bool(schedule.enabled) if schedule else False,
        "weekday": int(schedule.weekday) if schedule else 6,
        "hour_et": int(schedule.hour_et) if schedule else 2,
        "label": "Sunday at 2:00 AM ET",
        "last_trigger_date": schedule.last_trigger_date if schedule else None,
    }


@router.post("/weekly/schedule")
def set_weekly_schedule(
    payload: SparkieWeeklyScheduleRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    schedule = db.query(SparkieWeeklySchedule).filter_by(user_id=int(user.id)).first()
    if not schedule:
        schedule = SparkieWeeklySchedule(user_id=int(user.id), weekday=6, hour_et=2)
        db.add(schedule)
    schedule.enabled = bool(payload.enabled)
    db.commit()
    return {
        "ok": True,
        "enabled": bool(schedule.enabled),
        "label": "Sunday at 2:00 AM ET",
        "message": "Automatic Weekly Sparkie schedule updated.",
    }


@router.post("/history/clear")
def clear_history(
    payload: SparkieClearHistoryRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Confirmation is required to clear Sparkie history.")

    jobs = db.query(SparkieJob).filter(SparkieJob.user_id == user.id).all()
    if not jobs:
        return {
            "ok": True,
            "deleted_sessions": 0,
            "stopped_sessions": 0,
            "message": "Sparkie history is already clean.",
        }

    stopped = 0
    job_ids = [job.id for job in jobs]
    for job in jobs:
        stop_sparkie_job(db, job, reason="Sparkie history cleared.")
        stopped += 1
    db.query(SparkieEvent).filter(SparkieEvent.job_id.in_(job_ids)).delete(synchronize_session=False)
    db.query(SparkieCandidate).filter(SparkieCandidate.job_id.in_(job_ids)).delete(synchronize_session=False)
    deleted = db.query(SparkieJob).filter(SparkieJob.id.in_(job_ids)).delete(synchronize_session=False)
    db.commit()

    return {
        "ok": True,
        "deleted_sessions": deleted,
        "stopped_sessions": stopped,
        "message": f"Cleared {deleted} Sparkie job(s).",
    }


@router.delete("/history/{job_id}")
def delete_history_job(
    job_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    job = db.get(SparkieJob, job_id)
    if not job or int(job.user_id) != int(user.id):
        raise HTTPException(status_code=404, detail="Sparkie evaluation job not found.")
    stop_sparkie_job(db, job, reason="Sparkie job deleted.")
    db.query(SparkieEvent).filter(SparkieEvent.job_id == job.id).delete(synchronize_session=False)
    db.query(SparkieCandidate).filter(SparkieCandidate.job_id == job.id).delete(synchronize_session=False)
    db.delete(job)
    db.commit()
    return {"ok": True, "message": "Sparkie job deleted."}


def _latest_active_sparkie_v2_job(db: Session, user_id: int) -> SparkieJob | None:
    return (
        db.query(SparkieJob)
        .filter(SparkieJob.user_id == user_id)
        .filter(SparkieJob.status.in_(tuple(ACTIVE_STATUSES)))
        .order_by(SparkieJob.created_at.desc())
        .first()
    )


def _sparkie_v2_job_payload(db: Session, job: SparkieJob, *, include_details: bool) -> dict[str, Any]:
    _refresh_sparkie_v2_runtime_status(db, job)
    _refresh_sparkie_v2_replay_status(db, job)
    candidates = (
        db.query(SparkieCandidate)
        .filter(SparkieCandidate.job_id == job.id)
        .order_by(SparkieCandidate.score.desc().nullslast(), SparkieCandidate.id.asc())
        .limit(20 if include_details else 3)
        .all()
    )
    events = []
    if include_details:
        events = (
            db.query(SparkieEvent)
            .filter(SparkieEvent.job_id == job.id)
            .order_by(SparkieEvent.id.desc())
            .limit(30)
            .all()
        )
    created_at = job.created_at.isoformat() if job.created_at else None
    updated_at = job.updated_at.isoformat() if job.updated_at else None
    result_payload = _parse_json_value(job.result_json, {})
    request_payload = _parse_json_value(job.request_json, {})
    risk_first = isinstance(request_payload, dict) and request_payload.get("analysis_mode") == "risk_first_monthly_v1"
    risk_per_day_pct = float(request_payload.get("risk_per_day_pct") or 0.0) if isinstance(request_payload, dict) else 0.0
    risk_per_day_usd = float(request_payload.get("risk_per_day_usd") or 0.0) if isinstance(request_payload, dict) else 0.0
    full_cash_policy = (
        request_payload.get("cash_deployment_policy") == CASH_DEPLOYMENT_POLICY
        if isinstance(request_payload, dict)
        else False
    )
    selected_row = next(
        (
            row for row in candidates
            if row.symbol == job.best_symbol
            and row.interval == job.best_interval
            and row.algo_name == job.best_algo_name
        ),
        None,
    )
    best = None
    if job.best_symbol:
        # Candidate rows are the backtest evidence. Replay is displayed
        # separately instead of replacing this evidence with a one-session P/L.
        best = (
            _sparkie_candidate_payload(selected_row, job.target_period, job.target_profit)
            if selected_row
            else _sparkie_best_backtest_payload(job)
        )
    allocation = _sparkie_trade_allocation_usd(float(job.account_equity or 0.0))
    equity = float(job.account_equity or 0.0)
    target_profit = float(job.target_profit or 0.0)
    monthly_projection = _sparkie_monthly_projection((best or {}).get("daily_pnl"), equity) if risk_first else None
    live_elapsed_seconds = _sparkie_elapsed_seconds(job) if str(job.status or "") in ACTIVE_STATUSES else int(job.elapsed_seconds or 0)
    replay_verification = _sparkie_replay_verification_payload(db, job)
    replay_completed = bool(replay_verification and str(replay_verification.get("status") or "").lower() == "completed")
    replay_positive = bool(
        replay_completed
        and float(replay_verification.get("profit_loss") or 0.0) > 0
        and int(replay_verification.get("trades") or 0) > 0
    )
    live_readiness = _sparkie_live_readiness(best, float(job.confidence_level or 0.0))
    replay_override_gates = sorted({
        str(gate).strip()
        for gate in (
            request_payload.get("override_failed_gates", [])
            if isinstance(request_payload, dict)
            else []
        )
        if str(gate).strip()
    })
    non_overridable_live_gates = sorted(
        set(replay_override_gates) - SPARKIE_LIVE_OVERRIDABLE_GATES
    )
    confidence_live_override = bool(
        "profitable_day_confidence" in replay_override_gates
        and "confidence" in str(live_readiness.get("reason") or "").lower()
    )
    live_mirror_allowed = bool(
        job.replay_session_id
        and replay_completed
        and replay_positive
        and job.recommendation == "paper_candidate"
        and live_readiness["eligible"]
    )
    live_override_available = bool(
        job.replay_session_id
        and replay_completed
        and replay_positive
        and job.recommendation == "paper_override_candidate"
        and replay_override_gates
        and not non_overridable_live_gates
        and (live_readiness["eligible"] or confidence_live_override)
    )
    if non_overridable_live_gates:
        live_block_reason = (
            "These structural research gates cannot be overridden for live brokerage: "
            f"{', '.join(non_overridable_live_gates)}."
        )
    elif live_override_available:
        live_block_reason = (
            "Standard Live Mirror approval was not granted. A positive Replay allows an explicit user override "
            f"for: {', '.join(replay_override_gates)}."
        )
    else:
        live_block_reason = live_readiness["reason"]
    payload = {
        "ok": True,
        "job_id": job.id,
        "task_id": job.task_id,
        "status": job.status,
        "stage": job.stage,
        "message": job.message,
        "account_equity": float(job.account_equity or 0.0),
        "target_profit": float(job.target_profit or 0.0),
        "target_period": job.target_period,
        "analysis_mode": "risk_first_monthly_v1" if risk_first else "target_first_legacy",
        "risk_per_day_pct": risk_per_day_pct,
        "risk_per_day_usd": risk_per_day_usd,
        "monthly_projection": monthly_projection,
        "target_window_units": _sparkie_target_window_units(job.target_period),
        "target_profit_for_window": float(job.target_profit or 0.0) * _sparkie_target_window_units(job.target_period),
        "confidence_level": float(job.confidence_level or 0.0),
        "trade_allocation_usd": allocation,
        "cash_deployment_policy": CASH_DEPLOYMENT_POLICY if full_cash_policy else "legacy_partial_cash",
        "full_cash_results_valid": full_cash_policy,
        "target_math": {
            "account_return_pct": (target_profit / equity * 100.0) if equity else 0.0,
            "position_return_pct": (target_profit / allocation * 100.0) if allocation else 0.0,
            "allocation_share_of_equity_pct": (allocation / equity * 100.0) if equity else 0.0,
        },
        "daily_risk_policy": {
            "profit_target_usd": None if risk_first else target_profit if str(job.target_period or "daily") == "daily" else None,
            "loss_limit_usd": risk_per_day_usd if risk_first else target_profit * DAILY_LOSS_MULTIPLIER if str(job.target_period or "daily") == "daily" else None,
            "loss_multiplier": None if risk_first else DAILY_LOSS_MULTIPLIER,
            "stop_after_target": False if risk_first else True,
            "stop_after_loss_limit": True,
            "all_cash_at_risk": True,
            "loss_limit_is_not_guaranteed": True,
        },
        "day_trading_policy": {
            "eod_close": True,
            "overnight_positions_allowed": False,
            "message": "End-of-day close is required; Sparkie does not hold overnight positions.",
        },
        "progress_pct": round(float(job.progress_pct or 0.0), 1),
        "percent_complete": round(float(job.progress_pct or 0.0), 1),
        "elapsed_seconds": live_elapsed_seconds,
        "eta_seconds": int(job.eta_seconds) if job.eta_seconds is not None else None,
        "completed_steps": int(job.completed_steps or 0),
        "total_steps": int(job.total_steps or 0),
        "candidates_tested": int(job.candidates_tested or 0),
        "recommendation": job.recommendation,
        "decision_reason": job.decision_reason,
        "best_session": best,
        "best_candidate": best,
        "replay_session_id": job.replay_session_id,
        "replay_verification": replay_verification,
        "bot_setup": {
            "paper_allowed": bool(job.replay_session_id and replay_completed),
            "live_mirror_allowed": live_mirror_allowed,
            "live_mirror_recommended": live_mirror_allowed,
            "live_override_available": live_override_available,
            "live_override_gates": replay_override_gates,
            "live_mirror_block_reason": None if live_mirror_allowed else live_block_reason,
            "live_readiness": live_readiness,
        },
        "replay_gate_override": bool(
            isinstance(request_payload, dict) and request_payload.get("replay_gate_override")
        ),
        "override_failed_gates": replay_override_gates,
        "summary_file": result_payload.get("summary_file") if isinstance(result_payload, dict) else None,
        "backtest_timing": result_payload.get("backtest_timing") if isinstance(result_payload, dict) else None,
        "error_message": job.error_message,
        "created_at": created_at,
        "updated_at": updated_at,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "symbol_bucket": _parse_json_value(job.symbol_bucket_json, []),
        "symbol_source": request_payload.get("symbol_source", "legacy") if isinstance(request_payload, dict) else "legacy",
        "interval_policy": _parse_json_value(job.interval_policy_json, []),
        "algo_policy": _parse_json_value(job.algo_policy_json, []),
        "preview": {
            "account_equity": float(job.account_equity or 0.0),
            "target_profit": float(job.target_profit or 0.0),
            "target_period": job.target_period,
            "minimum_account_equity": MIN_ACCOUNT_EQUITY,
            "meets_minimum_equity": float(job.account_equity or 0.0) >= MIN_ACCOUNT_EQUITY,
        },
        "backtest_scan": {
            "status": job.stage,
            "message": job.message,
            "candidate_count": int(job.candidates_tested or 0),
            "finalist_count": 1 if job.best_symbol else 0,
            "finalist_combos": [best] if best else [],
        },
        "sessions": [],
        "next_step": _sparkie_v2_next_step(job),
    }
    if include_details:
        payload["candidates"] = [_sparkie_candidate_payload(row, job.target_period, job.target_profit) for row in candidates]
        payload["events"] = [_sparkie_event_payload(row) for row in events]
    else:
        payload["candidates"] = [_sparkie_candidate_payload(row, job.target_period, job.target_profit) for row in candidates]
    return payload


def _sparkie_best_backtest_payload(job: SparkieJob) -> dict[str, Any]:
    target_units = _sparkie_target_window_units(job.target_period)
    profit = float(job.best_profit_loss or 0.0)
    return {
        "symbol": job.best_symbol,
        "interval": job.best_interval,
        "algo_name": job.best_algo_name,
        "score": job.best_score,
        "profit_loss": profit,
        "total_profit_loss": profit,
        "estimated_profit_per_period": profit / target_units if target_units else profit,
        "trades": job.best_trades,
        "win_rate": job.best_win_rate,
    }


def _sparkie_replay_verification_payload(db: Session, job: SparkieJob) -> dict[str, Any] | None:
    if not job.replay_session_id:
        return None
    replay = db.get(ReplaySession, int(job.replay_session_id))
    if not replay:
        return {"session_id": job.replay_session_id, "status": "missing"}
    pnl, trades = _replay_session_pnl(db, int(replay.id))
    return {
        "session_id": int(replay.id),
        "status": str(replay.status or "").lower(),
        "start_date": replay.start_date,
        "end_date": replay.end_date,
        "calendar_days": (
            (datetime.fromisoformat(str(replay.end_date)).date() - datetime.fromisoformat(str(replay.start_date)).date()).days
            if replay.start_date and replay.end_date else None
        ),
        "profit_loss": pnl,
        "trades": trades,
        "error_message": replay.error_message,
    }


def _refresh_sparkie_v2_runtime_status(db: Session, job: SparkieJob) -> None:
    if str(job.status or "") not in ACTIVE_STATUSES:
        return
    elapsed_seconds = _sparkie_elapsed_seconds(job)
    max_minutes = int(os.getenv("SPARKIE_MAX_RUNTIME_MINUTES", "90"))
    if elapsed_seconds <= max_minutes * 60:
        if int(job.elapsed_seconds or 0) != elapsed_seconds:
            job.elapsed_seconds = elapsed_seconds
            job.updated_at = datetime.utcnow()
            db.commit()
        return

    job.status = "error"
    job.stage = "error"
    job.message = f"Sparkie stopped after exceeding the {max_minutes}-minute maximum runtime."
    job.error_message = job.message
    job.elapsed_seconds = elapsed_seconds
    job.eta_seconds = 0
    job.progress_pct = 100.0
    job.finished_at = datetime.utcnow()
    job.updated_at = datetime.utcnow()
    db.add(
        SparkieEvent(
            job_id=job.id,
            user_id=int(job.user_id),
            level="error",
            stage="error",
            message=job.message,
        )
    )
    db.commit()
    _stop_sparkie_process_id(job.task_id)


def _sparkie_process_pid(task_id: str | None) -> int | None:
    text = str(task_id or "")
    if not text.startswith(SPARKIE_PROCESS_PREFIX):
        return None
    try:
        pid = int(text.removeprefix(SPARKIE_PROCESS_PREFIX))
    except ValueError:
        return None
    return pid if pid > 0 else None


def _stop_sparkie_process_id(task_id: str | None) -> bool:
    pid = _sparkie_process_pid(task_id)
    if not pid:
        return False
    try:
        if os.name == "nt":
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except Exception:
            return False


def _sparkie_trade_allocation_usd(account_equity: float) -> float:
    equity = max(float(account_equity or 0.0), 0.0)
    return equity


def _sparkie_target_window_units(target_period: str | None) -> float:
    return {"daily": 10.0, "weekly": 2.0, "monthly": 0.5}.get(str(target_period or "daily").lower(), 10.0)


def _refresh_sparkie_v2_replay_status(db: Session, job: SparkieJob) -> None:
    if str(job.status or "") != "verifying_replay" or not job.replay_session_id:
        return
    replay = db.get(ReplaySession, int(job.replay_session_id))
    if not replay:
        job.status = "error"
        job.stage = "error"
        job.message = "Replay verification session was not found."
        job.error_message = job.message
        job.finished_at = datetime.utcnow()
        job.progress_pct = 100.0
        job.eta_seconds = 0
        db.commit()
        return
    replay_status = str(replay.status or "").upper()
    if replay_status not in {"COMPLETED", "ERROR", "STOPPED"}:
        job.elapsed_seconds = _sparkie_elapsed_seconds(job)
        job.updated_at = datetime.utcnow()
        db.commit()
        return

    pnl, trade_count = _replay_session_pnl(db, int(replay.id))
    if replay_status == "COMPLETED":
        job.status = "completed"
        job.stage = "completed"
        job.message = "Sparkie completed backtest and Replay verification."
        job.decision_reason = (
            f"Replay verification completed with P/L ${pnl:,.2f} across {trade_count} trade(s). "
            f"{job.decision_reason or ''}"
        ).strip()
    elif replay_status == "STOPPED":
        job.status = "stopped"
        job.stage = "stopped"
        job.message = "Sparkie Replay verification was stopped."
    else:
        job.status = "error"
        job.stage = "error"
        job.message = replay.error_message or "Sparkie Replay verification failed."
        job.error_message = job.message
    # best_* remains the selected backtest result. Replay is a separate
    # verification result and must never overwrite the selection evidence.
    job.progress_pct = 100.0
    job.completed_steps = max(int(job.total_steps or 0), int(job.completed_steps or 0))
    job.elapsed_seconds = _sparkie_elapsed_seconds(job)
    job.eta_seconds = 0
    job.finished_at = datetime.utcnow()
    job.updated_at = datetime.utcnow()
    db.commit()


def _sparkie_elapsed_seconds(job: SparkieJob) -> int:
    started_at = job.started_at or job.created_at
    if not started_at:
        return int(job.elapsed_seconds or 0)
    if started_at.tzinfo is not None:
        started_at = started_at.replace(tzinfo=None)
    return max(int((datetime.utcnow() - started_at).total_seconds()), 0)


def _sparkie_candidate_payload(
    row: SparkieCandidate,
    target_period: str | None = None,
    target_profit: float | None = None,
) -> dict[str, Any]:
    params = _parse_config_json(row.params_json)
    target_units = _sparkie_target_window_units(target_period)
    total_profit = float(row.profit_loss or 0.0)
    daily_target = float(target_profit or 0.0) if str(target_period or "daily") == "daily" else None
    daily_pnl = _sparkie_daily_pnl_summary(params.get("daily_pnl"), daily_target)
    period_profit = (
        float(daily_pnl["average_daily_profit_loss"])
        if str(target_period or "daily") == "daily" and daily_pnl.get("available")
        else total_profit / target_units if target_units else total_profit
    )
    return {
        "id": row.id,
        "symbol": row.symbol,
        "interval": row.interval,
        "algo_name": row.algo_name,
        "status": row.status,
        "score": row.score,
        "profit_loss": row.profit_loss,
        "total_profit_loss": row.profit_loss,
        "estimated_profit_per_period": period_profit,
        "trades": row.trades,
        "win_rate": row.win_rate,
        "max_drawdown": row.max_drawdown,
        "validation_profit_loss": row.validation_profit_loss,
        "validation_trades": row.validation_trades,
        "confidence": row.confidence,
        "trade_size": params.get("sparkie_trade_size"),
        "allocation_usd": params.get("sparkie_allocation_usd"),
        "reference_price": params.get("sparkie_reference_price"),
        "eod_close": params.get("sparkie_eod_close", True),
        "overnight_positions_allowed": params.get("sparkie_overnight_positions_allowed", False),
        "daily_pnl": daily_pnl,
        "params": params,
        "error_message": row.error_message,
    }


def _sparkie_daily_pnl_summary(raw_rows: Any, daily_target: float | None) -> dict[str, Any]:
    if not isinstance(raw_rows, list):
        return {"available": False, "days": []}
    days: list[dict[str, Any]] = []
    cumulative = 0.0
    for raw in raw_rows:
        if not isinstance(raw, dict) or not raw.get("date"):
            continue
        profit = float(raw.get("profit_loss") or 0.0)
        cumulative = round(cumulative + profit, 2)
        item = {
            "date": str(raw["date"]),
            "profit_loss": round(profit, 2),
            "trades": int(raw.get("trades") or 0),
            "cumulative_profit_loss": cumulative,
        }
        if daily_target is not None:
            item["target_met"] = profit >= daily_target
        days.append(item)
    if not days:
        return {"available": False, "days": []}
    hit_days = sum(1 for item in days if item.get("target_met") is True)
    profitable_days = sum(1 for item in days if float(item["profit_loss"]) > 0.0)
    target_interval = _wilson_interval(hit_days, len(days)) if daily_target is not None else None
    return {
        "available": True,
        "days": days,
        "days_tested": len(days),
        "daily_target": daily_target,
        "target_hit_days": hit_days if daily_target is not None else None,
        "target_missed_days": len(days) - hit_days if daily_target is not None else None,
        "target_hit_rate": round(hit_days / len(days), 4) if daily_target is not None else None,
        "profitable_days": profitable_days,
        "non_profitable_days": len(days) - profitable_days,
        "profitable_day_rate": round(profitable_days / len(days), 4),
        "no_trade_days": sum(1 for item in days if int(item["trades"]) == 0),
        "cumulative_profit_loss": cumulative,
        "average_daily_profit_loss": round(cumulative / len(days), 2),
        "historical_live_readiness": {
            "observed_target_hit_rate": round(hit_days / len(days), 4) if daily_target is not None else None,
            "target_hit_rate_95pct_low": target_interval[0] if target_interval else None,
            "target_hit_rate_95pct_high": target_interval[1] if target_interval else None,
            "evidence_level": "insufficient" if len(days) < 10 else "limited" if len(days) < 20 else "moderate",
            "message": "Historical evidence only; this is not a promise of live-trading profit.",
        },
    }


def _sparkie_monthly_projection(daily_pnl: Any, account_equity: float) -> dict[str, Any] | None:
    if not isinstance(daily_pnl, dict) or not daily_pnl.get("available"):
        return None
    profits = [float(row.get("profit_loss") or 0.0) for row in daily_pnl.get("days") or [] if isinstance(row, dict)]
    if not profits:
        return None
    window = min(21, len(profits))
    values = sorted(sum(profits[index:index + window]) for index in range(max(len(profits) - window + 1, 1)))
    def percentile(fraction: float) -> float:
        if len(values) == 1:
            return values[0]
        position = (len(values) - 1) * fraction
        lower, upper = int(math.floor(position)), int(math.ceil(position))
        return values[lower] if lower == upper else values[lower] + (values[upper] - values[lower]) * (position - lower)
    denominator = max(float(account_equity or 0.0), 1.0)
    conservative, typical, strong = percentile(0.25), percentile(0.50), percentile(0.75)
    return {
        "trading_days_per_month": window,
        "conservative_monthly_pnl": round(conservative, 2),
        "typical_monthly_pnl": round(typical, 2),
        "strong_monthly_pnl": round(strong, 2),
        "conservative_monthly_return_pct": round(conservative / denominator * 100.0, 2),
        "typical_monthly_return_pct": round(typical / denominator * 100.0, 2),
        "strong_monthly_return_pct": round(strong / denominator * 100.0, 2),
    }


def _sparkie_live_readiness(best: dict[str, Any] | None, requested_confidence: float) -> dict[str, Any]:
    """Conservatively decide whether historical daily evidence can unlock live.

    The Wilson lower bound prevents a small sample such as 4 target hits in 6
    days from being described as a 60% live expectation.
    """
    daily = (best or {}).get("daily_pnl") if isinstance(best, dict) else None
    if not isinstance(daily, dict) or not daily.get("available"):
        return {"eligible": False, "reason": "No daily P/L evidence is available for the selected candidate."}
    days = int(daily.get("days_tested") or 0)
    hit_rate = daily.get("target_hit_rate")
    readiness = daily.get("historical_live_readiness") or {}
    lower_bound = readiness.get("target_hit_rate_95pct_low")
    if hit_rate is None:
        profitable_days = int(daily.get("profitable_days") or 0)
        hit_rate = round(profitable_days / days, 4) if days else 0.0
        interval = _wilson_interval(profitable_days, days)
        lower_bound = interval[0] if interval else None
    if days < SPARKIE_MIN_LIVE_EVIDENCE_DAYS:
        return {
            "eligible": False,
            "reason": f"Only {days} historical daily observations are available; Sparkie requires at least {SPARKIE_MIN_LIVE_EVIDENCE_DAYS} before Live Mirror.",
            "days_tested": days,
            "observed_target_hit_rate": hit_rate,
            "target_hit_rate_95pct_low": lower_bound,
        }
    if lower_bound is None or float(lower_bound) < float(requested_confidence):
        return {
            "eligible": False,
            "reason": f"Historical confidence is not strong enough for Live Mirror: 95% lower profitable-day estimate is {float(lower_bound or 0.0) * 100:.1f}%, below the requested {float(requested_confidence) * 100:.1f}%.",
            "days_tested": days,
            "observed_target_hit_rate": hit_rate,
            "target_hit_rate_95pct_low": lower_bound,
        }
    return {
        "eligible": True,
        "reason": "Historical daily evidence meets Sparkie's Live Mirror gate; it is still not a profit guarantee.",
        "days_tested": days,
        "observed_target_hit_rate": hit_rate,
        "target_hit_rate_95pct_low": lower_bound,
    }


def _wilson_interval(successes: int, trials: int, z_score: float = 1.96) -> tuple[float, float] | None:
    if trials <= 0:
        return None
    observed = max(0.0, min(1.0, float(successes) / float(trials)))
    z_squared = z_score * z_score
    denominator = 1.0 + z_squared / trials
    center = (observed + z_squared / (2.0 * trials)) / denominator
    spread = z_score * math.sqrt((observed * (1.0 - observed) + z_squared / (4.0 * trials)) / trials) / denominator
    return round(max(0.0, center - spread), 4), round(min(1.0, center + spread), 4)


def _sparkie_event_payload(row: SparkieEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "level": row.level,
        "stage": row.stage,
        "message": row.message,
        "payload": _parse_json_value(row.payload_json, {}),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _sparkie_v2_next_step(job: SparkieJob) -> str:
    status = str(job.status or "")
    if status in ACTIVE_STATUSES:
        return "Sparkie is running in the background. You can close the laptop and return later from History."
    if status == "completed":
        if job.recommendation == "paper_candidate":
            return "Replay verification is queued for the final pick. Review it before considering Live Mirror."
        return "Sparkie recommends paper-only until stronger evidence appears."
    if status == "rejected":
        return "Sparkie rejected this goal or found no acceptable candidate. Adjust target or let the stock bucket expand later."
    if status == "stopped":
        return "Sparkie was stopped. Start a fresh evaluation when ready."
    return "Sparkie hit an error. Review events, then start a fresh evaluation."


def _parse_json_value(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


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
    # Optimizer results are ranked with one-share P/L. Replay converts a
    # capped dollar allocation into shares from the first bar price.
    return 1.0


def _sparkie_trade_quantity(
    *,
    user_id: int,
    symbol: str,
    interval: str,
    start_date: str,
    end_date: str,
    account_equity: float,
) -> float:
    provider = ReplayDataProvider(
        user_id=user_id,
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        interval=interval,
    )
    first_bar = provider.bars.iloc[0]
    price = float(first_bar.get("open") or first_bar.get("close") or 0.0)
    if not math.isfinite(price) or price <= 0:
        raise ValueError(f"Sparkie could not determine a valid starting price for {symbol}.")
    allocation = max(float(account_equity) * 0.10, 1.0)
    quantity = int(allocation // price)
    if quantity < 1 and price <= float(account_equity):
        quantity = 1
    if quantity < 1:
        raise ValueError(
            f"{symbol} costs about ${price:,.2f}, above available Sparkie account equity."
        )
    return float(quantity)


def _sparkie_replay_config(
    *,
    algo_name: str,
    account_equity: float,
    target_profit: float,
    target_period: str,
    finalist: dict | None = None,
) -> dict:
    finalist = finalist or {}
    daily_stop = max(25.0, float(account_equity) * 0.01)
    allocation = max(float(account_equity) * 0.10, 1.0)
    cfg = _build_mm_replay_config(
        algo_name=algo_name,
        eod_auto_close="on",
        allow_short_selling="on",
        stop_loss_usd=finalist.get("stop_loss_usd", finalist.get("hard_stop_usd", min(daily_stop, allocation * 0.02))),
        trailing_profit_usd=finalist.get("trailing_profit_usd", max(5.0, min(allocation * 0.01, float(target_profit) * 0.5))),
        stop_loss_pct=finalist.get("stop_loss_pct", finalist.get("per_share_stop_pct", DEFAULT_REPLAY_MM_CONFIG["stop_loss_pct"])),
        trailing_profit_pct=finalist.get("trailing_profit_pct", finalist.get("per_share_trailing_profit_pct", DEFAULT_REPLAY_MM_CONFIG["trailing_profit_pct"])),
        prob_trail_drop=finalist.get("prob_trail_drop", DEFAULT_REPLAY_MM_CONFIG["prob_trail_drop"]),
        prob_exit_mode=finalist.get("prob_exit_mode", DEFAULT_REPLAY_MM_CONFIG["prob_exit_mode"]),
        long_fixed_exit_prob=finalist.get("long_fixed_exit_prob", DEFAULT_REPLAY_MM_CONFIG["long_fixed_exit_prob"]),
        short_fixed_exit_prob=finalist.get("short_fixed_exit_prob", DEFAULT_REPLAY_MM_CONFIG["short_fixed_exit_prob"]),
        long_entry_prob=finalist.get("long_entry_prob", DEFAULT_REPLAY_MM_CONFIG["long_entry_prob"]),
        short_entry_prob=finalist.get("short_entry_prob", DEFAULT_REPLAY_MM_CONFIG["short_entry_prob"]),
        entry_confirmation_bars=finalist.get("entry_confirmation_bars", DEFAULT_REPLAY_MM_CONFIG["entry_confirmation_bars"]),
    )
    for key in (
        "prob_smoothing_bars",
        "min_prob_advantage",
        "model_refresh_mode",
        "model_max_age_minutes",
        "min_new_bars_before_retrain",
    ):
        if finalist.get(key) is not None:
            cfg[key] = finalist[key]
    cfg["daily_loss_limit_usd"] = daily_stop
    cfg["sparkie_allocation_usd"] = round(float(account_equity) * 0.10, 2)
    cfg["sparkie_optimizer_parameters_applied"] = bool(finalist)
    cfg["sparkie_fast_replay"] = True
    return cfg


def _sparkie_backtest_scan(
    *,
    user_id: int,
    symbols: list[str],
    intervals: list[str],
    algos: list[str],
    account_equity: float,
    lookback_days: int | None,
    finalist_count: int,
) -> dict:
    top_rows: list[dict] = []
    errors: list[str] = []
    allowed_algos = set(algos or ALLOWED_MM_ALGOS)
    allowed_symbols = set(symbols or [])
    allowed_intervals = set(intervals or [])
    optimizer_cache = _load_optimizer_cache()

    if not optimizer_cache or float(optimizer_cache.get("trade_size") or 0.0) != 1.0:
        scan = _queue_sparkie_optimizer_scan(
            user_id=user_id,
            symbols=symbols,
            intervals=intervals,
            account_equity=account_equity,
            lookback_days=lookback_days,
        )
        scan["errors"] = [
            "No compatible one-share Optimizer results found. Sparkie queued a normalized Optimizer scan automatically."
        ]
        return scan

    candidate_rows = list(optimizer_cache.get("top") or [])
    if not candidate_rows:
        candidate_rows = list(
            optimizer_cache.get("best_by_symbol_algo")
            or optimizer_cache.get("best_by_algo")
            or []
        )

    for row in candidate_rows:
        symbol = str(row.get("symbol") or "").upper()
        interval = str(row.get("interval") or "").lower()
        algo_name = str(row.get("algo_name") or "")
        if symbol not in allowed_symbols:
            continue
        if interval not in allowed_intervals:
            continue
        if algo_name not in allowed_algos:
            continue
        if float(row.get("total_profit") or 0.0) <= 0:
            continue
        if float(row.get("validation_total_profit") or 0.0) <= 0:
            continue
        if int(row.get("num_trades") or 0) < 3 or int(row.get("validation_num_trades") or 0) < 3:
            continue
        top_rows.append(_sparkie_backtest_row(row))

    top_rows.sort(
        key=lambda row: (
            float(row.get("score") or 0.0),
            float(row.get("total_profit") or 0.0),
            float(row.get("win_rate") or 0.0),
            -abs(float(row.get("max_drawdown") or 0.0)),
        ),
        reverse=True,
    )

    finalist_combos = _sparkie_unique_finalist_combos(top_rows, finalist_count)

    optimizer_job = None
    if not finalist_combos:
        optimizer_job = _queue_sparkie_optimizer_scan(
            user_id=user_id,
            symbols=symbols,
            intervals=intervals,
            account_equity=account_equity,
            lookback_days=lookback_days,
        )

    return {
        "status": "optimizing" if optimizer_job else "completed",
        "method": "cached_optimizer_prefilter",
        "optimizer_job_id": (optimizer_job or {}).get("optimizer_job_id"),
        "source_run_id": optimizer_cache.get("run_id") or "latest",
        "symbols_scanned": len({row.get("symbol") for row in top_rows if row.get("symbol")}),
        "intervals": intervals,
        "algos_requested": algos,
        "tested_combinations": int(optimizer_cache.get("tested_combinations") or 0),
        "candidate_count": len(top_rows),
        "finalist_count": len(finalist_combos),
        "finalist_combos": finalist_combos,
        "top": top_rows[:20],
        "errors": errors[:30],
        "message": (
            f"Sparkie picked {len(finalist_combos)} Replay finalist combo(s) from cached Optimizer results."
            if finalist_combos
            else "Sparkie found no cached finalists, so it queued an Optimizer scan automatically."
        ),
    }


def _sparkie_backtest_row(row: dict) -> dict:
    keys = (
        "symbol",
        "interval",
        "algo_name",
        "score",
        "confidence",
        "total_profit",
        "num_trades",
        "win_rate",
        "profit_factor",
        "max_drawdown",
        "validation_total_profit",
        "validation_num_trades",
        "validation_win_rate",
        "holdout_num_trades",
        "backtest_method_label",
        "long_entry_prob",
        "short_entry_prob",
        "prob_trail_drop",
        "hard_stop_usd",
        "stop_loss_usd",
        "trailing_profit_usd",
        "stop_loss_pct",
        "trailing_profit_pct",
        "per_share_stop_pct",
        "per_share_trailing_profit_pct",
        "prob_exit_mode",
        "long_fixed_exit_prob",
        "short_fixed_exit_prob",
        "prob_smoothing_bars",
        "entry_confirmation_bars",
        "min_prob_advantage",
        "model_refresh_mode",
        "model_max_age_minutes",
        "min_new_bars_before_retrain",
    )
    return {key: row.get(key) for key in keys}


def _sparkie_unique_finalist_combos(rows: list[dict], finalist_count: int) -> list[dict]:
    finalists: list[dict] = []
    seen_combos: set[tuple[str, str, str]] = set()
    for row in rows:
        key = (
            str(row.get("symbol") or "").upper(),
            str(row.get("interval") or "").lower(),
            str(row.get("algo_name") or ""),
        )
        if not all(key) or key in seen_combos:
            continue
        seen_combos.add(key)
        finalists.append(dict(row))
        if len(finalists) >= finalist_count:
            break
    return finalists


def _queue_sparkie_optimizer_scan(
    *,
    user_id: int,
    symbols: list[str],
    intervals: list[str],
    account_equity: float,
    lookback_days: int | None,
) -> dict:
    _cleanup_cheatsheet_jobs()
    optimizer_job_id = f"sparkie-opt-{user_id}-{uuid.uuid4().hex[:12]}"
    now = time.time()
    req = CheatSheetRequest(
        symbol="SPARKIE_LIST",
        intervals=tuple(intervals or ("5min",)),
        user_id=user_id,
        trade_size=_sparkie_trade_size(account_equity),
        builder_days=max(int(lookback_days or DEFAULT_REPLAY_MM_CONFIG["builder_days"]), 30),
        k_forward=int(DEFAULT_REPLAY_MM_CONFIG["k_forward"]),
        profile="quick",
        allow_short=True,
        eod_close=True,
    )
    with _cheatsheet_jobs_lock:
        _cheatsheet_jobs[optimizer_job_id] = {
            "user_id": user_id,
            "status": "queued",
            "message": "Sparkie queued Optimizer/backtest scan.",
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        _store_cheatsheet_job(optimizer_job_id, _cheatsheet_jobs[optimizer_job_id])

    queued = _queue_qqq_optimizer_job(
        job_id=optimizer_job_id,
        req=req,
        symbols=symbols,
        resume=False,
    )
    return {
        "status": "optimizing",
        "method": "sparkie_optimizer_batch",
        "optimizer_job_id": optimizer_job_id,
        "backend": queued.get("backend"),
        "task_id": queued.get("task_id"),
        "symbols_scanned": 0,
        "intervals": intervals,
        "tested_combinations": 0,
        "candidate_count": 0,
        "finalist_count": 0,
        "finalist_combos": [],
        "top": [],
        "errors": [],
        "message": f"Sparkie queued Optimizer/backtest scan for {len(symbols)} symbol(s).",
    }


def _create_sparkie_optimizer_job(
    *,
    db: Session,
    user_id: int,
    job_id: str,
    payload: SparkieEvaluationRequest,
    preview: dict,
    backtest_scan: dict,
    skipped_sessions: list[dict],
) -> dict:
    cfg = {
        "sparkie_job_id": job_id,
        "sparkie_optimizer_job_id": backtest_scan.get("optimizer_job_id"),
        "sparkie_optimizer_task_id": backtest_scan.get("task_id"),
        "sparkie_target_profit": float(payload.target_profit),
        "sparkie_target_period": payload.target_period,
        "sparkie_account_equity": float(payload.account_equity),
        "sparkie_confidence_level": float(payload.confidence_level),
        "sparkie_symbols": _clean_symbols(payload.symbols),
        "sparkie_intervals": _clean_intervals(payload.intervals),
        "sparkie_algos": _clean_algos(payload.algos),
        "sparkie_replay_finalists": int(payload.replay_finalists),
        "sparkie_used_backtest_prefilter": True,
        "sparkie_backtest_message": backtest_scan.get("message"),
    }
    session = ReplaySession(
        user_id=user_id,
        symbol="SPARKIE",
        start_date=preview.get("start_date"),
        end_date=preview.get("end_date"),
        interval="optimizer",
        algo_name="OptimizerPrefilter",
        speed=1.0,
        trade_size=_sparkie_trade_size(payload.account_equity),
        status="OPTIMIZING",
        config_json=json.dumps(cfg, separators=(",", ":"), sort_keys=True),
    )
    db.add(session)
    db.commit()
    return {
        "ok": True,
        "status": "optimizing",
        "job_id": job_id,
        "message": "Sparkie queued the Optimizer/backtest scan. Replay finalists will be queued automatically after Optimizer finishes.",
        "preview": preview,
        "backtest_scan": backtest_scan,
        "sessions": skipped_sessions + [
            {
                "session_id": session.id,
                "symbol": "SPARKIE",
                "interval": "optimizer",
                "algo_name": "OptimizerPrefilter",
                "status": "OPTIMIZING",
            }
        ],
        "next_step": "Sparkie is optimizing in the background. Keep this page open or return later; Sparkie will queue Replay finalists automatically.",
    }


def _replay_session_pnl(db: Session, session_id: int) -> tuple[float, int]:
    from app.models.replay import ReplayTradeHistory

    rows = db.query(ReplayTradeHistory.profit_loss).filter_by(session_id=session_id).all()
    values = [float(row[0] or 0.0) for row in rows]
    return sum(values), len(values)


def _sparkie_rows_for_job(db: Session, user_id: int, job_id: str) -> list[ReplaySession]:
    return (
        db.query(ReplaySession)
        .filter(ReplaySession.user_id == user_id)
        .filter(ReplaySession.config_json.like(f'%"sparkie_job_id":"{job_id}"%'))
        .order_by(ReplaySession.id.desc())
        .all()
    )


def _latest_active_sparkie_job_id(db: Session, user_id: int) -> str | None:
    rows = (
        db.query(ReplaySession)
        .filter(ReplaySession.user_id == user_id)
        .filter(ReplaySession.config_json.like('%"sparkie_job_id":"sparkie-%'))
        .filter(ReplaySession.status.in_(("PENDING", "QUEUED", "STARTING", "RUNNING", "OPTIMIZING", "PREPARING_REPLAY")))
        .order_by(ReplaySession.id.desc())
        .limit(100)
        .all()
    )
    if rows:
        _mark_stale_sparkie_sessions(db, rows)
        for row in rows:
            db.refresh(row)
    for row in rows:
        if str(row.status or "").upper() not in {"PENDING", "QUEUED", "STARTING", "RUNNING", "OPTIMIZING", "PREPARING_REPLAY"}:
            continue
        job_id = str(_parse_config_json(row.config_json).get("sparkie_job_id") or "")
        if job_id and SPARKIE_JOB_PATTERN.fullmatch(job_id):
            return job_id
    return None


def _stop_sparkie_rows(db: Session, rows: list[ReplaySession], *, reason: str) -> int:
    stopped = 0
    for row in rows:
        if str(row.status or "").upper() in {"COMPLETED", "STOPPED", "ERROR"}:
            continue
        cfg = _parse_config_json(row.config_json)
        if str(row.algo_name or "") == "OptimizerPrefilter":
            _cancel_sparkie_optimizer(cfg)
        else:
            try:
                from app.tasks.replay_tasks import stop_replay_session_task

                stop_replay_session_task.apply_async(args=(int(row.id),), queue="replay")
            except Exception:
                pass
        try:
            stop_session(db, int(row.id))
        except Exception:
            pass

        row.status = "STOPPED"
        row.error_message = reason
        row.updated_at = datetime.utcnow()
        row.stopped_at = datetime.utcnow()
        stopped += 1

    if stopped:
        db.commit()
    return stopped


def _cancel_sparkie_optimizer(cfg: dict) -> None:
    optimizer_job_id = str(cfg.get("sparkie_optimizer_job_id") or "")
    task_id = str(cfg.get("sparkie_optimizer_task_id") or "")
    if optimizer_job_id:
        with _cheatsheet_jobs_lock:
            job = dict(_cheatsheet_jobs.get(optimizer_job_id) or {})
            job.update(
                {
                    "status": "cancelled",
                    "cancel_requested": True,
                    "message": "Sparkie optimizer stop requested.",
                    "updated_at": time.time(),
                }
            )
            _cheatsheet_jobs[optimizer_job_id] = job
            _store_cheatsheet_job(optimizer_job_id, job)
    if task_id:
        try:
            from app.celery_app import celery_app

            celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
        except Exception:
            pass


def _sparkie_job_summary(
    db: Session,
    job_id: str,
    rows: list[ReplaySession],
    *,
    include_sessions: bool,
) -> dict:
    _mark_stale_sparkie_sessions(db, rows)
    for row in rows:
        db.refresh(row)

    optimizer_scan = _advance_sparkie_optimizer_job(db, job_id, rows)
    if optimizer_scan and optimizer_scan.get("replay_queued"):
        rows = _sparkie_rows_for_job(db, rows[0].user_id, job_id)

    preview = _sparkie_preview_from_sessions(rows)
    sessions = []
    completed = 0
    total_profit = 0.0
    total_trades = 0
    progress_units = 0.0
    replay_session_count = 0

    for row in rows:
        is_optimizer_placeholder = str(row.algo_name or "") == "OptimizerPrefilter"
        session_profit, trade_count = _replay_session_pnl(db, row.id)
        if not is_optimizer_placeholder and str(row.status or "").upper() == "COMPLETED":
            replay_session_count += 1
            total_profit += session_profit
            total_trades += trade_count
        elif not is_optimizer_placeholder:
            replay_session_count += 1

        status = str(row.status or "").upper()
        terminal = status in {"COMPLETED", "STOPPED", "ERROR", "DATA_ERROR", "OPTIMIZED"}
        if not is_optimizer_placeholder:
            if terminal:
                completed += 1
                progress_units += 1.0
            else:
                progress_units += _sparkie_session_progress(row)

        sessions.append(
            {
                "session_id": row.id,
                "symbol": row.symbol,
                "interval": row.interval,
                "algo_name": row.algo_name,
                "status": row.status,
                "profit_loss": round(session_profit, 2),
                "trade_count": trade_count,
                "quantity": float(row.trade_size or 0.0),
                "position_value_limit": round(
                    float(_parse_config_json(row.config_json).get("sparkie_allocation_usd") or 0.0),
                    2,
                ),
                "error_message": row.error_message,
                "current_bar_idx": row.current_bar_idx or 0,
                "total_bars": row.total_bars or 0,
                "progress_pct": round(_sparkie_session_progress(row) * 100.0, 1),
                "age_minutes": _session_age_minutes(row),
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        )

    total_sessions = replay_session_count
    replay_statuses = [
        str(row.status or "").upper()
        for row in rows
        if str(row.algo_name or "") != "OptimizerPrefilter"
    ]
    all_terminal = bool(total_sessions) and completed == total_sessions
    if all_terminal and any(value in {"ERROR", "DATA_ERROR"} for value in replay_statuses):
        status = "completed_with_errors"
    elif all_terminal and replay_statuses and all(value == "STOPPED" for value in replay_statuses):
        status = "stopped"
    else:
        status = "completed" if all_terminal else "running"
    if optimizer_scan and optimizer_scan.get("status") in {"error", "stopped"} and not total_sessions:
        status = str(optimizer_scan.get("status"))
    elif optimizer_scan and optimizer_scan.get("status") == "optimizing" and not any(
        str(row.algo_name or "") != "OptimizerPrefilter" for row in rows
    ):
        status = "optimizing"
    elif not total_sessions and optimizer_scan and optimizer_scan.get("status") == "completed":
        status = "completed"
    best_session = _best_sparkie_session(sessions)
    percent_complete = round((progress_units / total_sessions) * 100.0, 1) if total_sessions else 0.0
    created_at = min((row.created_at for row in rows if row.created_at), default=None)
    updated_at = max(
        ((row.updated_at or row.created_at) for row in rows if (row.updated_at or row.created_at)),
        default=None,
    )

    result = {
        "ok": True,
        "job_id": job_id,
        "status": status,
        "completed_sessions": completed,
        "total_sessions": total_sessions,
        "percent_complete": percent_complete,
        "total_profit_loss": round(total_profit, 2),
        "total_trades": total_trades,
        "best_session": best_session,
        "preview": preview,
        "backtest_scan": optimizer_scan or _sparkie_backtest_from_sessions(rows),
        "created_at": created_at.isoformat() if created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "next_step": _sparkie_status_next_step(status, total_profit, total_trades),
    }
    if include_sessions:
        result["sessions"] = sessions
    return result


def _sparkie_session_progress(row: ReplaySession) -> float:
    total_bars = int(row.total_bars or 0)
    if total_bars <= 0:
        return 0.0
    current_bar = max(int(row.current_bar_idx or 0), 0)
    return min(max((current_bar + 1) / total_bars, 0.0), 1.0)


def _advance_sparkie_optimizer_job(
    db: Session,
    job_id: str,
    rows: list[ReplaySession],
) -> dict | None:
    cfg = _first_sparkie_config(rows)
    optimizer_job_id = str(cfg.get("sparkie_optimizer_job_id") or "")
    if not optimizer_job_id:
        return None

    optimizer_rows = [row for row in rows if str(row.algo_name or "") == "OptimizerPrefilter"]
    if optimizer_rows:
        placeholder_status = str(optimizer_rows[0].status or "").upper()
        if placeholder_status in {"ERROR", "STOPPED"}:
            return {
                "status": placeholder_status.lower(),
                "method": "sparkie_optimizer_batch",
                "optimizer_job_id": optimizer_job_id,
                "message": optimizer_rows[0].error_message or "Sparkie optimizer did not finish.",
                "finalist_count": 0,
                "finalist_combos": [],
            }
        if placeholder_status == "PREPARING_REPLAY":
            return {
                "status": "preparing_replay",
                "method": "sparkie_optimizer_batch",
                "optimizer_job_id": optimizer_job_id,
                "message": "Sparkie is preparing data and queueing Replay finalists.",
                "finalist_count": 0,
                "finalist_combos": [],
            }

    real_replay_rows = [
        row
        for row in rows
        if str(row.algo_name or "") != "OptimizerPrefilter"
    ]
    snapshot = _snapshot_cheatsheet_job(optimizer_job_id, rows[0].user_id) or {}
    result = snapshot.get("result") or _load_optimizer_run(optimizer_job_id)
    status = str(snapshot.get("status") or (result or {}).get("batch_status") or "queued")

    if real_replay_rows:
        return _sparkie_backtest_from_sessions(rows)

    if status not in {"succeeded", "complete", "partial"} or not result:
        return {
            "status": "optimizing",
            "method": "sparkie_optimizer_batch",
            "optimizer_job_id": optimizer_job_id,
            "message": snapshot.get("message") or "Sparkie Optimizer/backtest scan is still running.",
            "symbols_scanned": int((result or {}).get("completed_symbol_count") or 0),
            "tested_combinations": int((result or {}).get("tested_combinations") or 0),
            "candidate_count": len((result or {}).get("top") or []),
            "finalist_count": 0,
            "finalist_combos": [],
            "top": [],
            "errors": list((result or {}).get("errors") or [])[:30],
        }

    finalists = _sparkie_finalists_from_optimizer_result(
        result=result,
        symbols=list(cfg.get("sparkie_symbols") or []),
        intervals=list(cfg.get("sparkie_intervals") or []),
        algos=list(cfg.get("sparkie_algos") or []),
        finalist_count=int(cfg.get("sparkie_replay_finalists") or 3),
    )
    scan = {
        "status": "completed",
        "method": "sparkie_optimizer_batch",
        "optimizer_job_id": optimizer_job_id,
        "source_run_id": result.get("run_id") or optimizer_job_id,
        "message": (
            f"Sparkie Optimizer scan finished and selected {len(finalists)} Replay finalist(s)."
            if finalists
            else "Sparkie Optimizer scan finished but found no Replay finalists."
        ),
        "symbols_scanned": int(result.get("completed_symbol_count") or 0),
        "tested_combinations": int(result.get("tested_combinations") or 0),
        "candidate_count": len(result.get("top") or []),
        "finalist_count": len(finalists),
        "finalist_combos": finalists,
        "top": [_sparkie_backtest_row(row) for row in (result.get("top") or [])[:20]],
        "errors": list(result.get("errors") or [])[:30],
    }
    if not finalists:
        for row in rows:
            if str(row.algo_name or "") == "OptimizerPrefilter":
                row.status = "ERROR"
                row.error_message = scan["message"]
                row.updated_at = datetime.utcnow()
        db.commit()
        return scan

    placeholder_id = int(optimizer_rows[0].id) if optimizer_rows else 0
    claimed = (
        db.query(ReplaySession)
        .filter(ReplaySession.id == placeholder_id)
        .filter(ReplaySession.status == "OPTIMIZING")
        .update(
            {
                ReplaySession.status: "PREPARING_REPLAY",
                ReplaySession.updated_at: datetime.utcnow(),
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if not claimed:
        return {
            **scan,
            "status": "preparing_replay",
            "message": "Another Sparkie request is already preparing these Replay finalists.",
        }

    data_errors = _prepare_replay_data(
        user_id=rows[0].user_id,
        symbols=sorted({str(item["symbol"]) for item in finalists}),
        intervals=sorted({str(item["interval"]) for item in finalists}),
        start_date=str(rows[0].start_date),
        end_date=str(rows[0].end_date),
    )
    if data_errors:
        scan["errors"] = (scan.get("errors") or []) + [
            f"{symbol}: {message}" for symbol, message in data_errors.items()
        ]
        finalists = [item for item in finalists if str(item["symbol"]) not in data_errors]
        scan["finalist_combos"] = finalists
        scan["finalist_count"] = len(finalists)
    if not finalists:
        for row in optimizer_rows:
            row.status = "ERROR"
            row.error_message = "Sparkie optimizer finished, but replay data could not be prepared for any finalist."
            row.updated_at = datetime.utcnow()
        db.commit()
        scan["status"] = "error"
        scan["message"] = optimizer_rows[0].error_message if optimizer_rows else scan["message"]
        return scan

    _queue_sparkie_replay_finalists(
        db=db,
        user_id=rows[0].user_id,
        job_id=job_id,
        cfg=cfg,
        rows=rows,
        finalists=finalists,
        scan=scan,
    )
    scan["replay_queued"] = True
    return scan


def _sparkie_finalists_from_optimizer_result(
    *,
    result: dict,
    symbols: list[str],
    intervals: list[str],
    algos: list[str],
    finalist_count: int,
) -> list[dict]:
    allowed_symbols = {str(symbol).upper() for symbol in symbols}
    allowed_intervals = {str(interval).lower() for interval in intervals}
    allowed_algos = set(algos or ALLOWED_MM_ALGOS)
    rows = []
    for row in list(result.get("top") or result.get("best_by_symbol_algo") or result.get("best_by_algo") or []):
        symbol = str(row.get("symbol") or "").upper()
        interval = str(row.get("interval") or "").lower()
        algo_name = str(row.get("algo_name") or "")
        if symbol not in allowed_symbols or interval not in allowed_intervals or algo_name not in allowed_algos:
            continue
        if float(row.get("total_profit") or 0.0) <= 0:
            continue
        if float(row.get("validation_total_profit") or 0.0) <= 0:
            continue
        if int(row.get("num_trades") or 0) < 3 or int(row.get("validation_num_trades") or 0) < 3:
            continue
        rows.append(_sparkie_backtest_row(row))
    rows.sort(
        key=lambda row: (
            float(row.get("score") or 0.0),
            float(row.get("total_profit") or 0.0),
            float(row.get("win_rate") or 0.0),
            -abs(float(row.get("max_drawdown") or 0.0)),
        ),
        reverse=True,
    )
    return _sparkie_unique_finalist_combos(rows, finalist_count)


def _queue_sparkie_replay_finalists(
    *,
    db: Session,
    user_id: int,
    job_id: str,
    cfg: dict,
    rows: list[ReplaySession],
    finalists: list[dict],
    scan: dict,
) -> None:
    for row in rows:
        if str(row.algo_name or "") == "OptimizerPrefilter":
            row.status = "OPTIMIZED"
            row.error_message = "Sparkie optimizer complete; replay finalists queued."
            row.updated_at = datetime.utcnow()

    queued_rows: list[tuple[ReplaySession, dict]] = []
    for item in finalists:
        replay_cfg = _sparkie_replay_config(
            algo_name=str(item["algo_name"]),
            account_equity=float(cfg.get("sparkie_account_equity") or 0.0),
            target_profit=float(cfg.get("sparkie_target_profit") or 0.0),
            target_period=str(cfg.get("sparkie_target_period") or "daily"),
            finalist=item,
        )
        replay_cfg.update(
            {
                "sparkie_job_id": job_id,
                "sparkie_optimizer_job_id": cfg.get("sparkie_optimizer_job_id"),
                "sparkie_target_profit": float(cfg.get("sparkie_target_profit") or 0.0),
                "sparkie_target_period": str(cfg.get("sparkie_target_period") or "daily"),
                "sparkie_account_equity": float(cfg.get("sparkie_account_equity") or 0.0),
                "sparkie_confidence_level": float(cfg.get("sparkie_confidence_level") or 0.60),
                "sparkie_used_backtest_prefilter": True,
                "sparkie_backtest_finalists": finalists,
                "sparkie_backtest_message": scan.get("message"),
            }
        )
        session = ReplaySession(
            user_id=user_id,
            symbol=str(item["symbol"]),
            start_date=rows[0].start_date,
            end_date=rows[0].end_date,
            interval=str(item["interval"]),
            algo_name=str(item["algo_name"]),
            speed=20.0,
            trade_size=_sparkie_trade_quantity(
                user_id=user_id,
                symbol=str(item["symbol"]),
                interval=str(item["interval"]),
                start_date=str(rows[0].start_date),
                end_date=str(rows[0].end_date),
                account_equity=float(cfg.get("sparkie_account_equity") or 0.0),
            ),
            status="QUEUED",
            config_json=json.dumps(replay_cfg, separators=(",", ":"), sort_keys=True),
        )
        db.add(session)
        db.flush()
        queued_rows.append((session, replay_cfg))

    # Workers must never receive session ids before their rows are committed.
    db.commit()
    for session, replay_cfg in queued_rows:
        try:
            from app.tasks.replay_tasks import start_replay_session_task

            async_result = start_replay_session_task.apply_async(args=(session.id,), queue="replay")
            session.error_message = None
            replay_cfg["sparkie_replay_task_id"] = async_result.id
            session.config_json = json.dumps(replay_cfg, separators=(",", ":"), sort_keys=True)
        except Exception as exc:
            session.status = "ERROR"
            session.error_message = f"Sparkie replay queue failed: {str(exc)[:400]}"
    db.commit()


def _sparkie_backtest_from_sessions(rows: list[ReplaySession]) -> dict | None:
    cfg = _first_sparkie_config(rows)
    if not cfg.get("sparkie_used_backtest_prefilter"):
        return None
    finalists = cfg.get("sparkie_backtest_finalists") or []
    return {
        "status": "completed",
        "method": "fast_backtest_prefilter",
        "message": cfg.get("sparkie_backtest_message") or f"Sparkie used backtest scan to pick {len(finalists)} replay finalist(s).",
        "finalist_count": len(finalists),
        "finalist_combos": finalists,
    }


def _mark_stale_sparkie_sessions(db: Session, rows: list[ReplaySession]) -> None:
    now = datetime.utcnow()
    changed = False
    for row in rows:
        status = str(row.status or "").upper()
        if status not in {"RUNNING", "QUEUED", "STARTING", "PENDING", "OPTIMIZING", "PREPARING_REPLAY"}:
            continue
        age_start = row.updated_at or row.started_at or row.created_at or now
        if age_start.tzinfo is not None:
            age_start = age_start.replace(tzinfo=None)
        current_bar = int(row.current_bar_idx or 0)
        total_bars = int(row.total_bars or 0)
        created_at = row.created_at or age_start
        if created_at.tzinfo is not None:
            created_at = created_at.replace(tzinfo=None)
        is_optimizer = str(row.algo_name or "") == "OptimizerPrefilter"
        max_minutes = SPARKIE_OPTIMIZER_MAX_MINUTES if is_optimizer else SPARKIE_REPLAY_MAX_MINUTES
        stalled = now - age_start > timedelta(minutes=SPARKIE_STALE_MINUTES)
        exceeded_deadline = now - created_at > timedelta(minutes=max_minutes)
        if stalled or exceeded_deadline:
            if is_optimizer:
                _cancel_sparkie_optimizer(_parse_config_json(row.config_json))
            else:
                try:
                    stop_session(db, int(row.id))
                except Exception:
                    pass
            detail = "with no replay progress" if current_bar <= 0 or total_bars <= 0 else "with no replay update"
            row.status = "ERROR"
            row.error_message = (
                f"Sparkie stopped this {'optimizer' if is_optimizer else 'replay'} after its "
                f"{max_minutes}-minute maximum runtime."
                if exceeded_deadline
                else f"Sparkie stopped this session after {SPARKIE_STALE_MINUTES} minutes {detail}."
            )
            row.updated_at = now
            row.stopped_at = now
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
    if status in {"completed_with_errors", "error"}:
        return "Sparkie finished with errors. Do not use Live Mirror; review the failed sessions and run a fresh evaluation."
    if status == "stopped":
        return "Sparkie was stopped. Start a fresh evaluation when ready."
    if status != "completed":
        return "Sparkie is still running replay sessions in the background. You can refresh this page or reopen Sparkie History later."
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
