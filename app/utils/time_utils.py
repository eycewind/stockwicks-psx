# app/utils/time_utils.py
import pytz
from datetime import datetime
ET = pytz.timezone("US/Eastern")
def get_now_et() -> datetime:
    """Returns the current time in US/Eastern timezone."""
    return datetime.now(ET)