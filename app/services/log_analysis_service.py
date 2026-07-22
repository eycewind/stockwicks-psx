# /var/stockwicks/clients/ashakil/app/services/log_analysis_service.py
import json
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


ACTION_RE = re.compile(
    r"\b("
    r"OPEN_LONG|OPEN_SHORT|HOLD_POSITION|HOLD_OPEN_TRADE|NO_ENTRY_SIGNAL|NO_SIGNAL|SAME_BAR_SKIP|COOLDOWN|"
    r"NO_DATA|RESAMPLE_FAILED|INVALID_PRICE|NO_FEATURES|NO_INFER_FEATURES|"
    r"NO_TRAIN_FEATURES|BAD_TRAIN_FEATURES|ONE_CLASS_TRAINING|STALE_FEATURES|"
    r"NO_PROBS|NO_SMOOTHED_PROB|NO_VALID_PROB|PROB_ALIGN_SHORT|OPEN_FAILED|"
    r"MARKET_CLOSED|STALE_PRICE_DATA|NO_CLOSED_BARS|FEATURE_BUILDER_UNSAFE|"
    r"INSUFFICIENT_DATA_FOR_SIGNALS|INSUFFICIENT_DATA|SIGNAL_GEN_FAILED|"
    r"ALREADY_PROCESSED_BAR|NOT_ENOUGH_SIGNAL_ROWS|SELL_SIGNAL_SHORT_DISABLED|"
    r"CLOSE_LONG(?:_[A-Z0-9_]+)?|CLOSE_SHORT(?:_[A-Z0-9_]+)?|"
    r"END_OF_DAY_CLOSE(?:_[A-Z0-9_]+)?|EOD_NO_OPEN_TRADE|EOD_NO_POSITION|"
    r"EXIT_[A-Z0-9_]+|ERROR(?::[^|\n]*)?"
    r")\b"
)

ERROR_ACTION_RE = re.compile(r"^(?:ERROR|LOG_PARSE_ERROR)\b", re.IGNORECASE)


def _safe_float(value: Any):
    try:
        if value in (None, "", "N/A", "NA", "-"):
            return None
        return float(value)
    except Exception:
        return None


def _safe_int(value: Any):
    try:
        if value in (None, "", "N/A", "NA", "-"):
            return None
        return int(float(value))
    except Exception:
        return None


def _symbol_from_filename(path: Path) -> Optional[str]:
    parts = path.stem.split("_")
    if len(parts) < 3 or parts[0] != "bot":
        return None
    for part in parts[2:]:
        if not part:
            continue
        if part.startswith("Algo") or part in {"1min", "5min", "10min", "15min", "30min", "1h", "1d", "1wk", "candles", "decisions"}:
            return None
        return part
    return None


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def _row_date(row: Dict[str, Any]) -> Optional[date]:
    for key in ("bar_time", "time", "log_time", "datetime", "ts_et"):
        parsed = _parse_date(row.get(key))
        if parsed:
            return parsed
    return None


