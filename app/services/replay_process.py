# /var/www/stockwicks/app/services/replay_process.py
"""
Replay Process Manager
======================

Starts and stops the orchestrator as a detached subprocess.

Why detached?
  - If the FastAPI worker restarts (deploy, gunicorn reload), the replay
    should keep running.
  - If the replay process dies, the FastAPI worker is unaffected.

How stop works?
  - `stop_session()` sends SIGTERM. The orchestrator's signal handler
    drains the current bar, flips `replay_sessions.status` to STOPPED,
    clears PID, and exits. We also set status='STOPPED' directly from
    here so polling clients see the intent immediately even before the
    orchestrator's own cursor flush.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.models.replay import ReplaySession

log = logging.getLogger("ReplayProcess")
if not log.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [ReplayProc] %(message)s"
    ))
    log.addHandler(h)
    log.setLevel(logging.INFO)


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))


# =============================================================================
# Liveness
# =============================================================================
def pid_is_alive(pid: int) -> bool:
    """Return True iff the OS has a process with this pid."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)   # 0 = signal probe, doesn't actually signal
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # process exists, owned by someone else
    except OSError:
        return False


# =============================================================================
# Start
# =============================================================================
def start_session(session_id: int) -> int:
    """
    Spawn the orchestrator as a detached subprocess.
    Returns the new PID. Does NOT block.

    Captures stdout/stderr to logs/replay/replay_session_<id>.log
    so startup crashes are visible instead of becoming silent zombies.
    """
    from pathlib import Path

    session_id = int(session_id)

    base_dir = Path(REPO_ROOT)
    log_dir = base_dir / "logs" / "replay"
    log_dir.mkdir(parents=True, exist_ok=True)

    log_path = log_dir / f"replay_session_{session_id}.log"

    cmd = [
        sys.executable,
        "-m",
        "app.scripts.replay.orchestrator",
        "--session-id",
        str(session_id),
    ]

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(base_dir)

    log.info(
        "Starting orchestrator for session=%s cmd=%s cwd=%s log=%s",
        session_id,
        " ".join(cmd),
        str(base_dir),
        str(log_path),
    )

    f = open(log_path, "ab", buffering=0)
    f.write(f"\n\n===== START replay session {session_id} =====\n".encode())

    proc = subprocess.Popen(
        cmd,
        cwd=str(base_dir),
        env=env,
        stdout=f,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )

    log.info(
        "Started orchestrator for session=%s pid=%s log=%s",
        session_id,
        proc.pid,
        str(log_path),
    )
    return int(proc.pid)


# =============================================================================
# Stop
# =============================================================================
def stop_session(db: Session, session_id: int, *, hard_kill_after: float = 20.0) -> bool:
    """
    Stop a running session by sending SIGTERM to its orchestrator PID.
    Also flips status='STOPPED' in DB so polling clients see it instantly
    (orchestrator will overwrite with its own final state on exit).

    Returns True if we sent a signal (or session was already finished),
    False if we couldn't find a valid PID at all.

    hard_kill_after: currently not used (we don't block on shutdown).
                     Kept as a parameter placeholder if you later add a
                     supervisor that escalates to SIGKILL.
    """
    sess = db.query(ReplaySession).filter_by(id=session_id).first()
    if not sess:
        return False

    # Mark intent — orchestrator will detect via DB poll or SIGTERM handler.
    # Only if currently running.
    if sess.status == "RUNNING":
        sess.status = "STOPPED"
        sess.updated_at = datetime.utcnow()
        db.commit()
        log.info(f"Session {session_id} marked STOPPED in DB")

    pid = sess.pid
    if not pid:
        log.info(f"Session {session_id} has no PID (already exited?)")
        return True

    if not pid_is_alive(pid):
        log.info(f"Session {session_id} pid={pid} already dead; clearing")
        sess.pid = None
        db.commit()
        return True

    # Send SIGTERM. Orchestrator's handler drains current bar then exits.
    try:
        os.kill(pid, signal.SIGTERM)
        log.info(f"Sent SIGTERM to session {session_id} pid={pid}")
        return True
    except ProcessLookupError:
        log.info(f"Process {pid} already gone")
        sess.pid = None
        db.commit()
        return True
    except PermissionError as e:
        log.error(f"Not allowed to signal pid={pid}: {e}")
        return False


# =============================================================================
# Reap stale sessions (safety net)
# =============================================================================
def reap_stale_sessions(db: Session) -> int:
    """
    Find sessions marked RUNNING whose PIDs are dead (orchestrator crashed
    without cleaning up). Flip them to ERROR.

    Called on page load and before every start, so the DB never lies about
    what's running.

    Returns the number of sessions reaped.
    """
    stale = (
        db.query(ReplaySession)
          .filter(ReplaySession.status == "RUNNING")
          .all()
    )
    reaped = 0
    now = datetime.utcnow()
    for s in stale:
        if s.pid and pid_is_alive(s.pid):
            continue   # truly running
        log.warning(
            f"Reaping stale session {s.id} (pid={s.pid}, user={s.user_id})"
        )
        s.status = "ERROR"
        s.error_message = "Orchestrator died without cleanup (reaped)"
        s.stopped_at = now
        s.pid = None
        reaped += 1

    if reaped:
        db.commit()
    return reaped


# =============================================================================
# Retention: delete completed/stopped/error sessions older than 7 days
# =============================================================================
def purge_old_sessions(db: Session, *, days: int = 7) -> int:
    """Delete sessions in a terminal state older than `days` days."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    q = (
        db.query(ReplaySession)
          .filter(ReplaySession.status.in_(("COMPLETED", "STOPPED", "ERROR")))
          .filter(ReplaySession.created_at < cutoff)
    )
    count = q.count()
    if count:
        q.delete(synchronize_session=False)
        db.commit()
        log.info(f"Purged {count} sessions older than {days} days")
    return count
