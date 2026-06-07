import re
from pathlib import Path
from typing import Any
from datetime import datetime

_LAST_CANDLE_RE = re.compile(
    r"Last Candle:\s+"
    r"(?P<time>[^|]+?)\s*\|\s*"
    r"O:\s*(?P<open>-?\d+(?:\.\d+)?)\s+"
    r"H:\s*(?P<high>-?\d+(?:\.\d+)?)\s+"
    r"L:\s*(?P<low>-?\d+(?:\.\d+)?)\s+"
    r"C:\s*(?P<close>-?\d+(?:\.\d+)?)\s+"
    r"V:\s*(?P<volume>\d+)",
    re.MULTILINE,
)

def _to_epoch_seconds(ts: str) -> int:
    """
    Convert '2026-05-18 15:50:00-04:00' to Unix epoch seconds.
    Lightweight Charts expects epoch seconds for intraday candles.
    """
    ts = ts.strip()
    dt = datetime.fromisoformat(ts)
    return int(dt.timestamp())

def parse_pretty_bot_log_candles(log_path: str | Path) -> list[dict[str, Any]]:
    path = Path(log_path)
    if not path.exists():
        return []

    text = path.read_text(errors="replace")
    candles: list[dict[str, Any]] = []
    seen: set[int] = set()

    for m in _LAST_CANDLE_RE.finditer(text):
        raw_time = m.group("time").strip()

        try:
            chart_time = _to_epoch_seconds(raw_time)
        except Exception:
            continue

        if chart_time in seen:
            continue
        seen.add(chart_time)

        candles.append({
            "time": chart_time,
            "open": float(m.group("open")),
            "high": float(m.group("high")),
            "low": float(m.group("low")),
            "close": float(m.group("close")),
            "volume": int(m.group("volume")),
        })

    return candles
