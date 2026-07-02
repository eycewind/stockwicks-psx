#!/usr/bin/env python3
"""
StockWicks Algo1_MM commercial runner.

Commercial clean model-only behavior:
- Algo1_MM -> Featureset_1
- One probability value drives direction: prob_up.
- Entry uses 3-bar smoothed prob_up crossing the 0.50 midline.
- No cooldown.
- No separate long/short threshold math.
- No OBV / volume / VWAP / candle-pattern external blockers.
- Exits only by guardrails:
    1) hard stop
    2) trailing profit protection
    3) trailing probability stop
    4) optional EOD close

Install target: app/scripts/stock_algos/Algo1_MM.py
"""

import warnings
warnings.filterwarnings("ignore", category=RuntimeWarning)

import os
os.environ["LOKY_MAX_CPU_COUNT"] = "4"

import json
import logging
from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import Any, Dict, List, Optional, Tuple

import joblib
import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.database.connection import SessionLocal
from app.models.paper_trading_bot import (
    PaperStockTradeBot,
    PaperStockBotOpenTrade,
)
from app.scripts.stock_algos.base_wiring import StockBaseRunner, _ET
from app.scripts.research import Featureset_1 as mm2
from app.scripts.ml.mm_live_helpers import predict_probability
from app.scripts.ml.model_refresh_policy import (
    DEFAULT_MIN_NEW_BARS_BEFORE_RETRAIN,
    DEFAULT_MODEL_MAX_AGE_MINUTES,
    DEFAULT_MODEL_REFRESH_MODE,
    load_or_train_model_with_policy,
    normalize_model_refresh_mode,
)
from app.services.paper_trade_service import open_position, close_position
from app.services.mm_core_engine import (
    MMCorePosition,
    MMCoreState,
    config_from_obj,
    evaluate_entry,
    evaluate_exit,
)
from app.utils.client_context import client_root

logger = logging.getLogger("Algo1_MM_Live")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [Algo1_MM_Live] %(message)s",
)

CLIENT_ROOT = str(client_root())
DATA_ROOT = os.getenv("DATA_DIR", os.path.join(CLIENT_ROOT, "data"))
MODEL_DIR = os.getenv("MODEL_DIR", os.path.join(CLIENT_ROOT, "models"))


DEFAULTS = {
    "builder_days": 30,
    "k_forward": 3,
    "model_refresh_mode": DEFAULT_MODEL_REFRESH_MODE,
    "model_max_age_minutes": DEFAULT_MODEL_MAX_AGE_MINUTES,
    "model_max_age_hours": DEFAULT_MODEL_MAX_AGE_MINUTES / 60.0,
    "min_new_bars_before_retrain": DEFAULT_MIN_NEW_BARS_BEFORE_RETRAIN,

    # Simple probability engine.
    # Uses avg of latest probability + previous 2 probabilities.
    "long_entry_prob": 0.60,
    "short_entry_prob": 0.40,
    "min_prob_advantage": 0.0,
    "prob_smoothing_bars": 3,
    "prob_trail_drop": 0.05,
    "prob_exit_mode": "trailing",
    "long_fixed_exit_prob": 0.40,
    "short_fixed_exit_prob": 0.60,

    # GUI-configurable guardrails.
    "hard_stop_usd": 300.0,
    "stop_loss_usd": 300.0,
    "trailing_profit_usd": 75.0,
    "stop_loss_pct": 0.0,
    "trailing_profit_pct": 0.0,
    "trailing_stop_activation": 75.0,
    "trailing_stop_distance": 75.0,
    "eod_close": True,

    # Internal/back-end only.
    "prediction_strategy": "model_only",
    "once_per_bar": True,
    "daily_loss_limit_usd": 5000.0,
}

@dataclass
class BotConfig:
    symbol: str = ""
    algo_name: str = "Algo1_MM"
    feature_set: str = "Featureset_1"

    builder_days: int = DEFAULTS["builder_days"]
    k_forward: int = DEFAULTS["k_forward"]
    model_refresh_mode: str = DEFAULTS["model_refresh_mode"]
    model_max_age_minutes: float = DEFAULTS["model_max_age_minutes"]
    model_max_age_hours: float = DEFAULTS["model_max_age_hours"]
    min_new_bars_before_retrain: int = DEFAULTS["min_new_bars_before_retrain"]

    # Simple probability engine.
    long_entry_prob: float = DEFAULTS["long_entry_prob"]
    short_entry_prob: float = DEFAULTS["short_entry_prob"]
    min_prob_advantage: float = DEFAULTS["min_prob_advantage"]
    prob_smoothing_bars: int = DEFAULTS["prob_smoothing_bars"]
    prob_trail_drop: float = DEFAULTS["prob_trail_drop"]
    prob_exit_mode: str = DEFAULTS["prob_exit_mode"]
    long_fixed_exit_prob: float = DEFAULTS["long_fixed_exit_prob"]
    short_fixed_exit_prob: float = DEFAULTS["short_fixed_exit_prob"]

    # Guardrails.
    hard_stop_usd: float = DEFAULTS["hard_stop_usd"]
    stop_loss_usd: float = DEFAULTS["stop_loss_usd"]
    trailing_profit_usd: float = DEFAULTS["trailing_profit_usd"]
    stop_loss_pct: float = DEFAULTS["stop_loss_pct"]
    trailing_profit_pct: float = DEFAULTS["trailing_profit_pct"]
    trailing_stop_activation: float = DEFAULTS["trailing_stop_activation"]
    trailing_stop_distance: float = DEFAULTS["trailing_stop_distance"]
    eod_close: bool = DEFAULTS["eod_close"]

    # Internal/back-end only.
    prediction_strategy: str = DEFAULTS["prediction_strategy"]
    once_per_bar: bool = DEFAULTS["once_per_bar"]
    daily_loss_limit_usd: float = DEFAULTS["daily_loss_limit_usd"]


