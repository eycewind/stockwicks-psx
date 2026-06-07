# app/utils/common/trading_window.py
from __future__ import annotations
from datetime import datetime, time, timedelta
import pytz

# Timezone (exported for callers that need it)
_ET = pytz.timezone("US/Eastern")

# Configurable market window (regular session)
# TRADING_START_ET = time(0, 0)      # open at midnight
# TRADING_END_ET   = time(23, 59)    # run until just before midnight
# ACTIVE_WEEKDAYS  = {0, 1, 2, 3, 4, 5, 6}  # Mon–Sun (all days)
TRADING_START_ET = time(0, 30)
TRADING_END_ET   = time(23, 55)
ACTIVE_WEEKDAYS  = {0, 1, 2, 3, 4, 5, 6}


def is_within_trading_window(now_et: datetime | None = None) -> bool:
    """
    Regular trading session gate (09:30–16:00 ET, Mon–Fri).
    """
    now_et = now_et or datetime.now(_ET)
    if now_et.weekday() not in ACTIVE_WEEKDAYS:
        return False
    t = now_et.time()
    return TRADING_START_ET <= t <= TRADING_END_ET

def is_market_open_now(now_et: datetime | None = None) -> bool:
    """
    A looser “market open” check that some strategies/bots prefer:
      - Open 00:00–23:55 ET to allow pre/post data flow, but still close briefly.
    If you want strict RTH only, call is_within_trading_window instead.
    """
    now_et = now_et or datetime.now(_ET)
    t = now_et.time()
    return time(0, 0) <= t < time(23, 55)

def max_bar_age_for_interval(interval: str) -> timedelta:
    """
    Freshness guard used by bots before acting on the latest bar.
    """
    interval = (interval or "1min").lower()
    return {
        "1min": timedelta(minutes=3),
        "5min":  timedelta(minutes=12),
        "10min": timedelta(minutes=20),
        "15min": timedelta(minutes=35),
        "30min": timedelta(minutes=80),
        "1h":   timedelta(minutes=140),
        "1d":   timedelta(days=2),
        "1wk":  timedelta(days=14),
    }.get(interval, timedelta(minutes=10))
