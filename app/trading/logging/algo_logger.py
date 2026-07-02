import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


class AlgoLogger:
    def __init__(self, log_dir: str):
        self.log_dir = Path(log_dir)

    def _safe_symbol(self, symbol: str) -> str:
        return str(symbol or "").upper().strip().replace("/", "_").replace(" ", "_")

    def _append_jsonl(self, path: Path, row: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        row.setdefault("ts_utc", datetime.utcnow().isoformat())
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    def log_candle(self, user_id: int, bot_id: int, symbol: str, interval: str, row: Dict[str, Any]) -> None:
        path = self.log_dir / str(user_id) / f"bot_{bot_id}_{self._safe_symbol(symbol)}_{interval}_candles.jsonl"
        self._append_jsonl(path, row)

    def log_decision(self, user_id: int, bot_id: int, symbol: str, algo: str, row: Dict[str, Any]) -> None:
        path = self.log_dir / str(user_id) / f"bot_{bot_id}_{self._safe_symbol(symbol)}_{algo}_decisions.jsonl"
        self._append_jsonl(path, row)

    def log_event(self, user_id: int, bot_id: int, symbol: str, algo: str, row: Dict[str, Any]) -> None:
        path = self.log_dir / str(user_id) / f"bot_{bot_id}_{self._safe_symbol(symbol)}_{algo}_events.jsonl"
        self._append_jsonl(path, row)
