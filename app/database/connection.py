# /var/www/stockwicks/app/database/connection.py
"""
StockWicks DB connection module (PostgreSQL + SQLAlchemy)

Goals:
- Prevent "stale connection after inactivity" crashes (pre-ping + keepalives + recycle)
- Prevent connection leaks (always rollback on exception + always close)
- Reduce "too many connections" risk (small per-process pool; ensure workers close sessions)
- Provide a safe session context manager for scripts/celery tasks

NOTE:
- This does NOT magically increase Postgres capacity. It makes your app *reuse* and *release*
  connections correctly so Postgres isn't forced to hold lots of idle sessions.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import configure_mappers, sessionmaker

from app.database.base import Base  # noqa: F401  (ensures metadata import side-effects)

# Load environment variables
load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://stockwicks_user:StockwicksSecurePass!@localhost/stockwicks",
).strip()


def _is_local_postgres(url: str) -> bool:
    u = (url or "").lower()
    return (
        "@localhost" in u
        or "@127.0.0.1" in u
        or "//localhost" in u
        or "//127.0.0.1" in u
    )


# ----------------------------------------------------------------------------
# Connection args
# ----------------------------------------------------------------------------
connect_args: dict = {}

# Disable SSL only for local postgres (do NOT disable for remote / managed DBs)
if _is_local_postgres(DATABASE_URL) and "sslmode=" not in DATABASE_URL:
    connect_args["sslmode"] = "disable"

# TCP keepalives help avoid dead sockets after periods of inactivity
# (safe for local + remote; psycopg2 supports these)
connect_args.update(
    {
        "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
        "keepalives": 1,
        "keepalives_idle": int(os.getenv("DB_KEEPALIVES_IDLE", "30")),
        "keepalives_interval": int(os.getenv("DB_KEEPALIVES_INTERVAL", "10")),
        "keepalives_count": int(os.getenv("DB_KEEPALIVES_COUNT", "5")),
        "application_name": os.getenv("DB_APP_NAME", "stockwicks"),
    }
)

# Optional server-side timeouts (Postgres GUCs)
# Tune carefully if you have long-running analytics queries.
# These are applied per-connection.
_statement_timeout_ms = os.getenv("DB_STATEMENT_TIMEOUT_MS")
_idle_in_tx_timeout_ms = os.getenv("DB_IDLE_IN_TX_TIMEOUT_MS")
_opts = []
if _statement_timeout_ms:
    _opts.append(f"-c statement_timeout={int(_statement_timeout_ms)}")
if _idle_in_tx_timeout_ms:
    _opts.append(f"-c idle_in_transaction_session_timeout={int(_idle_in_tx_timeout_ms)}")
if _opts:
    connect_args["options"] = " ".join(_opts)

# ----------------------------------------------------------------------------
# Engine pool sizing (PER PROCESS!)
# If you run multiple gunicorn workers + multiple celery workers, total connections
# can multiply quickly. Start small and scale only if needed.
# ----------------------------------------------------------------------------
POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "5"))
MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "3"))
POOL_TIMEOUT = int(os.getenv("DB_POOL_TIMEOUT", "30"))
POOL_RECYCLE = int(os.getenv("DB_POOL_RECYCLE", "1800"))  # seconds

engine = create_engine(
    DATABASE_URL,
    pool_size=POOL_SIZE,
    max_overflow=MAX_OVERFLOW,
    pool_timeout=POOL_TIMEOUT,
    pool_recycle=POOL_RECYCLE,
    pool_pre_ping=True,                 # drop dead connections automatically
    pool_use_lifo=True,                 # reuse newest connections first
    pool_reset_on_return="rollback",    # VERY IMPORTANT: clean tx state when returning to pool
    connect_args=connect_args,
    future=True,
)

# expire_on_commit=False avoids SQLAlchemy "lazy reload" queries after commit
# which can accidentally happen after a session is closed.
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    bind=engine,
    future=True,
)

# ---- Import commercial MVP models before mapper config ----
from app.models.user import User  # noqa: F401
from app.models.paper_trading import PaperAccount, PaperTrade, PaperOrder  # noqa: F401
from app.models.paper_trading_bot import (  # noqa: F401
    PaperStockTradeBot,
    PaperStockBotOpenTrade,
    PaperStockBotTradeHistory,
)
from app.models.schwab import SchwabAccount, BrokerConnection  # noqa: F401
# from app.models.replay import ReplaySession, ReplayOpenTrade, ReplayTradeHistory  # noqa: F401  # disabled: avoids circular import
from app.models.audit import AuditEvent  # noqa: F401
from app.models.notification_log import TradeNotificationLog  # noqa: F401
from app.models.paper_spx_0dte import PaperSPXOpenTrade, PaperSPXPick, PaperSPXTradeHistory  # noqa: F401
from app.models.spx_0dte_alert_subscription import SPX0DTEAlertSubscription  # noqa: F401


# ----------------------------------------------------------------------------
# FastAPI dependency (NO auto-commit here; routes/services decide commit/rollback)
# ----------------------------------------------------------------------------
def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
        # Do not commit here; caller decides.
    except Exception:
        # Ensure a failed request doesn't leave the connection in a bad transaction state.
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            db.close()
        except Exception:
            # If the connection is already dead, don't crash cleanup
            pass


# ----------------------------------------------------------------------------
# Context manager for scripts / celery tasks (auto-commit/rollback)
# Use this in tasks instead of `db = SessionLocal()`.
# ----------------------------------------------------------------------------
@contextmanager
def db_session() -> Generator:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            db.close()
        except Exception:
            pass


# ---- Finalize mappings ----
configure_mappers()
