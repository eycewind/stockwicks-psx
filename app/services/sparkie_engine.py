from __future__ import annotations

import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.models.replay import ReplaySession
from app.models.sparkie import SparkieCandidate, SparkieEvent, SparkieJob
from app.modules.replay.routes import DEFAULT_REPLAY_MM_CONFIG, _build_mm_replay_config
from app.scripts.replay.data_ingest import fetch_and_save
from app.scripts.replay.replay_data_provider import ReplayDataProvider
from app.services.backtest_cheatsheet_service import CheatSheetRequest, run_cheatsheet


MIN_ACCOUNT_EQUITY = 5000.0
DEFAULT_SYMBOL_BUCKET = ("AAPL", "NVDA", "AMD", "PLTR", "INTC")
DEFAULT_INTERVAL_POLICY = ("1min", "5min", "10min", "15min")
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
    workers = max(1, min(int(os.getenv("SPARKIE_BACKTEST_WORKERS", "3")), 4))

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

    _set_job(db, job, status="backtesting", stage="backtesting", message="Sparkie is backtesting candidates.")
    add_event(
        db,
        job,
        "backtesting",
        f"Backtesting {len(frames_by_symbol)} symbols across {len(intervals)} interval(s) and {len(algos)} algo(s).",
    )

    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _backtest_symbol,
                symbol,
                frames,
                intervals,
                int(job.user_id),
            ): symbol
            for symbol, frames in frames_by_symbol.items()
        }
        completed_backtests = 0
        for future in as_completed(futures):
            symbol = futures[future]
            if _stop_requested(db, job_id):
                pool.shutdown(wait=False, cancel_futures=True)
                return _mark_stopped(db, job)
            completed_backtests += 1
            try:
                result = future.result()
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
                    f"{symbol}: tested {result.get('tested_combinations') or 0} combinations, {len(symbol_rows)} candidates.",
                    {"symbol": symbol, "candidate_count": len(symbol_rows)},
                )
            except Exception as exc:
                errors.append(f"{symbol}: {exc}")
                add_event(db, job, "backtesting", f"{symbol}: backtest failed: {exc}", level="warning")
            _update_progress(
                db,
                job,
                completed_steps=len(symbols) + completed_backtests,
                started_at=started,
            )

    ranked = _rank_candidates(rows)
    _replace_candidates(db, job, ranked[:50], errors[:20])
    _set_job(
        db,
        job,
        status="scoring",
        stage="scoring",
        message="Sparkie is scoring the final pick.",
        candidates_tested=len(rows),
    )

    if not ranked:
        _finish_job(
            db,
            job,
            status="rejected",
            message="Sparkie did not find a positive, validated candidate.",
            recommendation="paper_only",
            decision_reason="Backtest scan returned no candidate with positive holdout and validation evidence.",
            result={"errors": errors[:30], "top": []},
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
        job.result_json = _json({"top": ranked[:10], "errors": errors[:30]})
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
            result={"top": ranked[:10], "errors": errors[:30]},
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
    if job.status not in TERMINAL_STATUSES:
        job.status = "stopped"
        job.stage = "stopped"
        job.message = reason
        job.error_message = reason
        job.stop_requested_at = datetime.utcnow()
        job.finished_at = datetime.utcnow()
        job.updated_at = datetime.utcnow()
        add_event(db, job, "stopped", reason, level="warning")
    if job.replay_session_id:
        try:
            from app.services.replay_process import stop_session

            stop_session(db, int(job.replay_session_id))
        except Exception:
            pass
    if job.task_id:
        try:
            from app.celery_app import celery_app

            celery_app.control.revoke(str(job.task_id), terminate=True, signal="SIGTERM")
        except Exception:
            pass


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


def _backtest_symbol(
    symbol: str,
    frames: dict[str, Any],
    intervals: list[str],
    user_id: int,
) -> dict[str, Any]:
    req = CheatSheetRequest(
        symbol=symbol,
        intervals=tuple(intervals),
        user_id=user_id,
        trade_size=1.0,
        builder_days=int(os.getenv("SPARKIE_BACKTEST_LOOKBACK_DAYS", "45")),
        k_forward=int(DEFAULT_REPLAY_MM_CONFIG["k_forward"]),
        profile=os.getenv("SPARKIE_BACKTEST_PROFILE", "quick"),
        allow_short=True,
        eod_close=True,
        oos_fraction=0.35,
        algo_names=tuple(sparkie_algo_policy()),
    )
    return run_cheatsheet(req, price_frames=frames)


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
    target_window = _target_for_backtest_window(job)
    profit = float(row.get("profit_loss") or 0.0)
    trades = int(row.get("trades") or 0)
    win_rate = float(row.get("win_rate") or 0.0)
    max_drawdown = abs(float(row.get("max_drawdown") or 0.0))
    if profit < target_window:
        return {
            "recommendation": "paper_only",
            "run_replay": True,
            "message": "Sparkie found a positive candidate, but it did not meet the target window.",
            "reason": f"Best backtest P/L ${profit:,.2f} is below target window ${target_window:,.2f}.",
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
        "reason": "Backtest met profit, trade count, win-rate, and drawdown gates. Replay verification is still required before Live Mirror.",
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
    allocation = max(float(job.account_equity) * 0.10, 1.0)
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
    allocation = max(float(account_equity) * 0.10, 1.0)
    quantity = int(allocation // price)
    if quantity < 1 and price <= float(account_equity) * 0.25:
        quantity = 1
    if quantity < 1:
        raise ValueError(f"{symbol} is above Sparkie's single-position cap.")
    return float(quantity)


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


def _update_progress(db: Session, job: SparkieJob, *, completed_steps: int, started_at: datetime) -> None:
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
    multiplier = {"daily": 10.0, "weekly": 2.0, "monthly": 0.5}.get(str(job.target_period), 10.0)
    return float(job.target_profit or 0.0) * multiplier


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
