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
import csv
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
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
from app.services.backtest_cheatsheet_service import CheatSheetRequest, _fetch_price_frame, run_cheatsheet
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

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

log = logging.getLogger("ReplayRoutes")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [ReplayRoutes] %(message)s"))
    log.addHandler(_h)
log.setLevel(logging.INFO)


CHEATSHEET_JOB_TTL_SEC = 60 * 60 * 36
_cheatsheet_executor = ThreadPoolExecutor(max_workers=int(os.getenv("CHEATSHEET_MAX_WORKERS", "2")))
_cheatsheet_batch_executor = ThreadPoolExecutor(max_workers=int(os.getenv("CHEATSHEET_BATCH_MAX_WORKERS", "1")))
_cheatsheet_jobs: dict[str, dict] = {}
_cheatsheet_jobs_lock = threading.Lock()

APP_PATH = Path(REPO_ROOT)
OPTIMIZER_CACHE_DIR = Path(os.getenv("DATA_DIR", "data")) / "strategy_optimizer"
OPTIMIZER_LATEST_JSON = OPTIMIZER_CACHE_DIR / "latest_recommendations.json"
OPTIMIZER_LATEST_CSV = OPTIMIZER_CACHE_DIR / "latest_recommendations.csv"
OPTIMIZER_MAX_BATCH_SYMBOLS = 10


def _cheatsheet_redis_client():
    try:
        import redis

        redis_url = os.getenv("REDIS_URL")
        if not redis_url:
            return None
        return redis.Redis.from_url(redis_url, decode_responses=True)
    except Exception:
        return None


def _cheatsheet_redis_key(job_id: str) -> str:
    return f"stockwicks:cheatsheet_job:{job_id}"


def _store_cheatsheet_job(job_id: str, job: dict) -> None:
    r = _cheatsheet_redis_client()
    if not r:
        return
    try:
        r.setex(_cheatsheet_redis_key(job_id), CHEATSHEET_JOB_TTL_SEC, json.dumps(job, default=str))
    except Exception as exc:
        log.warning("[CHEATSHEET] could not store job in Redis job_id=%s: %s", job_id, exc)


def _load_cheatsheet_job(job_id: str) -> dict | None:
    r = _cheatsheet_redis_client()
    if not r:
        return None
    try:
        raw = r.get(_cheatsheet_redis_key(job_id))
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        log.warning("[CHEATSHEET] could not load job from Redis job_id=%s: %s", job_id, exc)
        return None


def _update_cheatsheet_job(job_id: str, **updates) -> None:
    with _cheatsheet_jobs_lock:
        job = _cheatsheet_jobs.get(job_id)
        if not job:
            job = _load_cheatsheet_job(job_id) or {}
            if not job:
                return
        job.update(updates)
        job["updated_at"] = time.time()
        if job_id in _cheatsheet_jobs:
            _cheatsheet_jobs[job_id] = job
        _store_cheatsheet_job(job_id, job)


def _cleanup_cheatsheet_jobs() -> None:
    cutoff = time.time() - CHEATSHEET_JOB_TTL_SEC
    with _cheatsheet_jobs_lock:
        stale_ids = [
            job_id
            for job_id, job in _cheatsheet_jobs.items()
            if float(job.get("updated_at") or job.get("created_at") or 0) < cutoff
        ]
        for job_id in stale_ids:
            _cheatsheet_jobs.pop(job_id, None)


def _snapshot_cheatsheet_job(job_id: str, user_id: int) -> dict | None:
    redis_job = _load_cheatsheet_job(job_id)
    if redis_job and redis_job.get("user_id") == user_id:
        return {
            "job_id": job_id,
            "status": redis_job.get("status", "queued"),
            "message": redis_job.get("message"),
            "result": redis_job.get("result"),
            "error": redis_job.get("error"),
        }

    with _cheatsheet_jobs_lock:
        job = _cheatsheet_jobs.get(job_id)
        if not job or job.get("user_id") != user_id:
            return None
        return {
            "job_id": job_id,
            "status": job.get("status", "queued"),
            "message": job.get("message"),
            "result": job.get("result"),
            "error": job.get("error"),
        }