# In-memory state. This resets if the worker restarts. Price/hard stops still work.
_PROB_PEAK: Dict[int, float] = {}
_PROFIT_PEAK: Dict[int, float] = {}
_LAST_BAR_TS: Dict[int, str] = {}
_CONFIG_LOGGED_BOT_IDS: set[int] = set()


# -------------------- Small helpers --------------------

def _safe_float(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return float(default)
        v = float(value)
        return v if np.isfinite(v) else float(default)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int) -> int:
    try:
        if value is None or value == "":
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def _safe_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "y", "on", "enabled"}:
        return True
    if s in {"0", "false", "no", "n", "off", "disabled"}:
        return False
    return bool(default)


def _parse_json_field(value: Any) -> dict:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
            return loaded if isinstance(loaded, dict) else {}
        except Exception:
            return {}
    return {}


def _as_et_aware(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    try:
        if isinstance(ts, pd.Timestamp):
            ts = ts.to_pydatetime()
        if not isinstance(ts, datetime):
            ts = pd.to_datetime(ts).to_pydatetime()
        if ts.tzinfo is None:
            if hasattr(_ET, "localize"):
                return _ET.localize(ts)
            return ts.replace(tzinfo=_ET)
        return ts.astimezone(_ET)
    except Exception:
        return None


def _fmt_est(ts: Any) -> str:
    dt = _as_et_aware(ts)
    if dt is None:
        return "N/A"
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def _align_timestamp_for_subtract(left: Any, right: Any) -> tuple[pd.Timestamp, pd.Timestamp]:
    left_ts = pd.Timestamp(left)
    right_ts = pd.Timestamp(right)
    if left_ts.tzinfo is not None and right_ts.tzinfo is None:
        right_ts = right_ts.tz_localize(left_ts.tz)
    elif left_ts.tzinfo is None and right_ts.tzinfo is not None:
        right_ts = right_ts.tz_convert(_ET).tz_localize(None)
    elif left_ts.tzinfo is not None and right_ts.tzinfo is not None:
        right_ts = right_ts.tz_convert(left_ts.tz)
    return left_ts, right_ts


def _fetch_source_bars_for_bot(runner: StockBaseRunner, bot: PaperStockTradeBot, interval: str, cfg: BotConfig) -> pd.DataFrame:
    kwargs = {
        "interval": interval,
        "lookback_days": cfg.builder_days,
        "user_id": bot.user_id,
    }
    for drop_key in (None, "user_id", "lookback_days", "interval"):
        if drop_key:
            kwargs.pop(drop_key, None)
        try:
            return runner.fetch_source_bars(bot.symbol, **kwargs)
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc):
                raise
    return runner.fetch_source_bars(bot.symbol)


def _bot_json_config(bot: PaperStockTradeBot) -> dict:
    merged = {}
    for field in ("config_json", "settings", "params", "note"):
        if hasattr(bot, field):
            merged.update(_parse_json_field(getattr(bot, field)))
    return merged


def _resolve_algo_and_feature_set(bot: PaperStockTradeBot, js: dict) -> tuple[str, str]:
    """
    Fixed explicit pairing requested by StockWicks:
      Algo1_MM calls Featureset_1
    """
    return "Algo1_MM", "Featureset_1"


