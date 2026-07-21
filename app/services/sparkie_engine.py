from __future__ import annotations

import json
import logging
import math
import os
import signal
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.models.replay import ReplaySession
from app.models.sparkie import SparkieCandidate, SparkieEvent, SparkieJob
from app.modules.replay.routes import DEFAULT_REPLAY_MM_CONFIG, _build_mm_replay_config
from app.scripts.replay.data_ingest import fetch_and_save
from app.scripts.replay.replay_data_provider import ReplayDataProvider
from app.services.backtest_cheatsheet_service import CheatSheetRequest, run_cheatsheet


log = logging.getLogger(__name__)

MIN_ACCOUNT_EQUITY = 5000.0
MIN_TRADE_ALLOCATION_USD = 5000.0
DEFAULT_TRADE_ALLOCATION_PCT = 0.20
MAX_TRADE_ALLOCATION_PCT = 1.00
DEFAULT_SYMBOL_BUCKET = ("AAPL", "NVDA", "AMD", "PLTR", "INTC")
DEFAULT_INTERVAL_POLICY = ("15min", "10min", "5min", "1min")
DEFAULT_ALGO_POLICY = ("Algo1_MM", "Algo2_MM", "Algo3_MM")
TERMINAL_STATUSES = {"completed", "rejected", "error", "stopped"}
ACTIVE_STATUSES = {"queued", "preparing_data", "backtesting", "scoring", "verifying_replay"}
MIN_BACKTEST_BARS_PER_INTERVAL = 80
MIN_BACKTEST_TRADING_DAYS = 5


def sparkie_symbol_bucket() -> list[str]:
    raw = os.getenv("SPARKIE_SYMBOL_BUCKET", ",".join(DEFAULT_SYMBOL_BUCKET))
    symbols: list[str] = []
    for value in raw.split(","):
        symbol = value.upper().strip()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols or list(DEFAULT_SYMBOL_BUCKET)


def sparkie_interval_policy() -> list[str]:
    raw = os.getenv("SPARKIE_INTERVAL_POLICY", ",".join(DEFAULT_INTERVAL_POLICY))
    allowed = {"1min", "5min", "10min", "15min", "30min"}
    intervals: list[str] = []
    for value in raw.split(","):
        interval = value.lower().strip()
        if interval in allowed and interval not in intervals:
            intervals.append(interval)
    return intervals or list(DEFAULT_INTERVAL_POLICY)


def sparkie_algo_policy() -> list[str]:
    raw = os.getenv("SPARKIE_ALGO_POLICY", ",".join(DEFAULT_ALGO_POLICY))
    algos: list[str] = []
    for value in raw.split(","):
        algo = value.strip()
        if algo and algo not in algos:
            algos.append(algo)
    return algos or list(DEFAULT_ALGO_POLICY)


def create_sparkie_job(
    db: Session,
    *,
    job_id: str,
    user_id: int,
    account_equity: float,
    target_profit: float,
    target_period: str,
    confidence_level: float,
) -> SparkieJob:
    symbols = sparkie_symbol_bucket()
    intervals = sparkie_interval_policy()
    algos = sparkie_algo_policy()
    request = {
        "account_equity": float(account_equity),
        "target_profit": float(target_profit),
        "target_period": target_period,
        "confidence_level": float(confidence_level),
    }
    job = SparkieJob(
        id=job_id,
        user_id=user_id,
        status="queued",
        stage="queued",
        message="Sparkie job queued.",
        account_equity=float(account_equity),
        target_profit=float(target_profit),
        target_period=target_period,
        confidence_level=float(confidence_level),
        symbol_bucket_json=_json(symbols),
        interval_policy_json=_json(intervals),
        algo_policy_json=_json(algos),
        request_json=_json(request),
        total_steps=len(symbols) + 2,
        completed_steps=0,
        progress_pct=0.0,
    )
    db.add(job)
    db.flush()
    add_event(db, job, "queued", "Sparkie accepted the goal and queued evaluation.")
    return job


