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
from app.services.barchart_symbols import barchart_weekly_rows_with_source
from app.services.sparkie_risk_scoring import (
    SPARKIE_ANALYSIS_MODEL_VERSION,
    risk_adjusted_selection_metrics,
)


WEEKLY_ACTIVE_STATUSES = {"queued", "snapshotting", "running", "stopping"}
WEEKLY_TERMINAL_STATUSES = {"completed", "stopped", "error"}
WEEKLY_PROCESS_PREFIX = "sparkie-weekly-process:"
DEFAULT_BASELINE_CASH = 10_000.0
DEFAULT_BATCH_SIZE = 5
DAILY_LOSS_MULTIPLIER = 5.0
WEEKLY_UNIVERSE_POLICY = "barchart_price_volume_leaders_200"
SYMBOL_OUTCOME_ANALYZED = "analyzed"
SYMBOL_OUTCOME_NO_RESULT = "no_result"
SYMBOL_OUTCOME_DATA_FAILURE = "data_failure"
SYMBOL_OUTCOME_BACKTEST_FAILURE = "backtest_failure"
SYMBOL_FAILURE_OUTCOMES = {SYMBOL_OUTCOME_DATA_FAILURE, SYMBOL_OUTCOME_BACKTEST_FAILURE}
MIN_RECOMMENDATION_DAYS = max(20, int(os.getenv("SPARKIE_WEEKLY_MIN_RECOMMENDATION_DAYS", "20")))


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
        "universe_policy": WEEKLY_UNIVERSE_POLICY,
        "cash_policy": "full_cash_v1",
        "daily_loss_multiplier": DAILY_LOSS_MULTIPLIER,
        "analysis_model_version": SPARKIE_ANALYSIS_MODEL_VERSION,
        "execution_slippage_bps": float(os.getenv("SPARKIE_BACKTEST_SLIPPAGE_BPS", "10")),
        "max_position_pct_daily_dollar_volume": float(
            os.getenv("SPARKIE_MAX_POSITION_PCT_DAILY_DOLLAR_VOLUME", "0.01")
        ),
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
    run.message = "Saving Barchart's current 200 Price Volume Leaders."
    run.heartbeat_at = datetime.utcnow()
    db.commit()

    try:
        universe = _json_list(run.universe_json)
        if not universe:
            universe, ranking_source = barchart_weekly_rows_with_source(force_refresh=True)
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

            outcome = _process_weekly_symbol(
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
            if outcome in SYMBOL_FAILURE_OUTCOMES:
                run.failed_symbols = int(run.failed_symbols or 0) + 1
            # Every terminal symbol outcome is checkpointed so Resume advances.
            run.completed_symbols = int(run.completed_symbols or 0) + 1
            _store_symbol_checkpoint(db, run, universe_row, outcome=outcome)
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
        symbol_outcomes = _checkpoint_counts(db, run.id)
        run.failed_symbols = sum(symbol_outcomes.get(name, 0) for name in SYMBOL_FAILURE_OUTCOMES)
        run.message = (
            f"Weekly Sparkie analyzed {int(run.completed_symbols or 0)} symbols and saved "
            f"{result_count} strategy results."
        )
        run.progress_pct = 100.0
        run.eta_seconds = 0
        run.current_symbol = None
        run.heartbeat_at = datetime.utcnow()
        run.finished_at = datetime.utcnow()
        try:
            export_files = _write_weekly_result_exports(db, run, universe)
        except Exception as exc:
            # Results remain safely stored in PostgreSQL even if an optional
            # operator-facing export cannot be written.
            export_files = {"error": f"Could not write weekly exports: {str(exc)[:500]}"}
        run.summary_json = _json(
            {
                "result_count": result_count,
                "total_symbols": int(run.total_symbols or 0),
                "completed_symbols": int(run.completed_symbols or 0),
                "failed_symbols": int(run.failed_symbols or 0),
                "symbol_outcomes": symbol_outcomes,
                "daily_loss_multiplier": DAILY_LOSS_MULTIPLIER,
                "cash_policy": "full_cash_v1",
                "universe_policy": WEEKLY_UNIVERSE_POLICY,
                "exports": export_files,
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
    risk_per_day_pct: float,
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

    run_summary = _json_dict(run.summary_json)
    if run_summary.get("universe_policy") != WEEKLY_UNIVERSE_POLICY:
        raise ValueError(
            "The latest completed Weekly Sparkie catalog uses the retired Top/Bottom universe. "
            "Restart Weekly Sparkie to build the required 200 Price Volume Leaders catalog."
        )

    rows = db.query(SparkieWeeklyResult).filter_by(run_id=run.id).all()
    usable_rows = [row for row in rows if not row.error_message and row.daily_pnl_json]
    if not usable_rows:
        raise ValueError("The latest weekly catalog has no usable strategy results.")

    daily_risk_budget = round(float(account_equity) * float(risk_per_day_pct), 2)
    evaluated_requested = [
        match
        for row in usable_rows
        if (match := _evaluate_result(
            row,
            account_equity=float(account_equity),
            daily_risk_budget=daily_risk_budget,
            confidence_level=float(confidence_level),
        ))
    ]
    ranked_requested = _rank_matches(evaluated_requested)
    for rank, match in enumerate(ranked_requested, start=1):
        match["catalog_rank"] = rank
    requested = ranked_requested[0] if ranked_requested else None
    alternative_matches = _unique_symbol_alternatives(
        ranked_requested,
        exclude_symbol=str((requested or {}).get("symbol") or ""),
        limit=9,
    )
    risk_sensitivity = _risk_sensitivity(
        usable_rows,
        account_equity=float(account_equity),
        confidence_level=float(confidence_level),
    )
    sensitivity_winners = {
        str(item.get("symbol") or "")
        for item in risk_sensitivity
        if item.get("symbol")
    }
    same_winner_across_risk_levels = len(sensitivity_winners) == 1 and bool(sensitivity_winners)
    evidence_ready_count = sum(
        1 for match in evaluated_requested
        if int(match.get("days_tested") or 0) >= MIN_RECOMMENDATION_DAYS
    )
    modern_evidence_count = sum(
        1
        for match in evaluated_requested
        if bool((match.get("gates") or {}).get("execution_cost_model_present"))
    )
    gate_failures: dict[str, int] = {}
    for match in evaluated_requested:
        for gate, passed in (match.get("gates") or {}).items():
            if not passed:
                gate_failures[gate] = gate_failures.get(gate, 0) + 1
    return {
        "ok": True,
        "run_id": run.id,
        "catalog_completed_at": run.finished_at.isoformat() if run.finished_at else None,
        "catalog_symbols": int(run.completed_symbols or 0),
        "catalog_results": len(usable_rows),
        "evidence_ready_results": evidence_ready_count,
        "qualified_results_at_requested_cash": sum(1 for row in evaluated_requested if row["qualified"]),
        "catalog_analysis": {
            "evaluated_results": len(evaluated_requested),
            "qualified_results": sum(1 for row in evaluated_requested if row["qualified"]),
            "gate_failures": gate_failures,
            "minimum_daily_evidence_days": MIN_RECOMMENDATION_DAYS,
            "execution_model_ready_results": modern_evidence_count,
            "current_analysis_model_results": modern_evidence_count,
            "required_analysis_model_version": SPARKIE_ANALYSIS_MODEL_VERSION,
        },
        "recommendation_method": (
            "Applies the client's maximum daily loss and requested confidence as hard gates, then ranks qualified "
            "candidates by the conservative 95% lower profitable-day rate. Observed success, risk-adjusted evidence, "
            "holdout validation, and conservative monthly P/L are tiebreakers."
        ),
        "ranking_priority": (
            "1) daily-loss gate; 2) requested-confidence gate; 3) highest conservative profitable-day success; "
            "4) observed profitable-day rate; 5) risk-adjusted score; 6) conservative monthly P/L."
        ),
        "account_equity": round(float(account_equity), 2),
        "risk_per_day_pct": float(risk_per_day_pct),
        "daily_risk_budget": daily_risk_budget,
        "confidence_level": float(confidence_level),
        "all_cash_at_risk": True,
        "requested_cash_match": requested,
        "top_combinations": ranked_requested[:10],
        "alternative_matches": alternative_matches,
        "risk_sensitivity": risk_sensitivity,
        "same_winner_across_risk_levels": same_winner_across_risk_levels,
        "cash_scaling_note": (
            "Because every candidate deploys approximately 100% of cash, changing cash scales profit and loss "
            "approximately proportionally and often should not change the ranking. Whole-share rounding can cause "
            "small differences."
        ),
        "catalog_scope_note": (
            "This saved-result search compares the completed Barchart 200 Price Volume Leaders catalog. "
            "The stock-source and ticker fields above apply to a fresh Sparkie Evaluation, not to this catalog search."
        ),
        "weekly_risk_model_note": (
            "This catalog was backtested with a fixed 10% daily-loss lock. Risk requests above 10% can change "
            "eligibility and score weights, but they do not reconstruct a different intraday trade path from the "
            "saved summary. Fresh finalist verification reruns the selected symbol using the client's requested risk."
        ),
        "selection_diagnostic": (
            f"{next(iter(sensitivity_winners))} remains the top risk-adjusted candidate at every tested risk level."
            if same_winner_across_risk_levels
            else "The top candidate changes as the daily risk budget changes."
        ),
        "minimum_cash_match": None,
        "screening_only": True,
        "verification_required": True,
        "message": (
            "A risk-budget-compatible historical result was found; run a fresh finalist verification before bot setup."
            if requested and requested["qualified"]
            else (
                "The latest weekly catalog predates Sparkie's complete-evidence analysis model. "
                "Run Weekly Sparkie again so every tested strategy, execution cost, and liquidity check is retained."
                if modern_evidence_count == 0
                else
                "The saved catalog does not yet contain enough independent daily observations to make a recommendation. "
                "This is an evidence-data gap, not a finding that no profitable bot exists."
                if evidence_ready_count == 0
                else "No saved result met every daily-risk, validation, and evidence gate at the requested cash level."
            )
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
    result_symbol_count = (
        db.query(SparkieWeeklyResult.symbol)
        .filter_by(run_id=run.id)
        .filter(SparkieWeeklyResult.daily_pnl_json.isnot(None))
        .distinct()
        .count()
    )
    symbol_outcomes = _checkpoint_counts(db, run.id)
    now = datetime.utcnow()
    heartbeat_age = int((now - run.heartbeat_at).total_seconds()) if run.heartbeat_at else None
    summary = _json_dict(run.summary_json)
    universe_rows = _json_list(run.universe_json)
    universe_policy = summary.get("universe_policy") if isinstance(summary, dict) else None
    if not universe_policy:
        universe_policy = (
            WEEKLY_UNIVERSE_POLICY
            if not universe_rows or any(row.get("sparkieUniverseType") == "price_volume" for row in universe_rows)
            else "legacy_top_bottom_200"
        )
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
        "result_symbol_count": result_symbol_count,
        "symbol_outcomes": symbol_outcomes,
        "no_result_symbols": int(symbol_outcomes.get(SYMBOL_OUTCOME_NO_RESULT, 0)),
        "technical_failure_symbols": sum(
            int(symbol_outcomes.get(name, 0)) for name in SYMBOL_FAILURE_OUTCOMES
        ),
        "task_id": run.task_id,
        "heartbeat_at": run.heartbeat_at.isoformat() if run.heartbeat_at else None,
        "heartbeat_age_seconds": heartbeat_age,
        "error_message": run.error_message,
        "exports": summary.get("exports", {}) if isinstance(summary, dict) else {},
        "universe_policy": universe_policy,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "can_resume": str(run.status or "").lower() in {"stopped", "error"} and int(run.completed_symbols or 0) < int(run.total_symbols or 200),
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
) -> str:
    from app.services.sparkie_engine import _backtest_symbol_interval, refresh_symbol_data

    symbol = str(universe_row.get("symbol") or "").upper().strip()
    run.error_message = None
    lookback_days = max(120, int(os.getenv("SPARKIE_WEEKLY_LOOKBACK_DAYS", "120")))
    try:
        meta = refresh_symbol_data(
            user_id=int(run.user_id),
            symbol=symbol,
            intervals=intervals,
            lookback_days=lookback_days,
        )
    except Exception as exc:
        # A newly listed or illiquid symbol can lack enough intraday bars for
        # one interval. It is a failed catalog member, not a failed 200-stock
        # weekly run. The caller checkpoints it and moves to the next symbol.
        run.error_message = f"{symbol}: data preparation skipped: {str(exc)[:850]}"
        run.heartbeat_at = datetime.utcnow()
        run.message = f"Batch {run.current_batch}: {symbol} skipped because data was insufficient."
        db.commit()
        return SYMBOL_OUTCOME_DATA_FAILURE
    frames = meta.get("frames") or {}
    stored = 0
    completed_intervals = 0
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
                float(run.baseline_cash) * 0.10,
                builder_days=lookback_days,
            )
            completed_intervals += 1
            errors.extend(
                f"{interval}: {message}"
                for message in (result.get("errors") or [])
                if message
            )
            candidates = [
                row for row in (result.get("best_by_algo") or result.get("top") or [])
                if str(row.get("algo_name") or "") in algos
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
                        universe_rank=_universe_rank(universe_row),
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
                        params_json=_json({
                            **row,
                            "sparkie_universe_sentiment": universe_row.get("sparkieSentiment"),
                            "sparkie_universe_type": universe_row.get("sparkieUniverseType"),
                            "sparkie_universe_rank": _universe_rank(universe_row),
                            "sparkie_price_volume": _float_or_none(universe_row.get("priceVolume")),
                        }),
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
    if stored > 0:
        return SYMBOL_OUTCOME_ANALYZED
    if completed_intervals > 0:
        return SYMBOL_OUTCOME_NO_RESULT
    return SYMBOL_OUTCOME_BACKTEST_FAILURE


def _store_symbol_checkpoint(
    db: Session,
    run: SparkieWeeklyRun,
    universe_row: dict[str, Any],
    *,
    outcome: str,
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
            universe_rank=_universe_rank(universe_row),
            weighted_alpha=_float_or_none(universe_row.get("weightedAlpha")),
            interval="__checkpoint__",
            algo_name=str(outcome),
            baseline_cash=float(run.baseline_cash),
            baseline_shares=0,
            error_message=(
                (run.error_message or "Sparkie could not complete this symbol.")[:1000]
                if outcome in SYMBOL_FAILURE_OUTCOMES
                else None
            ),
            params_json=_json(
                {
                    "sparkie_symbol_outcome": outcome,
                    "sparkie_symbol_detail": (
                        (
                            run.error_message
                            or "Backtests completed, but no strategy result row was produced."
                        )
                        if outcome == SYMBOL_OUTCOME_NO_RESULT
                        else run.error_message
                    ),
                    "sparkie_universe_type": universe_row.get("sparkieUniverseType"),
                    "sparkie_universe_rank": _universe_rank(universe_row),
                    "sparkie_price_volume": _float_or_none(universe_row.get("priceVolume")),
                }
            ),
        )
    )


def _best_match(
    rows: list[SparkieWeeklyResult],
    *,
    account_equity: float,
    daily_risk_budget: float,
    confidence_level: float,
) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for row in rows:
        match = _evaluate_result(
            row,
            account_equity=account_equity,
            daily_risk_budget=daily_risk_budget,
            confidence_level=confidence_level,
        )
        if match:
            matches.append(match)
    if not matches:
        return None
    ranked = _rank_matches(matches)
    return ranked[0] if ranked else None


def _rank_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        matches,
        key=lambda item: (
            bool(item["qualified"]),
            float(item.get("positive_day_rate_95pct_low") or 0.0),
            float(item.get("positive_day_rate") or 0.0),
            float(item.get("selection_score") or 0.0),
            float(item.get("return_to_risk") or 0.0),
            float(item.get("validation_return_pct") or 0.0),
            float(item.get("conservative_monthly_pnl") or 0.0),
            -float(item.get("estimated_max_drawdown") or 0.0),
        ),
        reverse=True,
    )


def _unique_symbol_alternatives(
    matches: list[dict[str, Any]],
    *,
    exclude_symbol: str,
    limit: int,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen = {str(exclude_symbol or "").upper()}
    for match in matches:
        symbol = str(match.get("symbol") or "").upper()
        if not symbol or symbol in seen:
            continue
        selected.append(match)
        seen.add(symbol)
        if len(selected) >= int(limit):
            break
    return selected


def _risk_sensitivity(
    rows: list[SparkieWeeklyResult],
    *,
    account_equity: float,
    confidence_level: float,
) -> list[dict[str, Any]]:
    sensitivity: list[dict[str, Any]] = []
    for risk_pct in (0.10, 0.25, 0.50, 1.00):
        risk_budget = float(account_equity) * risk_pct
        match = _best_match(
            rows,
            account_equity=float(account_equity),
            daily_risk_budget=risk_budget,
            confidence_level=float(confidence_level),
        )
        sensitivity.append({
            "risk_per_day_pct": risk_pct,
            "daily_risk_budget": round(risk_budget, 2),
            "symbol": (match or {}).get("symbol"),
            "interval": (match or {}).get("interval"),
            "algo_name": (match or {}).get("algo_name"),
            "qualified": bool((match or {}).get("qualified")),
            "selection_score": (match or {}).get("selection_score"),
            "conservative_monthly_pnl": (match or {}).get("conservative_monthly_pnl"),
            "worst_daily_pnl": (match or {}).get("worst_daily_pnl"),
        })
    return sensitivity


def _evaluate_result(
    row: SparkieWeeklyResult,
    *,
    account_equity: float,
    daily_risk_budget: float,
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
    scaled = [float(item.get("profit_loss") or 0.0) * scale for item in daily_rows if isinstance(item, dict)]
    if not scaled:
        return None
    loss_hits = sum(1 for value in scaled if value <= -float(daily_risk_budget))
    equity_curve = []
    cumulative = 0.0
    for value in scaled:
        cumulative += value
        equity_curve.append(cumulative)
    peak = -math.inf
    max_drawdown = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, peak - value)
    avg = sum(scaled) / len(scaled)
    estimated_strategy_drawdown = abs(float(row.max_drawdown or 0.0)) * scale
    effective_drawdown = max(max_drawdown, estimated_strategy_drawdown)
    monthly = _monthly_profile(scaled, float(account_equity))
    params = _json_dict(row.params_json)
    analysis_model_version = str(params.get("sparkie_analysis_model_version") or "")
    execution_slippage_bps = float(params.get("sparkie_execution_slippage_bps") or 0.0)
    median_daily_dollar_volume = float(params.get("sparkie_median_daily_dollar_volume") or 0.0)
    max_position_pct_adv = float(
        params.get("sparkie_max_position_pct_daily_dollar_volume") or 0.01
    )
    liquidity_capacity_usd = median_daily_dollar_volume * max_position_pct_adv
    estimated_notional = shares * reference_price
    scaled_validation_pnl = float(row.validation_profit_loss or 0.0) * scale
    selection_metrics = risk_adjusted_selection_metrics(
        daily_profits=scaled,
        account_equity=float(account_equity),
        daily_risk_budget=float(daily_risk_budget),
        conservative_monthly_pnl=float(monthly.get("conservative_monthly_pnl") or 0.0),
        typical_monthly_pnl=float(monthly.get("typical_monthly_pnl") or 0.0),
        max_drawdown=effective_drawdown,
        validation_profit_loss=scaled_validation_pnl,
        confidence_preference=float(confidence_level),
    )
    gates = {
        "minimum_daily_evidence": len(scaled) >= MIN_RECOMMENDATION_DAYS,
        "positive_average": avg > 0,
        "validation_positive": float(row.validation_profit_loss or 0.0) > 0 and int(row.validation_trades or 0) >= 3,
        # A strategy can be profitable with a sub-50% trade win rate when
        # average winners exceed average losers. Daily loss, holdout
        # validation, trade count, and the monthly-return profile are the
        # relevant safety gates for Sparkie's risk-first selection.
        "trade_evidence": int(row.trades or 0) >= 5,
        "daily_loss_limit_respected": loss_hits == 0,
        "execution_cost_model_present": analysis_model_version == SPARKIE_ANALYSIS_MODEL_VERSION,
        "liquidity_capacity": bool(
            median_daily_dollar_volume > 0.0
            and estimated_notional <= liquidity_capacity_usd
        ),
        "profitable_day_confidence": (
            float(selection_metrics.get("positive_day_rate_95pct_low") or 0.0)
            >= float(confidence_level)
        ),
        "conservative_monthly_return_positive": bool(monthly.get("monthly_estimate_available")) and float(monthly["conservative_monthly_pnl"] or 0.0) > 0,
    }
    return {
        "qualified": all(gates.values()),
        "gates": gates,
        "symbol": row.symbol,
        "interval": row.interval,
        "algo_name": row.algo_name,
        "account_equity": round(float(account_equity), 2),
        "daily_risk_budget": round(float(daily_risk_budget), 2),
        "shares": shares,
        "estimated_notional": round(estimated_notional, 2),
        "cash_deployment_pct": round((estimated_notional / float(account_equity)) * 100.0, 2),
        "days_tested": len(scaled),
        "max_loss_days": loss_hits,
        "average_daily_pnl": round(avg, 2),
        "median_daily_pnl": round(statistics.median(scaled), 2),
        "cumulative_daily_pnl": round(sum(scaled), 2),
        "worst_daily_pnl": round(min(scaled), 2),
        **monthly,
        "estimated_max_drawdown": round(effective_drawdown, 2),
        "score": round(float(row.score or 0.0), 4),
        "win_rate": round(float(row.win_rate or 0.0), 4),
        "trades": int(row.trades or 0),
        "validation_profit_loss": round(scaled_validation_pnl, 2),
        "analysis_model_version": analysis_model_version,
        "execution_slippage_bps": round(execution_slippage_bps, 2),
        "median_daily_dollar_volume": round(median_daily_dollar_volume, 2),
        "liquidity_capacity_usd": round(liquidity_capacity_usd, 2),
        "position_pct_daily_dollar_volume": round(
            estimated_notional / median_daily_dollar_volume * 100.0,
            4,
        ) if median_daily_dollar_volume > 0 else None,
        "params": params,
        "universe_sentiment": params.get("sparkie_universe_sentiment"),
        "universe_type": params.get("sparkie_universe_type"),
        "universe_label": (
            "Barchart Price Volume Leaders"
            if params.get("sparkie_universe_type") == "price_volume"
            else params.get("sparkie_universe_sentiment")
        ),
        "price_volume": params.get("sparkie_price_volume"),
        **selection_metrics,
    }


def _monthly_profile(profits: list[float], account_equity: float) -> dict[str, Any]:
    if len(profits) < MIN_RECOMMENDATION_DAYS:
        return {
            "monthly_estimate_available": False,
            "conservative_monthly_pnl": None,
            "typical_monthly_pnl": None,
            "strong_monthly_pnl": None,
            "conservative_monthly_return_pct": None,
            "typical_monthly_return_pct": None,
            "strong_monthly_return_pct": None,
            "positive_day_rate": round(sum(value > 0 for value in profits) / len(profits), 4) if profits else 0.0,
        }
    window = min(21, len(profits))
    monthly = sorted(sum(profits[index:index + window]) for index in range(max(len(profits) - window + 1, 1)))
    def percentile(fraction: float) -> float:
        if len(monthly) == 1:
            return monthly[0]
        position = (len(monthly) - 1) * fraction
        low, high = int(math.floor(position)), int(math.ceil(position))
        return monthly[low] if low == high else monthly[low] + (monthly[high] - monthly[low]) * (position - low)
    denominator = max(float(account_equity), 1.0)
    conservative, typical, strong = percentile(0.25), percentile(0.50), percentile(0.75)
    return {
        "monthly_estimate_available": True,
        "conservative_monthly_pnl": round(conservative, 2),
        "typical_monthly_pnl": round(typical, 2),
        "strong_monthly_pnl": round(strong, 2),
        "conservative_monthly_return_pct": round(conservative / denominator * 100.0, 2),
        "typical_monthly_return_pct": round(typical / denominator * 100.0, 2),
        "strong_monthly_return_pct": round(strong / denominator * 100.0, 2),
        "positive_day_rate": round(sum(value > 0 for value in profits) / len(profits), 4),
    }


def _capital_ladder(requested_cash: float) -> list[float]:
    configured = os.getenv("SPARKIE_WEEKLY_CAPITAL_LADDER", "5000,10000,15000,25000,50000,75000,100000,150000,250000")
    values = {_float_or_none(value) for value in configured.split(",")}
    values.add(float(requested_cash))
    return sorted(value for value in values if value and value >= 2000)


def _wilson_interval(successes: int, trials: int, z_score: float = 1.96) -> tuple[float, float]:
    if trials <= 0:
        return 0.0, 0.0
    observed = max(0.0, min(1.0, float(successes) / float(trials)))
    z_squared = z_score * z_score
    denominator = 1.0 + z_squared / trials
    center = (observed + z_squared / (2.0 * trials)) / denominator
    spread = z_score * math.sqrt((observed * (1.0 - observed) + z_squared / (4.0 * trials)) / trials) / denominator
    return round(max(0.0, center - spread), 4), round(min(1.0, center + spread), 4)


def _universe_rank(row: dict[str, Any]) -> int | None:
    explicit_rank = _int_or_none(row.get("sparkieUniverseRank"))
    if explicit_rank is not None:
        return explicit_rank
    if str(row.get("sparkieSentiment") or "").lower() == "bearish":
        return _int_or_none(row.get("currentRankUsBottom100"))
    return _int_or_none(row.get("currentRankUsTop100"))


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


def _checkpoint_counts(db: Session, run_id: str) -> dict[str, int]:
    counts = {
        SYMBOL_OUTCOME_ANALYZED: 0,
        SYMBOL_OUTCOME_NO_RESULT: 0,
        SYMBOL_OUTCOME_DATA_FAILURE: 0,
        SYMBOL_OUTCOME_BACKTEST_FAILURE: 0,
    }
    rows = (
        db.query(SparkieWeeklyResult.algo_name, SparkieWeeklyResult.error_message)
        .filter_by(run_id=run_id, interval="__checkpoint__")
        .all()
    )
    for raw_outcome, error_message in rows:
        outcome = _normalize_symbol_outcome(raw_outcome, error_message)
        if outcome in counts:
            counts[outcome] += 1
    return counts


def _normalize_symbol_outcome(raw_outcome: Any, error_message: Any = None) -> str:
    outcome = str(raw_outcome or "").lower()
    detail = str(error_message or "").lower()
    if outcome == "completed":
        return SYMBOL_OUTCOME_ANALYZED
    if outcome != "failed":
        return outcome
    if "no validated result was produced" in detail:
        return SYMBOL_OUTCOME_NO_RESULT
    if any(
        marker in detail
        for marker in (
            "data preparation",
            "downloaded data",
            "no cached bars",
            "no schwab candles",
            "stale",
        )
    ):
        return SYMBOL_OUTCOME_DATA_FAILURE
    return SYMBOL_OUTCOME_BACKTEST_FAILURE


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
        "symbol", "symbolName", "sparkieUniverseRank", "sparkieUniverseType",
        "priceVolume", "volume", "previousVolume", "lastPrice", "priceChange",
        "percentChange", "tradeTime", "symbolType", "hasOptions",
    )
    with (base / "universe.csv").open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _weekly_export_base(run: SparkieWeeklyRun) -> Path:
    return Path(os.getenv("DATA_DIR", "data")) / str(run.user_id) / "sparkie" / "weekly" / run.id


def _write_weekly_result_exports(
    db: Session,
    run: SparkieWeeklyRun,
    universe: list[dict[str, Any]],
) -> dict[str, str]:
    """Write portable final exports without replacing the database catalog."""
    base = _weekly_export_base(run)
    base.mkdir(parents=True, exist_ok=True)
    rows = (
        db.query(SparkieWeeklyResult)
        .filter_by(run_id=run.id)
        .filter(SparkieWeeklyResult.daily_pnl_json.isnot(None))
        .order_by(SparkieWeeklyResult.score.desc().nullslast(), SparkieWeeklyResult.id.asc())
        .all()
    )
    results = [_weekly_result_export_row(row) for row in rows]
    checkpoints = (
        db.query(SparkieWeeklyResult)
        .filter_by(run_id=run.id, interval="__checkpoint__")
        .order_by(SparkieWeeklyResult.universe_rank.asc().nullslast(), SparkieWeeklyResult.id.asc())
        .all()
    )
    symbol_outcomes = [_weekly_symbol_outcome_export_row(row) for row in checkpoints]
    outcome_counts = _checkpoint_counts(db, run.id)
    universe_type_counts: dict[str, int] = {}
    for universe_row in universe:
        universe_type = str(universe_row.get("sparkieUniverseType") or "unknown")
        universe_type_counts[universe_type] = universe_type_counts.get(universe_type, 0) + 1

    results_json = base / "results.json"
    results_csv = base / "results.csv"
    outcomes_json = base / "symbol_outcomes.json"
    outcomes_csv = base / "symbol_outcomes.csv"
    summary_json = base / "summary.json"
    results_json.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    outcomes_json.write_text(json.dumps(symbol_outcomes, indent=2, default=str), encoding="utf-8")

    csv_fields = (
        "symbol", "universe_rank", "universe_type", "price_volume",
        "universe_sentiment", "weighted_alpha", "interval", "algo_name",
        "reference_price", "baseline_cash", "baseline_shares", "score", "average_daily_pnl",
        "median_daily_pnl", "profitable_day_rate", "max_daily_profit", "max_daily_loss",
        "max_drawdown", "trades", "win_rate", "validation_profit_loss", "validation_trades",
        "confidence", "daily_pnl_json", "params_json",
    )
    with results_csv.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    with outcomes_csv.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=("symbol", "universe_rank", "outcome", "detail"),
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(symbol_outcomes)

    summary = {
        "run_id": run.id,
        "generated_at": datetime.utcnow().isoformat(),
        "baseline_cash": float(run.baseline_cash or 0.0),
        "universe_symbols": len(universe),
        "universe_policy": WEEKLY_UNIVERSE_POLICY,
        "universe_by_type": universe_type_counts,
        "completed_symbols": int(run.completed_symbols or 0),
        "failed_symbols": int(run.failed_symbols or 0),
        "symbol_outcomes": outcome_counts,
        "strategy_results": len(results),
        "cash_policy": "full_cash_v1",
        "day_trading_policy": "end-of-day close; no overnight positions",
        "top_results_by_backtest_score": results[:20],
        "result_files": {"json": str(results_json), "csv": str(results_csv)},
        "symbol_outcome_files": {"json": str(outcomes_json), "csv": str(outcomes_csv)},
    }
    summary_json.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return {
        "summary_json": str(summary_json),
        "results_json": str(results_json),
        "results_csv": str(results_csv),
        "outcomes_json": str(outcomes_json),
        "outcomes_csv": str(outcomes_csv),
    }


def _weekly_result_export_row(row: SparkieWeeklyResult) -> dict[str, Any]:
    params = _json_dict(row.params_json)
    return {
        "symbol": row.symbol,
        "universe_rank": row.universe_rank,
        "universe_sentiment": params.get("sparkie_universe_sentiment", "unknown"),
        "universe_type": params.get("sparkie_universe_type", "unknown"),
        "price_volume": params.get("sparkie_price_volume"),
        "weighted_alpha": row.weighted_alpha,
        "interval": row.interval,
        "algo_name": row.algo_name,
        "reference_price": row.reference_price,
        "baseline_cash": row.baseline_cash,
        "baseline_shares": row.baseline_shares,
        "score": row.score,
        "average_daily_pnl": row.average_daily_pnl,
        "median_daily_pnl": row.median_daily_pnl,
        "profitable_day_rate": row.profitable_day_rate,
        "max_daily_profit": row.max_daily_profit,
        "max_daily_loss": row.max_daily_loss,
        "max_drawdown": row.max_drawdown,
        "trades": row.trades,
        "win_rate": row.win_rate,
        "validation_profit_loss": row.validation_profit_loss,
        "validation_trades": row.validation_trades,
        "confidence": row.confidence,
        "daily_pnl_json": row.daily_pnl_json,
        "params_json": row.params_json,
    }


def _weekly_symbol_outcome_export_row(row: SparkieWeeklyResult) -> dict[str, Any]:
    outcome = _normalize_symbol_outcome(row.algo_name, row.error_message)
    params = _json_dict(row.params_json)
    return {
        "symbol": row.symbol,
        "universe_rank": row.universe_rank,
        "outcome": outcome,
        "detail": row.error_message or params.get("sparkie_symbol_detail"),
    }


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
