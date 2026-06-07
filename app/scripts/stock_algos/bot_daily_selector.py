#!/usr/bin/env python3
"""
/var/www/stockwicks/app/scripts/stock_algos/bot_daily_selector.py

Optional helper: after symbol_selector_ai.py generates the daily picks,
toggle PaperStockTradeBot.is_active based on the selected symbols.

Run this before the open, or call it from Celery beat.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app.database.connection import SessionLocal  # noqa: E402
from app.models.paper_trading_bot import PaperStockTradeBot  # noqa: E402
from app.scripts.stock_algos.selector_gate import load_daily_selector  # noqa: E402

logger = logging.getLogger("bot_daily_selector")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [bot_daily_selector] %(message)s",
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--algo-name", default="AlgoMM")
    ap.add_argument("--trade-date", default=None, help="YYYY-MM-DD; default reads today's selector JSON")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sel = load_daily_selector(args.trade_date)
    if not sel:
        raise SystemExit("Selector JSON not found. Run symbol_selector_ai.py first.")

    allowed = {str(x["symbol"]).upper() for x in sel.get("selected_symbols", [])}
    db = SessionLocal()
    try:
        bots = db.query(PaperStockTradeBot).filter(PaperStockTradeBot.algo_name == args.algo_name).all()
        changed = []
        for bot in bots:
            symbol = str(getattr(bot, "symbol", "")).upper()
            should_be_active = symbol in allowed
            current = bool(getattr(bot, "is_active", False))
            if current != should_be_active:
                changed.append({"bot_id": bot.id, "symbol": symbol, "from": current, "to": should_be_active})
                if not args.dry_run:
                    bot.is_active = should_be_active
                    bot.updated_at = datetime.utcnow()

        if not args.dry_run:
            db.commit()

        print(json.dumps({"allowed_symbols": sorted(allowed), "changes": changed}, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
