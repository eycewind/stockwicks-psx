# app/scripts/stock_algos/algo_runner.py
"""
Lazy algo dispatcher.

Celery imports this file during startup. Do not import every algo runner here.
One missing dependency in an unused algo must not crash the worker.

Each algo runner is imported only when a bot actually uses that algo.
"""

import logging


def run_algo_bot_tick(bot, anchor_dt=None) -> None:
    """
    Single entry point so Celery tasks do not import old algo files directly.
    Supports passing anchor_dt for snapped market time.
    """
    name = (getattr(bot, "algo_name", "") or "").strip().lower()
    bot_id = getattr(bot, "id", None)

    try:
        if name == "algo1":
            from app.scripts.stock_algos.algo1_runner import run_algo1_bot_tick
            fn = run_algo1_bot_tick

        elif name == "algo2":
            from app.scripts.stock_algos.algo2_runner import run_algo2_bot_tick
            fn = run_algo2_bot_tick

        elif name == "algo3":
            from app.scripts.stock_algos.algo3_runner import run_algo3_bot_tick
            fn = run_algo3_bot_tick

        elif name in {"algomm", "algo1_mm", "algo2_mm", "algo3_mm"}:
            from app.scripts.stock_algos.algoMM_runner import run_algoMM_bot_tick
            fn = run_algoMM_bot_tick

        else:
            logging.warning(
                "[ALGO] Unknown algo_name='%s' for bot %s",
                getattr(bot, "algo_name", None),
                bot_id,
            )
            return

        if anchor_dt is not None:
            fn(bot_id, anchor_dt=anchor_dt)
        else:
            fn(bot_id)

    except Exception as e:
        logging.exception(
            "[ALGO] Runner failed for bot %s (%s): %s",
            bot_id,
            getattr(bot, "algo_name", None),
            e,
        )
