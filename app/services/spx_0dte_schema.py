"""Small schema bootstrap for the SPX 0DTE paper bot tables."""

from __future__ import annotations

import logging

from app.database.connection import engine
from app.models.paper_spx_0dte import PaperSPXOpenTrade, PaperSPXPick, PaperSPXTradeHistory
from app.models.spx_0dte_alert_subscription import SPX0DTEAlertSubscription

log = logging.getLogger(__name__)


def ensure_spx_0dte_tables() -> None:
    """Create SPX 0DTE tables if the client database does not have them yet."""
    for table in (
        PaperSPXPick.__table__,
        PaperSPXOpenTrade.__table__,
        PaperSPXTradeHistory.__table__,
        SPX0DTEAlertSubscription.__table__,
    ):
        table.create(bind=engine, checkfirst=True)
        log.info("Ensured SPX 0DTE table exists: %s", table.name)
