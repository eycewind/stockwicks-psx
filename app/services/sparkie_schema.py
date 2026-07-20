from __future__ import annotations

import logging

from app.database.connection import engine
from app.models.sparkie import SparkieCandidate, SparkieEvent, SparkieJob

log = logging.getLogger(__name__)


def ensure_sparkie_tables() -> None:
    """Create Sparkie v2 tables in client databases that do not have them yet."""
    for table in (SparkieJob.__table__, SparkieCandidate.__table__, SparkieEvent.__table__):
        table.create(bind=engine, checkfirst=True)
        log.info("Ensured Sparkie table exists: %s", table.name)