def run_sparkie_job(db: Session, job_id: str) -> dict[str, Any]:
    job = db.get(SparkieJob, job_id)
    if not job:
        return {"ok": False, "error": "Sparkie job not found."}

    started = datetime.utcnow()
    _set_job(
        db,
        job,
        status="preparing_data",
        stage="preparing_data",
        message="Sparkie is updating local market data.",
        started_at=started,
    )

    if float(job.account_equity or 0.0) < MIN_ACCOUNT_EQUITY:
        _finish_job(
            db,
            job,
            status="rejected",
            message="Sparkie requires at least $5,000 account equity.",
            recommendation="blocked",
            decision_reason="Account equity is below Sparkie's minimum gate.",
        )
        return {"ok": True, "status": job.status}

    symbols = _json_list(job.symbol_bucket_json) or list(DEFAULT_SYMBOL_BUCKET)
    intervals = _json_list(job.interval_policy_json) or list(DEFAULT_INTERVAL_POLICY)
    algos = set(_json_list(job.algo_policy_json) or list(DEFAULT_ALGO_POLICY))
    lookback_days = int(os.getenv("SPARKIE_DATA_LOOKBACK_DAYS", "45"))
    workers = max(1, min(int(os.getenv("SPARKIE_BACKTEST_WORKERS", "4")), 8))

    total_steps = max(len(symbols) + 2, 1)
    _set_job(db, job, total_steps=total_steps, completed_steps=0, progress_pct=1.0)

    frames_by_symbol: dict[str, dict[str, Any]] = {}
    for idx, symbol in enumerate(symbols, start=1):
        if _stop_requested(db, job_id):
            return _mark_stopped(db, job)
        try:
            meta = refresh_symbol_data(
                user_id=int(job.user_id),
                symbol=symbol,
                intervals=intervals,
                lookback_days=lookback_days,
            )
            frames_by_symbol[symbol] = meta["frames"]
            add_event(
                db,
                job,
                "preparing_data",
                (
                    f"{symbol}: data verified through {meta.get('last_bar') or 'latest cached bar'} "
                    f"({len(meta.get('unique_dates') or [])} trading days)."
                ),
                {
                    "symbol": symbol,
                    "rows": meta.get("rows"),
                    "interval_rows": meta.get("interval_rows"),
                    "last_bar": meta.get("last_bar"),
                    "unique_dates": meta.get("unique_dates"),
                },
            )
        except Exception as exc:
            add_event(db, job, "preparing_data", f"{symbol}: data unavailable: {exc}", level="warning")
        _update_progress(db, job, completed_steps=idx, started_at=started)

    if not frames_by_symbol:
        _finish_job(
            db,
            job,
            status="error",
            message="Sparkie could not prepare data for any bucket symbol.",
            recommendation="blocked",
            decision_reason="No symbols had usable local market data.",
        )
        return {"ok": True, "status": job.status}

    total_backtest_units = sum(
        1
        for frames in frames_by_symbol.values()
        for interval in intervals
        if interval in frames
    )
    total_backtest_steps = max(len(symbols) + total_backtest_units + 2, 1)
    _set_job(
        db,
        job,
        total_steps=total_backtest_steps,
        progress_pct=round((min(len(symbols), total_backtest_steps) / total_backtest_steps) * 100.0, 1),
        eta_seconds=None,
    )

    _set_job(
        db,
        job,
        status="backtesting",
        stage="backtesting",
        message="Sparkie is backtesting candidates.",
        eta_seconds=None,
    )
    add_event(
        db,
        job,
        "backtesting",
        f"Backtesting {len(frames_by_symbol)} symbols across {len(intervals)} interval(s) and {len(algos)} algo(s).",
        {
            "backtest_units": [
                {"symbol": symbol, "interval": interval}
                for symbol in frames_by_symbol
                for interval in intervals
            ],
            "workers": workers,
        },
    )
    db.commit()
    db.refresh(job)

    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    unit_timings: list[dict[str, Any]] = []
    completed_units = 0
    pending_units = sorted([
        (symbol, interval, frames[interval])
        for symbol, frames in frames_by_symbol.items()
        for interval in intervals
        if interval in frames
    ], key=lambda unit: (len(unit[2]) if hasattr(unit[2], "__len__") else 0, unit[1], unit[0]))
    for symbol, interval, frame in pending_units:
        add_event(
            db,
            job,
            "backtesting",
            f"{symbol} {interval}: queued backtest unit with {len(frame)} bars.",
            {"symbol": symbol, "interval": interval, "bars": len(frame)},
        )
    db.commit()
    db.refresh(job)

    pool = ThreadPoolExecutor(max_workers=workers)
    futures = {
        pool.submit(
            _backtest_symbol_interval,
            symbol,
            interval,
            frame,
            int(job.user_id),
            float(job.account_equity),
            str(job.id),
        ): (symbol, interval)
        for symbol, interval, frame in pending_units
    }
    pending = set(futures.keys())
    try:
        while pending:
            if _stop_requested(db, job_id):
                for pending_future in pending:
                    pending_future.cancel()
                pool.shutdown(wait=False, cancel_futures=True)
                return _mark_stopped(db, job)

            done, pending = wait(pending, timeout=5.0, return_when=FIRST_COMPLETED)
            if not done:
                continue

            for future in done:
                symbol, interval = futures[future]
                completed_units += 1
                try:
                    job = _fresh_job_for_write(db, job_id, job)
                    result = future.result()
                    timing = result.get("sparkie_timing") or {}
                    if timing:
                        unit_timings.append(timing)
                    symbol_rows = [
                        row for row in (result.get("top") or [])
                        if str(row.get("algo_name") or "") in algos
                    ]
                    rows.extend(symbol_rows)
                    errors.extend(str(err) for err in (result.get("errors") or []))
                    add_event(
                        db,
                        job,
                        "backtesting",
                        (
                            f"{symbol} {interval}: tested {result.get('tested_combinations') or 0} "
                            f"combinations, {len(symbol_rows)} candidates"
                            f"{_timing_message_suffix(timing)}."
                        ),
                        {
                            "symbol": symbol,
                            "interval": interval,
                            "candidate_count": len(symbol_rows),
                            "tested_combinations": result.get("tested_combinations") or 0,
                            "timing": timing,
                        },
                    )
                    _replace_candidates(db, job, _rank_candidates(rows)[:50], errors[:20])
                except Exception as exc:
                    errors.append(f"{symbol} {interval}: {exc}")
                    job = _fresh_job_for_write(db, job_id, job)
                    add_event(db, job, "backtesting", f"{symbol} {interval}: backtest failed: {exc}", level="warning")
                _update_progress(
                    db,
                    job,
                    completed_steps=len(symbols) + completed_units,
                    started_at=started,
                    candidates_tested=len(rows),
                )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    ranked = _rank_candidates(rows)
    _replace_candidates(db, job, ranked[:50], errors[:20])
    timing_summary = _backtest_timing_summary(unit_timings)
    summary_path = _write_summary_file(job, ranked[:50], errors[:50], timing_summary)
    _set_job(
        db,
        job,
        status="scoring",
        stage="scoring",
        message="Sparkie is scoring the final pick.",
        candidates_tested=len(rows),
        result_json=_json({"summary_file": str(summary_path), "top": ranked[:10], "errors": errors[:30], "backtest_timing": timing_summary}),
    )

    if not ranked:
        _finish_job(
            db,
            job,
            status="rejected",
            message="Sparkie did not find a positive, validated candidate.",
            recommendation="paper_only",
            decision_reason="Backtest scan returned no candidate with positive holdout and validation evidence.",
            result={"errors": errors[:30], "top": [], "backtest_timing": timing_summary, "summary_file": str(summary_path)},
        )
        return {"ok": True, "status": job.status}

    best = ranked[0]
    decision = _decision_for_candidate(job, best)
    _apply_best_candidate(db, job, best, decision)

    replay_session_id = None
    if decision["run_replay"]:
        _set_job(
            db,
            job,
            status="verifying_replay",
            stage="verifying_replay",
            message="Sparkie selected one finalist and queued Replay verification.",
        )
        try:
            replay_session_id = queue_replay_verification(db, job, best)
            add_event(
                db,
                job,
                "verifying_replay",
                f"Replay verification queued for {best['symbol']} {best['interval']} {best['algo_name']}.",
                {"replay_session_id": replay_session_id},
            )
        except Exception as exc:
            add_event(db, job, "verifying_replay", f"Replay verification could not be queued: {exc}", level="warning")

    if replay_session_id:
        job.status = "verifying_replay"
        job.stage = "verifying_replay"
        job.message = decision["message"]
        job.recommendation = decision["recommendation"]
        job.decision_reason = decision["reason"]
        job.replay_session_id = replay_session_id
        job.result_json = _json({"summary_file": str(summary_path), "top": ranked[:10], "errors": errors[:30], "backtest_timing": timing_summary})
        job.progress_pct = 95.0
        job.eta_seconds = None
        job.updated_at = datetime.utcnow()
        db.commit()
    else:
        _finish_job(
            db,
            job,
            status="completed",
            message=decision["message"],
            recommendation=decision["recommendation"],
            decision_reason=decision["reason"],
            result={"summary_file": str(summary_path), "top": ranked[:10], "errors": errors[:30], "backtest_timing": timing_summary},
        )
    return {"ok": True, "status": job.status, "job_id": job.id}


