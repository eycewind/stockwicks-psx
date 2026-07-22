from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from datetime import datetime
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


def _request_stop(run_id: str) -> None:
    from app.database.connection import SessionLocal
    from app.models.sparkie import SparkieWeeklyRun

    db = SessionLocal()
    try:
        run = db.get(SparkieWeeklyRun, run_id)
        if run:
            run.status = "stopping"
            run.stage = "stopping"
            run.message = "Weekly Sparkie stop requested; saving the current checkpoint."
            run.stop_requested_at = datetime.utcnow()
            run.heartbeat_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run resumable Weekly Sparkie research outside Celery.")
    parser.add_argument("run_id")
    args = parser.parse_args()
    _load_env()

    log_dir = Path(os.getenv("SPARKIE_LOG_DIR", str(ROOT / "logs")))
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=os.getenv("SPARKIE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "sparkie_weekly_runner.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    log = logging.getLogger("sparkie_weekly_runner")

    def stop_handler(_signum, _frame):
        log.warning("Weekly Sparkie termination requested run_id=%s", args.run_id)
        _request_stop(str(args.run_id))

    signal.signal(signal.SIGTERM, stop_handler)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, stop_handler)

    from app.database.connection import SessionLocal
    from app.services.sparkie_weekly_service import run_weekly_research

    db = SessionLocal()
    try:
        log.info("Weekly Sparkie start run_id=%s pid=%s", args.run_id, os.getpid())
        result = run_weekly_research(db, str(args.run_id))
        log.info("Weekly Sparkie finished run_id=%s result=%s", args.run_id, result)
        return 0 if result.get("ok") else 1
    except Exception:
        log.exception("Weekly Sparkie failed run_id=%s", args.run_id)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
