# app/utils/options/expiry_guard.py
from datetime import datetime, time, timedelta
import pytz

US_EASTERN = pytz.timezone("US/Eastern")

def _now_et():
    return datetime.now(US_EASTERN)

def _session(now=None):
    now = now or _now_et()
    close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return {
        "now": now,
        "after_close": now >= close,
        "is_weekend": now.weekday() >= 5,
        "close": close,
    }

def _normalize_expiry_dt(expiry):
    """
    expiry can be a date or datetime. Normalize to US/Eastern 16:00 of expiry date.
    """
    if isinstance(expiry, datetime):
        dt = expiry
    else:
        dt = datetime.combine(expiry, time(16, 0))
    if dt.tzinfo is None:
        dt = US_EASTERN.localize(dt)
    else:
        dt = dt.astimezone(US_EASTERN)
    return dt

def expiry_ok(expiry, now=None, min_hours_to_expiry=2):
    """
    Hard rules:
      - If AFTER close, do NOT allow same-day expirations (0-DTE same day).
      - Always require at least `min_hours_to_expiry` until the option 'expires' (4pm ET).
    """
    s = _session(now)
    now = s["now"]
    expiry_dt = _normalize_expiry_dt(expiry)
    hours_to_expiry = (expiry_dt - now).total_seconds() / 3600.0

    # no same-day after close
    if s["after_close"] and expiry_dt.date() == now.date():
        return False

    # generally avoid contracts that are about to die
    if hours_to_expiry < min_hours_to_expiry:
        return False

    return True

def filter_expiries(expiries, now=None, min_hours_to_expiry=2):
    """Return only expiries that pass expiry_ok, sorted soonest-first."""
    ok = [e for e in expiries if expiry_ok(e, now=now, min_hours_to_expiry=min_hours_to_expiry)]
    # sort by actual datetime
    ok_sorted = sorted(ok, key=lambda e: _normalize_expiry_dt(e))
    return ok_sorted