def _run_cheatsheet_job(job_id: str, req: CheatSheetRequest) -> None:
    with _cheatsheet_jobs_lock:
        job = _cheatsheet_jobs.get(job_id)
        if not job:
            return
        job["status"] = "running"
        job["message"] = "Strategy scan is running."
        job["updated_at"] = time.time()
        _store_cheatsheet_job(job_id, job)

    try:
        result = run_cheatsheet(req)
    except Exception as exc:
        log.exception("[CHEATSHEET] background job failed job_id=%s symbol=%s", job_id, req.symbol)
        with _cheatsheet_jobs_lock:
            job = _cheatsheet_jobs.get(job_id)
            if job:
                job["status"] = "failed"
                job["error"] = str(exc)
                job["message"] = "Strategy scan failed."
                job["updated_at"] = time.time()
                _store_cheatsheet_job(job_id, job)
        return

    with _cheatsheet_jobs_lock:
        job = _cheatsheet_jobs.get(job_id)
        if job:
            job["status"] = "succeeded"
            job["result"] = result
            job["message"] = "Strategy scan complete."
            job["updated_at"] = time.time()
            _store_cheatsheet_job(job_id, job)


def _parse_optimizer_symbols(symbols_text: str, max_symbols: int = OPTIMIZER_MAX_BATCH_SYMBOLS) -> list[str]:
    raw_symbols = [
        token.upper().strip()
        for token in re.split(r"[\s,;]+", symbols_text or "")
        if token.strip()
    ]
    symbols: list[str] = []
    for symbol in raw_symbols:
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", symbol):
            raise HTTPException(status_code=400, detail=f"Invalid symbol: {symbol}")
        if symbol not in symbols:
            symbols.append(symbol)
        if len(symbols) > max_symbols:
            raise HTTPException(status_code=400, detail=f"Enter at most {max_symbols} symbols per batch.")
    if not symbols:
        raise HTTPException(status_code=400, detail="Enter 1 to 10 comma-separated symbols.")
    return symbols


def _optimizer_cache_key(req: CheatSheetRequest) -> dict:
    return {
        "intervals": list(req.intervals),
        "user_id": req.user_id,
        "trade_size": float(req.trade_size),
        "builder_days": int(req.builder_days),
        "k_forward": int(req.k_forward),
        "profile": str(req.profile),
        "allow_short": bool(req.allow_short),
        "eod_close": bool(req.eod_close),
        "oos_fraction": float(req.oos_fraction),
    }


def _same_optimizer_settings(data: dict | None, req: CheatSheetRequest) -> bool:
    return bool(data and data.get("settings_key") == _optimizer_cache_key(req))


def _sort_optimizer_rows(rows: list[dict]) -> list[dict]:
    return sorted(
        rows,
        key=lambda r: (
            float(r.get("score") or 0.0),
            float(r.get("validation_total_profit") or 0.0),
            float(r.get("win_rate") or 0.0),
            -float(r.get("max_drawdown") or 0.0),
        ),
        reverse=True,
    )


def _symbol_from_error(error: str) -> str | None:
    prefix = str(error or "").split(":", 1)[0].strip().upper()
    return prefix or None


def _attempted_symbols_from_cache(data: dict | None) -> list[str]:
    if not data:
        return []
    if "symbols_attempted" not in data:
        return [
            str(s or "").upper().strip()
            for s in (data.get("symbols_scanned") or [])
            if str(s or "").strip()
        ]
    attempted = [
        str(s or "").upper().strip()
        for s in (data.get("symbols_attempted") or [])
        if str(s or "").strip()
    ]
    for err in data.get("errors") or []:
        symbol = _symbol_from_error(err)
        if symbol and symbol not in attempted:
            attempted.append(symbol)
    return attempted


