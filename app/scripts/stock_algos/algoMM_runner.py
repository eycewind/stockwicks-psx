#!/usr/bin/env python3
"""
Compatibility/dispatcher shim for older code that still imports algoMM_runner.

Canonical explicit modules:
  app.scripts.stock_algos.Algo1_MM -> app.scripts.research.Featureset_1
  app.scripts.stock_algos.Algo2_MM -> app.scripts.research.Featureset_2
  app.scripts.stock_algos.Algo3_MM -> app.scripts.research.Featureset_3
  app.scripts.stock_algos.Algo4_MM -> app.scripts.research.Featureset_4
  app.scripts.stock_algos.Algo5_MM -> app.scripts.research.Featureset_5
"""

from typing import Optional
from datetime import datetime

from app.database.connection import SessionLocal
from app.models.paper_trading_bot import PaperStockTradeBot


def run_algoMM_bot_tick(bot_id: int, anchor_dt: Optional[datetime] = None):
    db = SessionLocal()
    try:
        bot = db.query(PaperStockTradeBot).filter(PaperStockTradeBot.id == bot_id).first()
        algo_name = str(getattr(bot, "algo_name", "") or "Algo1_MM").strip() if bot else "Algo1_MM"
    finally:
        db.close()

    if algo_name == "Algo5_MM":
        from app.scripts.stock_algos.Algo5_MM import run_algoMM_bot_tick as _run
    elif algo_name == "Algo4_MM":
        from app.scripts.stock_algos.Algo4_MM import run_algoMM_bot_tick as _run
    elif algo_name == "Algo3_MM":
        from app.scripts.stock_algos.Algo3_MM import run_algoMM_bot_tick as _run
    elif algo_name == "Algo2_MM":
        from app.scripts.stock_algos.Algo2_MM import run_algoMM_bot_tick as _run
    else:
        from app.scripts.stock_algos.Algo1_MM import run_algoMM_bot_tick as _run

    return _run(bot_id, anchor_dt=anchor_dt)


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 2:
        run_algoMM_bot_tick(int(sys.argv[1]))
    else:
        print("Usage: python -m app.scripts.stock_algos.algoMM_runner <BOT_ID>")
