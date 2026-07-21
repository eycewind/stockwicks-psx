from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one Sparkie evaluation outside the shared Celery worker.")
    parser.add_argument("job_id", help="Sparkie job id to run")
    args = parser.parse_args()

    _load_env()
    log_dir = Path(os.getenv("SPARKIE_LOG_DIR", str(ROOT / "logs")))
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=os.getenv("SPARKIE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "sparkie_runner.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    log = logging.getLogger("sparkie_runner")

    from app.database.connection import SessionLocal
    from app.models.sparkie import SparkieEvent, SparkieJob
    from app.services.sparkie_engine import run_sparkie_job, stop_sparkie_job

    db = SessionLocal()
    try:
        job = db.get(SparkieJob, str(args.job_id))
        if not job:
            log.error("[SPARKIE] runner job not found job_id=%s", args.job_id)
            return 2
        log.info("[SPARKIE] runner start job_id=%s pid=%s", args.job_id, os.getpid())
        result = run_sparkie_job(db, str(args.job_id))
        log.info("[SPARKIE] runner done job_id=%s result=%s", args.job_id, result)
        return 0 if result.get("ok") else 1
    except Exception as exc:
        db.rollback()
        log.exception("[SPARKIE] runner failed job_id=%s", args.job_id)
        job = db.get(SparkieJob, str(args.job_id))
        if job:
            job.status = "error"
            job.stage = "error"
            job.message = "Sparkie runner failed."
            job.error_message = str(exc)[:1000]
            db.add(
                SparkieEvent(
                    job_id=job.id,
                    user_id=int(job.user_id),
                    level="error",
                    stage="error",
                    message=job.error_message,
                )
            )
            db.commit()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