def _load_bot_config(bot: PaperStockTradeBot) -> BotConfig:
    js = _bot_json_config(bot)
    cfg = BotConfig()
    cfg.symbol = str(getattr(bot, "symbol", "") or "")
    cfg.algo_name, cfg.feature_set = _resolve_algo_and_feature_set(bot, js)

    cfg.builder_days = _safe_int(js.get("builder_days", cfg.builder_days), cfg.builder_days)
    cfg.k_forward = _safe_int(js.get("k_forward", cfg.k_forward), cfg.k_forward)
    cfg.model_refresh_mode = normalize_model_refresh_mode(js.get("model_refresh_mode", cfg.model_refresh_mode))
    cfg.model_max_age_minutes = _safe_float(
        js.get("model_max_age_minutes", cfg.model_max_age_minutes),
        cfg.model_max_age_minutes,
    )
    cfg.model_max_age_hours = _safe_float(js.get("model_max_age_hours", cfg.model_max_age_hours), cfg.model_max_age_hours)
    if "model_max_age_minutes" not in js and "model_max_age_hours" in js:
        cfg.model_max_age_minutes = max(0.0, cfg.model_max_age_hours * 60.0)
    cfg.min_new_bars_before_retrain = max(
        0,
        _safe_int(
            js.get("min_new_bars_before_retrain", cfg.min_new_bars_before_retrain),
            cfg.min_new_bars_before_retrain,
        ),
    )

    cfg.long_entry_prob = _safe_float(
        js.get("long_entry_prob", js.get("entry_prob_long", cfg.long_entry_prob)),
        cfg.long_entry_prob,
    )
    cfg.short_entry_prob = _safe_float(
        js.get("short_entry_prob", js.get("entry_prob_short", cfg.short_entry_prob)),
        cfg.short_entry_prob,
    )
    cfg.min_prob_advantage = _safe_float(js.get("min_prob_advantage", cfg.min_prob_advantage), cfg.min_prob_advantage)
    cfg.prob_smoothing_bars = max(1, _safe_int(js.get("prob_smoothing_bars", cfg.prob_smoothing_bars), cfg.prob_smoothing_bars))
    cfg.prob_trail_drop = _safe_float(js.get("prob_trail_drop", cfg.prob_trail_drop), cfg.prob_trail_drop)
    cfg.prob_exit_mode = str(js.get("prob_exit_mode", cfg.prob_exit_mode) or cfg.prob_exit_mode).strip().lower()
    if cfg.prob_exit_mode not in {"trailing", "fixed"}:
        cfg.prob_exit_mode = DEFAULTS["prob_exit_mode"]
    cfg.long_fixed_exit_prob = _safe_float(
        js.get("long_fixed_exit_prob", js.get("prob_fixed_exit_prob", cfg.long_fixed_exit_prob)),
        cfg.long_fixed_exit_prob,
    )
    cfg.short_fixed_exit_prob = _safe_float(
        js.get("short_fixed_exit_prob", js.get("prob_fixed_exit_prob", cfg.short_fixed_exit_prob)),
        cfg.short_fixed_exit_prob,
    )

    cfg.stop_loss_usd = _safe_float(js.get("stop_loss_usd", js.get("hard_stop_usd", cfg.stop_loss_usd)), cfg.stop_loss_usd)
    cfg.hard_stop_usd = cfg.stop_loss_usd
    cfg.trailing_profit_usd = _safe_float(js.get("trailing_profit_usd", js.get("trailing_stop_distance", js.get("trailing_stop_activation", cfg.trailing_profit_usd))), cfg.trailing_profit_usd)
    cfg.trailing_stop_activation = cfg.trailing_profit_usd
    cfg.trailing_stop_distance = cfg.trailing_profit_usd
    cfg.stop_loss_pct = _safe_float(js.get("stop_loss_pct", js.get("per_share_stop_pct", cfg.stop_loss_pct)), cfg.stop_loss_pct)
    cfg.trailing_profit_pct = _safe_float(
        js.get("trailing_profit_pct", js.get("per_share_trailing_profit_pct", cfg.trailing_profit_pct)),
        cfg.trailing_profit_pct,
    )
    cfg.eod_close = _safe_bool(js.get("eod_close", js.get("eod_auto_close", cfg.eod_close)), cfg.eod_close)

    # Direct bot columns override defaults where present.
    for attr, target, kind in [
        ("hard_stop_usd", "stop_loss_usd", "float"),
        ("stop_loss_usd", "stop_loss_usd", "float"),
        ("trailing_profit_usd", "trailing_profit_usd", "float"),
        ("eod_auto_close", "eod_close", "bool"),
        ("eod_close", "eod_close", "bool"),
    ]:
        if hasattr(bot, attr) and getattr(bot, attr) is not None:
            val = getattr(bot, attr)
            if kind == "float":
                setattr(cfg, target, _safe_float(val, getattr(cfg, target)))
                if target == "stop_loss_usd":
                    cfg.hard_stop_usd = cfg.stop_loss_usd
                if target == "trailing_profit_usd":
                    cfg.trailing_stop_activation = cfg.trailing_profit_usd
                    cfg.trailing_stop_distance = cfg.trailing_profit_usd
            elif kind == "bool":
                setattr(cfg, target, _safe_bool(val, getattr(cfg, target)))

    # Force model-only behavior. Old config keys such as cooldown, volume gate,
    # OBV gate, min advantage, separate long/short thresholds, probability-floor
    # exits, and opposite-signal exits are intentionally ignored.
    cfg.prediction_strategy = "model_only"

    bot_id = int(getattr(bot, "id", 0) or 0)
    if bot_id not in _CONFIG_LOGGED_BOT_IDS:
        _CONFIG_LOGGED_BOT_IDS.add(bot_id)
        logger.warning(
            "[LIVE CONFIG APPLIED] bot_id=%s algo=%s long_entry_prob=%.3f "
            "short_entry_prob=%.3f prob_exit_mode=%s prob_trail_drop=%.3f "
            "long_fixed_exit_prob=%.3f short_fixed_exit_prob=%.3f hard_stop_usd=%.2f "
            "trailing_stop_activation=%.2f trailing_stop_distance=%.2f "
            "model_refresh_mode=%s model_max_age_minutes=%.1f min_new_bars_before_retrain=%s",
            bot_id,
            cfg.algo_name,
            float(cfg.long_entry_prob or 0.0),
            float(cfg.short_entry_prob or 0.0),
            cfg.prob_exit_mode,
            float(cfg.prob_trail_drop or 0.0),
            float(cfg.long_fixed_exit_prob or 0.0),
            float(cfg.short_fixed_exit_prob or 0.0),
            float(cfg.hard_stop_usd or 0.0),
            float(cfg.trailing_stop_activation or 0.0),
            float(cfg.trailing_stop_distance or 0.0),
            cfg.model_refresh_mode,
            float(cfg.model_max_age_minutes or 0.0),
            int(cfg.min_new_bars_before_retrain or 0),
        )

    return cfg


