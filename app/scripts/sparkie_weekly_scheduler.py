from __future__ import annotations

import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:
        pass

    from app.database.connection import SessionLocal
    from app.models.sparkie import SparkieWeeklySchedule
    from app.services.sparkie_weekly_service import (
        WEEKLY_ACTIVE_STATUSES,
        WEEKLY_PROCESS_PREFIX,
        create_weekly_run,
        latest_weekly_run,
        run_weekly_research,
    )

    now_et = datetime.now(ZoneInfo("America/New_York"))
    trigger_date = now_et.date().isoformat()
    db = SessionLocal()
    exit_code = 0
    active_run_id: str | None = None

    def stop_handler(_signum, _frame):
        if active_run_id:
            from app.scripts.sparkie_weekly_runner import _request_stop

            _request_stop(active_run_id)

    signal.signal(signal.SIGTERM, stop_handler)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, stop_handler)
    try:
        schedules = db.query(SparkieWeeklySchedule).filter_by(enabled=True).all()
        for schedule in schedules:
            if int(schedule.weekday or 6) != now_et.weekday():
                continue
            if schedule.last_trigger_date == trigger_date:
                continue
            existing = latest_weekly_run(db, int(schedule.user_id))
            if existing and str(existing.status or "").lower() in WEEKLY_ACTIVE_STATUSES:
                continue
            run = create_weekly_run(db, user_id=int(schedule.user_id), run_mode="scheduled")
            active_run_id = run.id
            run.task_id = f"{WEEKLY_PROCESS_PREFIX}{os.getpid()}"
            schedule.last_trigger_date = trigger_date
            db.commit()
            try:
                run_weekly_research(db, run.id)
            except Exception:
                exit_code = 1
            finally:
                active_run_id = None
        return exit_code
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
