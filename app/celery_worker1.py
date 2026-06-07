# app/celery_worker.py
# app/celery_worker.py
import os
import sys
from celery import Celery
from celery.schedules import crontab
from kombu import Queue
from dotenv import load_dotenv

# Ensure project root (/var/www/stockwicks) is on PYTHONPATH
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

load_dotenv()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery = Celery("stockwicks", broker=REDIS_URL, backend=REDIS_URL)

# Import task modules so their decorators run and register tasks
import app.tasks.stock_tasks    # noqa: F401
import app.tasks.option_tasks   # noqa: F401
import app.tasks.history        # noqa: F401   # <-- NEW

# Queues
celery.conf.task_queues = (
    Queue("celery"),   # default
    Queue("options"),  # options-only
)
celery.conf.task_default_queue = "celery"

# Route option tasks (both canonical and alias names) to "options"
celery.conf.task_routes = {
    "app.tasks.option_tasks.*": {"queue": "options"},
    "app.tasks.start_option_paper_bot_task": {"queue": "options"},  # <-- alias
}

# Global config – all in Eastern Time
celery.conf.update(
    timezone="America/New_York",
    enable_utc=False,
    task_track_started=True,
    task_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
)

# Beat schedule (times are ET)
celery.conf.beat_schedule = {
    # Stock bots
    "run-1min-bots": {
        "task": "app.tasks.stock_tasks.run_stock_bots_for_interval",
        "schedule": crontab(),  # every minute ET
        "args": ("1min",),
    },
    "run-5min-bots": {
        "task": "app.tasks.stock_tasks.run_stock_bots_for_interval",
        "schedule": crontab(minute="*/5"),
        "args": ("5min",),
    },
    "run-15min-bots": {
        "task": "app.tasks.stock_tasks.run_stock_bots_for_interval",
        "schedule": crontab(minute="*/15"),
        "args": ("15min",),
    },
    "run-daily-bots": {
        "task": "app.tasks.stock_tasks.run_stock_bots_for_interval",
        "schedule": crontab(hour=15, minute=55),
        "args": ("1d",),
    },

    # Option bots (routed via task_routes to "options")
    "options-bots-every-5min": {
        "task": "app.tasks.option_tasks.option_bots_tick_task",
        "schedule": crontab(minute="*/5"),
    },
    "options-bots-manage-5min": {
        "task": "app.tasks.option_tasks.option_bots_manage_task",
        "schedule": crontab(minute="*/5"),
    },

    # 🔥 New: EOD auto-close (16:01 ET daily)
    "eod-auto-close": {
        "task": "app.tasks.stock_tasks.eod_auto_close_task",
        "schedule": crontab(hour=16, minute=1),  # 4:01 PM ET
    },

    # 🔥 New: Hourly export of trade history to CSV

    # 🔥 New: Every 2 minutes export of trade history to CSV (for testing)
    "update-history-csv-test": {
        "task": "app.tasks.history.update_history_csv",
        "schedule": crontab(minute="*/2"),  # every 2 minutes
        "args": (116,),  # user_id
    },
    # "update-history-csv-hourly": {
    #     "task": "app.tasks.history.update_history_csv",
    #     "schedule": crontab(minute=0, hour="*"),  # every hour at :00
    #     "args": (116,),  # user_id
    # },
}