def _prepare_X(feat_df: pd.DataFrame, feat_names: List[str]) -> pd.DataFrame:
    X = feat_df.reindex(columns=feat_names).copy()
    X = X.replace([np.inf, -np.inf], np.nan).ffill().bfill()
    med = X.median(numeric_only=True)
    X = X.fillna(med)
    all_nan = [c for c in X.columns if X[c].isna().all()]
    if all_nan:
        X[all_nan] = 0.0
    return X.fillna(0.0).astype(float)


def _model_path(cfg: BotConfig, symbol: str, interval: str) -> str:
    safe_symbol = str(symbol).replace("/", "_").replace(" ", "_").upper()
    safe_interval = str(interval).replace("/", "_").replace(" ", "_")
    filename = f"mm_{cfg.algo_name}_{cfg.feature_set}_{safe_symbol}_{safe_interval}_k{cfg.k_forward}.joblib"
    return os.path.join(MODEL_DIR, filename)


def _model_is_stale(path: str, max_age_hours: float) -> bool:
    if not os.path.exists(path):
        return True
    if max_age_hours is None or max_age_hours <= 0:
        return False
    age_sec = datetime.now().timestamp() - os.path.getmtime(path)
    return age_sec > float(max_age_hours) * 3600.0


def _load_or_train_model(
    model_path: str,
    symbol: str,
    interval: str,
    feat_df: pd.DataFrame,
    feat_names: List[str],
    cfg: BotConfig,
):
    return load_or_train_model_with_policy(
        model_path=model_path,
        symbol=symbol,
        interval=interval,
        feat_df=feat_df,
        feat_names=feat_names,
        cfg=cfg,
        prepare_X=_prepare_X,
        logger=logger,
    )


# -------------------- Logging --------------------

def _round2(v: Any) -> Any:
    try:
        if v is None:
            return None
        if isinstance(v, (np.floating, np.integer)):
            v = v.item()
        if isinstance(v, (int, float)):
            if np.isfinite(float(v)):
                return round(float(v), 2)
            return None
    except Exception:
        pass
    return v


def _fmt2(v: Any) -> str:
    try:
        if v is None:
            return "N/A"
        if isinstance(v, (np.floating, np.integer)):
            v = v.item()
        if isinstance(v, (int, float)) and np.isfinite(float(v)):
            return f"{float(v):.2f}"
    except Exception:
        pass
    return str(v)


def _round_log_value(v: Any) -> Any:
    try:
        if v is None:
            return None
        if isinstance(v, (np.floating, np.integer)):
            v = v.item()
        if isinstance(v, float):
            if np.isfinite(v):
                return round(v, 2)
            return None
        if isinstance(v, int):
            return v
        if isinstance(v, dict):
            return {k: _round_log_value(val) for k, val in v.items()}
        if isinstance(v, list):
            return [_round_log_value(x) for x in v]
    except Exception:
        pass
    return v


def _fmt_log_value(v: Any) -> str:
    try:
        if v is None:
            return "N/A"
        if isinstance(v, (np.floating, np.integer)):
            v = v.item()
        if isinstance(v, (int, float)) and np.isfinite(float(v)):
            return f"{float(v):.2f}"
    except Exception:
        pass
    return str(v)


def _json_safe(v: Any) -> Any:
    try:
        if isinstance(v, (np.floating, np.integer)):
            return v.item()
        if isinstance(v, (pd.Timestamp, datetime)):
            return _fmt_est(v)
        if isinstance(v, np.ndarray):
            return v.tolist()
        if pd.isna(v):
            return None
    except Exception:
        pass
    return v


def _append_jsonl(path: str, row: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=_json_safe, separators=(",", ":")) + "\n")
    except Exception:
        logger.warning("Failed writing jsonl %s", path, exc_info=True)


def _safe_symbol_for_files(symbol: str) -> str:
    return str(symbol or "").upper().strip().replace("/", "_").replace(" ", "_")


