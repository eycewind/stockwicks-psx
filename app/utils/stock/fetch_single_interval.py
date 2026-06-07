# app/scripts/fetch_single_interval.py
# app/scripts/fetch_single_interval.py
import os, time, logging
from app.scripts.core_bot_engine import save_price_data_to_csv

DEFAULT_DATA_DIR = "/var/www/stockwicks/data"

# sensible freshness windows per interval
STALE_BY_INTERVAL = {
    "1min": 70,         # ~1 bar + buffer
    "5min": 5 * 60,
    "10min": 10 * 60,
    "15min": 15 * 60,
    "30min": 30 * 60,
    "1d": 12 * 60 * 60, # half-day
    "1wk": 3 * 24 * 60 * 60,
}

def _lock_path(csv_path: str) -> str:
    return csv_path + ".lock"

def _acquire_lock(lockfile: str) -> bool:
    try:
        # atomic create; fail if exists
        fd = os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
        return True
    except FileExistsError:
        return False

def _release_lock(lockfile: str) -> None:
    try:
        os.unlink(lockfile)
    except FileNotFoundError:
        pass

def fetch_if_needed(user_id: int, symbol: str, interval: str,
                    force: bool = False, stale_after_sec: int | None = None,
                    data_dir: str = DEFAULT_DATA_DIR) -> str:
    """
    Ensures <user>_<symbol>_<interval>_data.csv is fresh.
    - force=True  → re-fetch unconditionally
    - Otherwise   → refresh if file is missing or older than staleness window
    Uses a simple lockfile to avoid two workers fetching the same thing concurrently.
    """
    user_dir = os.path.join(data_dir, str(user_id))
    os.makedirs(user_dir, exist_ok=True)

    path = os.path.join(user_dir, f"{user_id}_{symbol}_{interval}_data.csv")
    lockfile = _lock_path(path)

    # choose staleness window
    if stale_after_sec is None:
        stale_after_sec = STALE_BY_INTERVAL.get(interval, 90)

    def _needs_refresh() -> bool:
        if force:
            return True
        if not os.path.exists(path):
            return True
        age = time.time() - os.path.getmtime(path)
        return age > stale_after_sec

    if not _needs_refresh():
        age = int(time.time() - os.path.getmtime(path))
        logging.info(f"[{symbol}] {interval} data fresh ({age}s) — reuse")
        return path

    # refresh with lock
    if not _acquire_lock(lockfile):
        # someone else is fetching; wait a short moment and return whatever exists
        for _ in range(10):
            time.sleep(0.2)
            if os.path.exists(path) and not os.path.exists(lockfile):
                break
        return path

    try:
        action = "force-refresh" if force else "refresh"
        logging.info(f"[{symbol}] {action} {interval} data")

        # 🆕 Cleanup: remove any old files before fetching
        for f in os.listdir(user_dir):
            if f.startswith(f"{user_id}_{symbol}_{interval}_") and f.endswith(".csv"):
                try:
                    os.remove(os.path.join(user_dir, f))
                    logging.info(f"[{symbol}] removed stale file {f}")
                except Exception as e:
                    logging.warning(f"[{symbol}] could not remove old file {f}: {e}")

        return save_price_data_to_csv(user_id, symbol, interval, output_dir=data_dir)
    finally:
        _release_lock(lockfile)
