import json
import re
import time
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from typing import Literal

from app.database.connection import get_db
from app.models.replay import ReplayOpenTrade, ReplaySession, ReplayTradeHistory
from app.routes.auth import get_current_user
from app.modules.replay.routes import (
    ALLOWED_MM_ALGOS,
    DEFAULT_REPLAY_MM_CONFIG,
    _cheatsheet_jobs,
    _cheatsheet_jobs_lock,
    _cleanup_cheatsheet_jobs,
    _build_mm_replay_config,
    _load_optimizer_run,
    _load_optimizer_cache,
    _queue_qqq_optimizer_job,
    _snapshot_cheatsheet_job,
    _store_cheatsheet_job,
)
from app.services.backtest_cheatsheet_service import CheatSheetRequest
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
    use_backtest_prefilter: bool = True
    replay_finalists: int = Field(3, ge=1, le=12)


class SparkieClearHistoryRequest(BaseModel):
    confirm: bool = False


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

    job_id = f"sparkie-{user.id}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

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

    backtest_scan = None
    if payload.use_backtest_prefilter:
        backtest_scan = _sparkie_backtest_scan(
            user_id=user.id,
            symbols=runnable_symbols,
            intervals=intervals,
            algos=algos,
            account_equity=payload.account_equity,
            lookback_days=payload.lookback_days,
            finalist_count=payload.replay_finalists,
        )
        finalist_combos = backtest_scan.get("finalist_combos") or []
        if finalist_combos:
            combos = [
                (str(item["symbol"]), str(item["interval"]), str(item["algo_name"]))
                for item in finalist_combos
            ][: payload.max_sessions]
        else:
            combos = []

    if not combos:
        if payload.use_backtest_prefilter and backtest_scan and backtest_scan.get("optimizer_job_id"):
            return _create_sparkie_optimizer_job(
                db=db,
                user_id=user.id,
                job_id=job_id,
                payload=payload,
                preview=preview.to_dict(),
                backtest_scan=backtest_scan,
                skipped_sessions=skipped_sessions,
            )
        return {
            "ok": False,
            "status": "blocked",
            "job_id": None,
            "message": "Sparkie could not prepare replay data or find backtest finalists for the selected symbols.",
            "preview": preview.to_dict(),
            "backtest_scan": backtest_scan,
            "sessions": skipped_sessions,
        }

    trade_size = _sparkie_trade_size(payload.account_equity)
    sessions = []

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
        cfg["sparkie_used_backtest_prefilter"] = bool(payload.use_backtest_prefilter)
        cfg["sparkie_backtest_finalists"] = (backtest_scan or {}).get("finalist_combos") or []
        cfg["sparkie_backtest_message"] = (backtest_scan or {}).get("message")
        cfg["sparkie_optimizer_job_id"] = (backtest_scan or {}).get("optimizer_job_id")

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
        "backtest_scan": backtest_scan,
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

    rows = _sparkie_rows_for_job(db, user.id, job_id)
    if not rows:
        raise HTTPException(status_code=404, detail="Sparkie evaluation job not found.")

    return _sparkie_job_summary(db, job_id, rows, include_sessions=True)