def _log_candle_jsonl(log_dir: str, bot: PaperStockTradeBot, interval: str, df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    row = df.iloc[-1]
    ts = row.name
    ts_et = _fmt_est(ts)
    path = os.path.join(log_dir, f"bot_{bot.id}_{_safe_symbol_for_files(bot.symbol)}_{interval}_candles.jsonl")
    _append_jsonl(
        path,
        {
            "ts_et": ts_et,
            "bot_id": bot.id,
            "symbol": bot.symbol,
            "interval": interval,
            "open": float(row.get("open", 0.0)),
            "high": float(row.get("high", 0.0)),
            "low": float(row.get("low", 0.0)),
            "close": float(row.get("close", 0.0)),
            "volume": float(row.get("volume", 0.0)),
        },
    )


def log_trade_decision(
    log_file: str,
    decision: str,
    prob_up: float,
    prob_down: float,
    data_len: int,
    bar_close_px: float,
    bar_open_px: float,
    open_trade: Optional[PaperStockBotOpenTrade] = None,
    thresholds: Optional[dict] = None,
    model_path: str = "",
    symbol: str = "",
    interval: str = "",
    cfg: Optional[BotConfig] = None,
    reason: str = "",
    df: Optional[pd.DataFrame] = None,
    X: Optional[Any] = None,
    feat_cols: Optional[List[str]] = None,
    prob_up_avg: Optional[float] = None,
    prob_up_avg_prev: Optional[float] = None,
):
    ts_est = datetime.now(_ET)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    feature_snapshot = {}
    if X is not None and feat_cols:
        try:
            last = X.iloc[-1] if isinstance(X, pd.DataFrame) else pd.Series(X[-1], index=feat_cols)
            for c in feat_cols:
                feature_snapshot[c] = _round_log_value(last.get(c, 0.0))
        except Exception:
            feature_snapshot = {}

    position = "FLAT"
    if open_trade:
        position = f"{str(open_trade.position_side).upper()} @ {float(open_trade.entry_price or 0.0):.2f}"

    # Human-readable legacy log.
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 88 + "\n")
            f.write(f"{_fmt_est(ts_est)} | {symbol} {interval} | {decision} | {reason}\n")
            if cfg:
                f.write(
                    f"algo={cfg.algo_name} feature_set={cfg.feature_set} "
                    f"long_entry={cfg.long_entry_prob:.2f} "
                    f"short_entry={cfg.short_entry_prob:.2f} "
                    f"smoothing={cfg.prob_smoothing_bars} "
                    f"prob_exit_mode={cfg.prob_exit_mode} "
                    f"prob_trail_drop={cfg.prob_trail_drop:.3f} "
                    f"long_fixed_exit_prob={cfg.long_fixed_exit_prob:.3f} "
                    f"short_fixed_exit_prob={cfg.short_fixed_exit_prob:.3f}\n"
                )
            f.write(
                f"prob_up={_fmt_log_value(prob_up)} prob_down={_fmt_log_value(prob_down)} "
                f"prob_up_avg={_fmt_log_value(prob_up_avg)} "
                f"prev_avg={_fmt_log_value(prob_up_avg_prev)}\n"
            )
            f.write(f"close={_fmt_log_value(bar_close_px)} open={_fmt_log_value(bar_open_px)} rows={data_len} position={position}\n")
            f.write(f"model={model_path}\n")
            if feature_snapshot:
                f.write("features=" + json.dumps(feature_snapshot, default=_json_safe) + "\n")
    except Exception:
        logger.warning("Failed writing text log %s", log_file, exc_info=True)

    if cfg:
        log_dir = os.path.dirname(log_file)
        safe_symbol = _safe_symbol_for_files(symbol)
        decision_path = os.path.join(log_dir, f"bot_{safe_symbol}_{interval}_decisions_unkeyed.jsonl")
        if open_trade is not None:
            bot_id_for_name = getattr(open_trade, "bot_id", "unknown")
        else:
            # Parse bot id from log filename fallback: bot_{id}_{symbol}_...
            try:
                bot_id_for_name = os.path.basename(log_file).split("_")[1]
            except Exception:
                bot_id_for_name = "unknown"

        decision_path = os.path.join(log_dir, f"bot_{bot_id_for_name}_{safe_symbol}_{cfg.algo_name}_decisions.jsonl")
        _append_jsonl(
            decision_path,
            {
                "ts_et": _fmt_est(ts_est),
                "bot_id": bot_id_for_name,
                "symbol": symbol,
                "interval": interval,
                "algo": cfg.algo_name,
                "feature_set": cfg.feature_set,
                "features_used": len(feat_cols or []),
                "action": decision,
                "reason": reason,
                "entry_rule": (f"PROB_AVG_{cfg.prob_smoothing_bars}_CROSS_LONG_{cfg.long_entry_prob:.2f}_SHORT_{cfg.short_entry_prob:.2f}"),
                "exit_rules": ["HARD_STOP", "TRAILING_PROFIT", "PROB_TRAIL_OR_FIXED", "EOD_OPTIONAL"],
                "prob_up": _round_log_value(prob_up),
                "prob_down": _round_log_value(prob_down),
                "prob_up_avg": _round_log_value(prob_up_avg),
                "prob_up_avg_prev": _round_log_value(prob_up_avg_prev),
                "close": _round_log_value(bar_close_px),
                "open": _round_log_value(bar_open_px),
                "position": position,
                "model_path": model_path,
                "features": _round_log_value(feature_snapshot),
            },
        )


# -------------------- Trading logic --------------------

def get_smart_execution_price(df: pd.DataFrame, execution_type: str = "close") -> float:
    try:
        if df is None or df.empty:
            return 0.0
        return float(df.iloc[-1].get("close", 0.0))
    except Exception:
        return 0.0


def _execute_order(
    db: Session,
    bot: PaperStockTradeBot,
    side: str,
    qty: float,
    price: float | None,
    actor: str,
    symbol: str | None = None,
):
    symbol = symbol or bot.symbol
    price = float(price or 0.0)
    position_side = "long" if side.upper() == "BUY" else "short"

    logger.info(
        "EXEC_ORDER: bot=%s symbol=%s side=%s position_side=%s qty=%.2f price=%.4f actor=%s",
        bot.id, symbol, side, position_side, float(qty or 0.0), price, actor,
    )
    return open_position(db, bot, position_side, price)


def _cleanup_exit_state(bot_id: int) -> None:
    _PROB_PEAK.pop(int(bot_id), None)
    _PROFIT_PEAK.pop(int(bot_id), None)


def _position_pnl(open_trade: PaperStockBotOpenTrade, current_price: float) -> tuple[float, str, float, float]:
    side = str(open_trade.position_side or "").lower()
    entry = float(open_trade.entry_price or 0.0)
    qty = float(open_trade.quantity or 0.0)

    if side == "long":
        pnl = (current_price - entry) * qty
        per_share_loss = entry - current_price
    else:
        pnl = (entry - current_price) * qty
        per_share_loss = current_price - entry

    return pnl, side, entry, per_share_loss