def refresh_symbol_data(
    *,
    user_id: int,
    symbol: str,
    intervals: list[str],
    lookback_days: int,
) -> dict[str, Any]:
    try:
        return _refresh_symbol_data_once(
            user_id=user_id,
            symbol=symbol,
            intervals=intervals,
            lookback_days=lookback_days,
            force=False,
        )
    except Exception:
        return _refresh_symbol_data_once(
            user_id=user_id,
            symbol=symbol,
            intervals=intervals,
            lookback_days=lookback_days,
            force=True,
        )


def _refresh_symbol_data_once(
    *,
    user_id: int,
    symbol: str,
    intervals: list[str],
    lookback_days: int,
    force: bool,
) -> dict[str, Any]:
    _path, meta = fetch_and_save(user_id=user_id, symbol=symbol, days=lookback_days, force=force)
    _validate_ingest_meta(symbol=symbol, meta=meta)
    end_date = datetime.utcnow().date()
    start_date = end_date - timedelta(days=max(lookback_days, 10))
    frames: dict[str, Any] = {}
    row_count = 0
    interval_rows: dict[str, int] = {}
    for interval in intervals:
        provider = ReplayDataProvider(
            user_id=user_id,
            symbol=symbol,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            interval=interval,
        )
        frame = provider.bars.copy()
        if frame is None or frame.empty:
            raise RuntimeError(f"No cached bars for {symbol} {interval}.")
        normalized = _normalize_backtest_frame(frame)
        if len(normalized) < MIN_BACKTEST_BARS_PER_INTERVAL:
            raise RuntimeError(
                f"Downloaded data for {symbol} {interval} has only {len(normalized)} usable bars; "
                f"Sparkie needs at least {MIN_BACKTEST_BARS_PER_INTERVAL} before backtesting."
            )
        frames[interval] = normalized
        interval_rows[interval] = len(normalized)
        row_count += len(frame)
    return {
        "symbol": symbol,
        "frames": frames,
        "rows": row_count,
        "interval_rows": interval_rows,
        "last_bar": getattr(meta, "last_bar", None),
        "unique_dates": list(getattr(meta, "unique_dates", []) or []),
    }


