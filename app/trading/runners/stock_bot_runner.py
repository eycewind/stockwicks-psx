#/var/stockwicks/clients/ashakil/app/trading/runners/stock_bot_runner.py
import logging
import os
from typing import Any, Optional

from app.models.paper_trading_bot import PaperStockTradeBot
from app.scripts.stock_algos.algo_runner import run_algo_bot_tick
from app.trading.logging.algo_logger import AlgoLogger
from app.scripts.stock_algos.Algo1_MM import run_algoMM_bot_tick as run_algo1_mm_tick
from app.scripts.stock_algos.Algo2_MM import run_algoMM_bot_tick as run_algo2_mm_tick
from app.scripts.stock_algos.Algo4_MM import run_algoMM_bot_tick as run_algo4_mm_tick

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def run_stock_bot_tick(
    bot: PaperStockTradeBot,
    *,
    anchor_dt: Optional[Any] = None,
) -> dict:
    """
    Commercial stock bot runner bridge.

    MVP behavior:
    - Keeps existing legacy Algo runner behavior.
    - Adds commercial safety prechecks.
    - Adds standard JSONL event wrapper.
    - Later this becomes:
      MarketDataProvider -> BaseStrategy -> RiskManager -> TradeRouter -> AlgoLogger.
    """

    if not bot:
        return {"ok": False, "reason": "missing_bot"}

    client_slug = os.getenv("CLIENT_SLUG", "unknown")
    log_dir = os.getenv("LOG_DIR", "logs")

    algo_logger = AlgoLogger(log_dir)

    user_id = int(getattr(bot, "user_id", 0) or 0)
    bot_id = int(getattr(bot, "id", 0) or 0)
    symbol = getattr(bot, "symbol", "UNKNOWN") or "UNKNOWN"
    interval = getattr(bot, "interval", "UNKNOWN") or "UNKNOWN"
    algo_name = getattr(bot, "algo_name", "UNKNOWN") or "UNKNOWN"

    if _env_bool("EMERGENCY_STOP", False):
        algo_logger.log_event(
            user_id,
            bot_id,
            symbol,
            algo_name,
            {
                "client": client_slug,
                "user_id": user_id,
                "bot_id": bot_id,
                "symbol": symbol,
                "interval": interval,
                "algo": algo_name,
                "event": "EMERGENCY_STOP",
                "reason": "EMERGENCY_STOP_ENV_TRUE",
            },
        )
        return {"ok": False, "reason": "emergency_stop"}

    if not _env_bool("PAPER_TRADING_ENABLED", True):
        algo_logger.log_event(
            user_id,
            bot_id,
            symbol,
            algo_name,
            {
                "client": client_slug,
                "user_id": user_id,
                "bot_id": bot_id,
                "symbol": symbol,
                "interval": interval,
                "algo": algo_name,
                "event": "SKIP",
                "reason": "PAPER_TRADING_DISABLED",
            },
        )
        return {"ok": False, "reason": "paper_trading_disabled"}

    try:
        algo_logger.log_event(
            user_id,
            bot_id,
            symbol,
            algo_name,
            {
                "client": client_slug,
                "user_id": user_id,
                "bot_id": bot_id,
                "symbol": symbol,
                "interval": interval,
                "algo": algo_name,
                "event": "BOT_TICK_START",
                "reason": "COMMERCIAL_RUNNER_BRIDGE",
            },
        )

        if algo_name == "Algo1_MM":
            result = run_algo1_mm_tick(bot_id, anchor_dt=anchor_dt)
        elif algo_name == "Algo2_MM":
            result = run_algo2_mm_tick(bot_id, anchor_dt=anchor_dt)
        elif algo_name == "Algo3_MM":
            from app.scripts.stock_algos.algo3_runner import run_algo3_bot_tick as run_smi_tick
            result = run_smi_tick(bot_id, anchor_dt=anchor_dt)
        elif algo_name == "Algo4_MM":
            result = run_algo4_mm_tick(bot_id, anchor_dt=anchor_dt)
        elif algo_name == "Algo5_MM":
            from app.scripts.stock_algos.algo2_runner import run_algo2_bot_tick as run_macd_tick
            result = run_macd_tick(bot_id, anchor_dt=anchor_dt)
        else:
            result = run_algo_bot_tick(bot, anchor_dt=anchor_dt)

        algo_logger.log_event(
            user_id,
            bot_id,
            symbol,
            algo_name,
            {
                "client": client_slug,
                "user_id": user_id,
                "bot_id": bot_id,
                "symbol": symbol,
                "interval": interval,
                "algo": algo_name,
                "event": "BOT_TICK_DONE",
                "reason": "LEGACY_RUNNER_COMPLETED",
                "result": result,
            },
        )

        return {"ok": True, "result": result}

    except Exception as exc:
        logger.exception("Commercial stock bot runner failed for bot_id=%s", bot_id)

        algo_logger.log_event(
            user_id,
            bot_id,
            symbol,
            algo_name,
            {
                "client": client_slug,
                "user_id": user_id,
                "bot_id": bot_id,
                "symbol": symbol,
                "interval": interval,
                "algo": algo_name,
                "event": "ERROR",
                "reason": "COMMERCIAL_RUNNER_EXCEPTION",
                "error": str(exc),
            },
        )

        return {"ok": False, "reason": "exception", "error": str(exc)}
