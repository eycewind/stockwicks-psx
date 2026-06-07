#/var/www/stockwicks/app/utils/logging_config.py
# /var/www/stockwicks/app/utils/logging_config.py
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Single base dir for per-bot logs (matches your stock bot location)
BASE_LOG_DIR = Path("/var/www/stockwicks/data")

def setup_bot_logger(user_id: int, bot_id: int, symbol: str | None = None, algo: str | None = None) -> logging.Logger:
    """
    Returns a per-bot logger that writes under:
      /var/www/stockwicks/data/<user_id>/bot_<bot_id>_<SYMBOL>_<ALGO>.OPTIONS.log

    If symbol or algo are missing, they are omitted.
    Set OPTION_BOT_CONSOLE_LOG=1 to also echo logs to console (INFO+).
    """
    user_dir = BASE_LOG_DIR / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)

    parts = [f"bot_{bot_id}"]
    if symbol:
        parts.append(symbol.upper())
    if algo:
        parts.append(algo)
    parts.append("OPTIONS")
    log_file = user_dir / ("_".join(parts) + ".log")

    logger_name = f"option_bot_{user_id}_{bot_id}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if not logger.handlers:
        fh = RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,  # 5MB
            backupCount=3
        )
        fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

        # Optional console output for CLI runs
        if os.getenv("OPTION_BOT_CONSOLE_LOG", "0") == "1":
            ch = logging.StreamHandler()
            ch.setLevel(logging.INFO)
            ch.setFormatter(fmt)
            logger.addHandler(ch)

    return logger
