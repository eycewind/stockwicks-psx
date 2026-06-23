from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

from app.services.backtest_cheatsheet_service import CheatSheetRequest

logger = logging.getLogger(__name__)


@shared_task(
    name="app.tasks.optimizer_tasks.run_qqq_optimizer_batch",
    queue="replay",
    soft_time_limit=60 * 60 * 10,
    time_limit=60 * 60 * 12,
)
def run_qqq_optimizer_batch(
    job_id: str,
    req_payload: dict[str, Any],
    symbols: list[str],
    limit: int | None = None,
    max_new_symbols: int | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    """
    Run the custom-symbol optimizer in a real Celery worker instead of a web thread.

    The route module owns the cache/progress helpers, so this task imports the
    runner lazily to avoid making the normal web import path depend on Celery.
    """
    from app.modules.replay.routes import _run_qqq_batch_job

    req = CheatSheetRequest(
        symbol=str(req_payload.get("symbol") or "CUSTOM_LIST"),
        intervals=tuple(req_payload.get("intervals") or ("5min",)),
        user_id=int(req_payload["user_id"]) if req_payload.get("user_id") is not None else None,
        trade_size=float(req_payload.get("trade_size") or 100.0),
        builder_days=int(req_payload.get("builder_days") or 30),
        k_forward=int(req_payload.get("k_forward") or 3),
        profile=str(req_payload.get("profile") or "quick"),
        allow_short=bool(req_payload.get("allow_short", True)),
        eod_close=bool(req_payload.get("eod_close", True)),
        oos_fraction=float(req_payload.get("oos_fraction") or 0.20),
    )

    logger.info(
        "[OPTIMIZER] starting custom symbol batch job_id=%s symbols=%s intervals=%s max_new_symbols=%s resume=%s",
        job_id,
        symbols,
        req.intervals,
        max_new_symbols,
        resume,
    )
    _run_qqq_batch_job(
        job_id,
        req,
        symbols=symbols,
        limit=limit,
        max_new_symbols=max_new_symbols,
        resume=resume,
    )
    return {
        "ok": True,
        "job_id": job_id,
        "queued_in": "celery",
    }