def should_enter_trade(
    prob_up_avg: float,
    prob_up_avg_prev: Optional[float],
    cfg: BotConfig,
    allow_short: bool = True,
) -> Tuple[bool, str, str]:
    decision = evaluate_entry(
        prob_up_avg=prob_up_avg,
        prob_up_avg_prev=prob_up_avg_prev,
        cfg=config_from_obj(cfg, allow_short=allow_short),
    )
    return decision.should_act, decision.action, decision.reason


def should_exit_trade(
    open_trade: PaperStockBotOpenTrade,
    current_price: float,
    prob_up_avg: float,
    cfg: BotConfig,
    now_et: Optional[datetime] = None,
) -> Tuple[bool, str]:
    if not open_trade:
        return False, ""

    bot_id = int(open_trade.bot_id)
    side = str(open_trade.position_side or "").lower()
    conviction = prob_up_avg if side == "long" else 1.0 - prob_up_avg
    state = MMCoreState(
        prob_peak=float(_PROB_PEAK.get(bot_id, conviction)),
        profit_peak=float(_PROFIT_PEAK.get(bot_id, 0.0)),
    )
    decision = evaluate_exit(
        MMCorePosition(
            side=side,
            entry_price=float(open_trade.entry_price or 0.0),
            quantity=float(open_trade.quantity or 0.0),
        ),
        current_price=float(current_price or 0.0),
        prob_up_avg=float(prob_up_avg),
        cfg=config_from_obj(cfg),
        state=state,
        now_et=_as_et_aware(now_et or datetime.now(_ET)),
    )
    next_state = decision.state or state
    if decision.should_act:
        _cleanup_exit_state(bot_id)
    else:
        _PROB_PEAK[bot_id] = float(next_state.prob_peak or 0.0)
        _PROFIT_PEAK[bot_id] = float(next_state.profit_peak or 0.0)
    return decision.should_act, decision.reason


# -------------------- Main tick --------------------