def _validate_ingest_meta(*, symbol: str, meta: Any) -> None:
    total_rows = int(getattr(meta, "total_rows", 0) or 0)
    unique_dates = list(getattr(meta, "unique_dates", []) or [])
    last_bar_text = str(getattr(meta, "last_bar", "") or "")
    if total_rows <= 0:
        raise RuntimeError(f"Downloaded data for {symbol} is empty.")
    if len(unique_dates) < MIN_BACKTEST_TRADING_DAYS:
        raise RuntimeError(
            f"Downloaded data for {symbol} has only {len(unique_dates)} trading day(s); "
            f"Sparkie needs at least {MIN_BACKTEST_TRADING_DAYS}."
        )
    if last_bar_text:
        try:
            last_bar = datetime.fromisoformat(last_bar_text)
            age_days = (datetime.utcnow().date() - last_bar.date()).days
            if age_days > 4:
                raise RuntimeError(
                    f"Downloaded data for {symbol} is stale; latest bar is {last_bar_text}."
                )
        except ValueError:
            raise RuntimeError(f"Downloaded data for {symbol} has invalid metadata timestamp.")


def _normalize_backtest_frame(frame: Any) -> pd.DataFrame:
    """
    Replay cache stores naive Eastern timestamps. The MM feature builders work
    with timezone-aware UTC bars, so normalize here before direct backtesting.
    Without this, features and prices both exist but align to zero rows.
    """
    df = pd.DataFrame(frame).copy()
    if df.empty:
        return df
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, errors="coerce")
    df = df[df.index.notna()]
    if df.index.tz is None:
        df.index = df.index.tz_localize("America/New_York").tz_convert("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    needed = ["open", "high", "low", "close", "volume"]
    df = df[[col for col in needed if col in df.columns]].sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df


def queue_replay_verification(db: Session, job: SparkieJob, best: dict[str, Any]) -> int:
    end_date = datetime.utcnow().date()
    start_date = end_date - timedelta(days=int(os.getenv("SPARKIE_REPLAY_LOOKBACK_DAYS", "10")))
    trade_size = _trade_quantity(
        user_id=int(job.user_id),
        symbol=str(best["symbol"]),
        interval=str(best["interval"]),
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        account_equity=float(job.account_equity),
    )
    cfg = _replay_config_from_candidate(job, best)
    session = ReplaySession(
        user_id=int(job.user_id),
        symbol=str(best["symbol"]),
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        interval=str(best["interval"]),
        algo_name=str(best["algo_name"]),
        speed=20.0,
        trade_size=float(trade_size),
        status="QUEUED",
        config_json=_json(cfg),
    )
    db.add(session)
    db.flush()
    from app.tasks.replay_tasks import start_replay_session_task

    async_result = start_replay_session_task.apply_async(args=(int(session.id),), queue="replay")
    cfg["sparkie_replay_task_id"] = async_result.id
    session.config_json = _json(cfg)
    job.replay_session_id = int(session.id)
    db.flush()
    return int(session.id)


def stop_sparkie_job(db: Session, job: SparkieJob, reason: str = "Sparkie stop requested.") -> None:
    revoke_requested = _revoke_sparkie_task(job)
    if job.status not in TERMINAL_STATUSES:
        job.status = "stopped"
        job.stage = "stopped"
        job.message = reason
        job.error_message = reason
        job.stop_requested_at = datetime.utcnow()
        job.finished_at = datetime.utcnow()
        job.updated_at = datetime.utcnow()
        add_event(
            db,
            job,
            "stopped",
            f"{reason} Celery revoke requested." if revoke_requested else reason,
            level="warning",
        )
    elif revoke_requested:
        add_event(db, job, "stopped", "Sparkie revoke requested for an already-stopped background task.", level="warning")
    if job.replay_session_id:
        try:
            from app.services.replay_process import stop_session

            stop_session(db, int(job.replay_session_id))
        except Exception:
            pass


def _revoke_sparkie_task(job: SparkieJob) -> bool:
    if not job.task_id:
        return False
    pid = _sparkie_process_pid(job.task_id)
    if pid:
        return _stop_sparkie_process(pid)
    try:
        from app.celery_app import celery_app

        celery_app.control.revoke(str(job.task_id), terminate=True, signal="SIGTERM")
        log.warning("[SPARKIE] revoke requested job_id=%s task_id=%s", job.id, job.task_id)
        return True
    except Exception:
        log.exception("[SPARKIE] failed to revoke job_id=%s task_id=%s", job.id, job.task_id)
        return False


def _sparkie_process_pid(task_id: str | None) -> int | None:
    text = str(task_id or "")
    prefix = "sparkie-process:"
    if not text.startswith(prefix):
        return None
    try:
        pid = int(text.removeprefix(prefix))
    except ValueError:
        return None
    return pid if pid > 0 else None


def _stop_sparkie_process(pid: int) -> bool:
    try:
        if os.name == "nt":
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(pid, signal.SIGTERM)
        log.warning("[SPARKIE] process stop requested pid=%s", pid)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
            log.warning("[SPARKIE] process stop requested pid=%s fallback=true", pid)
            return True
        except Exception:
            log.exception("[SPARKIE] failed to stop process pid=%s", pid)
            return False


def _fresh_job_for_write(db: Session, job_id: str, fallback: SparkieJob) -> SparkieJob:
    try:
        db.rollback()
    except Exception:
        pass
    try:
        job = db.get(SparkieJob, job_id)
        if job:
            return job
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    return fallback


def add_event(
    db: Session,
    job: SparkieJob,
    stage: str,
    message: str,
    payload: dict[str, Any] | None = None,
    *,
    level: str = "info",
) -> None:
    db.add(
        SparkieEvent(
            job_id=job.id,
            user_id=int(job.user_id),
            level=level,
            stage=stage,
            message=message,
            payload_json=_json(payload) if payload else None,
        )
    )
    db.flush()


def _backtest_symbol_interval(
    symbol: str,
    interval: str,
    frame: Any,
    user_id: int,
    account_equity: float,
    job_id: str | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    log.info(
        "[SPARKIE] backtest unit start symbol=%s interval=%s rows=%s user_id=%s",
        symbol,
        interval,
        len(frame) if hasattr(frame, "__len__") else "?",
        user_id,
    )
    if job_id:
        _add_backtest_unit_event(
            job_id=job_id,
            user_id=user_id,
            symbol=symbol,
            interval=interval,
            message=(
                f"{symbol} {interval}: running backtest unit with "
                f"{len(frame) if hasattr(frame, '__len__') else '?'} bars."
            ),
        )
    trade_size, allocation_usd, reference_price = _trade_quantity_from_frame(
        frame,
        account_equity=account_equity,
    )
    req = CheatSheetRequest(
        symbol=symbol,
        intervals=(interval,),
        user_id=user_id,
        trade_size=trade_size,
        builder_days=int(os.getenv("SPARKIE_BACKTEST_LOOKBACK_DAYS", "45")),
        k_forward=int(DEFAULT_REPLAY_MM_CONFIG["k_forward"]),
        profile=os.getenv("SPARKIE_BACKTEST_PROFILE", "sparkie_probe"),
        allow_short=True,
        eod_close=True,
        oos_fraction=0.35,
        algo_names=tuple(sparkie_algo_policy()),
        model_refresh_mode=os.getenv("SPARKIE_MODEL_REFRESH_MODE", "fixed"),
        model_max_age_minutes=float(os.getenv("SPARKIE_MODEL_MAX_AGE_MINUTES", "0")),
        min_new_bars_before_retrain=int(os.getenv("SPARKIE_MIN_NEW_BARS_BEFORE_RETRAIN", "0")),
    )
    result = run_cheatsheet(req, price_frames={interval: frame})
    elapsed = max(time.monotonic() - started, 0.001)
    bars = len(frame) if hasattr(frame, "__len__") else 0
    result["sparkie_timing"] = {
        "symbol": symbol,
        "interval": interval,
        "bars": int(bars or 0),
        "duration_seconds": round(elapsed, 2),
        "bars_per_second": round((float(bars or 0) / elapsed), 2) if bars else None,
        "tested_combinations": int(result.get("tested_combinations") or 0),
        "candidate_count": len(result.get("top") or []),
        "trade_size": float(trade_size),
        "allocation_usd": round(float(allocation_usd), 2),
        "reference_price": round(float(reference_price), 4),
    }
    for row in result.get("top") or []:
        row["sparkie_trade_size"] = float(trade_size)
        row["sparkie_allocation_usd"] = round(float(allocation_usd), 2)
        row["sparkie_reference_price"] = round(float(reference_price), 4)
    log.info(
        "[SPARKIE] backtest unit done symbol=%s interval=%s trade_size=%s allocation=%.2f tested=%s rows=%s seconds=%.1f",
        symbol,
        interval,
        trade_size,
        allocation_usd,
        result.get("tested_combinations"),
        len(result.get("top") or []),
        elapsed,
    )
    return result


def _add_backtest_unit_event(*, job_id: str, user_id: int, symbol: str, interval: str, message: str) -> None:
    try:
        from app.database.connection import SessionLocal

        unit_db = SessionLocal()
        try:
            job = unit_db.get(SparkieJob, job_id)
            if job:
                add_event(
                    unit_db,
                    job,
                    "backtesting",
                    message,
                    {"symbol": symbol, "interval": interval, "status": "running"},
                )
                unit_db.commit()
        finally:
            unit_db.close()
    except Exception:
        log.exception("[SPARKIE] failed writing backtest unit event job_id=%s symbol=%s interval=%s", job_id, symbol, interval)


def _timing_message_suffix(timing: dict[str, Any]) -> str:
    if not timing:
        return ""
    seconds = float(timing.get("duration_seconds") or 0.0)
    bars_per_second = timing.get("bars_per_second")
    if bars_per_second is None:
        return f" in {seconds:,.1f}s"
    return f" in {seconds:,.1f}s ({float(bars_per_second):,.0f} bars/sec)"


def _backtest_timing_summary(unit_timings: list[dict[str, Any]]) -> dict[str, Any]:
    if not unit_timings:
        return {
            "completed_units": 0,
            "total_duration_seconds": 0.0,
            "slowest_units": [],
            "by_interval": {},
        }
    timings = sorted(
        unit_timings,
        key=lambda row: float(row.get("duration_seconds") or 0.0),
        reverse=True,
    )
    by_interval: dict[str, dict[str, Any]] = {}
    for row in unit_timings:
        interval = str(row.get("interval") or "")
        stats = by_interval.setdefault(
            interval,
            {
                "units": 0,
                "bars": 0,
                "duration_seconds": 0.0,
                "tested_combinations": 0,
                "candidates": 0,
            },
        )
        stats["units"] += 1
        stats["bars"] += int(row.get("bars") or 0)
        stats["duration_seconds"] += float(row.get("duration_seconds") or 0.0)
        stats["tested_combinations"] += int(row.get("tested_combinations") or 0)
        stats["candidates"] += int(row.get("candidate_count") or 0)
    for stats in by_interval.values():
        duration = max(float(stats.get("duration_seconds") or 0.0), 0.001)
        stats["duration_seconds"] = round(duration, 2)
        stats["bars_per_second"] = round(float(stats.get("bars") or 0) / duration, 2)
    total_duration = sum(float(row.get("duration_seconds") or 0.0) for row in unit_timings)
    total_bars = sum(int(row.get("bars") or 0) for row in unit_timings)
    return {
        "completed_units": len(unit_timings),
        "total_duration_seconds": round(total_duration, 2),
        "total_bars": total_bars,
        "bars_per_second": round(total_bars / max(total_duration, 0.001), 2),
        "slowest_units": timings[:5],
        "by_interval": by_interval,
    }


def _write_summary_file(job: SparkieJob, ranked: list[dict[str, Any]], errors: list[str], timing_summary: dict[str, Any] | None = None) -> Path:
    base_dir = Path(os.getenv("DATA_DIR", "data")) / str(job.user_id) / "sparkie"
    base_dir.mkdir(parents=True, exist_ok=True)
    path = base_dir / f"{job.id}_summary.json"
    payload = {
        "job_id": job.id,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "account_equity": float(job.account_equity or 0.0),
        "target_profit": float(job.target_profit or 0.0),
        "target_period": job.target_period,
        "symbol_bucket": _json_list(job.symbol_bucket_json),
        "interval_policy": _json_list(job.interval_policy_json),
        "algo_policy": _json_list(job.algo_policy_json),
        "trade_allocation_usd": _trade_allocation_usd(float(job.account_equity or 0.0)),
        "candidate_count": len(ranked),
        "top": ranked,
        "errors": errors,
        "backtest_timing": timing_summary or {},
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _rank_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = []
    for row in rows:
        total_profit = float(row.get("total_profit") or 0.0)
        validation_profit = float(row.get("validation_total_profit") or 0.0)
        trades = int(row.get("num_trades") or 0)
        validation_trades = int(row.get("validation_num_trades") or 0)
        if total_profit <= 0 or validation_profit <= 0:
            continue
        if trades < 3 or validation_trades < 3:
            continue
        candidates.append(_candidate_row(row))
    candidates.sort(
        key=lambda row: (
            float(row.get("score") or 0.0),
            float(row.get("profit_loss") or 0.0),
            float(row.get("win_rate") or 0.0),
            -abs(float(row.get("max_drawdown") or 0.0)),
        ),
        reverse=True,
    )
    return candidates


def _candidate_row(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "symbol",
        "interval",
        "algo_name",
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
        "sparkie_trade_size",
        "sparkie_allocation_usd",
        "sparkie_reference_price",
    )
    return {
        "symbol": str(row.get("symbol") or "").upper(),
        "interval": str(row.get("interval") or "").lower(),
        "algo_name": str(row.get("algo_name") or ""),
        "score": float(row.get("score") or 0.0),
        "profit_loss": float(row.get("total_profit") or 0.0),
        "trades": int(row.get("num_trades") or 0),
        "win_rate": float(row.get("win_rate") or 0.0),
        "max_drawdown": float(row.get("max_drawdown") or 0.0),
        "validation_profit_loss": float(row.get("validation_total_profit") or 0.0),
        "validation_trades": int(row.get("validation_num_trades") or 0),
        "confidence": _confidence_number(row.get("confidence")),
        "trade_size": float(row.get("sparkie_trade_size") or 0.0),
        "allocation_usd": float(row.get("sparkie_allocation_usd") or 0.0),
        "params": {key: row.get(key) for key in keys if row.get(key) is not None},
    }


def _replace_candidates(db: Session, job: SparkieJob, rows: list[dict[str, Any]], errors: list[str]) -> None:
    db.query(SparkieCandidate).filter(SparkieCandidate.job_id == job.id).delete(synchronize_session=False)
    for row in rows:
        db.add(
            SparkieCandidate(
                job_id=job.id,
                user_id=int(job.user_id),
                symbol=row["symbol"],
                interval=row["interval"],
                algo_name=row["algo_name"],
                score=row["score"],
                profit_loss=row["profit_loss"],
                trades=row["trades"],
                win_rate=row["win_rate"],
                max_drawdown=row["max_drawdown"],
                validation_profit_loss=row["validation_profit_loss"],
                validation_trades=row["validation_trades"],
                confidence=row["confidence"],
                params_json=_json(row.get("params") or {}),
            )
        )
    for error in errors:
        add_event(db, job, "backtesting", error, level="warning")
    db.flush()


def _decision_for_candidate(job: SparkieJob, row: dict[str, Any]) -> dict[str, Any]:
    target_units = _target_window_units(job.target_period)
    profit = float(row.get("profit_loss") or 0.0)
    estimated_period_profit = profit / target_units if target_units else profit
    target_period = str(job.target_period or "daily")
    trades = int(row.get("trades") or 0)
    win_rate = float(row.get("win_rate") or 0.0)
    max_drawdown = abs(float(row.get("max_drawdown") or 0.0))
    if estimated_period_profit < float(job.target_profit or 0.0):
        return {
            "recommendation": "paper_only",
            "run_replay": True,
            "message": "Sparkie found a positive candidate, but it did not meet the minimum target.",
            "reason": (
                f"Estimated {target_period} backtest P/L ${estimated_period_profit:,.2f} "
                f"is below the user target ${float(job.target_profit or 0.0):,.2f} "
                f"({target_units:g} {target_period} unit evidence window total: ${profit:,.2f})."
            ),
        }
    if trades < 5 or win_rate < 0.50:
        return {
            "recommendation": "paper_only",
            "run_replay": True,
            "message": "Sparkie found a candidate, but evidence is not strong enough for Live Mirror.",
            "reason": "Trade count or win rate is below Sparkie's evidence threshold.",
        }
    if max_drawdown > float(job.account_equity) * 0.05:
        return {
            "recommendation": "paper_only",
            "run_replay": True,
            "message": "Sparkie found profit, but drawdown is too large for Live Mirror.",
            "reason": "Backtest drawdown exceeds Sparkie's 5% account risk rail.",
        }
    return {
        "recommendation": "paper_candidate",
        "run_replay": True,
        "message": "Sparkie found a positive candidate and queued Replay verification.",
        "reason": (
            f"Estimated {target_period} backtest P/L ${estimated_period_profit:,.2f} "
            f"is greater than or equal to the user target ${float(job.target_profit or 0.0):,.2f}, "
            f"with trade count, win-rate, and drawdown gates met. "
            f"Replay verification is still required before Live Mirror."
        ),
    }


def _apply_best_candidate(db: Session, job: SparkieJob, row: dict[str, Any], decision: dict[str, Any]) -> None:
    job.best_symbol = row["symbol"]
    job.best_interval = row["interval"]
    job.best_algo_name = row["algo_name"]
    job.best_score = row["score"]
    job.best_profit_loss = row["profit_loss"]
    job.best_trades = row["trades"]
    job.best_win_rate = row["win_rate"]
    job.recommendation = decision["recommendation"]
    job.decision_reason = decision["reason"]
    job.updated_at = datetime.utcnow()
    add_event(
        db,
        job,
        "scoring",
        f"Best pick: {row['symbol']} {row['interval']} {row['algo_name']} P/L ${row['profit_loss']:,.2f}.",
        row,
    )
    db.flush()


def _replay_config_from_candidate(job: SparkieJob, row: dict[str, Any]) -> dict[str, Any]:
    params = dict(row.get("params") or {})
    allocation = _trade_allocation_usd(float(job.account_equity or 0.0))
    cfg = _build_mm_replay_config(
        algo_name=str(row["algo_name"]),
        eod_auto_close="on",
        allow_short_selling="on",
        stop_loss_usd=params.get("stop_loss_usd", params.get("hard_stop_usd", min(allocation * 0.02, 50.0))),
        trailing_profit_usd=params.get("trailing_profit_usd", max(5.0, min(allocation * 0.01, float(job.target_profit) * 0.5))),
        stop_loss_pct=params.get("stop_loss_pct", params.get("per_share_stop_pct", DEFAULT_REPLAY_MM_CONFIG["stop_loss_pct"])),
        trailing_profit_pct=params.get("trailing_profit_pct", params.get("per_share_trailing_profit_pct", DEFAULT_REPLAY_MM_CONFIG["trailing_profit_pct"])),
        prob_trail_drop=params.get("prob_trail_drop", DEFAULT_REPLAY_MM_CONFIG["prob_trail_drop"]),
        prob_exit_mode=params.get("prob_exit_mode", DEFAULT_REPLAY_MM_CONFIG["prob_exit_mode"]),
        long_fixed_exit_prob=params.get("long_fixed_exit_prob", DEFAULT_REPLAY_MM_CONFIG["long_fixed_exit_prob"]),
        short_fixed_exit_prob=params.get("short_fixed_exit_prob", DEFAULT_REPLAY_MM_CONFIG["short_fixed_exit_prob"]),
        long_entry_prob=params.get("long_entry_prob", DEFAULT_REPLAY_MM_CONFIG["long_entry_prob"]),
        short_entry_prob=params.get("short_entry_prob", DEFAULT_REPLAY_MM_CONFIG["short_entry_prob"]),
        entry_confirmation_bars=params.get("entry_confirmation_bars", DEFAULT_REPLAY_MM_CONFIG["entry_confirmation_bars"]),
    )
    for key in ("model_refresh_mode", "model_max_age_minutes", "min_new_bars_before_retrain", "prob_smoothing_bars", "min_prob_advantage"):
        if params.get(key) is not None:
            cfg[key] = params[key]
    cfg.update(
        {
            "sparkie_job_id": job.id,
            "sparkie_v2": True,
            "sparkie_account_equity": float(job.account_equity),
            "sparkie_target_profit": float(job.target_profit),
            "sparkie_target_period": job.target_period,
            "sparkie_allocation_usd": round(allocation, 2),
            "sparkie_fast_replay": True,
        }
    )
    return cfg


def _trade_quantity(
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
    allocation = _trade_allocation_usd(float(account_equity or 0.0))
    quantity = int(allocation // price)
    if quantity < 1 and price <= float(account_equity) * 0.25:
        quantity = 1
    if quantity < 1:
        raise ValueError(f"{symbol} is above Sparkie's single-position cap.")
    return float(quantity)


def _trade_allocation_usd(account_equity: float) -> float:
    equity = max(float(account_equity or 0.0), 0.0)
    if equity <= 0:
        return 0.0
    min_allocation = float(os.getenv("SPARKIE_MIN_TRADE_ALLOCATION_USD", str(MIN_TRADE_ALLOCATION_USD)))
    allocation_pct = float(os.getenv("SPARKIE_TRADE_ALLOCATION_PCT", str(DEFAULT_TRADE_ALLOCATION_PCT)))
    max_pct = float(os.getenv("SPARKIE_MAX_TRADE_ALLOCATION_PCT", str(MAX_TRADE_ALLOCATION_PCT)))
    target = max(equity * allocation_pct, min_allocation)
    cap = max(equity * max_pct, 1.0)
    return max(1.0, min(target, cap, equity))


def _trade_quantity_from_frame(frame: Any, *, account_equity: float) -> tuple[float, float, float]:
    df = pd.DataFrame(frame)
    if df.empty:
        raise ValueError("Sparkie cannot size trade from empty price frame.")
    first_bar = df.iloc[0]
    price = float(first_bar.get("open") or first_bar.get("close") or 0.0)
    if not math.isfinite(price) or price <= 0:
        raise ValueError("Sparkie cannot size trade without a valid starting price.")
    allocation = _trade_allocation_usd(float(account_equity or 0.0))
    quantity = int(allocation // price)
    if quantity < 1 and price <= float(account_equity or 0.0):
        quantity = 1
    if quantity < 1:
        raise ValueError("Sparkie account allocation cannot buy one share for this symbol.")
    return float(quantity), float(quantity) * price, price


def _set_job(db: Session, job: SparkieJob, **updates: Any) -> None:
    for key, value in updates.items():
        setattr(job, key, value)
    job.updated_at = datetime.utcnow()
    db.flush()
    db.commit()
    db.refresh(job)


def _finish_job(
    db: Session,
    job: SparkieJob,
    *,
    status: str,
    message: str,
    recommendation: str,
    decision_reason: str,
    replay_session_id: int | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    job.status = status
    job.stage = status
    job.message = message
    job.recommendation = recommendation
    job.decision_reason = decision_reason
    if replay_session_id:
        job.replay_session_id = replay_session_id
    job.result_json = _json(result or {})
    job.progress_pct = 100.0
    job.completed_steps = max(int(job.total_steps or 0), int(job.completed_steps or 0))
    job.elapsed_seconds = _elapsed(job.started_at)
    job.eta_seconds = 0
    job.finished_at = datetime.utcnow()
    job.updated_at = datetime.utcnow()
    add_event(db, job, status, message, {"recommendation": recommendation, "reason": decision_reason})
    db.commit()


def _update_progress(
    db: Session,
    job: SparkieJob,
    *,
    completed_steps: int,
    started_at: datetime,
    candidates_tested: int | None = None,
) -> None:
    total = max(int(job.total_steps or 1), 1)
    completed = max(0, min(int(completed_steps), total))
    elapsed = max(int((datetime.utcnow() - started_at).total_seconds()), 0)
    pct = round((completed / total) * 100.0, 1)
    eta = None
    if completed > 0 and completed < total:
        eta = int((elapsed / completed) * (total - completed))
    job.completed_steps = completed
    job.elapsed_seconds = elapsed
    job.eta_seconds = eta
    job.progress_pct = pct
    if candidates_tested is not None:
        job.candidates_tested = int(candidates_tested)
    job.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(job)


def _mark_stopped(db: Session, job: SparkieJob) -> dict[str, Any]:
    job.status = "stopped"
    job.stage = "stopped"
    job.message = "Sparkie was stopped by request."
    job.finished_at = datetime.utcnow()
    job.updated_at = datetime.utcnow()
    add_event(db, job, "stopped", job.message, level="warning")
    db.commit()
    return {"ok": True, "status": "stopped", "job_id": job.id}


def _stop_requested(db: Session, job_id: str) -> bool:
    job = db.get(SparkieJob, job_id)
    return bool(job and (job.stop_requested_at or str(job.status or "").lower() == "stopped"))


def _target_for_backtest_window(job: SparkieJob) -> float:
    multiplier = _target_window_units(job.target_period)
    return float(job.target_profit or 0.0) * multiplier


def _target_window_units(target_period: str | None) -> float:
    return {"daily": 10.0, "weekly": 2.0, "monthly": 0.5}.get(str(target_period or "daily").lower(), 10.0)


def _elapsed(started_at: datetime | None) -> int:
    if not started_at:
        return 0
    return max(int((datetime.utcnow() - started_at).total_seconds()), 0)


def _confidence_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").lower()
    if text == "high":
        return 0.8
    if text == "medium":
        return 0.6
    if text == "low":
        return 0.4
    return None


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    except Exception:
        return []
    return []
