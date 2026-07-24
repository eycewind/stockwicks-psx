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
from app.services.barchart_symbols import barchart_top_symbols
from app.services.sparkie_risk_scoring import (
    SPARKIE_ANALYSIS_MODEL_VERSION,
    risk_adjusted_selection_metrics,
)
from app.services.stocktwits_symbols import stocktwits_ranked_symbols


log = logging.getLogger(__name__)

MIN_ACCOUNT_EQUITY = 2000.0
# Sparkie is intentionally an all-cash, one-position day-trading agent.
# Share rounding can leave a small residual cash balance, but sizing always
# starts from the entire client equity rather than a percentage allocation.
CASH_DEPLOYMENT_POLICY = "full_cash_v1"
DEFAULT_SYMBOL_BUCKET = ("AAPL", "NVDA", "AMD", "PLTR", "INTC")
DEFAULT_INTERVAL_POLICY = ("15min", "10min", "5min")
DEFAULT_ALGO_POLICY = ("Algo1_MM", "Algo2_MM", "Algo3_MM")
TERMINAL_STATUSES = {"completed", "rejected", "error", "stopped"}
ACTIVE_STATUSES = {"queued", "preparing_data", "backtesting", "scoring", "verifying_replay"}
MIN_BACKTEST_BARS_PER_INTERVAL = 80
MIN_BACKTEST_TRADING_DAYS = 5
# A 5-minute research series needs enough independent sessions for a
# validation window plus a 20-day holdout.  The 1-minute replay cache is kept
# short and is never used as the sole monthly-return evidence source.
MIN_NATIVE_RESEARCH_TRADING_DAYS = 60
# Finalist Replay is a separate recent validation window. Twenty-one calendar
# days gives approximately three complete US trading weeks.
MIN_FINALIST_REPLAY_CALENDAR_DAYS = 21
MIN_FINALIST_DAILY_EVIDENCE_DAYS = 20
DEFAULT_EXECUTION_SLIPPAGE_BPS = 10.0
DEFAULT_MAX_POSITION_PCT_DAILY_DOLLAR_VOLUME = 0.01


def sparkie_symbol_bucket_with_source() -> tuple[list[str], str]:
    source = os.getenv("SPARKIE_SYMBOL_SOURCE", "stocktwits_most_active").lower().strip()
    if source == "barchart_top":
        try:
            symbols = barchart_top_symbols(5)
            if symbols:
                return symbols, "barchart_top"
        except Exception as exc:
            log.warning("[SPARKIE] Barchart Top 5 unavailable; using configured bucket: %s", exc)
    if source == "stocktwits_most_active":
        try:
            symbols = stocktwits_ranked_symbols("most_active")
            if symbols:
                return symbols, "stocktwits_most_active"
        except Exception as exc:
            log.warning("[SPARKIE] Stocktwits Most Active unavailable; using configured bucket: %s", exc)
    raw = os.getenv("SPARKIE_SYMBOL_BUCKET", ",".join(DEFAULT_SYMBOL_BUCKET))
    symbols: list[str] = []
    for value in raw.split(","):
        symbol = value.upper().strip()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols or list(DEFAULT_SYMBOL_BUCKET), "configured_fallback"


def sparkie_symbol_bucket() -> list[str]:
    return sparkie_symbol_bucket_with_source()[0]