def run_algoMM_bot_tick(
    bot_id: int,
    anchor_dt: Optional[datetime] = None,
    _backfill: bool = False,
):
    db: Session = SessionLocal()
    runner = StockBaseRunner()

    decision = "NONE"
    reason = ""
    data_len = 0
    prob_up = 0.0
    prob_down = 0.0
    prob_up_avg = None
    prob_up_avg_prev = None
    bar_close_px = 0.0
    bar_open_px = 0.0
    open_trade = None
    bot = None
    log_file = ""
    model_path = ""
    X = None
    feat_names: List[str] = []
    cfg = None

    try:
        bot = db.query(PaperStockTradeBot).filter_by(id=bot_id).first()
        if not bot:
            logger.warning("Bot %s not found", bot_id)
            return

        cfg = _load_bot_config(bot)

        if anchor_dt is not None:
            now_et = _as_et_aware(anchor_dt) or datetime.now(_ET)
        else:
            now_et = datetime.now(_ET)

        interval = bot.interval or "1min"
        log_dir = os.path.join(DATA_ROOT, str(bot.user_id))
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f"bot_{bot.id}_{_safe_symbol_for_files(bot.symbol)}_{cfg.algo_name}.log")

        df_raw = _fetch_source_bars_for_bot(runner, bot, interval, cfg)
        if df_raw is None or df_raw.empty:
            decision = "NO_DATA"
            reason = "RAW_DF_EMPTY"
            log_trade_decision(log_file, decision, prob_up, prob_down, data_len, bar_close_px, bar_open_px, cfg=cfg, reason=reason)
            return

        latest_time, _, df = runner.resample_interval(df_raw, interval, bot.symbol)
        if latest_time is None or df is None or df.empty:
            decision = "RESAMPLE_FAILED"
            reason = "RESAMPLE_EMPTY"
            log_trade_decision(log_file, decision, prob_up, prob_down, data_len, bar_close_px, bar_open_px, cfg=cfg, reason=reason)
            return

        df = df.sort_index()

        if anchor_dt is not None:
            anchor_et = _as_et_aware(anchor_dt)
            if anchor_et is not None:
                anchor_ts = pd.Timestamp(anchor_et)
                if df.index.tz is None:
                    anchor_ts = anchor_ts.tz_localize(None)
                else:
                    anchor_ts = anchor_ts.tz_convert(df.index.tz)
                df = df[df.index <= anchor_ts].sort_index()
                if df.empty:
                    decision = "NO_ANCHOR_DATA"
                    reason = f"NO_BARS_UP_TO_{anchor_ts}"
                    log_trade_decision(log_file, decision, prob_up, prob_down, data_len, bar_close_px, bar_open_px, cfg=cfg, reason=reason)
                    return

        # Live should behave like replay when the scheduler wakes up after one
        # or more closed candles: process each missed bar sequentially instead
        # of jumping straight to the newest snapshot.
        if not _backfill and anchor_dt is None:
            last_seen = _LAST_BAR_TS.get(bot.id)
            if last_seen:
                try:
                    last_ts = pd.Timestamp(last_seen)
                    if df.index.tz is not None and last_ts.tzinfo is None:
                        last_ts = last_ts.tz_localize(df.index.tz)
                    elif df.index.tz is None and last_ts.tzinfo is not None:
                        last_ts = last_ts.tz_convert(_ET).tz_localize(None)
                    pending = [ts for ts in df.index if pd.Timestamp(ts) > last_ts]
                except Exception:
                    pending = []
                if len(pending) > 1:
                    max_backfill_bars = 10
                    for ts in pending[-max_backfill_bars:]:
                        run_algoMM_bot_tick(bot_id, anchor_dt=ts, _backfill=True)
                    return

        data_len = len(df)
        bar_close_px = float(df["close"].iloc[-1])
        bar_open_px = float(df["open"].iloc[-1])
        _log_candle_jsonl(log_dir, bot, interval, df)

        if bar_close_px <= 0 or not np.isfinite(bar_close_px):
            decision = "INVALID_PRICE"
            reason = "NON_POSITIVE_OR_NAN_PRICE"
            log_trade_decision(log_file, decision, prob_up, prob_down, data_len, bar_close_px, bar_open_px, cfg=cfg, reason=reason, df=df)
            return

        if not hasattr(mm2, "build_feature_matrix_from_df") or not hasattr(mm2, "build_training_features_from_df"):
            decision = "FEATURE_BUILDER_UNSAFE"
            reason = "algomm_feature_sets missing live-safe builders"
            log_trade_decision(log_file, decision, prob_up, prob_down, data_len, bar_close_px, bar_open_px, cfg=cfg, reason=reason, df=df)
            return

        infer_feat_df = mm2.build_feature_matrix_from_df(
            df=df,
            symbol=bot.symbol,
            interval=interval,
            feature_set=cfg.feature_set,
        )
        train_feat_df = mm2.build_training_features_from_df(
            df=df,
            symbol=bot.symbol,
            interval=interval,
            k_forward=cfg.k_forward,
            feature_set=cfg.feature_set,
        )

        if infer_feat_df is None or infer_feat_df.empty or len(infer_feat_df) < cfg.prob_smoothing_bars + 1:
            decision = "NO_INFER_FEATURES"
            reason = f"INFER_FEATURE_DF_TOO_SHORT rows={0 if infer_feat_df is None else len(infer_feat_df)}"
            log_trade_decision(log_file, decision, prob_up, prob_down, data_len, bar_close_px, bar_open_px, cfg=cfg, reason=reason, df=df)
            return

        if train_feat_df is None or train_feat_df.empty or len(train_feat_df) < 30:
            decision = "NO_TRAIN_FEATURES"
            reason = f"TRAIN_FEATURE_DF_TOO_SHORT rows={0 if train_feat_df is None else len(train_feat_df)}"
            log_trade_decision(log_file, decision, prob_up, prob_down, data_len, bar_close_px, bar_open_px, cfg=cfg, reason=reason, df=df, X=infer_feat_df, feat_cols=list(infer_feat_df.columns))
            return

        if "y" not in train_feat_df.columns or "w" not in train_feat_df.columns:
            decision = "BAD_TRAIN_FEATURES"
            reason = "MISSING_y_OR_w"
            log_trade_decision(log_file, decision, prob_up, prob_down, data_len, bar_close_px, bar_open_px, cfg=cfg, reason=reason, df=df, X=infer_feat_df, feat_cols=list(infer_feat_df.columns))
            return

        if train_feat_df["y"].nunique() < 2:
            decision = "ONE_CLASS_TRAINING"
            reason = "ONLY_ONE_CLASS_IN_TRAIN_FEATURES"
            log_trade_decision(log_file, decision, prob_up, prob_down, data_len, bar_close_px, bar_open_px, cfg=cfg, reason=reason, df=df, X=infer_feat_df, feat_cols=list(infer_feat_df.columns))
            return

        if hasattr(mm2, "get_feature_columns"):
            default_feat_names = mm2.get_feature_columns(cfg.feature_set)
        else:
            default_feat_names = [c for c in train_feat_df.columns if c not in ("y", "w")]

        model_path = _model_path(cfg, bot.symbol, interval)

        model, feat_names = _load_or_train_model(
            model_path=model_path,
            symbol=bot.symbol,
            interval=interval,
            feat_df=train_feat_df,
            feat_names=list(default_feat_names),
            cfg=cfg,
        )

        X = _prepare_X(infer_feat_df, feat_names)

        try:
            latest_price_ts, latest_feat_ts = _align_timestamp_for_subtract(df.index[-1], X.index[-1])
            feature_lag_sec = (latest_price_ts - latest_feat_ts).total_seconds()
        except Exception:
            feature_lag_sec = None

        if feature_lag_sec is not None and abs(feature_lag_sec) > 60 * 10:
            decision = "STALE_FEATURES"
            reason = f"FEATURE_LAG_{int(feature_lag_sec)}s price_ts={df.index[-1]} feat_ts={X.index[-1]}"
            log_trade_decision(log_file, decision, prob_up, prob_down, data_len, bar_close_px, bar_open_px, cfg=cfg, reason=reason, df=df, X=X, feat_cols=feat_names)
            return

        probs = predict_probability(model, X, feat_names)
        if probs is None or len(probs) < cfg.prob_smoothing_bars + 1:
            decision = "NO_PROBS"
            reason = f"PREDICT_PROBS_TOO_SHORT rows={0 if probs is None else len(probs)}"
            log_trade_decision(log_file, decision, prob_up, prob_down, data_len, bar_close_px, bar_open_px, cfg=cfg, reason=reason, df=df, X=X, feat_cols=feat_names)
            return

        prob_raw = pd.Series(probs, index=X.index, name="prob_up").astype(float)
        prob_avg = prob_raw.rolling(window=cfg.prob_smoothing_bars, min_periods=cfg.prob_smoothing_bars).mean().dropna()
        if len(prob_avg) < 2:
            decision = "NO_SMOOTHED_PROB"
            reason = "NEED_PREVIOUS_AND_CURRENT_PROB_AVG"
            log_trade_decision(log_file, decision, prob_up, prob_down, data_len, bar_close_px, bar_open_px, cfg=cfg, reason=reason, df=df, X=X, feat_cols=feat_names)
            return

        prob_up = float(prob_raw.iloc[-1])
        prob_down = 1.0 - prob_up
        prob_up_avg = float(prob_avg.iloc[-1])
        prob_up_avg_prev = float(prob_avg.iloc[-2])

        if not np.isfinite(prob_up_avg) or not np.isfinite(prob_up_avg_prev):
            decision = "NO_VALID_PROB"
            reason = "PROB_AVG_NAN_OR_INF"
            log_trade_decision(log_file, decision, prob_up, prob_down, data_len, bar_close_px, bar_open_px, cfg=cfg, reason=reason, df=df, X=X, feat_cols=feat_names, prob_up_avg=prob_up_avg, prob_up_avg_prev=prob_up_avg_prev)
            return

        open_trade = db.query(PaperStockBotOpenTrade).filter_by(bot_id=bot.id).first()

        # No cooldown. only once-per-bar duplicate protection for exact repeated ticks.
        bar_key = str(df.index[-1])
        if cfg.once_per_bar and open_trade is None and _LAST_BAR_TS.get(bot.id) == bar_key:
            decision = "SAME_BAR_SKIP"
            reason = f"ALREADY_EVALUATED_BAR_{bar_key}"
            log_trade_decision(log_file, decision, prob_up, prob_down, data_len, bar_close_px, bar_open_px, open_trade, model_path=model_path, symbol=bot.symbol, interval=interval, cfg=cfg, reason=reason, df=df, X=X, feat_cols=feat_names, prob_up_avg=prob_up_avg, prob_up_avg_prev=prob_up_avg_prev)
            return
        _LAST_BAR_TS[bot.id] = bar_key

        if open_trade is None:
            allow_short = bool(getattr(bot, "allow_short_selling", True))
            should_enter, enter_direction, entry_reason = should_enter_trade(
                prob_up_avg=prob_up_avg,
                prob_up_avg_prev=prob_up_avg_prev,
                cfg=cfg,
                allow_short=allow_short,
            )

            if not should_enter:
                decision = "NO_ENTRY_SIGNAL"
                reason = entry_reason
            else:
                qty = float(getattr(bot, "trade_size", 0.0) or 0.0)
                exec_price = get_smart_execution_price(df)
                side = "BUY" if enter_direction == "LONG" else "SELL"
                _execute_order(
                    db=db,
                    bot=bot,
                    side=side,
                    qty=qty,
                    price=exec_price,
                    actor=f"{cfg.algo_name}_OPEN_{enter_direction}",
                    symbol=bot.symbol,
                )
                db.commit()

                open_trade = db.query(PaperStockBotOpenTrade).filter_by(bot_id=bot.id).first()
                if open_trade is None:
                    decision = "OPEN_FAILED"
                    reason = "open_position_returned_none_or_no_open_trade_created"
                else:
                    _cleanup_exit_state(bot.id)
                    conviction = prob_up_avg if enter_direction == "LONG" else 1.0 - prob_up_avg
                    _PROB_PEAK[bot.id] = float(conviction)
                    _PROFIT_PEAK[bot.id] = 0.0
                    decision = f"OPEN_{enter_direction}"
                    reason = entry_reason

        else:
            should_exit, exit_reason = should_exit_trade(
                open_trade=open_trade,
                current_price=bar_close_px,
                prob_up_avg=prob_up_avg,
                cfg=cfg,
                now_et=now_et,
            )

            if should_exit:
                exec_price = get_smart_execution_price(df)
                close_position(db, open_trade, exec_price)
                db.commit()
                _cleanup_exit_state(bot.id)
                decision = f"EXIT_{exit_reason}"
                reason = exit_reason
                open_trade = None
            else:
                decision = "HOLD_POSITION"
                reason = "GUARDRAILS_NOT_HIT"

        log_trade_decision(
            log_file,
            decision,
            prob_up,
            prob_down,
            data_len,
            bar_close_px,
            bar_open_px,
            open_trade,
            model_path=model_path,
            symbol=bot.symbol,
            interval=interval,
            cfg=cfg,
            reason=reason,
            df=df,
            X=X,
            feat_cols=feat_names,
            prob_up_avg=prob_up_avg,
            prob_up_avg_prev=prob_up_avg_prev,
        )

    except Exception as e:
        db.rollback()
        decision = f"ERROR: {e}"
        reason = f"EXCEPTION: {e}"
        logger.error("[AlgoMM] tick error for bot %s: %s", bot_id, e, exc_info=True)
        if bot:
            log_trade_decision(
                log_file,
                decision,
                prob_up,
                prob_down,
                data_len,
                bar_close_px,
                bar_open_px,
                open_trade,
                cfg=cfg,
                reason=reason,
                df=df if "df" in locals() else None,
                X=X,
                feat_cols=feat_names,
                prob_up_avg=prob_up_avg,
                prob_up_avg_prev=prob_up_avg_prev,
            )
    finally:
        if db:
            db.close()


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 2:
        run_algoMM_bot_tick(int(sys.argv[1]))
    else:
        print("Usage: python -m app.scripts.stock_algos.Algo1_MM <BOT_ID>")
