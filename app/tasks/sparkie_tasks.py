from __future__ import annotations

import logging

from celery import shared_task

from app.database.connection import SessionLocal
from app.models.sparkie import SparkieJob
from app.services.sparkie_engine import run_sparkie_job

logger = logging.getLogger(__name__)


@shared_task(
    name="app.tasks.sparkie_tasks.run_sparkie_evaluation_task",
    queue="replay",
    soft_time_limit=60 * 45,
    time_limit=60 * 50,
)
def run_sparkie_evaluation_task(job_id: str) -> dict:
    db = SessionLocal()
    try:
        return run_sparkie_job(db, str(job_id))
    except Exception as exc:
        db.rollback()
        logger.exception("[SPARKIE] evaluation failed job_id=%s", job_id)
        job = db.get(SparkieJob, str(job_id))
        if job:
            job.status = "error"
            job.stage = "error"
            job.message = "Sparkie evaluation failed."
            job.error_message = str(exc)[:1000]
            db.commit()
        return {"ok": False, "job_id": str(job_id), "error": str(exc)}
    finally:
        db.close()