def sparkie_interval_policy() -> list[str]:
    raw = os.getenv("SPARKIE_INTERVAL_POLICY", ",".join(DEFAULT_INTERVAL_POLICY))
    allowed = {"1min", "5min", "10min", "15min", "30min"}
    intervals: list[str] = []
    for value in raw.split(","):
        interval = value.lower().strip()
        # 1-minute Schwab history is intentionally reserved for the final
        # three-week Replay. It is too short for a credible monthly research
        # comparison and would otherwise poison the catalog evidence.
        if interval in allowed and interval != "1min" and interval not in intervals:
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
    symbol_bucket: list[str] | None = None,
    symbol_source: str = "own_list",
    risk_per_day_pct: float | None = None,
) -> SparkieJob:
    # A user may intentionally narrow or replace the configured universe for
    # one evaluation. An empty selection keeps the operator's default policy.
    symbols = list(symbol_bucket or []) or sparkie_symbol_bucket()
    intervals = sparkie_interval_policy()
    algos = sparkie_algo_policy()
    request = {
        "account_equity": float(account_equity),
        "target_profit": float(target_profit),
        "target_period": target_period,
        "confidence_level": float(confidence_level),
        "symbol_bucket": symbols,
        "symbol_source": symbol_source,
        "cash_deployment_policy": CASH_DEPLOYMENT_POLICY,
    }
    if risk_per_day_pct is not None:
        risk_pct = max(0.10, min(float(risk_per_day_pct), 1.0))
        risk_budget = round(float(account_equity) * risk_pct, 2)
        request.update(
            {
                "analysis_mode": "risk_first_monthly_v1",
                "risk_per_day_pct": risk_pct,
                "risk_per_day_usd": risk_budget,
            }
        )
        # Legacy columns remain populated for compatibility; in risk-first
        # jobs this value is a daily loss budget, never a profit target.
        target_profit = risk_budget
        target_period = "risk_profile"
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
            message=f"Sparkie requires at least ${MIN_ACCOUNT_EQUITY:,.0f} account equity.",
            recommendation="blocked",
            decision_reason="Account equity is below Sparkie's minimum gate.",
        )
        return {"ok": True, "status": job.status}

    symbols = _json_list(job.symbol_bucket_json) or list(DEFAULT_SYMBOL_BUCKET)
    intervals = _json_list(job.interval_policy_json) or list(DEFAULT_INTERVAL_POLICY)
    algos = set(_json_list(job.algo_policy_json) or list(DEFAULT_ALGO_POLICY))
    # Native 5-minute history supports a genuine multi-week holdout.  The
    # short Schwab 1-minute cache remains available only for final Replay.
    lookback_days = max(120, int(os.getenv("SPARKIE_DATA_LOOKBACK_DAYS", "120")))
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
    daily_loss_limit = _risk_first_budget(job) if _is_risk_first_job(job) else float(job.account_equity or 0.0) * 0.10
    futures = {
        pool.submit(
            _backtest_symbol_interval,
            symbol,
            interval,
            frame,
            int(job.user_id),
            float(job.account_equity),
            str(job.id),
            daily_loss_limit,
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
    # Raw strategy score is useful for exploration, but not sufficient for a
    # final choice. Prefer candidates with strong daily target and risk evidence.
    ranked.sort(key=lambda row: _final_selection_sort_key(job, row), reverse=True)
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
                f"{MIN_FINALIST_REPLAY_CALENDAR_DAYS}-calendar-day Replay verification queued for {best['symbol']} {best['interval']} {best['algo_name']}.",
                {"replay_session_id": replay_session_id, "minimum_calendar_days": MIN_FINALIST_REPLAY_CALENDAR_DAYS},
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
    # Retain a 1-minute cache for the final Replay.  Schwab can provide a
    # shorter 1-minute history, so it must not determine monthly research
    # evidence for 5/10/15-minute candidates.
    replay_meta = None
    if "1min" in intervals:
        _path, replay_meta = fetch_and_save(
            user_id=user_id,
            symbol=symbol,
            days=lookback_days,
            force=force,
            interval="1min",
        )
        _validate_ingest_meta(symbol=symbol, meta=replay_meta)
    end_date = datetime.utcnow().date()
    start_date = end_date - timedelta(days=max(lookback_days, 10))
    frames: dict[str, Any] = {}
    row_count = 0
    interval_rows: dict[str, int] = {}
    native_5min_frame: pd.DataFrame | None = None
    native_5min_meta: Any = None

    if any(interval in {"5min", "10min", "15min", "30min"} for interval in intervals):
        native_path, native_5min_meta = fetch_and_save(
            user_id=user_id,
            symbol=symbol,
            days=lookback_days,
            force=force,
            interval="5min",
        )
        _validate_ingest_meta(
            symbol=symbol,
            meta=native_5min_meta,
            minimum_trading_days=MIN_NATIVE_RESEARCH_TRADING_DAYS,
        )
        native_5min_frame = _load_native_research_frame(
            native_path,
            start_date=start_date,
            end_date=end_date,
        )

    for interval in intervals:
        if interval == "1min":
            provider = ReplayDataProvider(
                user_id=user_id,
                symbol=symbol,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                interval=interval,
            )
            frame = _normalize_backtest_frame(provider.bars.copy())
        else:
            frame = _resample_native_research_frame(native_5min_frame, interval)
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
        "last_bar": getattr(native_5min_meta or replay_meta, "last_bar", None),
        "unique_dates": list(getattr(native_5min_meta or replay_meta, "unique_dates", []) or []),
    }


def _validate_ingest_meta(*, symbol: str, meta: Any, minimum_trading_days: int = MIN_BACKTEST_TRADING_DAYS) -> None:
    total_rows = int(getattr(meta, "total_rows", 0) or 0)
    unique_dates = list(getattr(meta, "unique_dates", []) or [])
    last_bar_text = str(getattr(meta, "last_bar", "") or "")
    if total_rows <= 0:
        raise RuntimeError(f"Downloaded data for {symbol} is empty.")
    if len(unique_dates) < minimum_trading_days:
        raise RuntimeError(
            f"Downloaded data for {symbol} has only {len(unique_dates)} trading day(s); "
            f"Sparkie needs at least {minimum_trading_days}."
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


def _load_native_research_frame(path: Path, *, start_date: Any, end_date: Any) -> pd.DataFrame:
    """Load longer native 5-minute research history without Replay resampling."""
    frame = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
    frame.index = pd.to_datetime(frame.index, errors="coerce")
    frame = frame[frame.index.notna()]
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    frame = frame[(frame.index >= start) & (frame.index <= end)]
    frame = frame.between_time("09:30", "16:00")
    if frame.empty:
        raise RuntimeError(f"Native 5-minute research cache has no RTH bars in the requested window: {path}")
    return _normalize_backtest_frame(frame)


def _resample_native_research_frame(frame: pd.DataFrame | None, interval: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    if interval == "5min":
        return frame.copy()
    rules = {"10min": "10min", "15min": "15min", "30min": "30min"}
    rule = rules.get(interval)
    if not rule:
        raise ValueError(f"Unsupported native research interval: {interval}")
    return (
        frame.resample(rule, label="left", closed="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["open"])
    )


def queue_replay_verification(db: Session, job: SparkieJob, best: dict[str, Any]) -> int:
    end_date = datetime.utcnow().date()
    configured_days = int(os.getenv("SPARKIE_REPLAY_LOOKBACK_DAYS", str(MIN_FINALIST_REPLAY_CALENDAR_DAYS)))
    replay_days = max(MIN_FINALIST_REPLAY_CALENDAR_DAYS, configured_days)
    start_date = end_date - timedelta(days=replay_days)
    # The finalist alone receives a fresh short 1-minute cache for Replay;
    # this is deliberately separate from longer native research history.
    fetch_and_save(
        user_id=int(job.user_id),
        symbol=str(best["symbol"]),
        days=replay_days,
        force=False,
        interval="1min",
    )
    trade_size = _trade_quantity(
        user_id=int(job.user_id),
        symbol=str(best["symbol"]),
        interval=str(best["interval"]),
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        account_equity=float(job.account_equity),
    )
    cfg = _replay_config_from_candidate(job, best)
    cfg["sparkie_finalist_replay_calendar_days"] = replay_days
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
    session_id = int(session.id)
    job.replay_session_id = session_id

    # The replay worker uses a separate database connection. Commit the new
    # session before publishing its id, otherwise a fast worker can consume the
    # task while this row is still invisible and leave it QUEUED forever.
    db.commit()

    from app.tasks.replay_tasks import start_replay_session_task

    try:
        async_result = start_replay_session_task.apply_async(args=(session_id,), queue="replay")
    except Exception as exc:
        db.rollback()
        session = db.query(ReplaySession).filter_by(id=session_id).first()
        if session is not None:
            session.status = "ERROR"
            session.error_message = f"Sparkie replay queue failed: {str(exc)[:400]}"
        db.commit()
        raise

    session = db.query(ReplaySession).filter_by(id=session_id).first()
    if session is not None:
        cfg["sparkie_replay_task_id"] = async_result.id
        session.config_json = _json(cfg)
        session.error_message = None
    db.commit()
    return session_id


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
    daily_loss_limit_usd: float = 0.0,
    builder_days: int | None = None,
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
    median_daily_dollar_volume = _median_daily_dollar_volume(frame)
    execution_slippage_bps = max(
        0.0,
        float(os.getenv("SPARKIE_BACKTEST_SLIPPAGE_BPS", str(DEFAULT_EXECUTION_SLIPPAGE_BPS))),
    )
    max_position_pct_adv = max(
        0.0001,
        float(
            os.getenv(
                "SPARKIE_MAX_POSITION_PCT_DAILY_DOLLAR_VOLUME",
                str(DEFAULT_MAX_POSITION_PCT_DAILY_DOLLAR_VOLUME),
            )
        ),
    )
    req = CheatSheetRequest(
        symbol=symbol,
        intervals=(interval,),
        user_id=user_id,
        trade_size=trade_size,
        builder_days=max(120, int(builder_days or os.getenv("SPARKIE_BACKTEST_LOOKBACK_DAYS", "120"))),
        k_forward=int(DEFAULT_REPLAY_MM_CONFIG["k_forward"]),
        profile=os.getenv("SPARKIE_BACKTEST_PROFILE", "sparkie_probe"),
        allow_short=True,
        eod_close=True,
        # Keep enough independent post-selection sessions for a monthly
        # return estimate; the native research cache is at least 60 days.
        oos_fraction=0.65,
        algo_names=tuple(sparkie_algo_policy()),
        model_refresh_mode=os.getenv("SPARKIE_MODEL_REFRESH_MODE", "fixed"),
        model_max_age_minutes=float(os.getenv("SPARKIE_MODEL_MAX_AGE_MINUTES", "0")),
        min_new_bars_before_retrain=int(os.getenv("SPARKIE_MIN_NEW_BARS_BEFORE_RETRAIN", "0")),
        daily_loss_limit_usd=max(float(daily_loss_limit_usd or 0.0), 0.0),
        execution_slippage_bps=execution_slippage_bps,
        commission_per_share=max(
            0.0,
            float(os.getenv("SPARKIE_BACKTEST_COMMISSION_PER_SHARE", "0")),
        ),
        trade_allocation_usd=float(account_equity),
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
        "median_daily_dollar_volume": round(float(median_daily_dollar_volume), 2),
        "execution_slippage_bps": execution_slippage_bps,
    }
    evidence_rows: list[dict[str, Any]] = []
    seen_evidence_rows: set[int] = set()
    for row in [
        *(result.get("top") or []),
        *(result.get("best_by_algo") or []),
    ]:
        row_identity = id(row)
        if row_identity in seen_evidence_rows:
            continue
        seen_evidence_rows.add(row_identity)
        evidence_rows.append(row)
    for row in evidence_rows:
        row["sparkie_trade_size"] = float(trade_size)
        row["sparkie_allocation_usd"] = round(float(allocation_usd), 2)
        row["sparkie_cash_deployment_policy"] = CASH_DEPLOYMENT_POLICY
        row["sparkie_reference_price"] = round(float(reference_price), 4)
        row["sparkie_analysis_model_version"] = SPARKIE_ANALYSIS_MODEL_VERSION
        row["sparkie_execution_slippage_bps"] = execution_slippage_bps
        row["sparkie_median_daily_dollar_volume"] = round(float(median_daily_dollar_volume), 2)
        row["sparkie_max_position_pct_daily_dollar_volume"] = max_position_pct_adv
        row["sparkie_eod_close"] = True
        row["sparkie_overnight_positions_allowed"] = False
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
    request_payload = _json_dict(job.request_json)
    risk_first = _is_risk_first_job(job)
    payload = {
        "job_id": job.id,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "account_equity": float(job.account_equity or 0.0),
        "analysis_mode": "risk_first_monthly_v1" if risk_first else "target_first_legacy",
        "daily_risk_budget_usd": _risk_first_budget(job) if risk_first else None,
        "target_profit": None if risk_first else float(job.target_profit or 0.0),
        "target_period": None if risk_first else job.target_period,
        "symbol_bucket": _json_list(job.symbol_bucket_json),
        "symbol_source": request_payload.get("symbol_source", "legacy"),
        "interval_policy": _json_list(job.interval_policy_json),
        "algo_policy": _json_list(job.algo_policy_json),
        "day_trading_policy": {
            "eod_close": True,
            "overnight_positions_allowed": False,
            "message": "Sparkie day-trading mode forces end-of-day close in backtest and Replay.",
        },
        "trade_allocation_usd": _trade_allocation_usd(float(job.account_equity or 0.0)),
        "cash_deployment_policy": CASH_DEPLOYMENT_POLICY,
        "final_selection_criteria": {
            "minimum_daily_evidence_days": MIN_FINALIST_DAILY_EVIDENCE_DAYS,
            "research_confidence_preference": float(job.confidence_level or 0.0),
            "minimum_trades": 5,
            "minimum_win_rate": 0.50,
            "daily_loss_budget_usd": _risk_first_budget(job) if risk_first else None,
            "daily_loss_limit_multiple": None if risk_first else 5.0,
            "finalist_replay_minimum_calendar_days": MIN_FINALIST_REPLAY_CALENDAR_DAYS,
        },
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
        "daily_pnl",
        "sparkie_trade_size",
        "sparkie_allocation_usd",
        "sparkie_cash_deployment_policy",
        "sparkie_reference_price",
        "sparkie_analysis_model_version",
        "sparkie_execution_slippage_bps",
        "sparkie_median_daily_dollar_volume",
        "sparkie_max_position_pct_daily_dollar_volume",
        "sparkie_eod_close",
        "sparkie_overnight_positions_allowed",
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
    if _is_risk_first_job(job):
        return _risk_first_decision(job, row)
    target_units = _target_window_units(job.target_period)
    profit = float(row.get("profit_loss") or 0.0)
    target_period = str(job.target_period or "daily")
    daily_stats = _daily_target_stats(
        (row.get("params") or {}).get("daily_pnl"),
        float(job.target_profit or 0.0),
    ) if target_period == "daily" else None
    estimated_period_profit = (
        float(daily_stats["average_daily_profit_loss"])
        if daily_stats and daily_stats.get("available")
        else profit / target_units if target_units else profit
    )
    trades = int(row.get("trades") or 0)
    win_rate = float(row.get("win_rate") or 0.0)
    max_drawdown = abs(float(row.get("max_drawdown") or 0.0))
    if target_period == "daily" and (not daily_stats or not daily_stats.get("available")):
        return _paper_only_decision("Sparkie has no usable daily P/L evidence for this finalist.")
    if daily_stats and int(daily_stats["days_tested"]) < MIN_FINALIST_DAILY_EVIDENCE_DAYS:
        return _paper_only_decision(
            f"Only {daily_stats['days_tested']} daily observations are available; Sparkie requires at least {MIN_FINALIST_DAILY_EVIDENCE_DAYS} before final Replay verification."
        )
    if estimated_period_profit < float(job.target_profit or 0.0):
        return {
            "recommendation": "paper_only",
            "run_replay": False,
            "message": "Sparkie found a positive candidate, but it did not meet the minimum target.",
            "reason": (
                f"Average {target_period} backtest P/L ${estimated_period_profit:,.2f} "
                f"is below the user target ${float(job.target_profit or 0.0):,.2f} "
                f"({_daily_target_evidence_text(daily_stats, target_units, target_period, profit)})."
            ),
        }
    if daily_stats and daily_stats.get("target_hit_rate_95pct_low", 0.0) < float(job.confidence_level or 0.0):
        return {
            "recommendation": "paper_only",
            "run_replay": False,
            "message": "Sparkie averaged the target, but historical confidence is not strong enough.",
            "reason": (
                f"95% lower target-hit estimate is {daily_stats['target_hit_rate_95pct_low'] * 100:,.1f}% "
                f"from {daily_stats['target_hit_days']} of {daily_stats['days_tested']} daily target hits, below the requested "
                f"{float(job.confidence_level or 0.0) * 100:,.1f}% confidence level."
            ),
        }
    if daily_stats and float(daily_stats["worst_daily_profit_loss"]) < -float(job.target_profit or 0.0) * 5.0:
        return _paper_only_decision(
            f"Historical worst daily P/L ${daily_stats['worst_daily_profit_loss']:,.2f} exceeded Sparkie's daily loss limit."
        )
    if trades < 5 or win_rate < 0.50:
        return {
            "recommendation": "paper_only",
            "run_replay": False,
            "message": "Sparkie found a candidate, but evidence is not strong enough for Live Mirror.",
            "reason": "Trade count or win rate is below Sparkie's evidence threshold.",
        }
    if max_drawdown > float(job.account_equity) * 0.05:
        return {
            "recommendation": "paper_only",
            "run_replay": False,
            "message": "Sparkie found profit, but drawdown is too large for Live Mirror.",
            "reason": "Backtest drawdown exceeds Sparkie's 5% account risk rail.",
        }
    return {
        "recommendation": "paper_candidate",
        "run_replay": True,
        "message": "Sparkie found a positive candidate and queued Replay verification.",
        "reason": (
            f"Average {target_period} backtest P/L ${estimated_period_profit:,.2f} "
            f"is greater than or equal to the user target ${float(job.target_profit or 0.0):,.2f}, "
            f"with {_daily_target_evidence_text(daily_stats, target_units, target_period, profit)}, "
            f"trade count, win-rate, and drawdown gates met. "
            f"A {MIN_FINALIST_REPLAY_CALENDAR_DAYS}-calendar-day finalist Replay verification is now required before Live Mirror."
        ),
    }


def _daily_target_stats(raw_rows: Any, target_profit: float) -> dict[str, Any]:
    if not isinstance(raw_rows, list):
        return {"available": False}
    profits = [float(row.get("profit_loss") or 0.0) for row in raw_rows if isinstance(row, dict) and row.get("date")]
    if not profits:
        return {"available": False}
    hit_days = sum(1 for profit in profits if profit >= target_profit)
    hit_interval = _wilson_interval(hit_days, len(profits))
    return {
        "available": True,
        "days_tested": len(profits),
        "target_hit_days": hit_days,
        "target_hit_rate": hit_days / len(profits),
        "target_hit_rate_95pct_low": hit_interval[0],
        "target_hit_rate_95pct_high": hit_interval[1],
        "average_daily_profit_loss": sum(profits) / len(profits),
        "worst_daily_profit_loss": min(profits),
    }


def _final_selection_sort_key(job: SparkieJob, row: dict[str, Any]) -> tuple[float, ...]:
    """Prefer candidates that meet final daily-target criteria, not raw score."""
    if _is_risk_first_job(job):
        return _risk_first_selection_sort_key(job, row)
    daily = _daily_target_stats((row.get("params") or {}).get("daily_pnl"), float(job.target_profit or 0.0))
    max_drawdown = abs(float(row.get("max_drawdown") or 0.0))
    qualified = bool(
        daily.get("available")
        and int(daily.get("days_tested") or 0) >= MIN_FINALIST_DAILY_EVIDENCE_DAYS
        and float(daily.get("average_daily_profit_loss") or 0.0) >= float(job.target_profit or 0.0)
        and float(daily.get("target_hit_rate_95pct_low") or 0.0) >= float(job.confidence_level or 0.0)
        and float(daily.get("worst_daily_profit_loss") or 0.0) >= -float(job.target_profit or 0.0) * 5.0
        and int(row.get("trades") or 0) >= 5
        and float(row.get("win_rate") or 0.0) >= 0.50
        and max_drawdown <= float(job.account_equity or 0.0) * 0.05
    )
    return (
        float(qualified),
        float(daily.get("target_hit_rate_95pct_low") or 0.0),
        float(daily.get("target_hit_rate") or 0.0),
        float(daily.get("average_daily_profit_loss") or 0.0),
        float(row.get("validation_profit_loss") or 0.0),
        -max_drawdown,
        float(row.get("score") or 0.0),
    )


def _paper_only_decision(reason: str) -> dict[str, Any]:
    return {
        "recommendation": "paper_only",
        "run_replay": False,
        "message": "Sparkie did not find a finalist that meets the full selection criteria.",
        "reason": reason,
    }


def _is_risk_first_job(job: SparkieJob) -> bool:
    request = _json_dict(job.request_json)
    return request.get("analysis_mode") == "risk_first_monthly_v1"


def _risk_first_budget(job: SparkieJob) -> float:
    request = _json_dict(job.request_json)
    return max(0.0, float(request.get("risk_per_day_usd") or job.target_profit or 0.0))


def _monthly_return_profile(raw_rows: Any, account_equity: float) -> dict[str, Any]:
    profits = [float(row.get("profit_loss") or 0.0) for row in raw_rows if isinstance(row, dict) and row.get("date")]
    if not profits:
        return {"available": False}
    window = min(21, len(profits))
    monthly_windows = [sum(profits[index:index + window]) for index in range(max(len(profits) - window + 1, 1))]
    monthly_windows.sort()

    def percentile(fraction: float) -> float:
        if len(monthly_windows) == 1:
            return monthly_windows[0]
        position = (len(monthly_windows) - 1) * fraction
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        if lower == upper:
            return monthly_windows[lower]
        return monthly_windows[lower] + (monthly_windows[upper] - monthly_windows[lower]) * (position - lower)

    conservative, typical, strong = percentile(0.25), percentile(0.50), percentile(0.75)
    denominator = max(float(account_equity or 0.0), 1.0)
    return {
        "available": True,
        "days_tested": len(profits),
        "trading_days_per_month": window,
        "conservative_monthly_pnl": round(conservative, 2),
        "typical_monthly_pnl": round(typical, 2),
        "strong_monthly_pnl": round(strong, 2),
        "conservative_monthly_return_pct": round(conservative / denominator * 100.0, 2),
        "typical_monthly_return_pct": round(typical / denominator * 100.0, 2),
        "strong_monthly_return_pct": round(strong / denominator * 100.0, 2),
        "worst_daily_pnl": round(min(profits), 2),
        "positive_day_rate": round(sum(value > 0 for value in profits) / len(profits), 4),
    }


def _risk_first_decision(job: SparkieJob, row: dict[str, Any]) -> dict[str, Any]:
    raw_daily = (row.get("params") or {}).get("daily_pnl")
    profile = _monthly_return_profile(raw_daily, float(job.account_equity or 0.0))
    risk_budget = _risk_first_budget(job)
    if not profile.get("available") or int(profile["days_tested"]) < MIN_FINALIST_DAILY_EVIDENCE_DAYS:
        return _paper_only_decision(
            f"Sparkie needs at least {MIN_FINALIST_DAILY_EVIDENCE_DAYS} daily observations to estimate a monthly return."
        )
    if float(profile["worst_daily_pnl"]) < -risk_budget:
        return _paper_only_decision(
            f"Historical worst day ${profile['worst_daily_pnl']:,.2f} exceeded the client's ${risk_budget:,.2f} daily risk budget."
        )
    if int(row.get("trades") or 0) < 5:
        return _paper_only_decision("Trade count is below Sparkie's minimum evidence threshold.")
    if float(row.get("validation_profit_loss") or 0.0) <= 0 or int(row.get("validation_trades") or 0) < 3:
        return _paper_only_decision("The holdout validation result is not positive enough.")
    if float(profile["conservative_monthly_pnl"]) <= 0:
        return _paper_only_decision("The conservative historical monthly P/L scenario is not positive.")
    execution = _execution_evidence(row)
    if not execution["execution_cost_model_present"]:
        return _paper_only_decision("The backtest does not include Sparkie's required execution-cost model.")
    if not execution["liquidity_capacity_passed"]:
        return _paper_only_decision(
            "The proposed full-cash position exceeds Sparkie's configured share of median daily dollar volume."
        )
    selection_metrics = _risk_first_selection_metrics(job, row, profile)
    confidence_low = float(selection_metrics.get("positive_day_rate_95pct_low") or 0.0)
    requested_confidence = float(job.confidence_level or 0.0)
    if confidence_low < requested_confidence:
        return _paper_only_decision(
            f"The conservative profitable-day estimate is {confidence_low:.1%}, below the requested "
            f"{requested_confidence:.1%} confidence."
        )
    return {
        "recommendation": "paper_candidate",
        "run_replay": True,
        "message": "Sparkie selected the strongest risk-adjusted monthly-return candidate and queued Replay verification.",
        "reason": (
            f"With a daily risk budget of ${risk_budget:,.2f}, historical monthly scenarios are "
            f"${profile['conservative_monthly_pnl']:,.2f} conservative, ${profile['typical_monthly_pnl']:,.2f} typical, and "
            f"${profile['strong_monthly_pnl']:,.2f} strong. A {MIN_FINALIST_REPLAY_CALENDAR_DAYS}-calendar-day Replay is required before Live Mirror."
        ),
        "monthly_return_profile": profile,
        "selection_metrics": selection_metrics,
    }


def _risk_first_selection_sort_key(job: SparkieJob, row: dict[str, Any]) -> tuple[float, ...]:
    profile = _monthly_return_profile((row.get("params") or {}).get("daily_pnl"), float(job.account_equity or 0.0))
    risk_budget = _risk_first_budget(job)
    metrics = _risk_first_selection_metrics(job, row, profile)
    execution = _execution_evidence(row)
    qualified = bool(
        profile.get("available")
        and int(profile.get("days_tested") or 0) >= MIN_FINALIST_DAILY_EVIDENCE_DAYS
        and float(profile.get("worst_daily_pnl") or 0.0) >= -risk_budget
        and float(profile.get("conservative_monthly_pnl") or 0.0) > 0
        and float(row.get("validation_profit_loss") or 0.0) > 0
        and int(row.get("validation_trades") or 0) >= 3
        and int(row.get("trades") or 0) >= 5
        and float(metrics.get("positive_day_rate_95pct_low") or 0.0) >= float(job.confidence_level or 0.0)
        and execution["execution_cost_model_present"]
        and execution["liquidity_capacity_passed"]
    )
    return (
        float(qualified),
        float(metrics.get("positive_day_rate_95pct_low") or 0.0),
        float(metrics.get("positive_day_rate") or 0.0),
        float(metrics.get("selection_score") or 0.0),
        float(metrics.get("return_to_risk") or 0.0),
        float(metrics.get("validation_return_pct") or 0.0),
        float(profile.get("conservative_monthly_pnl") or 0.0),
        -abs(float(row.get("max_drawdown") or 0.0)),
        float(row.get("score") or 0.0),
    )


def _execution_evidence(row: dict[str, Any]) -> dict[str, Any]:
    params = row.get("params") or {}
    model_version = str(params.get("sparkie_analysis_model_version") or "")
    allocation = float(params.get("sparkie_allocation_usd") or 0.0)
    median_dollar_volume = float(params.get("sparkie_median_daily_dollar_volume") or 0.0)
    max_position_pct = float(
        params.get("sparkie_max_position_pct_daily_dollar_volume")
        or DEFAULT_MAX_POSITION_PCT_DAILY_DOLLAR_VOLUME
    )
    capacity = median_dollar_volume * max_position_pct
    return {
        "execution_cost_model_present": model_version == SPARKIE_ANALYSIS_MODEL_VERSION,
        "analysis_model_version": model_version,
        "execution_slippage_bps": float(params.get("sparkie_execution_slippage_bps") or 0.0),
        "median_daily_dollar_volume": median_dollar_volume,
        "liquidity_capacity_usd": capacity,
        "liquidity_capacity_passed": bool(
            median_dollar_volume > 0.0 and allocation > 0.0 and allocation <= capacity
        ),
    }


def _risk_first_selection_metrics(
    job: SparkieJob,
    row: dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_profile = profile or _monthly_return_profile(
        (row.get("params") or {}).get("daily_pnl"),
        float(job.account_equity or 0.0),
    )
    daily_rows = (row.get("params") or {}).get("daily_pnl")
    profits = [
        float(item.get("profit_loss") or 0.0)
        for item in (daily_rows if isinstance(daily_rows, list) else [])
        if isinstance(item, dict) and item.get("date")
    ]
    return risk_adjusted_selection_metrics(
        daily_profits=profits,
        account_equity=float(job.account_equity or 0.0),
        daily_risk_budget=_risk_first_budget(job),
        conservative_monthly_pnl=float(resolved_profile.get("conservative_monthly_pnl") or 0.0),
        typical_monthly_pnl=float(resolved_profile.get("typical_monthly_pnl") or 0.0),
        max_drawdown=abs(float(row.get("max_drawdown") or 0.0)),
        validation_profit_loss=float(row.get("validation_profit_loss") or 0.0),
        confidence_preference=float(job.confidence_level or 0.0),
    )


def _wilson_interval(successes: int, trials: int, z_score: float = 1.96) -> tuple[float, float]:
    if trials <= 0:
        return 0.0, 0.0
    observed = max(0.0, min(1.0, float(successes) / float(trials)))
    z_squared = z_score * z_score
    denominator = 1.0 + z_squared / trials
    center = (observed + z_squared / (2.0 * trials)) / denominator
    spread = z_score * math.sqrt((observed * (1.0 - observed) + z_squared / (4.0 * trials)) / trials) / denominator
    return round(max(0.0, center - spread), 4), round(min(1.0, center + spread), 4)


def _daily_target_evidence_text(
    daily_stats: dict[str, Any] | None,
    target_units: float,
    target_period: str,
    total_profit: float,
) -> str:
    if daily_stats and daily_stats.get("available"):
        return (
            f"target met {daily_stats['target_hit_days']} of {daily_stats['days_tested']} trading days "
            f"({daily_stats['target_hit_rate'] * 100:,.1f}% hit rate), cumulative P/L ${total_profit:,.2f}"
        )
    return f"{target_units:g} {target_period} unit evidence window total: ${total_profit:,.2f}"


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
    risk_first = _is_risk_first_job(job)
    daily_target = 0.0 if risk_first else float(job.target_profit) if str(job.target_period or "").lower() == "daily" else 0.0
    daily_loss_limit = _risk_first_budget(job) if risk_first else daily_target * 5.0
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
            "sparkie_cash_deployment_policy": CASH_DEPLOYMENT_POLICY,
            "sparkie_eod_close": True,
            "sparkie_overnight_positions_allowed": False,
            "sparkie_fast_replay": True,
            "daily_profit_target_usd": daily_target,
            "daily_loss_limit_usd": daily_loss_limit,
            "daily_loss_multiplier": None if risk_first else 5.0,
            "stop_trading_after_daily_target": daily_target > 0,
            "stop_trading_after_daily_loss": daily_loss_limit > 0,
            "sparkie_analysis_mode": "risk_first_monthly_v1" if risk_first else "target_first_legacy",
            "full_cash_risk_acknowledged": True,
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
    if quantity < 1 and price <= float(account_equity):
        quantity = 1
    if quantity < 1:
        raise ValueError(f"{symbol} is above Sparkie's single-position cap.")
    return float(quantity)


def _trade_allocation_usd(account_equity: float) -> float:
    equity = max(float(account_equity or 0.0), 0.0)
    return equity


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


def _median_daily_dollar_volume(frame: Any) -> float:
    df = pd.DataFrame(frame).copy()
    if df.empty:
        return 0.0
    close_col = "close" if "close" in df.columns else "Close" if "Close" in df.columns else None
    volume_col = "volume" if "volume" in df.columns else "Volume" if "Volume" in df.columns else None
    if not close_col or not volume_col:
        return 0.0
    close = pd.to_numeric(df[close_col], errors="coerce").fillna(0.0)
    volume = pd.to_numeric(df[volume_col], errors="coerce").fillna(0.0)
    index = pd.to_datetime(df.index, errors="coerce")
    valid = ~index.isna()
    if not valid.any():
        return 0.0
    daily = pd.Series((close * volume).to_numpy()[valid], index=index[valid]).groupby(
        index[valid].date
    ).sum()
    if daily.empty:
        return 0.0
    return float(daily.tail(20).median())


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


def _json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}