def _parse_features(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return {str(k): _safe_float(v) for k, v in raw.items()}
    if not isinstance(raw, str):
        return {}

    stripped = raw.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            loaded = json.loads(stripped)
            if isinstance(loaded, dict):
                return {str(k): _safe_float(v) for k, v in loaded.items()}
        except Exception:
            pass

    features: Dict[str, Any] = {}
    for part in stripped.split(","):
        if "=" not in part:
            continue
        key, value = part.strip().split("=", 1)
        features[key.strip()] = _safe_float(value.strip())
    return features


def _split_log_blocks(text: str) -> List[str]:
    blocks = [
        b for b in re.split(r"\n={20,}\n", text)
        if b.strip() and ("ACTION:" in b or re.search(r"\|\s*[A-Z_]+(?:\s*\|)", b))
    ]

    # Handles the box-style Algo4/AlgoMM logs even when UTF-8 box chars are mojibake.
    starts = [m.start() for m in re.finditer(r"(?:^|\n).{0,4}(?:\[20\d{2}-|Model name:)", text)]
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        block = text[start:end]
        if "ACTION:" in block:
            blocks.append(block)

    return blocks


def normalize_row(row: Dict[str, Any], path: Optional[Path] = None) -> Dict[str, Any]:
    out = dict(row)
    if path is not None:
        out.setdefault("source_file", path.name)

    source_file = str(out.get("source_file") or (path.name if path else ""))
    out["symbol"] = out.get("symbol") or (_symbol_from_filename(path) if path else None) or "UNKNOWN"

    if out.get("decision") and not out.get("action"):
        out["action"] = out.get("decision")

    if out.get("action"):
        action_match = ACTION_RE.search(str(out["action"]))
        out["action"] = action_match.group(1) if action_match else str(out["action"]).strip()

    if out.get("reason") is not None:
        out["reason"] = str(out["reason"]).strip()

    action_text = str(out.get("action") or "")
    reason_text = str(out.get("reason") or "")
    out["is_error"] = bool(ERROR_ACTION_RE.search(action_text) or "EXCEPTION:" in reason_text or "ERROR:" in reason_text)

    if out.get("ts_et") and not out.get("time"):
        out["time"] = out.get("ts_et")
    if out.get("datetime") and not out.get("time"):
        out["time"] = out.get("datetime")
    if not out.get("bar_time"):
        out["bar_time_inferred"] = True
        out["bar_time"] = out.get("ts_et") or out.get("time")

    for key in (
        "prob_up", "prob_down", "prob_up_avg", "prob_up_avg_prev",
        "open", "high", "low", "close", "price", "volume",
    ):
        if key in out:
            out[key] = _safe_float(out[key])

    if out.get("price") is None and out.get("close") is not None:
        out["price"] = out.get("close")

    out["features"] = _parse_features(out.get("features") or {})

    if "candles.jsonl" in source_file or (
        not out.get("action") and all(out.get(k) is not None for k in ("open", "high", "low", "close"))
    ):
        out["row_type"] = "candle"
    else:
        out["row_type"] = "decision"

    return out


def parse_pretty_algo_log(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(errors="replace")
    rows: List[Dict[str, Any]] = []
    fallback_symbol = _symbol_from_filename(path)

    for block in _split_log_blocks(text):
        row: Dict[str, Any] = {
            "source_file": path.name,
            "row_type": "decision",
            "symbol": fallback_symbol or "UNKNOWN",
        }

        box_header = re.search(r"\[(.*?)\]\s*(.*)", block)
        if box_header:
            row["log_time"] = box_header.group(1).strip()
            action_match = ACTION_RE.search(box_header.group(2))
            if action_match:
                row["action"] = action_match.group(1)

        legacy_header = re.search(
            r"^([0-9:\- ]+\s+[A-Z]{2,4})\s*\|\s*([A-Z0-9./_-]*)\s*([A-Za-z0-9]+)?\s*\|\s*(.*?)\s*\|\s*(.*)$",
            block.strip(),
            re.MULTILINE,
        )
        if legacy_header:
            row["time"] = legacy_header.group(1).strip()
            row["symbol"] = legacy_header.group(2).strip() or row["symbol"]
            row["interval"] = (legacy_header.group(3) or "").strip()
            row["action"] = legacy_header.group(4).strip()
            row["reason"] = legacy_header.group(5).strip()

        patterns = {
            "model_name": r"Model name:\s*(.*)",
            "symbol": r"Symbol:\s*(\S+)",
            "time": r"Time:\s*(.*)",
            "data_candles": r"Data:\s*(\d+)\s*candles",
            "latest_bar_time": r"Latest Bar Time:\s*(.*)",
            "bar_age": r"Bar Age:\s*(.*)",
            "interval": r"Interval:\s*(\S*)",
            "action": r"ACTION:\s*(.*)",
            "reason": r"REASON:\s*(.*)",
            "algo": r"\balgo=([A-Za-z0-9_]+)",
            "feature_set": r"\bfeature_set=([A-Za-z0-9_]+)",
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, block)
            if match:
                row[key] = match.group(1).strip()

        probs = re.search(r"Probabilities:\s*UP:\s*([0-9.]+)\s*\|\s*DOWN:\s*([0-9.]+)", block)
        if probs:
            row["prob_up"] = probs.group(1)
            row["prob_down"] = probs.group(2)

        legacy_probs = re.search(
            r"prob_up=([0-9.NA/-]+)\s+prob_down=([0-9.NA/-]+)"
            r"(?:\s+prob_up_avg=([0-9.NA/-]+))?(?:\s+prev_avg=([0-9.NA/-]+))?",
            block,
        )
        if legacy_probs:
            row["prob_up"] = legacy_probs.group(1)
            row["prob_down"] = legacy_probs.group(2)
            row["prob_up_avg"] = legacy_probs.group(3)
            row["prob_up_avg_prev"] = legacy_probs.group(4)

        candle = re.search(
            r"Last Candle:\s*(.*?)\s*\|\s*O:\s*([0-9.]+)\s*H:\s*([0-9.]+)\s*L:\s*([0-9.]+)\s*C:\s*([0-9.]+)\s*V:([0-9.]+)",
            block,
        )
        if candle:
            row["bar_time"] = candle.group(1).strip()
            row["open"] = candle.group(2)
            row["high"] = candle.group(3)
            row["low"] = candle.group(4)
            row["close"] = candle.group(5)
            row["volume"] = candle.group(6)

        legacy_price = re.search(r"close=([0-9.]+)\s+open=([0-9.]+)\s+rows=(\d+)\s+position=(.*)", block)
        if legacy_price:
            row["close"] = legacy_price.group(1)
            row["open"] = legacy_price.group(2)
            row["data_candles"] = legacy_price.group(3)
            row["position"] = legacy_price.group(4).strip()

        position = re.search(r"Position:\s*(.*)", block)
        if position:
            row["position"] = position.group(1).strip()

        features_match = re.search(r"(?:Features|features)\s*[:=]\s*(.*)", block)
        if features_match:
            row["features"] = features_match.group(1).strip()

        indicator_features: Dict[str, Any] = {}
        smi_state = re.search(r"Algo3 State:\s*SMI=([^\s,\n]+)", block)
        if smi_state:
            indicator_features["smi"] = smi_state.group(1)

        macd_state = re.search(
            r"MACD State:\s*Close=([^,\n]+),\s*MACD=([^,\n]+),\s*Signal=([^,\n]+),\s*Hist=([^,\n]+),\s*ATR14=([^\s,\n]+)",
            block,
        )
        if macd_state:
            indicator_features.update({
                "close": macd_state.group(1),
                "macd": macd_state.group(2),
                "macd_signal": macd_state.group(3),
                "macd_hist": macd_state.group(4),
                "atr14": macd_state.group(5),
            })
            row.setdefault("close", macd_state.group(1))

        signals = re.search(r"Signals:\s*Buy=([^,\n]+),\s*Sell=([^\s,\n]+)", block)
        if signals:
            indicator_features["buy_signal"] = 1.0 if signals.group(1).strip() == "True" else 0.0
            indicator_features["sell_signal"] = 1.0 if signals.group(2).strip() == "True" else 0.0

        if indicator_features:
            existing_features = _parse_features(row.get("features") or {})
            existing_features.update({k: _safe_float(v) for k, v in indicator_features.items()})
            row["features"] = existing_features

        rows.append(normalize_row(row, path))

    return rows


def _path_kind(path: Path) -> str:
    name = path.name
    if name.endswith("_candles.jsonl"):
        return "candles"
    if name.endswith("_decisions.jsonl"):
        return "decisions"
    return "log"


def _bot_id_from_filename(path: Path) -> Optional[str]:
    match = re.match(r"^bot_([^_]+)_", path.name)
    return match.group(1) if match else None


def find_algo_logs(base_dir: Path, symbol: Optional[str] = None) -> List[Path]:
    candidates = []
    for folder in [base_dir / "logs", base_dir / "data"]:
        if folder.exists():
            candidates.extend(folder.rglob("bot_*_Algo*.log"))
            candidates.extend(folder.rglob("bot_*_Algo*_decisions.jsonl"))
            candidates.extend(folder.rglob("bot_*_candles.jsonl"))

    paths = sorted(set(candidates))
    if symbol:
        target = symbol.upper()
        paths = [
            path for path in paths
            if (_symbol_from_filename(path) or "").upper() == target
        ]
    return paths


def latest_algo_logs(base_dir: Path, symbol: Optional[str] = None) -> List[Path]:
    paths = find_algo_logs(base_dir, symbol=symbol)
    grouped: Dict[tuple[str, str, str], List[Path]] = {}
    for path in paths:
        path_symbol = (_symbol_from_filename(path) or "UNKNOWN").upper()
        bot_id = _bot_id_from_filename(path)
        if bot_id:
            # Paper bots and Replay sessions have independent numeric ID
            # sequences. Keep their directories in the identity so bot #430 in
            # logs/ cannot collide with Replay #430 in data/.
            source_namespace = str(path.parent.resolve())
            grouped.setdefault((path_symbol, bot_id, source_namespace), []).append(path)

    latest_group_by_symbol: Dict[str, List[Path]] = {}
    latest_group_score: Dict[str, tuple[int, float]] = {}
    for (path_symbol, _bot_id, _source_namespace), group in grouped.items():
        # Anchor selection to trading decisions when available. This prevents a
        # recently updated candle file from a different bot/session being mixed
        # with the selected entries and exits.
        decision_files = [path for path in group if _path_kind(path) == "decisions"]
        anchor_files = decision_files or [path for path in group if _path_kind(path) == "log"] or group
        score = (1 if decision_files else 0, max(path.stat().st_mtime for path in anchor_files))
        current = latest_group_by_symbol.get(path_symbol)
        if current is None or score > latest_group_score.get(path_symbol, (-1, -1.0)):
            latest_group_by_symbol[path_symbol] = group
            latest_group_score[path_symbol] = score

    selected: List[Path] = []
    for group in latest_group_by_symbol.values():
        latest_by_kind: Dict[str, Path] = {}
        for path in group:
            kind = _path_kind(path)
            current = latest_by_kind.get(kind)
            if current is None or path.stat().st_mtime > current.stat().st_mtime:
                latest_by_kind[kind] = path
        # Structured decisions contain more reliable fields than the legacy
        # text log. Use the text log only when no decision JSONL exists.
        if "decisions" in latest_by_kind:
            selected.append(latest_by_kind["decisions"])
        elif "log" in latest_by_kind:
            selected.append(latest_by_kind["log"])
        # Keep every interval for the selected bot/session. chart_payload then
        # selects the interval declared by the decision rows.
        selected.extend(path for path in group if _path_kind(path) == "candles")

    return sorted(selected, key=lambda path: path.name)


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    with path.open("r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(normalize_row(json.loads(line), path))
            except Exception:
                continue
    return rows


def load_rows(
    base_dir: Path,
    symbol: Optional[str] = None,
    start_date: Optional[Any] = None,
    end_date: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    selected_paths = latest_algo_logs(base_dir, symbol=symbol)
    for path in selected_paths:
        try:
            if path.suffix == ".log":
                rows.extend(parse_pretty_algo_log(path))
            elif path.suffix == ".jsonl":
                rows.extend(load_jsonl(path))
        except Exception as exc:
            rows.append(normalize_row({
                "source_file": path.name,
                "symbol": _symbol_from_filename(path) or "UNKNOWN",
                "action": "ERROR",
                "reason": f"LOG_PARSE_ERROR: {exc}",
                "row_type": "decision",
            }, path))

    if symbol:
        rows = [r for r in rows if str(r.get("symbol") or "").upper() == symbol.upper()]

    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if start or end:
        filtered = []
        for row in rows:
            row_date = _row_date(row)
            if row_date is None:
                continue
            if start and row_date < start:
                continue
            if end and row_date > end:
                continue
            filtered.append(row)
        rows = filtered

    deduped: List[Dict[str, Any]] = []
    seen = set()
    for row in rows:
        key = (
            row.get("source_file"),
            row.get("row_type"),
            row.get("bar_time") or row.get("time"),
            row.get("action"),
            row.get("reason"),
            row.get("close"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    rows = deduped

    rows.sort(key=lambda r: r.get("bar_time") or r.get("time") or "")
    return rows


def _decision_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [r for r in rows if r.get("row_type") != "candle" and r.get("action")]


def _candle_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [r for r in rows if r.get("row_type") == "candle"]


def normalize_reason(reason: str) -> str:
    reason = str(reason or "")
    if reason.startswith("LAST_TRADE_AT"):
        return "COOLDOWN"
    for token in (
        "BOTH_PROBS_BELOW_THRESHOLDS",
        "OBV_BLOCKS_LONG",
        "OBV_BLOCKS_SHORT",
        "LOW_VOLUME",
        "LONG_CONDITIONS_MET",
        "SHORT_CONDITIONS_MET",
        "EXIT_CONDITIONS_NOT_MET",
        "GUARDRAILS_NOT_HIT",
        "PROB_TRAIL_DROP",
        "NO_PREVIOUS_PROB_AVG_FOR_CROSS",
        "NO_PREVIOUS_PROB_AVG",
        "BULLISH_BUT_NO_NEW_LONG_CROSS",
        "BEARISH_BUT_NO_NEW_SHORT_CROSS",
        "BULLISH_BUT_CONFIRMATION",
        "BEARISH_BUT_CONFIRMATION",
        "LONG_BLOCKED_BY_DOWN_FLOW_REGIME",
        "SHORT_BLOCKED_BY_UP_FLOW_REGIME",
    ):
        if token in reason:
            return token
    if reason.startswith("EXCEPTION:"):
        return reason.replace("EXCEPTION:", "ERROR:", 1).strip()
    return reason or "UNKNOWN"


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_action: Dict[str, int] = {}
    by_reason: Dict[str, int] = {}
    by_symbol: Dict[str, int] = {}
    decisions = _decision_rows(rows)
    errors = error_rows(rows)

    for row in decisions:
        action = row.get("action") or "UNKNOWN"
        reason = normalize_reason(row.get("reason") or "UNKNOWN")
        symbol = row.get("symbol") or "UNKNOWN"
        by_action[action] = by_action.get(action, 0) + 1
        by_reason[reason] = by_reason.get(reason, 0) + 1
        by_symbol[symbol] = by_symbol.get(symbol, 0) + 1

    return {
        "rows": len(decisions),
        "raw_rows": len(rows),
        "by_action": by_action,
        "by_reason": by_reason,
        "by_symbol": by_symbol,
        "errors": len(errors),
        "error_rows": errors[:25],
    }


def error_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    errors = []
    for row in _decision_rows(rows):
        if not row.get("is_error"):
            continue
        errors.append({
            "time": row.get("bar_time") or row.get("time") or row.get("log_time"),
            "symbol": row.get("symbol"),
            "source_file": row.get("source_file"),
            "action": row.get("action"),
            "reason": row.get("reason"),
        })
    return errors


def _parse_chart_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None)
    except ValueError:
        pass
    cleaned = re.sub(r"\s+(?:EST|EDT|ET|UTC)$", "", text, flags=re.IGNORECASE)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def _align_events_to_candles(events: List[Dict[str, Any]], candles: List[Dict[str, Any]]) -> None:
    """Snap trade markers to their candle, including legacy Replay wall-clock logs."""
    parsed_candles = [(_parse_chart_datetime(candle.get("time")), candle) for candle in candles]
    parsed_candles = [(stamp, candle) for stamp, candle in parsed_candles if stamp is not None]
    if not parsed_candles:
        return

    cursor_by_symbol: Dict[str, int] = {}
    for event in events:
        symbol = str(event.get("symbol") or "UNKNOWN").upper()
        matching_indices = [
            index
            for index, (_stamp, candle) in enumerate(parsed_candles)
            if str(candle.get("symbol") or symbol).upper() == symbol
        ]
        if not matching_indices:
            matching_indices = list(range(len(parsed_candles)))
        event_stamp = _parse_chart_datetime(event.get("time"))
        nearest_index = None
        nearest_seconds = None
        if event_stamp is not None:
            for index in matching_indices:
                candle_stamp, _candle = parsed_candles[index]
                distance = abs((candle_stamp - event_stamp).total_seconds())
                if nearest_seconds is None or distance < nearest_seconds:
                    nearest_index = index
                    nearest_seconds = distance

        # Normal live logs are already close to the candle timestamp; snapping
        # removes seconds/timezone formatting differences on Plotly's x-axis.
        if nearest_index is not None and nearest_seconds is not None and nearest_seconds <= 3600:
            event["time"] = parsed_candles[nearest_index][1]["time"]
            cursor_by_symbol[symbol] = nearest_index
            event.pop("_bar_time_inferred", None)
            continue

        # Older Replay decision JSONL used the job's wall clock (often 04:03 ET)
        # rather than simulated bar time. Its logged execution price is the
        # candle close, so align monotonically by closest close price.
        if event.get("_bar_time_inferred"):
            price = _safe_float(event.get("price"))
            if price is not None:
                start_index = max(cursor_by_symbol.get(symbol, 0), 0)
                candidates = []
                for index in matching_indices:
                    if index < start_index:
                        continue
                    close = _safe_float(parsed_candles[index][1].get("close"))
                    if close is not None:
                        candidates.append((abs(close - price), index))
                if candidates:
                    difference, matched_index = min(candidates)
                    if difference <= max(abs(price) * 0.01, 0.25):
                        event["time"] = parsed_candles[matched_index][1]["time"]
                        cursor_by_symbol[symbol] = matched_index
        event.pop("_bar_time_inferred", None)


def chart_payload(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    candles = []
    price_points = []
    decisions = []
    events = []
    event_actions = {"OPEN_LONG", "OPEN_SHORT"}
    seen_candle_times = set()
    seen_price_times = set()

    interval_counts = Counter(
        str(row.get("interval"))
        for row in _decision_rows(rows)
        if row.get("interval")
    )
    decision_interval = interval_counts.most_common(1)[0][0] if interval_counts else None
    selected_candle_rows = [
        row
        for row in _candle_rows(rows)
        if not decision_interval or str(row.get("interval") or "") == decision_interval
    ]
    if not selected_candle_rows:
        selected_candle_rows = _candle_rows(rows)

    for row in selected_candle_rows + [row for row in rows if row.get("row_type") != "candle"]:
        bar_time = row.get("bar_time")
        has_ohlc = all(row.get(k) is not None for k in ("open", "high", "low", "close"))
        if bar_time and has_ohlc and bar_time not in seen_candle_times:
            seen_candle_times.add(bar_time)
            candles.append({
                "time": bar_time,
                "symbol": row.get("symbol"),
                "interval": row.get("interval"),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume"),
            })

        price = row.get("price") or row.get("close")
        if bar_time and price is not None and bar_time not in seen_price_times:
            seen_price_times.add(bar_time)
            price_points.append({
                "time": bar_time,
                "price": price,
                "open": row.get("open"),
                "close": row.get("close"),
                "action": row.get("action"),
                "reason": row.get("reason"),
            })

        if row.get("row_type") == "candle":
            continue

        time_value = bar_time or row.get("time")
        action = row.get("action")
        decisions.append({
            "time": time_value,
            "symbol": row.get("symbol"),
            "action": action,
            "reason": row.get("reason"),
            "reason_group": normalize_reason(row.get("reason") or ""),
            "prob_up": row.get("prob_up_avg") if row.get("prob_up_avg") is not None else row.get("prob_up"),
            "prob_down": (1.0 - row["prob_up_avg"]) if row.get("prob_up_avg") is not None else row.get("prob_down"),
            "price": row.get("price") or row.get("close"),
            "position": row.get("position"),
            "features": row.get("features") or {},
        })

        action_text = str(action or "")
        if (
            action in event_actions
            or action_text.startswith("EXIT_")
            or action_text.startswith("CLOSE_")
            or action_text.startswith("END_OF_DAY_CLOSE")
        ):
            events.append({
                "time": time_value,
                "_bar_time_inferred": bool(row.get("bar_time_inferred")),
                "symbol": row.get("symbol"),
                "interval": row.get("interval"),
                "action": action,
                "reason": row.get("reason"),
                "price": row.get("price") or row.get("close"),
                "prob_up": row.get("prob_up"),
                "prob_down": row.get("prob_down"),
            })

    _align_events_to_candles(events, candles)

    return {
        "candles": candles,
        "price_points": price_points,
        "decisions": decisions,
        "events": events,
    }


def blocked_entries(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    blocked = []
    for row in _decision_rows(rows):
        action = row.get("action")
        reason = row.get("reason") or ""
        prob_up = row.get("prob_up_avg") if row.get("prob_up_avg") is not None else row.get("prob_up")
        prob_down = (1.0 - row["prob_up_avg"]) if row.get("prob_up_avg") is not None else row.get("prob_down")
        model_wanted_long = prob_up is not None and prob_up >= 0.60
        model_wanted_short = prob_down is not None and prob_down >= 0.60

        if action in {"NO_ENTRY_SIGNAL", "COOLDOWN"} and (model_wanted_long or model_wanted_short):
            blocked.append({
                "time": row.get("bar_time") or row.get("time"),
                "symbol": row.get("symbol"),
                "wanted": "LONG" if model_wanted_long else "SHORT",
                "prob_up": prob_up,
                "prob_down": prob_down,
                "blocked_by": normalize_reason(reason),
                "raw_reason": reason,
                "close": row.get("close") or row.get("price"),
                "features": row.get("features") or {},
            })
    return blocked


def feature_series(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    decisions = _decision_rows(rows)
    feature_names = sorted({key for row in decisions for key in (row.get("features") or {}).keys()})
    series = {name: [] for name in feature_names}
    times = []

    for row in decisions:
        times.append(row.get("bar_time") or row.get("time"))
        features = row.get("features") or {}
        for name in feature_names:
            series[name].append(features.get(name))

    return {"times": times, "features": series}
