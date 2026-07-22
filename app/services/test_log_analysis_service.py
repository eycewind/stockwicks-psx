import os
from pathlib import Path

from app.services.log_analysis_service import chart_payload, latest_algo_logs


def test_latest_logs_keep_decisions_and_candles_on_same_bot(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    decision = log_dir / "bot_10_MU_Algo2_MM_decisions.jsonl"
    matching_candles = log_dir / "bot_10_MU_5min_candles.jsonl"
    unrelated_candles = log_dir / "bot_99_MU_1min_candles.jsonl"
    for path in (decision, matching_candles, unrelated_candles):
        path.write_text("\n", encoding="utf-8")
    os.utime(decision, (100, 100))
    os.utime(matching_candles, (90, 90))
    os.utime(unrelated_candles, (200, 200))

    selected = latest_algo_logs(tmp_path, symbol="MU")

    assert decision in selected
    assert matching_candles in selected
    assert unrelated_candles not in selected


def test_legacy_replay_event_is_aligned_to_matching_candle_close():
    rows = [
        {
            "row_type": "candle",
            "bar_time": "2026-07-21 09:30:00 EDT",
            "open": 99.0,
            "high": 100.5,
            "low": 98.5,
            "close": 100.0,
        },
        {
            "row_type": "candle",
            "bar_time": "2026-07-21 09:35:00 EDT",
            "open": 100.0,
            "high": 101.5,
            "low": 99.5,
            "close": 101.0,
        },
        {
            "row_type": "decision",
            "bar_time": "2026-07-22 04:03:00 EDT",
            "bar_time_inferred": True,
            "symbol": "MU",
            "action": "OPEN_LONG",
            "reason": "TEST",
            "price": 101.0,
        },
    ]

    payload = chart_payload(rows)

    assert payload["events"][0]["time"] == "2026-07-21 09:35:00 EDT"
    assert "_bar_time_inferred" not in payload["events"][0]
