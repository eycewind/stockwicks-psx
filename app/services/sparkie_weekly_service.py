from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import statistics
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.models.sparkie import SparkieWeeklyResult, SparkieWeeklyRun
from app.services.barchart_symbols import barchart_bullish_rows_with_source


WEEKLY_ACTIVE_STATUSES = {"queued", "snapshotting", "running", "stopping"}
WEEKLY_TERMINAL_STATUSES = {"completed", "stopped", "error"}
WEEKLY_PROCESS_PREFIX = "sparkie-weekly-process:"
DEFAULT_BASELINE_CASH = 10_000.0
DEFAULT_BATCH_SIZE = 5
DAILY_LOSS_MULTIPLIER = 5.0


def create_weekly_run(
    db: Session,
    *,
    user_id: int,
    run_mode: str = "manual",
    baseline_cash: float = DEFAULT_BASELINE_CASH,
) -> SparkieWeeklyRun:
    active = (
        db.query(SparkieWeeklyRun)
        .filter(SparkieWeeklyRun.user_id == int(user_id))
        .filter(SparkieWeeklyRun.status.in_(tuple(WEEKLY_ACTIVE_STATUSES)))
        .order_by(SparkieWeeklyRun.created_at.desc())
        .first()
    )
    if active:
        return active

    from app.services.sparkie_engine import sparkie_algo_policy, sparkie_interval_policy

    intervals = sparkie_interval_policy()
    algos = sparkie_algo_policy()
    config_payload = {
        "intervals": intervals,
        "algos": algos,
        "baseline_cash": float(baseline_cash),
        "batch_size": DEFAULT_BATCH_SIZE,
        "cash_policy": "full_cash_v1",
        "daily_loss_multiplier": DAILY_LOSS_MULTIPLIER,
    }
    config_hash = hashlib.sha256(
        json.dumps(config_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    run_id = f"sparkie-weekly-{int(user_id)}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    run = SparkieWeeklyRun(
        id=run_id,
        user_id=int(user_id),
        status="queued",
        stage="queued",
        message="Weekly Sparkie research is queued.",
        run_mode=str(run_mode or "manual"),
        baseline_cash=float(baseline_cash),
        batch_size=DEFAULT_BATCH_SIZE,
        intervals_json=_json(intervals),
        algos_json=_json(algos),
        config_hash=config_hash,
        progress_pct=0.0,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def run_weekly_research(db: Session, run_id: str) -> dict[str, Any]:
    run = db.get(SparkieWeeklyRun, str(run_id))
    if not run:
        return {"ok": False, "error": "Weekly Sparkie run not found."}

    started = run.started_at or datetime.utcnow()
    if run.stop_requested_at or str(run.status or "").lower() in {"stopping", "stopped"}:
        return _finish_stopped(db, run, started)
    run.started_at = started
    run.finished_at = None
    run.stop_requested_at = None
    run.error_message = None
    run.status = "snapshotting"
    run.stage = "snapshotting"
    run.message = "Saving the current Stock Bullish Top 100 universe."
    run.heartbeat_at = datetime.utcnow()
    db.commit()

    try:
        universe = _json_list(run.universe_json)
        if not universe:
            universe, ranking_source = barchart_bullish_rows_with_source(force_refresh=True)
            universe = [{**row, "sparkieRankingSource": ranking_source} for row in universe]
            run.universe_json = _json(universe)
            run.total_symbols = len(universe)
            _write_universe_snapshot(run, universe)
            db.commit()
        else:
            run.total_symbols = len(universe)

        intervals = _json_list(run.intervals_json)
        algos = set(_json_list(run.algos_json))
        completed_symbols = _completed_symbols(db, run.id)
        run.completed_symbols = len(completed_symbols)
        run.status = "running"
        run.stage = "running"
        run.message = "Weekly Sparkie research is processing five-symbol batches."
        run.heartbeat_at = datetime.utcnow()
        db.commit()

        pending_rows = [row for row in universe if str(row.get("symbol") or "").upper() not in completed_symbols]
        for pending_index, universe_row in enumerate(pending_rows):
            db.expire_all()
            run = db.get(SparkieWeeklyRun, run.id)
            if not run or run.stop_requested_at or str(run.status or "").lower() in {"stopping", "stopped"}:
                return _finish_stopped(db, run, started)

            symbol = str(universe_row.get("symbol") or "").upper().strip()
            if not symbol:
                continue
            run.current_symbol = symbol
            run.current_batch = (int(run.completed_symbols or 0) // max(int(run.batch_size or 5), 1)) + 1
            run.message = f"Batch {run.current_batch}: processing {symbol}."
            run.heartbeat_at = datetime.utcnow()
            _update_timing(run, started)
            db.commit()

            success = _process_weekly_symbol(
                db,
                run=run,
                universe_row=universe_row,
                intervals=intervals,
                algos=algos,
            )
            db.expire_all()
            run = db.get(SparkieWeeklyRun, run.id)
            if not run or run.stop_requested_at or str(run.status or "").lower() in {"stopping", "stopped"}:
                return _finish_stopped(db, run, started)
            if success:
                _store_symbol_checkpoint(db, run, universe_row, failed=False)
                run.completed_symbols = int(run.completed_symbols or 0) + 1
            else:
                run.failed_symbols = int(run.failed_symbols or 0) + 1
                # A failed symbol is still checkpointed so Resume advances.
                run.completed_symbols = int(run.completed_symbols or 0) + 1
                _store_symbol_checkpoint(db, run, universe_row, failed=True)
            run.current_symbol = None
            run.heartbeat_at = datetime.utcnow()
            _update_timing(run, started)
            db.commit()

        result_count = (
            db.query(SparkieWeeklyResult)
            .filter_by(run_id=run.id)
            .filter(SparkieWeeklyResult.daily_pnl_json.isnot(None))
            .count()
        )
        run.status = "completed"
        run.stage = "completed"
        run.message = f"Weekly Sparkie research completed with {result_count} saved strategy results."
        run.progress_pct = 100.0
        run.eta_seconds = 0
        run.current_symbol = None
        run.heartbeat_at = datetime.utcnow()
        run.finished_at = datetime.utcnow()
        run.summary_json = _json(
            {
                "result_count": result_count,
                "total_symbols": int(run.total_symbols or 0),
                "completed_symbols": int(run.completed_symbols or 0),
                "failed_symbols": int(run.failed_symbols or 0),
                "daily_loss_multiplier": DAILY_LOSS_MULTIPLIER,
                "cash_policy": "full_cash_v1",
            }
        )
        _update_timing(run, started)
        db.commit()
        return {"ok": True, "status": run.status, "run_id": run.id}
    except Exception as exc:
        db.rollback()
        run = db.get(SparkieWeeklyRun, str(run_id))
        if run:
            run.status = "error"
            run.stage = "error"
            run.message = "Weekly Sparkie research failed. Resume is available."
            run.error_message = str(exc)[:1000]
            run.finished_at = datetime.utcnow()
            run.heartbeat_at = datetime.utcnow()
            _update_timing(run, started)
            db.commit()
        raise


def weekly_recommendation(
    db: Session,
    *,
    user_id: int,
    account_equity: float,
    daily_target: float,
    confidence_level: float,
) -> dict[str, Any]:
    run = (
        db.query(SparkieWeeklyRun)
        .filter(SparkieWeeklyRun.user_id == int(user_id))
        .filter(SparkieWeeklyRun.status == "completed")
        .order_by(SparkieWeeklyRun.finished_at.desc().nullslast(), SparkieWeeklyRun.created_at.desc())
        .first()
    )
    if not run:
        raise ValueError("Complete a Weekly Sparkie research run before using saved recommendations.")

    rows = db.query(SparkieWeeklyResult).filter_by(run_id=run.id).all()
    usable_rows = [row for row in rows if not row.error_message and row.daily_pnl_json]
    if not usable_rows:
        raise ValueError("The latest weekly catalog has no usable strategy results.")

    requested = _best_match(
        usable_rows,
        account_equity=float(account_equity),
        daily_target=float(daily_target),
        confidence_level=float(confidence_level),
    )
    ladder = _capital_ladder(float(account_equity))
    minimum = None
    for cash in ladder:
        candidate = _best_match(
            usable_rows,
            account_equity=cash,
            daily_target=float(daily_target),
            confidence_level=float(confidence_level),
        )
        if candidate and candidate["qualified"]:
            minimum = candidate
            break

    return {
        "ok": True,
        "run_id": run.id,
        "catalog_completed_at": run.finished_at.isoformat() if run.finished_at else None,
        "catalog_symbols": int(run.completed_symbols or 0),
        "catalog_results": len(usable_rows),
        "account_equity": round(float(account_equity), 2),
        "daily_target": round(float(daily_target), 2),
        "daily_loss_limit": round(float(daily_target) * DAILY_LOSS_MULTIPLIER, 2),
        "daily_loss_multiplier": DAILY_LOSS_MULTIPLIER,
        "confidence_level": float(confidence_level),
        "all_cash_at_risk": True,
        "requested_cash_match": requested,
        "minimum_cash_match": minimum,
        "screening_only": True,
        "verification_required": True,
        "message": (
            "A matching historical catalog result was found; run a fresh finalist verification before bot setup."
            if requested and requested["qualified"]
            else "No saved result met every target, confidence, and drawdown gate at the requested cash level."
        ),
    }


def weekly_run_payload(db: Session, run: SparkieWeeklyRun | None) -> dict[str, Any] | None:
    if not run:
        return None
    result_count = (
        db.query(SparkieWeeklyResult)
        .filter_by(run_id=run.id)
        .filter(SparkieWeeklyResult.daily_pnl_json.isnot(None))
        .count()
    )
    now = datetime.utcnow()
    heartbeat_age = int((now - run.heartbeat_at).total_seconds()) if run.heartbeat_at else None
    return {
        "run_id": run.id,
        "status": run.status,
        "stage": run.stage,
        "message": run.message,
        "run_mode": run.run_mode,
        "baseline_cash": float(run.baseline_cash or 0.0),
        "batch_size": int(run.batch_size or 0),
        "total_symbols": int(run.total_symbols or 0),
        "completed_symbols": int(run.completed_symbols or 0),
        "failed_symbols": int(run.failed_symbols or 0),
        "current_batch": int(run.current_batch or 0),
        "current_symbol": run.current_symbol,
        "progress_pct": float(run.progress_pct or 0.0),
        "elapsed_seconds": int(run.elapsed_seconds or 0),
        "eta_seconds": run.eta_seconds,
        "result_count": result_count,
        "task_id": run.task_id,
        "heartbeat_at": run.heartbeat_at.isoformat() if run.heartbeat_at else None,
        "heartbeat_age_seconds": heartbeat_age,
        "error_message": run.error_message,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "can_resume": str(run.status or "").lower() in {"stopped", "error"} and int(run.completed_symbols or 0) < int(run.total_symbols or 100),
    }


def latest_weekly_run(db: Session, user_id: int) -> SparkieWeeklyRun | None:
    return (
        db.query(SparkieWeeklyRun)
        .filter(SparkieWeeklyRun.user_id == int(user_id))
        .order_by(SparkieWeeklyRun.created_at.desc())
        .first()
    )


def _process_weekly_symbol(
    db: Session,
    *,
    run: SparkieWeeklyRun,
    universe_row: dict[str, Any],
    intervals: list[str],
    algos: set[str],
) -> bool:
    from app.services.sparkie_engine import _backtest_symbol_interval, refresh_symbol_data

    symbol = str(universe_row.get("symbol") or "").upper().strip()
    run.error_message = None
    lookback_days = int(os.getenv("SPARKIE_WEEKLY_LOOKBACK_DAYS", "90"))
    meta = refresh_symbol_data(
        user_id=int(run.user_id),
        symbol=symbol,
        intervals=intervals,
        lookback_days=lookback_days,
    )
    frames = meta.get("frames") or {}
    stored = 0
    errors: list[str] = []
    db.query(SparkieWeeklyResult).filter_by(run_id=run.id, symbol=symbol).delete(synchronize_session=False)
    db.commit()

    for interval in intervals:
        db.expire_all()
        fresh_run = db.get(SparkieWeeklyRun, run.id)
        if fresh_run and fresh_run.stop_requested_at:
            break
        frame = frames.get(interval)
        if frame is None or getattr(frame, "empty", True):
            errors.append(f"{interval}: no usable data")
            continue
        try:
            result = _backtest_symbol_interval(
                symbol,
                interval,
                frame,
                int(run.user_id),
                float(run.baseline_cash),
                None,
            )
            candidates = [
                row for row in (result.get("top") or [])
                if str(row.get("algo_name") or "") in algos
                and float(row.get("total_profit") or 0.0) > 0
                and float(row.get("validation_total_profit") or 0.0) > 0
            ]
            best_by_algo: dict[str, dict[str, Any]] = {}
            for row in candidates:
                algo_name = str(row.get("algo_name") or "")
                if algo_name and algo_name not in best_by_algo:
                    best_by_algo[algo_name] = row
            timing = result.get("sparkie_timing") or {}
            for row in best_by_algo.values():
                daily_rows = row.get("daily_pnl") if isinstance(row.get("daily_pnl"), list) else []
                profits = [float(item.get("profit_loss") or 0.0) for item in daily_rows if isinstance(item, dict)]
                if not profits:
                    continue
                reference_price = float(timing.get("reference_price") or 0.0)
                baseline_shares = int(float(timing.get("trade_size") or 0.0))
                db.add(
                    SparkieWeeklyResult(
                        run_id=run.id,
                        user_id=int(run.user_id),
                        symbol=symbol,
                        universe_rank=_int_or_none(universe_row.get("currentRankUsTop100")),
                        weighted_alpha=_float_or_none(universe_row.get("weightedAlpha")),
                        interval=str(interval),
                        algo_name=str(row.get("algo_name") or ""),
                        reference_price=reference_price or None,
                        baseline_cash=float(run.baseline_cash),
                        baseline_shares=max(baseline_shares, 1),
                        score=float(row.get("score") or 0.0),
                        average_daily_pnl=sum(profits) / len(profits),
                        median_daily_pnl=statistics.median(profits),
                        profitable_day_rate=sum(1 for value in profits if value > 0) / len(profits),
                        max_daily_profit=max(profits),
                        max_daily_loss=min(profits),
                        max_drawdown=float(row.get("max_drawdown") or 0.0),
                        trades=int(row.get("num_trades") or 0),
                        win_rate=float(row.get("win_rate") or 0.0),
                        validation_profit_loss=float(row.get("validation_total_profit") or 0.0),
                        validation_trades=int(row.get("validation_num_trades") or 0),
                        confidence=_confidence_value(row.get("confidence")),
                        daily_pnl_json=_json(daily_rows),
                        params_json=_json(row),
                    )
                )
                stored += 1
            db.commit()
        except Exception as exc:
            db.rollback()
            errors.append(f"{interval}: {exc}")

        fresh_run = db.get(SparkieWeeklyRun, run.id)
        if fresh_run:
            fresh_run.heartbeat_at = datetime.utcnow()
            fresh_run.message = f"Batch {fresh_run.current_batch}: {symbol} {interval} completed."
            db.commit()

    if stored <= 0 and errors:
        run.error_message = "; ".join(errors)[:1000]
    return stored > 0


def _store_symbol_checkpoint(
    db: Session,
    run: SparkieWeeklyRun,
    universe_row: dict[str, Any],
    *,
    failed: bool,
) -> None:
    symbol = str(universe_row.get("symbol") or "").upper().strip()
    if not symbol:
        return
    db.query(SparkieWeeklyResult).filter_by(
        run_id=run.id,
        symbol=symbol,
        interval="__checkpoint__",
    ).delete(synchronize_session=False)
    db.add(
        SparkieWeeklyResult(
            run_id=run.id,
            user_id=int(run.user_id),
            symbol=symbol,
            universe_rank=_int_or_none(universe_row.get("currentRankUsTop100")),
            weighted_alpha=_float_or_none(universe_row.get("weightedAlpha")),
            interval="__checkpoint__",
            algo_name="failed" if failed else "completed",
            baseline_cash=float(run.baseline_cash),
            baseline_shares=0,
            error_message=(
                (run.error_message or "No validated result was produced for this symbol.")[:1000]
                if failed
                else None
            ),
        )
    )


def _best_match(
    rows: list[SparkieWeeklyResult],
    *,
    account_equity: float,
    daily_target: float,
    confidence_level: float,
) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for row in rows:
        match = _evaluate_result(
            row,
            account_equity=account_equity,
            daily_target=daily_target,
            confidence_level=confidence_level,
        )
        if match:
            matches.append(match)
    if not matches:
        return None
    matches.sort(
        key=lambda item: (
            bool(item["qualified"]),
            float(item["target_hit_rate"]),
            float(item["average_daily_pnl"]),
            float(item["score"]),
            -float(item["estimated_max_drawdown"]),
        ),
        reverse=True,
    )
    return matches[0]


def _evaluate_result(
    row: SparkieWeeklyResult,
    *,
    account_equity: float,
    daily_target: float,
    confidence_level: float,
) -> dict[str, Any] | None:
    reference_price = float(row.reference_price or 0.0)
    baseline_shares = int(row.baseline_shares or 0)
    daily_rows = _json_list(row.daily_pnl_json)
    if reference_price <= 0 or baseline_shares <= 0 or not daily_rows:
        return None
    shares = int(float(account_equity) // reference_price)
    if shares < 1:
        return None
    scale = shares / baseline_shares
    loss_limit = float(daily_target) * DAILY_LOSS_MULTIPLIER
    scaled = [float(item.get("profit_loss") or 0.0) * scale for item in daily_rows if isinstance(item, dict)]
    if not scaled:
        return None
    controlled = [min(float(daily_target), max(-loss_limit, value)) for value in scaled]
    target_hits = sum(1 for value in scaled if value >= float(daily_target))
    loss_hits = sum(1 for value in scaled if value <= -loss_limit)
    equity_curve = []
    cumulative = 0.0
    for value in controlled:
        cumulative += value
        equity_curve.append(cumulative)
    peak = -math.inf
    max_drawdown = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, peak - value)
    hit_rate = target_hits / len(scaled)
    avg = sum(controlled) / len(controlled)
    estimated_strategy_drawdown = abs(float(row.max_drawdown or 0.0)) * scale
    effective_drawdown = max(max_drawdown, estimated_strategy_drawdown)
    gates = {
        "positive_average": avg > 0,
        "target_confidence": hit_rate >= float(confidence_level),
        "validation_positive": float(row.validation_profit_loss or 0.0) > 0 and int(row.validation_trades or 0) >= 3,
        "trade_evidence": int(row.trades or 0) >= 5 and float(row.win_rate or 0.0) >= 0.50,
        "drawdown_within_5pct": effective_drawdown <= float(account_equity) * 0.05,
    }
    return {
        "qualified": all(gates.values()),
        "gates": gates,
        "symbol": row.symbol,
        "interval": row.interval,
        "algo_name": row.algo_name,
        "account_equity": round(float(account_equity), 2),
        "daily_target": round(float(daily_target), 2),
        "daily_loss_limit": round(loss_limit, 2),
        "shares": shares,
        "estimated_notional": round(shares * reference_price, 2),
        "cash_deployment_pct": round((shares * reference_price / float(account_equity)) * 100.0, 2),
        "days_tested": len(scaled),
        "target_hit_days": target_hits,
        "target_hit_rate": round(hit_rate, 4),
        "max_loss_days": loss_hits,
        "average_daily_pnl": round(avg, 2),
        "median_daily_pnl": round(statistics.median(controlled), 2),
        "estimated_max_drawdown": round(effective_drawdown, 2),
        "score": round(float(row.score or 0.0), 4),
        "win_rate": round(float(row.win_rate or 0.0), 4),
        "trades": int(row.trades or 0),
        "params": _json_dict(row.params_json),
    }


def _capital_ladder(requested_cash: float) -> list[float]:
    configured = os.getenv("SPARKIE_WEEKLY_CAPITAL_LADDER", "5000,10000,15000,25000,50000,75000,100000,150000,250000")
    values = {_float_or_none(value) for value in configured.split(",")}
    values.add(float(requested_cash))
    return sorted(value for value in values if value and value >= 5000)


def _completed_symbols(db: Session, run_id: str) -> set[str]:
    return {
        str(symbol or "").upper()
        for (symbol,) in (
            db.query(SparkieWeeklyResult.symbol)
            .filter_by(run_id=run_id, interval="__checkpoint__")
            .distinct()
            .all()
        )
        if symbol
    }


def _finish_stopped(db: Session, run: SparkieWeeklyRun | None, started: datetime) -> dict[str, Any]:
    if not run:
        return {"ok": False, "status": "stopped"}
    run.status = "stopped"
    run.stage = "stopped"
    run.message = "Weekly Sparkie research stopped. Resume will continue from the last saved symbol."
    run.current_symbol = None
    run.finished_at = datetime.utcnow()
    run.heartbeat_at = datetime.utcnow()
    _update_timing(run, started)
    db.commit()
    return {"ok": True, "status": run.status, "run_id": run.id}


def _update_timing(run: SparkieWeeklyRun, started: datetime) -> None:
    elapsed = max(int((datetime.utcnow() - started).total_seconds()), 0)
    completed = int(run.completed_symbols or 0)
    total = max(int(run.total_symbols or 0), 1)
    run.elapsed_seconds = elapsed
    run.progress_pct = round(min(completed / total, 1.0) * 100.0, 1)
    if completed > 0 and completed < total:
        run.eta_seconds = int((elapsed / completed) * (total - completed))
    elif completed >= total:
        run.eta_seconds = 0


def _write_universe_snapshot(run: SparkieWeeklyRun, rows: list[dict[str, Any]]) -> None:
    base = Path(os.getenv("DATA_DIR", "data")) / str(run.user_id) / "sparkie" / "weekly" / run.id
    base.mkdir(parents=True, exist_ok=True)
    (base / "universe.json").write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    fields = (
        "symbol", "symbolName", "weightedAlpha", "currentRankUsTop100", "previousRank",
        "lastPrice", "priceChange", "percentChange", "highPrice1y", "lowPrice1y",
        "percentChange1y", "tradeTime",
    )
    with (base / "universe.csv").open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _confidence_value(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return {"high": 0.8, "medium": 0.6, "low": 0.3}.get(str(value or "").lower())


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _json_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _float_or_none(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None
