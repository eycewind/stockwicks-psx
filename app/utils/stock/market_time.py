import pytz
from datetime import datetime, timedelta, time as dtime

_ET = pytz.timezone("US/Eastern")

def is_market_open_now(now_et: datetime | None = None) -> bool:
    """Custom 'market open' rule: 00:00–23:55 ET open, closed otherwise."""
    now_et = now_et or datetime.now(_ET)
    t = now_et.time()
    return dtime(0, 0) <= t < dtime(23, 55)

def max_bar_age_for_interval(interval: str) -> timedelta:
    interval = (interval or "1min").lower()
    return {
        "1min": timedelta(minutes=3),
        "5min": timedelta(minutes=12),
        "10min": timedelta(minutes=20),
        "15min": timedelta(minutes=35),
        "30min": timedelta(minutes=80),
        "1h": timedelta(minutes=140),
        "1d": timedelta(days=2),
        "1wk": timedelta(days=14),
    }.get(interval, timedelta(minutes=10))
