#/var/stockwicks/clients/ashakil/app/celery_worker.py
"""
Commercial Celery Worker – ashakil client only

Queues:
- stock:   stock paper/live bot ticks and stock risk management
- replay:  replay simulator tasks only
- broker:  Schwab token refresh / broker maintenance
- default: fallback utility queue

No option tasks.
No SPX 0DTE tasks.
"""

import os
import sys

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_process_init, worker_process_shutdown
from kombu import Queue
from dotenv import load_dotenv

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

load_dotenv()

CLIENT_SLUG = os.getenv("CLIENT_SLUG", "ashakil")
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/1")
SCHWAB_REFRESH_USER_ID = int(os.getenv("SCHWAB_REFRESH_USER_ID", "3"))

celery = Celery(
    f"stockwicks_{CLIENT_SLUG}",
    broker=REDIS_URL,
    backend=REDIS_URL,
)


@worker_process_init.connect
def _dispose_engine_on_fork(**kwargs):
    try:
        from app.database.connection import engine
        engine.dispose()
    except Exception:
        pass


@worker_process_shutdown.connect
def _dispose_engine_on_shutdown(**kwargs):
    try:
        from app.database.connection import engine
        engine.dispose()
    except Exception:
        pass


# ── Commercial tasks ─────────────────────────────────────────────
# Keep imports explicit so Celery registers task names.
import app.tasks.stock_tasks  # noqa: F401
import app.tasks.schwab_tasks  # noqa: F401
import app.tasks.replay_tasks  # noqa: F401
import app.tasks.optimizer_tasks  # noqa: F401


# ── Queues & routing ─────────────────────────────────────────────
celery.conf.task_queues = (
    Queue("default"),
    Queue("stock"),
    Queue("replay"),
    Queue("broker"),
)

celery.conf.task_default_queue = "default"

celery.conf.task_routes = {
    "app.tasks.stock_tasks.*": {"queue": "stock"},
    "app.tasks.replay_tasks.*": {"queue": "replay"},
    "app.tasks.optimizer_tasks.*": {"queue": "replay"},
    "app.tasks.schwab_tasks.*": {"queue": "broker"},
}


# ── General Celery config ─────────────────────────────────────────
celery.conf.update(
    timezone="America/New_York",
    enable_utc=False,
    task_track_started=True,
    task_serializer="json",
    accept_content=["json"],
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    task_soft_time_limit=240,
    task_time_limit=300,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
)


# ── Beat schedule ─────────────────────────────────────────────────
celery.conf.beat_schedule = {
    # Schwab token refresh: refreshes BOTH market + trade tokens.
    # Default user_id is 3 for /var/stockwicks/clients/ashakil/data/3.
    "refresh-schwab-tokens-every-5min": {
        "task": "app.tasks.schwab_tasks.auto_refresh_user_token",
        "schedule": 300.0,
        "args": (),
        "options": {"queue": "broker"},
    },

    # Stock bots
    "run-stock-bots-1min": {
        "task": "app.tasks.stock_tasks.run_stock_bots_for_interval",
        "schedule": crontab(minute="*"),
        "args": ("1min",),
        "options": {"queue": "stock"},
    },
    "run-stock-bots-5min": {
        "task": "app.tasks.stock_tasks.run_stock_bots_for_interval",
        "schedule": crontab(minute="*/5"),
        "args": ("5min",),
        "options": {"queue": "stock"},
    },
    "run-stock-bots-10min": {
        "task": "app.tasks.stock_tasks.run_stock_bots_for_interval",
        "schedule": crontab(minute="*/10"),
        "args": ("10min",),
        "options": {"queue": "stock"},
    },
    "run-stock-bots-15min": {
        "task": "app.tasks.stock_tasks.run_stock_bots_for_interval",
        "schedule": crontab(minute="*/15"),
        "args": ("15min",),
        "options": {"queue": "stock"},
    },
    "run-stock-bots-30min": {
        "task": "app.tasks.stock_tasks.run_stock_bots_for_interval",
        "schedule": crontab(minute="*/30"),
        "args": ("30min",),
        "options": {"queue": "stock"},
    },
    "run-stock-bots-1d": {
        "task": "app.tasks.stock_tasks.run_stock_bots_for_interval",
        "schedule": crontab(minute="10", hour="16", day_of_week="1-5"),
        "args": ("1d",),
        "options": {"queue": "stock"},
    },

    # Stock risk management
    "update-open-trades-prices": {
        "task": "app.tasks.stock_tasks.update_open_trades_prices",
        "schedule": crontab(minute="*/2"),
        "options": {"queue": "stock"},
    },
    "pnl-exit-open-trades": {
        "task": "app.tasks.stock_tasks.pnl_exit_open_trades",
        "schedule": 60.0,
        "args": (200.0, 500.0, "live"),
        "options": {"queue": "stock"},
    },
    "eod-auto-close": {
        "task": "app.tasks.stock_tasks.eod_close_open_trades",
        "schedule": crontab(minute=58, hour=15, day_of_week="1-5"),
        "args": ("live", False),
        "options": {"queue": "stock"},
    },

    # Replay maintenance. Replay simulation ticks should be queued by replay UI/API,
    # not by stock_tasks.py.
    "cleanup-stale-replay-sessions": {
        "task": "app.tasks.replay_tasks.cleanup_stale_replay_sessions",
        "schedule": 60.0,
        "options": {"queue": "replay"},
    },
}
