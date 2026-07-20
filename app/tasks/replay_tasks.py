# /var/stockwicks/clients/ashakil/app/tasks/replay_tasks.py
"""
Celery tasks for replay simulator only.

This version bridges the existing PID-based replay orchestrator into the
new replay queue, so the web route no longer spawns replay directly.

Queue:
- replay

Important:
- Web form parameters are saved into ReplaySession.config_json.
- This task logs ReplaySession.config_json before starting the old replay
  orchestrator so we can prove whether UI parameters reached Celery.
- The old PID-based replay process still controls the actual replay loop.
  If config reaches this task but not the algo, the next fix is inside
  app/services/replay_process.py or the child replay runner.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict

from celery import shared_task

from app.database.connection import SessionLocal
from app.models.replay import ReplaySession
from app.services.replay_process import pid_is_alive, start_session, stop_session

logger = logging.getLogger(__name__)


# =============================================================================
# Redis stop flags
# =============================================================================

def _redis_client():
    try:
        import redis

        redis_url = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/1")
        return redis.Redis.from_url(redis_url, decode_responses=True)
    except Exception:
        logger.debug("[REPLAY] Redis unavailable", exc_info=True)
        return None


def set_replay_stop_flag(session_id: int, ttl_seconds: int = 86400) -> None:
    r = _redis_client()
    if not r:
        return

    session_id = int(session_id)
    r.set(f"stockwicks:replay:{session_id}:stop_requested", "1", ex=ttl_seconds)
    r.delete(f"stockwicks:replay:{session_id}:running")


def clear_replay_stop_flag(session_id: int) -> None:
    r = _redis_client()
    if not r:
        return

    session_id = int(session_id)
    r.delete(f"stockwicks:replay:{session_id}:stop_requested")


def should_stop_replay(session_id: int) -> bool:
    r = _redis_client()
    if not r:
        return False

    session_id = int(session_id)
    return r.get(f"stockwicks:replay:{session_id}:stop_requested") == "1"


# =============================================================================
# Helpers
# =============================================================================

def _parse_config_json(value: Any) -> Dict[str, Any]:
    if not value:
        return {}

    if isinstance(value, dict):
        return value

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    return {}


def _log_replay_config_from_db(sess: ReplaySession) -> Dict[str, Any]:
    """
    Logs the replay config stored by the route.

    This proves:
      td_replay.html
        -> app/routes/replay.py
        -> ReplaySession.config_json
        -> app/tasks/replay_tasks.py

    If these values are correct here but the algo still uses defaults,
    the issue is inside replay_process.py or the replay child runner.
    """
    cfg = _parse_config_json(getattr(sess, "config_json", None))

    logger.warning(
        "[REPLAY CONFIG FROM DB] session_id=%s symbol=%s interval=%s algo=%s "
        "long_entry_prob=%s short_entry_prob=%s prob_trail_drop=%s "
        "stop_loss_usd=%s trailing_profit_usd=%s allow_short=%s eod_close=%s "
        "config_keys=%s",
        getattr(sess, "id", None),
        getattr(sess, "symbol", None),
        getattr(sess, "interval", None),
        getattr(sess, "algo_name", None),
        cfg.get("long_entry_prob"),
        cfg.get("short_entry_prob"),
        cfg.get("prob_trail_drop"),
        cfg.get("stop_loss_usd", cfg.get("hard_stop_usd")),
        cfg.get("trailing_profit_usd", cfg.get("trailing_stop_distance")),
        cfg.get("allow_short"),
        cfg.get("eod_close"),
        sorted(cfg.keys()),
    )

    return cfg


def _mark_session_error(db, session_id: int, message: str) -> None:
    try:
        sess = db.query(ReplaySession).filter_by(id=int(session_id)).first()
        if sess:
            sess.status = "ERROR"
            sess.error_message = str(message)[:500]
            db.commit()
    except Exception:
        db.rollback()
        logger.exception("[REPLAY] Failed marking session error session_id=%s", session_id)


# =============================================================================
# Tasks
# =============================================================================

@shared_task(name="app.tasks.replay_tasks.start_replay_session_task", queue="replay")
def start_replay_session_task(session_id: int):
    """
    Start the existing replay orchestrator from Celery.

    Old route behavior:
        pid = start_session(session.id)
        session.pid = pid

    New behavior:
        route queues this Celery task
        this task calls start_session(session_id)

    Note:
        This still uses the old PID-based orchestrator. This task proves
        config_json reached Celery, but the child process must also load and
        pass config_json into the replay algo runner.
    """
    session_id = int(session_id)
    clear_replay_stop_flag(session_id)

    db = SessionLocal()
    try:
        sess = db.query(ReplaySession).filter_by(id=session_id).first()
        if not sess:
            logger.warning("[REPLAY] start requested but session not found id=%s", session_id)
            return {
                "ok": False,
                "session_id": session_id,
                "error": "SESSION_NOT_FOUND",
            }

        current_status = str(sess.status or "").upper()
        if current_status not in {"PENDING", "QUEUED", "STARTING", "CREATED"}:
            logger.info(
                "[REPLAY] start skipped because status=%s session_id=%s",
                sess.status,
                session_id,
            )
            return {
                "ok": True,
                "session_id": session_id,
                "skipped": True,
                "reason": "SESSION_NOT_STARTABLE",
            }

        # Mark as STARTING before spawning.
        sess.status = "STARTING"
        sess.error_message = None
        db.commit()
        db.refresh(sess)

        # Hard proof that web parameters reached this Celery task.
        cfg = _log_replay_config_from_db(sess)

        logger.info(
            "[REPLAY] starting PID replay orchestrator session_id=%s symbol=%s interval=%s algo=%s",
            session_id,
            getattr(sess, "symbol", None),
            getattr(sess, "interval", None),
            getattr(sess, "algo_name", None),
        )

        # Existing PID-based replay process. The child process must load
        # ReplaySession.config_json by session_id.
        pid = start_session(session_id, no_sleep=bool(cfg.get("sparkie_fast_replay")))

        sess = db.query(ReplaySession).filter_by(id=session_id).first()
        if sess:
            sess.pid = pid

            # Keep STARTING here. The child orchestrator should switch to RUNNING
            # after loading bars and initializing cursor/total_bars.
            #
            # If your child process does NOT set RUNNING, change this to RUNNING
            # temporarily:
            #     sess.status = "RUNNING"
            sess.status = "STARTING"
            sess.error_message = None
            db.commit()

        logger.info(
            "[REPLAY] spawned PID replay orchestrator session_id=%s pid=%s prob_trail_drop=%s",
            session_id,
            pid,
            cfg.get("prob_trail_drop"),
        )

        return {
            "ok": True,
            "session_id": session_id,
            "pid": pid,
            "status": "STARTING",
            "config": {
                "long_entry_prob": cfg.get("long_entry_prob"),
                "short_entry_prob": cfg.get("short_entry_prob"),
                "prob_trail_drop": cfg.get("prob_trail_drop"),
                "stop_loss_usd": cfg.get("stop_loss_usd", cfg.get("hard_stop_usd")),
                "trailing_profit_usd": cfg.get("trailing_profit_usd", cfg.get("trailing_stop_distance")),
            },
        }

    except Exception as e:
        db.rollback()
        logger.exception("[REPLAY] Failed starting session_id=%s", session_id)
        _mark_session_error(db, session_id, f"Failed to start replay: {e}")
        return {
            "ok": False,
            "session_id": session_id,
            "error": str(e),
        }

    finally:
        try:
            db.close()
        except Exception:
            pass


@shared_task(name="app.tasks.replay_tasks.stop_replay_session_task", queue="replay")
def stop_replay_session_task(session_id: int):
    """
    Stop replay session and kill old PID-based orchestrator when present.
    """
    session_id = int(session_id)
    set_replay_stop_flag(session_id)

    db = SessionLocal()
    try:
        sess = db.query(ReplaySession).filter_by(id=session_id).first()
        if not sess:
            logger.warning("[REPLAY] stop requested but session not found id=%s", session_id)
            return {
                "ok": False,
                "session_id": session_id,
                "error": "SESSION_NOT_FOUND",
            }

        try:
            stop_session(db, session_id)
        except Exception:
            logger.exception("[REPLAY] stop_session failed session_id=%s", session_id)

        sess = db.query(ReplaySession).filter_by(id=session_id).first()
        if sess:
            sess.status = "STOPPED"
            if hasattr(sess, "stopped_at"):
                sess.stopped_at = datetime.utcnow()
            db.commit()

        logger.info("[REPLAY] stopped session_id=%s", session_id)
        return {
            "ok": True,
            "session_id": session_id,
            "status": "STOPPED",
        }

    except Exception as e:
        db.rollback()
        logger.exception("[REPLAY] Failed stopping session_id=%s", session_id)
        return {
            "ok": False,
            "session_id": session_id,
            "error": str(e),
        }

    finally:
        try:
            db.close()
        except Exception:
            pass


@shared_task(name="app.tasks.replay_tasks.run_replay_session_tick", queue="replay")
def run_replay_session_tick(session_id: int):
    """
    Placeholder for future single-tick replay mode.

    Current replay runtime is still the existing PID-based orchestrator started
    by start_session(). This task is intentionally a no-op until the replay loop
    is refactored into a true Celery tick runner.
    """
    session_id = int(session_id)

    if should_stop_replay(session_id):
        return {
            "ok": True,
            "session_id": session_id,
            "skipped": True,
            "reason": "STOP_REQUESTED",
        }

    return {
        "ok": True,
        "session_id": session_id,
        "ran": False,
        "reason": "PID_ORCHESTRATOR_MODE",
    }


@shared_task(name="app.tasks.replay_tasks.cleanup_stale_replay_sessions", queue="replay")
def cleanup_stale_replay_sessions(max_age_minutes: int = 60):
    """
    Mark stale RUNNING/STARTING/QUEUED replay sessions stopped.

    PID-based RUNNING sessions are considered stale if pid is missing/dead or
    updated_at is older than max_age_minutes when that column exists.
    """
    db = SessionLocal()
    cleaned = 0

    try:
        sessions = db.query(ReplaySession).all()
        cutoff = datetime.utcnow() - timedelta(minutes=int(max_age_minutes))

        for sess in sessions:
            status = str(getattr(sess, "status", "") or "").upper()
            if status not in {"PENDING", "RUNNING", "STARTING", "QUEUED", "OPTIMIZING", "PREPARING_REPLAY"}:
                continue

            pid = getattr(sess, "pid", None)
            should_clean = False
            cleanup_reason = "Cleaned stale replay session"
            cfg = _parse_config_json(getattr(sess, "config_json", None))
            is_sparkie = bool(cfg.get("sparkie_job_id"))
            is_optimizer = str(getattr(sess, "algo_name", "") or "") == "OptimizerPrefilter"
            created_at = getattr(sess, "created_at", None)
            if created_at is not None and is_sparkie:
                try:
                    created_cmp = created_at.replace(tzinfo=None) if created_at.tzinfo else created_at
                    sparkie_limit = 60 if is_optimizer else 30
                    if created_cmp < datetime.utcnow() - timedelta(minutes=sparkie_limit):
                        should_clean = True
                        cleanup_reason = (
                            f"Sparkie stopped this {'optimizer' if is_optimizer else 'replay'} "
                            f"after its {sparkie_limit}-minute maximum runtime."
                        )
                except Exception:
                    pass

            if status in {"PENDING", "QUEUED"}:
                if created_at is not None:
                    try:
                        created_cmp = created_at.replace(tzinfo=None) if created_at.tzinfo else created_at
                        should_clean = created_cmp < cutoff
                    except Exception:
                        should_clean = False

            elif pid and not pid_is_alive(pid):
                should_clean = True

            updated_at = getattr(sess, "updated_at", None)
            if updated_at is not None:
                try:
                    updated_cmp = updated_at.replace(tzinfo=None) if updated_at.tzinfo else updated_at
                    if updated_cmp < cutoff and not pid:
                        should_clean = True
                except Exception:
                    pass

            if should_clean:
                set_replay_stop_flag(int(sess.id))
                if pid and pid_is_alive(pid):
                    try:
                        stop_session(db, int(sess.id))
                    except Exception:
                        logger.exception("[REPLAY] failed stopping stale pid session_id=%s", sess.id)
                optimizer_task_id = str(cfg.get("sparkie_optimizer_task_id") or "")
                if is_optimizer and optimizer_task_id:
                    try:
                        from app.celery_app import celery_app

                        celery_app.control.revoke(optimizer_task_id, terminate=True, signal="SIGTERM")
                    except Exception:
                        logger.exception("[REPLAY] failed revoking stale Sparkie optimizer task=%s", optimizer_task_id)
                sess.status = "ERROR" if is_sparkie else "STOPPED"
                if hasattr(sess, "stopped_at"):
                    sess.stopped_at = datetime.utcnow()
                if hasattr(sess, "error_message"):
                    sess.error_message = cleanup_reason
                cleaned += 1

        db.commit()
        logger.info("[REPLAY] cleanup_stale_replay_sessions cleaned=%s", cleaned)
        return {
            "ok": True,
            "cleaned": cleaned,
        }

    except Exception as e:
        db.rollback()
        logger.exception("[REPLAY] cleanup_stale_replay_sessions failed")
        return {
            "ok": False,
            "error": str(e),
        }

    finally:
        try:
            db.close()
        except Exception:
            pass