@router.get("/history")
def evaluation_history(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    rows = (
        db.query(ReplaySession)
        .filter(ReplaySession.user_id == user.id)
        .filter(ReplaySession.config_json.like('%"sparkie_job_id":"sparkie-%'))
        .order_by(ReplaySession.id.desc())
        .limit(500)
        .all()
    )

    grouped: dict[str, list[ReplaySession]] = {}
    for row in rows:
        job_id = str(_parse_config_json(row.config_json).get("sparkie_job_id") or "")
        if not job_id:
            continue
        grouped.setdefault(job_id, []).append(row)
        if len(grouped) >= 10 and all(len(value) > 0 for value in grouped.values()):
            continue

    jobs = []
    for job_id, job_rows in list(grouped.items())[:10]:
        jobs.append(_sparkie_job_summary(db, job_id, job_rows, include_sessions=False))

    return {
        "ok": True,
        "jobs": jobs,
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

    stopped = _stop_sparkie_rows(db, rows, reason="Sparkie stop requested.")

    return {
        "ok": True,
        "job_id": job_id,
        "stopped_sessions": stopped,
        "message": "Sparkie stop requested for active replay sessions.",
    }


@router.post("/stop-all")
def stop_all_evaluations(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    rows = (
        db.query(ReplaySession)
        .filter(ReplaySession.user_id == user.id)
        .filter(ReplaySession.config_json.like('%"sparkie_job_id":"sparkie-%'))
        .filter(ReplaySession.status.in_(("PENDING", "QUEUED", "STARTING", "RUNNING")))
        .order_by(ReplaySession.id.desc())
        .all()
    )
    stopped = _stop_sparkie_rows(db, rows, reason="Sparkie stop-all requested.")

    return {
        "ok": True,
        "stopped_sessions": stopped,
        "message": f"Sparkie stop-all requested for {stopped} active replay sessions.",
    }


@router.post("/history/clear")
def clear_history(
    payload: SparkieClearHistoryRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Confirmation is required to clear Sparkie history.")

    rows = (
        db.query(ReplaySession)
        .filter(ReplaySession.user_id == user.id)
        .filter(ReplaySession.config_json.like('%"sparkie_job_id":"sparkie-%'))
        .order_by(ReplaySession.id.desc())
        .all()
    )
    if not rows:
        return {
            "ok": True,
            "deleted_sessions": 0,
            "stopped_sessions": 0,
            "message": "Sparkie history is already clean.",
        }

    stopped = _stop_sparkie_rows(db, rows, reason="Sparkie history cleared.")
    session_ids = [int(row.id) for row in rows if row.id is not None]

    db.query(ReplayOpenTrade).filter(ReplayOpenTrade.session_id.in_(session_ids)).delete(
        synchronize_session=False
    )
    db.query(ReplayTradeHistory).filter(ReplayTradeHistory.session_id.in_(session_ids)).delete(
        synchronize_session=False
    )
    deleted = (
        db.query(ReplaySession)
        .filter(ReplaySession.user_id == user.id)
        .filter(ReplaySession.id.in_(session_ids))
        .delete(synchronize_session=False)
    )
    db.commit()

    return {
        "ok": True,
        "deleted_sessions": deleted,
        "stopped_sessions": stopped,
        "message": f"Cleared {deleted} Sparkie replay session(s).",
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

    if not optimizer_cache:
        scan = _queue_sparkie_optimizer_scan(
            user_id=user_id,
            symbols=symbols,
            intervals=intervals,
            account_equity=account_equity,
            lookback_days=lookback_days,
        )
        scan["errors"] = [
            "No cached Optimizer/backtest results found. Sparkie queued an Optimizer scan automatically."
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

    finalist_rows = top_rows[:finalist_count]
    finalist_combos = [
        {
            "symbol": row["symbol"],
            "interval": row["interval"],
            "algo_name": row["algo_name"],
            "score": row.get("score"),
            "total_profit": row.get("total_profit"),
            "win_rate": row.get("win_rate"),
            "num_trades": row.get("num_trades"),
            "max_drawdown": row.get("max_drawdown"),
        }
        for row in finalist_rows
    ]

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
    )
    return {key: row.get(key) for key in keys}


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


def _stop_sparkie_rows(db: Session, rows: list[ReplaySession], *, reason: str) -> int:
    stopped = 0
    for row in rows:
        if str(row.status or "").upper() in {"COMPLETED", "STOPPED", "ERROR"}:
            continue
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

    for row in rows:
        session_profit, trade_count = _replay_session_pnl(db, row.id)
        total_profit += session_profit
        total_trades += trade_count

        status = str(row.status or "").upper()
        terminal = status in {"COMPLETED", "STOPPED", "ERROR", "DATA_ERROR"}
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
                "error_message": row.error_message,
                "current_bar_idx": row.current_bar_idx or 0,
                "total_bars": row.total_bars or 0,
                "progress_pct": round(_sparkie_session_progress(row) * 100.0, 1),
                "age_minutes": _session_age_minutes(row),
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        )

    total_sessions = len(rows)
    status = "completed" if total_sessions and completed == total_sessions else "running"
    if optimizer_scan and optimizer_scan.get("status") == "optimizing" and not any(
        str(row.algo_name or "") != "OptimizerPrefilter" for row in rows
    ):
        status = "optimizing"
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
    return [
        {
            "symbol": row["symbol"],
            "interval": row["interval"],
            "algo_name": row["algo_name"],
            "score": row.get("score"),
            "total_profit": row.get("total_profit"),
            "win_rate": row.get("win_rate"),
            "num_trades": row.get("num_trades"),
            "max_drawdown": row.get("max_drawdown"),
        }
        for row in rows[:finalist_count]
    ]


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

    for item in finalists:
        replay_cfg = _sparkie_replay_config(
            algo_name=str(item["algo_name"]),
            account_equity=float(cfg.get("sparkie_account_equity") or 0.0),
            target_profit=float(cfg.get("sparkie_target_profit") or 0.0),
            target_period=str(cfg.get("sparkie_target_period") or "daily"),
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
            trade_size=_sparkie_trade_size(float(cfg.get("sparkie_account_equity") or 0.0)),
            status="PENDING",
            config_json=json.dumps(replay_cfg, separators=(",", ":"), sort_keys=True),
        )
        db.add(session)
        db.flush()
        try:
            from app.tasks.replay_tasks import start_replay_session_task

            async_result = start_replay_session_task.apply_async(args=(session.id,), queue="replay")
            session.status = "QUEUED"
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
