# app/utils/options/logging_utils.py

import logging
import os
from logging.handlers import RotatingFileHandler

# Define the project root and the log directory path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../'))
LOG_DIR = os.path.join(PROJECT_ROOT, 'logs')

# Ensure the log directory exists (it's good practice to have this in the code too)
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

# --- Configure the Main Logger ---

# Create a logger instance
log = logging.getLogger('option_bots')
log.setLevel(logging.INFO)

# Prevent logs from propagating to the root logger
log.propagate = False

# Create a formatter to define the log message structure
formatter = logging.Formatter(
    '%(asctime)s - [%(levelname)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# --- Create Handlers ---

# 1. Console Handler: To print logs to your terminal screen
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

# 2. File Handler: To save logs to a file (e.g., logs/bots.log)
# RotatingFileHandler prevents the log file from getting too large.
file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, 'bots.log'),
    maxBytes=5*1024*1024,  # 5 MB per file
    backupCount=5          # Keep 5 old log files
)
file_handler.setFormatter(formatter)


# --- Add Handlers to the Logger ---
# Avoid adding handlers if they already exist (prevents duplicate logs)
if not log.handlers:
    log.addHandler(console_handler)
    log.addHandler(file_handler)


# --- Your Custom Logging Functions ---

def log_task(bot_id, user_id, symbol, algo_name, message, level="info"):
    """Logs a high-level task message."""
    msg = f"[TASK] Bot {bot_id} ({symbol}|{algo_name}): {message}"
    if level == "error":
        log.error(msg, exc_info=True)
    else:
        log.info(msg)

def log_algo(bot_id, user_id, symbol, algo_name, message, level="info"):
    """Logs a detailed algorithm decision message."""
    msg = f"[ALGO] Bot {bot_id} ({symbol}|{algo_name}): {message}"
    if level == "error":
        log.error(msg, exc_info=True)
    elif level == "warning":
        log.warning(msg)
    else:
        log.info(msg)