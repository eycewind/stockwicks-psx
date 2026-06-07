# /var/stockwicks/clients/ashakil/app/services/log_analysis_service.py
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


ACTION_RE = re.compile(
    r"\b("
    r"OPEN_LONG|OPEN_SHORT|HOLD_POSITION|NO_ENTRY_SIGNAL|SAME_BAR_SKIP|COOLDOWN|"
    r"NO_DATA|RESAMPLE_FAILED|INVALID_PRICE|NO_FEATURES|NO_INFER_FEATURES|"
    r"NO_TRAIN_FEATURES|BAD_TRAIN_FEATURES|ONE_CLASS_TRAINING|STALE_FEATURES|"
    r"NO_PROBS|NO_SMOOTHED_PROB|NO_VALID_PROB|PROB_ALIGN_SHORT|OPEN_FAILED|"
    r"EXIT_[A-Z0-9_]+|ERROR(?::[^|\n]*)?"
    r")\b"
)


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
    match = re.match(r"bot_(?:\d+|unknown)_([^_]+)_", path.name)
    return match.group(1) if match else None


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

    if out.get("ts_et") and not out.get("time"):
        out["time"] = out.get("ts_et")
    if out.get("datetime") and not out.get("time"):
        out["time"] = out.get("datetime")
    if not out.get("bar_time"):
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

        features_match = re.search(r"(?:Features|features):\s*(.*)", block)
        if features_match:
            row["features"] = features_match.group(1).strip()

        rows.append(normalize_row(row, path))

    return rows


def find_algo_logs(base_dir: Path) -> List[Path]:
    candidates = []
    for folder in [base_dir / "logs", base_dir / "data"]:
        if folder.exists():
            candidates.extend(folder.rglob("bot_*_Algo*.log"))
            candidates.extend(folder.rglob("bot_*_*_decisions.jsonl"))
            candidates.extend(folder.rglob("bot_*_*_candles.jsonl"))
    return sorted(set(candidates))


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


def load_rows(base_dir: Path, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in find_algo_logs(base_dir):
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
        "BULLISH_BUT_NO_NEW_LONG_CROSS",
        "BEARISH_BUT_NO_NEW_SHORT_CROSS",
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
    }


def chart_payload(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    candles = []
    decisions = []
    events = []
    event_actions = {"OPEN_LONG", "OPEN_SHORT"}
    seen_candle_times = set()

    for row in _candle_rows(rows) + rows:
        bar_time = row.get("bar_time")
        has_ohlc = all(row.get(k) is not None for k in ("open", "high", "low", "close"))
        if bar_time and has_ohlc and bar_time not in seen_candle_times:
            seen_candle_times.add(bar_time)
            candles.append({
                "time": bar_time,
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume"),
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

        if action in event_actions or str(action or "").startswith("EXIT_"):
            events.append({
                "time": time_value,
                "symbol": row.get("symbol"),
                "action": action,
                "reason": row.get("reason"),
                "price": row.get("price") or row.get("close"),
                "prob_up": row.get("prob_up"),
                "prob_down": row.get("prob_down"),
            })

    return {"candles": candles, "decisions": decisions, "events": events}


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