def _dedupe_optimizer_errors(errors: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for error in errors:
        text = str(error or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return deduped


def _build_optimizer_cache_result(
    *,
    req_template: CheatSheetRequest,
    symbols: list[str],
    all_top: list[dict],
    all_best: list[dict],
    errors: list[str],
    tested_combinations: int,
    started_at: str,
    status: str,
    last_symbol: str | None = None,
    attempted_symbols: list[str] | None = None,
) -> dict:
    errors = _dedupe_optimizer_errors(errors)
    symbols_with_rows = sorted({str(r.get("symbol") or "").upper() for r in all_best if r.get("symbol")})
    attempted_set = {
        str(s or "").upper().strip()
        for s in (attempted_symbols or symbols_with_rows)
        if str(s or "").strip()
    }
    for err in errors:
        symbol = _symbol_from_error(err)
        if symbol:
            attempted_set.add(symbol)
    attempted = [s for s in symbols if s in attempted_set]
    top_rows = _sort_optimizer_rows(all_top or all_best)
    return {
        "symbol": "CUSTOM_LIST",
        "intervals": req_template.intervals,
        "profile": req_template.profile,
        "settings_key": _optimizer_cache_key(req_template),
        "backtest_method": "cached_custom_symbol_batch",
        "backtest_method_label": "Cached custom symbol batch",
        "backtest_explanation": (
            "This cache is built from the user-provided symbol list, capped at 10 symbols. "
            "Partial results are saved after every symbol."
        ),
        "batch_status": status,
        "oos_fraction": float(req_template.oos_fraction),
        "tested_combinations": tested_combinations,
        "symbols_scanned": symbols_with_rows,
        "symbols_attempted": attempted,
        "symbols_requested": symbols,
        "symbol_count": len(symbols),
        "completed_symbol_count": len(attempted),
        "symbols_with_recommendations_count": len(symbols_with_rows),
        "failed_symbol_count": max(len(attempted) - len(symbols_with_rows), 0),
        "remaining_symbol_count": max(len(symbols) - len(attempted), 0),
        "last_symbol": last_symbol,
        "generated_at": datetime.utcnow().isoformat(),
        "started_at": started_at,
        "cache_json": str(OPTIMIZER_LATEST_JSON),
        "cache_csv": str(OPTIMIZER_LATEST_CSV),
        "top": top_rows[:50],
        "best_by_algo": _sort_optimizer_rows(all_best)[:200],
        "best_by_symbol_algo": all_best,
        "errors": errors,
    }


def _cheatsheet_request_payload(req: CheatSheetRequest) -> dict:
    return {
        "symbol": req.symbol,
        "intervals": list(req.intervals),
        "user_id": req.user_id,
        "trade_size": req.trade_size,
        "builder_days": req.builder_days,
        "k_forward": req.k_forward,
        "profile": req.profile,
        "allow_short": req.allow_short,
        "eod_close": req.eod_close,
        "oos_fraction": req.oos_fraction,
    }


def _queue_qqq_optimizer_job(
    *,
    job_id: str,
    req: CheatSheetRequest,
    symbols: list[str],
    limit: int | None = None,
    max_new_symbols: int | None = None,
    resume: bool = True,
) -> str:
    try:
        from app.tasks.optimizer_tasks import run_qqq_optimizer_batch

        run_qqq_optimizer_batch.apply_async(
            args=(job_id, _cheatsheet_request_payload(req), symbols, limit, max_new_symbols, resume),
            queue="replay",
        )
        return "celery"
    except Exception as exc:
        log.warning("[CHEATSHEET] Celery optimizer queue failed; falling back to thread: %s", exc)
        _cheatsheet_batch_executor.submit(
            _run_qqq_batch_job,
            job_id,
            req,
            symbols,
            limit=limit,
            max_new_symbols=max_new_symbols,
            resume=resume,
        )
        return "thread"


def _write_optimizer_cache(result: dict) -> None:
    OPTIMIZER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, default=str, indent=2)
    OPTIMIZER_LATEST_JSON.write_text(payload, encoding="utf-8")

    rows = result.get("best_by_symbol_algo") or result.get("top") or []
    csv_fields = [
        "symbol",
        "interval",
        "algo_name",
        "feature_set",
        "confidence",
        "total_profit",
        "num_trades",
        "win_rate",
        "max_drawdown",
        "score",
        "stop_loss_pct",
        "trailing_profit_pct",
        "long_entry_prob",
        "short_entry_prob",
        "prob_exit_mode",
        "prob_trail_drop",
        "long_fixed_exit_prob",
        "short_fixed_exit_prob",
        "first_test_bar",
        "last_test_bar",
        "backtest_method",
    ]
    with OPTIMIZER_LATEST_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _load_optimizer_cache() -> dict | None:
    if not OPTIMIZER_LATEST_JSON.exists():
        return None
    try:
        data = json.loads(OPTIMIZER_LATEST_JSON.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as exc:
        log.warning("[CHEATSHEET] could not read optimizer cache: %s", exc)
        return None


def _filter_optimizer_cache(data: dict, symbol: str | None = None) -> dict:
    if not symbol:
        return data
    wanted = symbol.upper().strip()
    if not wanted:
        return data

    filtered = dict(data)
    top = [r for r in (data.get("top") or []) if str(r.get("symbol") or "").upper() == wanted]
    all_best = [
        r
        for r in (data.get("best_by_symbol_algo") or data.get("best_by_algo") or [])
        if str(r.get("symbol") or "").upper() == wanted
    ]
    if not top:
        top = sorted(
            all_best,
            key=lambda r: (
                float(r.get("score") or 0.0),
                float(r.get("validation_total_profit") or 0.0),
                float(r.get("win_rate") or 0.0),
                -float(r.get("max_drawdown") or 0.0),
            ),
            reverse=True,
        )[:50]
    filtered["symbol"] = wanted
    filtered["top"] = top
    filtered["best_by_algo"] = all_best
    filtered["best_by_symbol_algo"] = all_best
    filtered["filtered_from_cache"] = True
    return filtered


def _optimizer_worker_count(env_name: str, default: int, cap: int) -> int:
    try:
        value = int(os.getenv(env_name, str(default)) or default)
    except Exception:
        value = default
    return max(1, min(value, cap))


def _is_schwab_market_auth_error(value: object) -> bool:
    text = str(value or "").lower()
    return (
        "schwab market" in text
        and (
            "token" in text
            or "unauthorized" in text
            or "401" in text
            or "reconnect schwab market data" in text
        )
    )


def _prefetch_optimizer_symbol_data(
    symbol: str,
    req_template: CheatSheetRequest,
) -> tuple[str, dict[str, object], list[str]]:
    frames: dict[str, object] = {}
    errors: list[str] = []
    for interval in req_template.intervals:
        try:
            frames[interval] = _fetch_price_frame(symbol, interval, req_template.builder_days, req_template.user_id)
        except Exception as exc:
            if _is_schwab_market_auth_error(exc):
                raise
            errors.append(f"{symbol} {interval}: {exc}")
    return symbol, frames, errors


def _run_prefetched_optimizer_symbol(
    symbol: str,
    req_template: CheatSheetRequest,
    price_frames: dict[str, object],
) -> tuple[str, dict]:
    req = CheatSheetRequest(
        symbol=symbol,
        intervals=req_template.intervals,
        user_id=req_template.user_id,
        trade_size=req_template.trade_size,
        builder_days=req_template.builder_days,
        k_forward=req_template.k_forward,
        profile=req_template.profile,
        allow_short=req_template.allow_short,
        eod_close=req_template.eod_close,
        oos_fraction=req_template.oos_fraction,
    )
    return symbol, run_cheatsheet(req, price_frames=price_frames)


def _run_qqq_batch_job(
    job_id: str,
    req_template: CheatSheetRequest,
    symbols: list[str],
    *,
    limit: int | None = None,
    max_new_symbols: int | None = None,
    resume: bool = True,
) -> None:
    try:
        symbols = list(symbols or [])
        if limit:
            symbols = symbols[:limit]
        if len(symbols) > OPTIMIZER_MAX_BATCH_SYMBOLS:
            raise ValueError(f"Optimizer batch is capped at {OPTIMIZER_MAX_BATCH_SYMBOLS} symbols.")
        started_at = datetime.utcnow().isoformat()
        existing = _load_optimizer_cache() if resume else None
        if _same_optimizer_settings(existing, req_template):
            all_top: list[dict] = list(existing.get("top") or [])
            all_best: list[dict] = list(existing.get("best_by_symbol_algo") or existing.get("best_by_algo") or [])
            errors: list[str] = list(existing.get("errors") or [])
            tested_combinations = int(existing.get("tested_combinations") or 0)
            started_at = str(existing.get("started_at") or started_at)
            attempted_symbols = _attempted_symbols_from_cache(existing)
        else:
            all_top = []
            all_best = []
            errors = []
            tested_combinations = 0
            attempted_symbols = []
        attempted_symbol_set = {s.upper() for s in attempted_symbols}

        _update_cheatsheet_job(
            job_id,
            status="running",
            message=f"Custom batch preparing: {len(attempted_symbol_set)}/{len(symbols)} symbols already attempted.",
        )

        pending_symbols = [symbol for symbol in symbols if symbol not in attempted_symbol_set]
        if max_new_symbols is not None:
            pending_symbols = pending_symbols[:max(0, max_new_symbols)]

        prefetch_workers = _optimizer_worker_count("OPTIMIZER_PREFETCH_WORKERS", 1, 1)
        backtest_workers = _optimizer_worker_count("OPTIMIZER_BACKTEST_WORKERS", 1, 2)
        prefetched_frames: dict[str, dict[str, object]] = {}

        if pending_symbols:
            _update_cheatsheet_job(
                job_id,
                status="running",
                message=(
                    f"Custom batch downloading price data for {len(pending_symbols)} symbols "
                    f"with {prefetch_workers} worker."
                ),
            )

        downloaded_count = 0
        auth_blocked = False
        with ThreadPoolExecutor(max_workers=prefetch_workers) as prefetch_pool:
            future_map = {
                prefetch_pool.submit(_prefetch_optimizer_symbol_data, symbol, req_template): symbol
                for symbol in pending_symbols
            }
            for future in as_completed(future_map):
                symbol = future_map[future]
                downloaded_count += 1
                try:
                    _, frames, symbol_errors = future.result()
                except Exception as exc:
                    if _is_schwab_market_auth_error(exc):
                        frames = {}
                        symbol_errors = [f"{symbol}: {exc}"]
                        attempted_symbol_set.add(symbol)
                        auth_blocked = True
                        for pending_future in future_map:
                            if pending_future is not future:
                                pending_future.cancel()
                    else:
                        frames = {}
                        symbol_errors = [f"{symbol}: {exc}"]
                        log.exception("[CHEATSHEET] Custom batch prefetch failed for symbol=%s", symbol)
                if auth_blocked:
                    errors.extend(symbol_errors)
                    partial_result = _build_optimizer_cache_result(
                        req_template=req_template,
                        symbols=symbols,
                        all_top=all_top,
                        all_best=all_best,
                        errors=errors,
                        tested_combinations=tested_combinations,
                        started_at=started_at,
                        status="auth_failed",
                        last_symbol=symbol,
                        attempted_symbols=sorted(attempted_symbol_set),
                    )
                    _write_optimizer_cache(partial_result)
                    _update_cheatsheet_job(
                        job_id,
                        result=partial_result,
                        message="Custom batch stopped: reconnect Schwab Market Data, then rerun optimizer.",
                    )
                    break

                if symbol_errors:
                    errors.extend(f"{symbol}: {err}" for err in symbol_errors)
                if frames:
                    prefetched_frames[symbol] = frames
                else:
                    attempted_symbol_set.add(symbol)

                if downloaded_count == len(pending_symbols) or downloaded_count % 5 == 0:
                    partial_result = _build_optimizer_cache_result(
                        req_template=req_template,
                        symbols=symbols,
                        all_top=all_top,
                        all_best=all_best,
                        errors=errors,
                        tested_combinations=tested_combinations,
                        started_at=started_at,
                        status="downloading",
                        last_symbol=symbol,
                        attempted_symbols=sorted(attempted_symbol_set),
                    )
                    _write_optimizer_cache(partial_result)
                    _update_cheatsheet_job(
                        job_id,
                        result=partial_result,
                        message=(
                            f"Custom batch downloading: {downloaded_count}/{len(pending_symbols)} "
                            f"symbols fetched. {len(prefetched_frames)} ready for backtest."
                        ),
                    )

        if auth_blocked:
            pending_symbols = []

        if prefetched_frames:
            _update_cheatsheet_job(
                job_id,
                message=(
                    f"Custom batch backtesting {len(prefetched_frames)} prefetched symbols "
                    f"with up to {backtest_workers} workers."
                ),
            )

        completed_backtests = 0
        with ThreadPoolExecutor(max_workers=backtest_workers) as backtest_pool:
            future_map = {
                backtest_pool.submit(_run_prefetched_optimizer_symbol, symbol, req_template, frames): symbol
                for symbol, frames in prefetched_frames.items()
            }
            for future in as_completed(future_map):
                symbol = future_map[future]
                completed_backtests += 1
                try:
                    _, result = future.result()
                    tested_combinations += int(result.get("tested_combinations") or 0)
                    all_top.extend(result.get("top") or [])
                    all_best.extend(result.get("best_by_algo") or [])
                    errors.extend(f"{symbol}: {e}" for e in (result.get("errors") or []))
                except Exception as exc:
                    log.exception("[CHEATSHEET] Custom batch failed for symbol=%s", symbol)
                    errors.append(f"{symbol}: {exc}")

                attempted_symbol_set.add(symbol)
                partial_result = _build_optimizer_cache_result(
                    req_template=req_template,
                    symbols=symbols,
                    all_top=all_top,
                    all_best=all_best,
                    errors=errors,
                    tested_combinations=tested_combinations,
                    started_at=started_at,
                    status="running",
                    last_symbol=symbol,
                    attempted_symbols=sorted(attempted_symbol_set),
                )
                _write_optimizer_cache(partial_result)
                _update_cheatsheet_job(
                        job_id,
                        result=partial_result,
                        message=(
                        f"Custom batch backtesting: {completed_backtests}/{len(prefetched_frames)} "
                        f"prefetched symbols complete. Total attempted: "
                        f"{partial_result['completed_symbol_count']}/{len(symbols)}."
                    ),
                )

        all_best.sort(
            key=lambda r: (
                str(r.get("symbol") or ""),
                str(r.get("interval") or ""),
                str(r.get("algo_name") or ""),
            )
        )

        if auth_blocked:
            status = "auth_failed"
        else:
            status = "complete" if len(attempted_symbol_set) >= len(symbols) else "partial"
        result = _build_optimizer_cache_result(
            req_template=req_template,
            symbols=symbols,
            all_top=all_top,
            all_best=all_best,
            errors=errors,
            tested_combinations=tested_combinations,
            started_at=started_at,
            status=status,
            attempted_symbols=sorted(attempted_symbol_set),
        )
        _write_optimizer_cache(result)

        if status == "complete":
            message = f"Custom batch complete. Saved {len(all_best)} recommendations."
        elif status == "auth_failed":
            message = "Custom batch stopped: reconnect Schwab Market Data, then rerun optimizer."
        else:
            message = f"Custom partial run complete. Attempted {result['completed_symbol_count']}/{len(symbols)} symbols."
        _update_cheatsheet_job(
            job_id,
            status="succeeded",
            result=result,
            message=message,
        )
    except Exception as exc:
        log.exception("[CHEATSHEET] Custom batch job failed job_id=%s", job_id)
        _update_cheatsheet_job(
            job_id,
            status="failed",
            error=str(exc),
            message="Custom batch failed.",
        )


# Commercial MM replay config: keep aligned with paper_trade_bot.py and
# app/scripts/stock_algos/Algo1_MM.py / Algo2_MM.py / Algo3_MM.py / Algo4_MM.py / Algo5_MM.py.
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
    "model_refresh_mode": DEFAULT_MODEL_REFRESH_MODE,
    "model_max_age_minutes": DEFAULT_MODEL_MAX_AGE_MINUTES,
    "min_new_bars_before_retrain": DEFAULT_MIN_NEW_BARS_BEFORE_RETRAIN,
    "force_retrain_each_tick": False,
    "builder_days": 30,
    "k_forward": 3,
    "model_max_age_hours": DEFAULT_MODEL_MAX_AGE_MINUTES / 60.0,
    "daily_loss_limit_usd": 5000.0,
    "replay_training_warmup_days": 45,
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
        "replay_training_warmup_days": DEFAULT_REPLAY_MM_CONFIG["replay_training_warmup_days"],
        "force_retrain_each_tick": False,
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
            "stop_loss_usd": float(
                cfg.get("stop_loss_usd", cfg.get("hard_stop_usd", DEFAULT_REPLAY_MM_CONFIG["stop_loss_usd"]))
            ),
            "hard_stop_usd": float(
                cfg.get("stop_loss_usd", cfg.get("hard_stop_usd", DEFAULT_REPLAY_MM_CONFIG["stop_loss_usd"]))
            ),
            "trailing_profit_usd": float(
                cfg.get(
                    "trailing_profit_usd",
                    cfg.get("trailing_stop_distance", cfg.get("trailing_stop_activation", DEFAULT_REPLAY_MM_CONFIG["trailing_profit_usd"])),
                )
            ),
            "stop_loss_pct": float(
                cfg.get("stop_loss_pct", cfg.get("per_share_stop_pct", DEFAULT_REPLAY_MM_CONFIG["stop_loss_pct"]))
            ),
            "trailing_profit_pct": float(
                cfg.get(
                    "trailing_profit_pct",
                    cfg.get("per_share_trailing_profit_pct", DEFAULT_REPLAY_MM_CONFIG["trailing_profit_pct"]),
                )
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
@router.get("/analysis/strategy-optimizer")
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
@router.post("/analysis/strategy-optimizer/api/run")
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

    req = CheatSheetRequest(
        symbol=symbol,
        intervals=parsed_intervals,
        user_id=user.id,
        trade_size=max(float(trade_size or 1.0), 1.0),
        builder_days=max(int(builder_days or DEFAULT_REPLAY_MM_CONFIG["builder_days"]), 10),
        k_forward=max(int(k_forward or DEFAULT_REPLAY_MM_CONFIG["k_forward"]), 1),
        profile=str(profile or "quick").lower(),
        allow_short=_checkbox_on(allow_short_selling),
        eod_close=_checkbox_on(eod_auto_close),
    )
    _cleanup_cheatsheet_jobs()
    job_id = uuid.uuid4().hex
    now = time.time()
    with _cheatsheet_jobs_lock:
        _cheatsheet_jobs[job_id] = {
            "user_id": user.id,
            "status": "queued",
            "message": "Strategy scan queued.",
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        _store_cheatsheet_job(job_id, _cheatsheet_jobs[job_id])
    _cheatsheet_executor.submit(_run_cheatsheet_job, job_id, req)
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job_id,
            "status": "queued",
            "message": "Strategy scan started. This page will update when it finishes.",
        },
    )


@router.get("/analysis/cheatsheet/api/latest")
@router.get("/analysis/strategy-optimizer/api/latest")
@router.get("/auth/backtest-cheatsheet/api/latest")
def latest_backtest_cheatsheet(
    symbol: str = "",
    user: User = Depends(get_current_user),
):
    data = _load_optimizer_cache()
    if not data:
        raise HTTPException(
            status_code=404,
            detail="No cached recommendations found. Run a custom symbol batch first.",
        )
    return JSONResponse(_filter_optimizer_cache(data, symbol))


@router.post("/analysis/cheatsheet/api/run-qqq-batch")
@router.post("/analysis/strategy-optimizer/api/run-qqq-batch")
@router.post("/auth/backtest-cheatsheet/api/run-qqq-batch")
def run_qqq_backtest_cheatsheet(
    symbols: str = Form(...),
    intervals: str = Form("5min"),
    trade_size: float = Form(100.0),
    builder_days: int = Form(DEFAULT_REPLAY_MM_CONFIG["builder_days"]),
    k_forward: int = Form(DEFAULT_REPLAY_MM_CONFIG["k_forward"]),
    profile: str = Form("quick"),
    allow_short_selling: str = Form("on"),
    eod_auto_close: str = Form("on"),
    user: User = Depends(get_current_user),
):
    parsed_symbols = _parse_optimizer_symbols(symbols)
    parsed_intervals = tuple(
        i.strip().lower()
        for i in str(intervals or "5min").replace(";", ",").split(",")
        if i.strip()
    )
    allowed_intervals = {"1min", "5min", "10min", "15min", "30min", "1d"}
    if not parsed_intervals or any(i not in allowed_intervals for i in parsed_intervals):
        raise HTTPException(status_code=400, detail="Choose one or more supported intervals")

    req = CheatSheetRequest(
        symbol="CUSTOM_LIST",
        intervals=parsed_intervals,
        user_id=user.id,
        trade_size=max(float(trade_size or 1.0), 1.0),
        builder_days=max(int(builder_days or DEFAULT_REPLAY_MM_CONFIG["builder_days"]), 10),
        k_forward=max(int(k_forward or DEFAULT_REPLAY_MM_CONFIG["k_forward"]), 1),
        profile=str(profile or "quick").lower(),
        allow_short=_checkbox_on(allow_short_selling),
        eod_close=_checkbox_on(eod_auto_close),
    )
    _cleanup_cheatsheet_jobs()
    job_id = uuid.uuid4().hex
    now = time.time()
    with _cheatsheet_jobs_lock:
        _cheatsheet_jobs[job_id] = {
            "user_id": user.id,
            "status": "queued",
            "message": "Custom symbol batch scan queued.",
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        _store_cheatsheet_job(job_id, _cheatsheet_jobs[job_id])
    backend = _queue_qqq_optimizer_job(job_id=job_id, req=req, symbols=parsed_symbols, resume=False)
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job_id,
            "status": "queued",
            "backend": backend,
            "message": f"Custom batch queued for {len(parsed_symbols)} symbol(s).",
        },
    )


@router.post("/analysis/cheatsheet/api/run-next-qqq-symbol")
@router.post("/analysis/strategy-optimizer/api/run-next-qqq-symbol")
@router.post("/auth/backtest-cheatsheet/api/run-next-qqq-symbol")
def run_next_qqq_symbol_cheatsheet(
    symbols: str = Form(...),
    intervals: str = Form("5min"),
    trade_size: float = Form(100.0),
    builder_days: int = Form(DEFAULT_REPLAY_MM_CONFIG["builder_days"]),
    k_forward: int = Form(DEFAULT_REPLAY_MM_CONFIG["k_forward"]),
    profile: str = Form("quick"),
    allow_short_selling: str = Form("on"),
    eod_auto_close: str = Form("on"),
    user: User = Depends(get_current_user),
):
    parsed_symbols = _parse_optimizer_symbols(symbols)
    parsed_intervals = tuple(
        i.strip().lower()
        for i in str(intervals or "5min").replace(";", ",").split(",")
        if i.strip()
    )
    allowed_intervals = {"1min", "5min", "10min", "15min", "30min", "1d"}
    if not parsed_intervals or any(i not in allowed_intervals for i in parsed_intervals):
        raise HTTPException(status_code=400, detail="Choose one or more supported intervals")

    req = CheatSheetRequest(
        symbol="CUSTOM_LIST",
        intervals=parsed_intervals,
        user_id=user.id,
        trade_size=max(float(trade_size or 1.0), 1.0),
        builder_days=max(int(builder_days or DEFAULT_REPLAY_MM_CONFIG["builder_days"]), 10),
        k_forward=max(int(k_forward or DEFAULT_REPLAY_MM_CONFIG["k_forward"]), 1),
        profile=str(profile or "quick").lower(),
        allow_short=_checkbox_on(allow_short_selling),
        eod_close=_checkbox_on(eod_auto_close),
    )
    _cleanup_cheatsheet_jobs()
    job_id = uuid.uuid4().hex
    now = time.time()
    with _cheatsheet_jobs_lock:
        _cheatsheet_jobs[job_id] = {
            "user_id": user.id,
            "status": "queued",
            "message": "Single symbol scan queued.",
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        _store_cheatsheet_job(job_id, _cheatsheet_jobs[job_id])
    backend = _queue_qqq_optimizer_job(job_id=job_id, req=req, symbols=parsed_symbols, max_new_symbols=1, resume=False)
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job_id,
            "status": "queued",
            "backend": backend,
            "message": "First symbol from your list queued.",
        },
    )


@router.get("/analysis/cheatsheet/api/status/{job_id}")
@router.get("/analysis/strategy-optimizer/api/status/{job_id}")
@router.get("/auth/backtest-cheatsheet/api/status/{job_id}")
def backtest_cheatsheet_status(
    job_id: str,
    user: User = Depends(get_current_user),
):
    _cleanup_cheatsheet_jobs()
    snapshot = _snapshot_cheatsheet_job(job_id, user.id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Scan job not found")
    return JSONResponse(snapshot)


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
            "model_refresh_mode": cfg.get("model_refresh_mode", DEFAULT_REPLAY_MM_CONFIG["model_refresh_mode"]),
            "model_max_age_minutes": cfg.get("model_max_age_minutes", DEFAULT_REPLAY_MM_CONFIG["model_max_age_minutes"]),
            "min_new_bars_before_retrain": cfg.get(
                "min_new_bars_before_retrain",
                DEFAULT_REPLAY_MM_CONFIG["min_new_bars_before_retrain"],
            ),
            "force_retrain_each_tick": cfg.get("model_refresh_mode") == "every_bar",
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
