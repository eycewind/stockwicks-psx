#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/stock_algos/algoMM_replay_runner.py
"""
Algo1_MM / Algo2_MM / Algo3_MM / Algo4_MM / Algo5_MM Replay Runner
=================================

Replay wrapper for the commercial production model-only runners:
  - app.scripts.stock_algos.Algo1_MM  -> Featureset_1
  - app.scripts.stock_algos.Algo2_MM  -> Featureset_2
  - app.scripts.stock_algos.Algo3_MM  -> Featureset_3
  - app.scripts.stock_algos.Algo4_MM  -> Featureset_4
  - app.scripts.stock_algos.Algo5_MM  -> Featureset_5

Goal: mimic production decisions exactly while keeping replay DB/services isolated.
Charting, tables, API responses, and frontend layout are intentionally unchanged.

Replay-only substitutions:
  - PaperStockTradeBot      -> ReplaySession / SimpleNamespace adapter
  - PaperStockBotOpenTrade  -> ReplayOpenTrade / SimpleNamespace adapter
  - open_position           -> open_position_replay
  - close_position          -> close_position_replay
  - live StockBaseRunner    -> ReplayDataProvider visible bars + warmup bars

Important safety note:
  The trading logic, feature builders, model class, probability smoothing, entry,
  and exit logic are delegated to the selected production module. Model files are
  replay-prefixed by session id to avoid overwriting live-production models.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.database.connection import SessionLocal
from app.models.replay import ReplaySession
from app.services.replay_trade_service import (
    open_position_replay,
    close_position_replay,
    update_open_trade_mark_replay,
    get_open_trade,
    get_session_pnl,
)
from app.services.mm_core_engine import (
    MMCorePosition,
    MMCoreState,
    config_from_obj,
    evaluate_entry,
    evaluate_exit,
)
from app.scripts.replay.replay_data_provider import ReplayDataProvider
from app.scripts.stock_algos.base_wiring import _ET
from app.scripts.ml.model_refresh_policy import (
    DEFAULT_MIN_NEW_BARS_BEFORE_RETRAIN,
    DEFAULT_MODEL_MAX_AGE_MINUTES,
    DEFAULT_MODEL_REFRESH_MODE,
    normalize_model_refresh_mode,
)

logger = logging.getLogger("AlgoMM_Replay")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [AlgoMM_Replay] %(message)s",
    )

CLIENT_ROOT = os.getenv("CLIENT_ROOT", "/var/stockwicks/clients/ashakil")
DATA_ROOT = os.getenv("DATA_DIR", os.path.join(CLIENT_ROOT, "data"))
MODEL_DIR = os.getenv("MODEL_DIR", os.path.join(CLIENT_ROOT, "models"))
REPLAY_MODEL_DIR = os.getenv("REPLAY_MODEL_DIR", os.path.join(DATA_ROOT, "replay_models"))

ALLOWED_MM_ALGOS = {
    "Algo1_MM": "app.scripts.stock_algos.Algo1_MM",
    "Algo2_MM": "app.scripts.stock_algos.Algo2_MM",
    "Algo3_MM": "app.scripts.stock_algos.Algo3_MM",
    "Algo4_MM": "app.scripts.stock_algos.Algo4_MM",
    "Algo5_MM": "app.scripts.stock_algos.Algo5_MM",
    "Algo_SMI": "app.scripts.stock_algos.Algo1_MM",
    "Algo_MACD": "app.scripts.stock_algos.Algo1_MM",
}

# Backward compatibility if an older session row says AlgoMM.
DEFAULT_ALGO_NAME = "Algo1_MM"
_CONFIG_LOGGED_SESSION_IDS: set[int] = set()


def _normalize_algo_name(name: Any) -> str:
    algo = str(name or DEFAULT_ALGO_NAME).strip()
    if algo == "AlgoMM":
        return DEFAULT_ALGO_NAME
    if algo in ALLOWED_MM_ALGOS:
        return algo
    raise ValueError(
        f"Unsupported MM replay algo {algo!r}. "
        f"Allowed: {', '.join(sorted(ALLOWED_MM_ALGOS))}"
    )


def _live_module(algo_name: Any):
    algo = _normalize_algo_name(algo_name)
    return importlib.import_module(ALLOWED_MM_ALGOS[algo])


def _parse_json_field(s: Any) -> Dict[str, Any]:
    if isinstance(s, dict):
        return s
    if not isinstance(s, str) or not s.strip():
        return {}
    try:
        loaded = json.loads(s)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        try:
            loaded = json.loads(s.replace("'", '"'))
            return loaded if isinstance(loaded, dict) else {}
        except Exception:
            return {}


def _safe_float(v: Any, default: float) -> float:
    try:
        if v is None or v == "":
            return float(default)
        out = float(v)
        return out if np.isfinite(out) else float(default)
    except Exception:
        return float(default)


def _safe_int(v: Any, default: int) -> int:
    try:
        if v is None or v == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _safe_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in {"1", "true", "yes", "y", "on", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "disabled"}:
            return False
    return bool(default)


def _as_et(ts: Any) -> datetime:
    if isinstance(ts, pd.Timestamp):
        ts = ts.to_pydatetime()
    if ts.tzinfo is None:
        return _ET.localize(ts) if hasattr(_ET, "localize") else ts.replace(tzinfo=_ET)
    return ts.astimezone(_ET)


def _normalize_ohlcv_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy().sort_index()
    if out.index.tz is None:
        out.index = out.index.tz_localize(_ET)
    else:
        out.index = out.index.tz_convert(_ET)
    needed = ["open", "high", "low", "close", "volume"]
    for c in needed:
        if c not in out.columns:
            raise ValueError(f"Replay df missing column: {c}")
    out = out[needed].dropna(subset=["open", "high", "low", "close"]).sort_index()
    if out.index.has_duplicates:
        out = out[~out.index.duplicated(keep="last")]
    return out


def _build_replay_df(provider: ReplayDataProvider, bar_idx: int) -> pd.DataFrame:
    return _normalize_ohlcv_df(provider.bars_up_to(bar_idx))


def _coerce_date_minus_days(value: Any, days: int):
    try:
        if isinstance(value, str):
            dt = pd.to_datetime(value).to_pydatetime()
            return (dt - timedelta(days=int(days))).date()
        if hasattr(value, "date"):
            return value - timedelta(days=int(days))
        return value
    except Exception:
        return value


def _build_replay_source_df(
    session: ReplaySession,
    visible_df: pd.DataFrame,
    now_et: datetime,
    cfg: Any,
) -> pd.DataFrame:
    """
    Build production-like candles: warmup history + replay visible candles, filtered
    strictly to <= current replay timestamp to avoid look-ahead.
    """
    if visible_df is None or visible_df.empty:
        return pd.DataFrame()

    # The live MM runner fetches about 30 trading days when no explicit
    # lookback is passed. Replay dates are calendar days, so use roughly
    # 45 calendar days to cover the same number of market sessions.
    warmup_days = max(
        45,
        int(getattr(cfg, "replay_training_warmup_days", getattr(cfg, "builder_days", 45)) or 45),
    )
    try:
        warm_start = _coerce_date_minus_days(getattr(session, "start_date", None), warmup_days)
        warm_provider = ReplayDataProvider(
            user_id=session.user_id,
            symbol=session.symbol,
            start_date=warm_start,
            end_date=session.end_date,
            interval=session.interval,
        )
        try:
            warm_df = warm_provider.bars_up_to(10**9)
        except Exception:
            warm_df = getattr(warm_provider, "df", None) or getattr(warm_provider, "bars", None)

        warm_df = _normalize_ohlcv_df(warm_df)
        if warm_df is not None and not warm_df.empty:
            warm_df = warm_df[warm_df.index <= now_et].sort_index()
            if len(warm_df) >= len(visible_df):
                return warm_df
    except Exception as e:
        logger.warning("Replay warmup source failed; using visible_df only: %s", e)

    return visible_df


def _load_replay_config(session: ReplaySession) -> Tuple[Any, Any, Dict[str, Any]]:
    """Return (production module, production BotConfig, config_json dict)."""
    js = _parse_json_field(getattr(session, "config_json", None))
    algo_name = _normalize_algo_name(js.get("algo_name") or getattr(session, "algo_name", None))
    live = _live_module(algo_name)
    cfg = live.BotConfig()

    cfg.symbol = str(getattr(session, "symbol", "") or "")
    cfg.algo_name = algo_name
    feature_sets = {
        "Algo1_MM": "Featureset_1",
        "Algo2_MM": "Featureset_2",
        "Algo3_MM": "Featureset_3",
        "Algo4_MM": "Featureset_4",
        "Algo5_MM": "Featureset_5",
        "Algo_SMI": "SMI",
        "Algo_MACD": "MACD",
    }
    cfg.feature_set = feature_sets.get(algo_name, "Featureset_1")

    # Exact production keys used by Algo1_MM/Algo2_MM/Algo3_MM.
    for key, caster in [
        ("builder_days", _safe_int),
        ("k_forward", _safe_int),
        ("model_refresh_mode", lambda v, d: normalize_model_refresh_mode(v, d)),
        ("model_max_age_minutes", _safe_float),
        ("model_max_age_hours", _safe_float),
        ("min_new_bars_before_retrain", _safe_int),
        ("long_entry_prob", _safe_float),
        ("short_entry_prob", _safe_float),
        ("prob_smoothing_bars", _safe_int),
        ("prob_trail_drop", _safe_float),
        ("prob_exit_mode", lambda v, d: str(v or d)),
        ("long_fixed_exit_prob", _safe_float),
        ("short_fixed_exit_prob", _safe_float),
        ("stop_loss_usd", _safe_float),
        ("trailing_profit_usd", _safe_float),
        ("hard_stop_usd", _safe_float),
        ("trailing_stop_activation", _safe_float),
        ("trailing_stop_distance", _safe_float),
        ("stop_loss_pct", _safe_float),
        ("trailing_profit_pct", _safe_float),
        ("per_share_trailing_profit_pct", _safe_float),
        ("eod_close", _safe_bool),
        ("prediction_strategy", lambda v, d: str(v or d)),
        ("once_per_bar", _safe_bool),
        ("daily_loss_limit_usd", _safe_float),
        ("long_threshold", _safe_float),
        ("short_threshold", _safe_float),
        ("long_exit_threshold", _safe_float),
        ("short_exit_threshold", _safe_float),
        ("min_prob_advantage", _safe_float),
        ("min_volume_multiplier", _safe_float),
        ("cooldown_sec", _safe_int),
        ("per_share_stop_pct", _safe_float),
        ("obv_slope_threshold", _safe_float),
    ]:
        if hasattr(cfg, key):
            current = getattr(cfg, key)
            setattr(cfg, key, caster(js.get(key, current), current))

    # Support older config aliases used elsewhere in the app.
    if hasattr(cfg, "long_entry_prob"):
        cfg.long_entry_prob = _safe_float(
            js.get("long_entry_prob", js.get("entry_prob_long", cfg.long_entry_prob)),
            cfg.long_entry_prob,
        )
    if hasattr(cfg, "short_entry_prob"):
        cfg.short_entry_prob = _safe_float(
            js.get("short_entry_prob", js.get("entry_prob_short", cfg.short_entry_prob)),
            cfg.short_entry_prob,
        )
    if hasattr(cfg, "long_threshold"):
        cfg.long_threshold = _safe_float(
            js.get("long_threshold", js.get("long_entry_prob", cfg.long_threshold)),
            cfg.long_threshold,
        )
    if hasattr(cfg, "short_threshold"):
        cfg.short_threshold = _safe_float(
            js.get("short_threshold", js.get("short_entry_prob", cfg.short_threshold)),
            cfg.short_threshold,
        )
    if cfg.algo_name == "Algo4_MM":
        # Repair sessions created while the route was still zeroing legacy
        # blockers for every MM algo. Algo4 is the production-legacy path.
        if hasattr(cfg, "min_prob_advantage") and float(getattr(cfg, "min_prob_advantage", 0.0) or 0.0) <= 0:
            cfg.min_prob_advantage = float(getattr(live, "DEFAULTS", {}).get("min_prob_advantage", 0.03))
        if hasattr(cfg, "min_volume_multiplier") and float(getattr(cfg, "min_volume_multiplier", 0.0) or 0.0) <= 0:
            cfg.min_volume_multiplier = float(getattr(live, "DEFAULTS", {}).get("min_volume_multiplier", 0.1))
        if hasattr(cfg, "cooldown_sec") and int(getattr(cfg, "cooldown_sec", 0) or 0) <= 0:
            cfg.cooldown_sec = int(getattr(live, "DEFAULTS", {}).get("cooldown_sec", 60))
        if hasattr(cfg, "obv_slope_threshold") and float(getattr(cfg, "obv_slope_threshold", 0.0) or 0.0) <= 0:
            cfg.obv_slope_threshold = float(getattr(live, "DEFAULTS", {}).get("obv_slope_threshold", 0.1))

    cfg.prob_smoothing_bars = max(1, int(getattr(cfg, "prob_smoothing_bars", 3) or 3))
    cfg.k_forward = max(1, int(getattr(cfg, "k_forward", 3) or 3))
    cfg.stop_loss_usd = _safe_float(
        js.get("stop_loss_usd", js.get("hard_stop_usd", getattr(cfg, "hard_stop_usd", 300.0))),
        300.0,
    )
    cfg.hard_stop_usd = cfg.stop_loss_usd
    cfg.trailing_profit_usd = _safe_float(
        js.get(
            "trailing_profit_usd",
            js.get("trailing_stop_distance", js.get("trailing_stop_activation", getattr(cfg, "trailing_profit_usd", 75.0))),
        ),
        75.0,
    )
    cfg.trailing_stop_activation = cfg.trailing_profit_usd
    cfg.trailing_stop_distance = cfg.trailing_profit_usd
    cfg.stop_loss_pct = _safe_float(js.get("stop_loss_pct", js.get("per_share_stop_pct", getattr(cfg, "stop_loss_pct", 0.0))), 0.0)
    cfg.per_share_stop_pct = cfg.stop_loss_pct
    cfg.trailing_profit_pct = _safe_float(
        js.get("trailing_profit_pct", js.get("per_share_trailing_profit_pct", getattr(cfg, "trailing_profit_pct", 0.0))),
        0.0,
    )
    cfg.per_share_trailing_profit_pct = cfg.trailing_profit_pct
    cfg.prob_exit_mode = str(getattr(cfg, "prob_exit_mode", "trailing") or "trailing").strip().lower()
    if cfg.prob_exit_mode not in {"trailing", "fixed"}:
        cfg.prob_exit_mode = "trailing"
    if "prob_fixed_exit_prob" in js:
        if hasattr(cfg, "long_fixed_exit_prob") and "long_fixed_exit_prob" not in js:
            cfg.long_fixed_exit_prob = _safe_float(js.get("prob_fixed_exit_prob"), cfg.long_fixed_exit_prob)
        if hasattr(cfg, "short_fixed_exit_prob") and "short_fixed_exit_prob" not in js:
            cfg.short_fixed_exit_prob = _safe_float(js.get("prob_fixed_exit_prob"), cfg.short_fixed_exit_prob)
    cfg.prediction_strategy = "model_only"
    if not hasattr(cfg, "model_refresh_mode"):
        cfg.model_refresh_mode = DEFAULT_MODEL_REFRESH_MODE
    if "model_refresh_mode" in js:
        cfg.model_refresh_mode = normalize_model_refresh_mode(js.get("model_refresh_mode"), DEFAULT_MODEL_REFRESH_MODE)
    elif _safe_bool(js.get("replay_force_retrain_each_bar", js.get("force_retrain_each_tick", False)), False):
        cfg.model_refresh_mode = "every_bar"
    else:
        cfg.model_refresh_mode = normalize_model_refresh_mode(getattr(cfg, "model_refresh_mode", DEFAULT_MODEL_REFRESH_MODE))

    if not hasattr(cfg, "model_max_age_minutes"):
        cfg.model_max_age_minutes = DEFAULT_MODEL_MAX_AGE_MINUTES
    cfg.model_max_age_minutes = _safe_float(
        js.get("model_max_age_minutes", getattr(cfg, "model_max_age_minutes", DEFAULT_MODEL_MAX_AGE_MINUTES)),
        DEFAULT_MODEL_MAX_AGE_MINUTES,
    )
    if "model_max_age_minutes" not in js and "model_max_age_hours" in js:
        cfg.model_max_age_minutes = max(
            0.0,
            _safe_float(js.get("model_max_age_hours"), DEFAULT_MODEL_MAX_AGE_MINUTES / 60.0) * 60.0,
        )
    cfg.model_max_age_hours = cfg.model_max_age_minutes / 60.0
    if not hasattr(cfg, "min_new_bars_before_retrain"):
        cfg.min_new_bars_before_retrain = DEFAULT_MIN_NEW_BARS_BEFORE_RETRAIN
    cfg.min_new_bars_before_retrain = max(
        0,
        _safe_int(
            js.get("min_new_bars_before_retrain", cfg.min_new_bars_before_retrain),
            DEFAULT_MIN_NEW_BARS_BEFORE_RETRAIN,
        ),
    )

    session_id = int(getattr(session, "id", 0) or 0)
    if session_id not in _CONFIG_LOGGED_SESSION_IDS:
        _CONFIG_LOGGED_SESSION_IDS.add(session_id)
        logger.warning(
            "[REPLAY CONFIG APPLIED] session_id=%s algo=%s long_entry_prob=%.3f "
            "short_entry_prob=%.3f prob_exit_mode=%s prob_trail_drop=%.3f "
            "long_fixed_exit_prob=%.3f short_fixed_exit_prob=%.3f stop_loss_usd=%.2f "
            "trailing_profit_usd=%.2f "
            "long_threshold=%.3f short_threshold=%.3f min_prob_advantage=%.3f "
            "min_volume_multiplier=%.3f cooldown_sec=%s obv_slope_threshold=%.3f "
            "model_refresh_mode=%s model_max_age_minutes=%.1f min_new_bars_before_retrain=%s",
            session_id,
            cfg.algo_name,
            float(getattr(cfg, "long_entry_prob", 0.0) or 0.0),
            float(getattr(cfg, "short_entry_prob", 0.0) or 0.0),
            getattr(cfg, "prob_exit_mode", "trailing"),
            float(getattr(cfg, "prob_trail_drop", 0.0) or 0.0),
            float(getattr(cfg, "long_fixed_exit_prob", 0.0) or 0.0),
            float(getattr(cfg, "short_fixed_exit_prob", 0.0) or 0.0),
            float(getattr(cfg, "stop_loss_usd", 0.0) or 0.0),
            float(getattr(cfg, "trailing_profit_usd", 0.0) or 0.0),
            float(getattr(cfg, "long_threshold", 0.0) or 0.0),
            float(getattr(cfg, "short_threshold", 0.0) or 0.0),
            float(getattr(cfg, "min_prob_advantage", 0.0) or 0.0),
            float(getattr(cfg, "min_volume_multiplier", 0.0) or 0.0),
            getattr(cfg, "cooldown_sec", None),
            float(getattr(cfg, "obv_slope_threshold", 0.0) or 0.0),
            getattr(cfg, "model_refresh_mode", DEFAULT_MODEL_REFRESH_MODE),
            float(getattr(cfg, "model_max_age_minutes", 0.0) or 0.0),
            int(getattr(cfg, "min_new_bars_before_retrain", 0) or 0),
        )

    # Replay-only controls. They do not alter production decision rules.
    cfg.replay_training_warmup_days = max(
        45,
        _safe_int(
            js.get("replay_training_warmup_days", max(int(getattr(cfg, "builder_days", 45)), 45)),
            max(int(getattr(cfg, "builder_days", 45)), 45),
        ),
    )
    cfg.replay_train_min_rows = _safe_int(js.get("replay_train_min_rows", 30), 30)
    cfg.replay_force_retrain_each_bar = cfg.model_refresh_mode == "every_bar"

    return live, cfg, js


def _replay_model_path(session: ReplaySession, cfg: Any, interval: str) -> str:
    """Replay-isolated version of production naming, so live models are not overwritten."""
    safe_symbol = str(session.symbol).replace("/", "_").replace(" ", "_").upper()
    safe_interval = str(interval).replace("/", "_").replace(" ", "_")
    filename = (
        f"replay_{session.id}_mm_{cfg.algo_name}_{cfg.feature_set}_"
        f"{safe_symbol}_{safe_interval}_k{cfg.k_forward}.joblib"
    )
    return os.path.join(REPLAY_MODEL_DIR, filename)


def _make_open_trade_adapter(open_trade: Any, session_id: int) -> Any:
    if open_trade is None:
        return None
    return SimpleNamespace(
        bot_id=int(session_id),
        position_side=getattr(open_trade, "position_side", None),
        entry_price=getattr(open_trade, "entry_price", None),
        quantity=getattr(open_trade, "quantity", None),
    )


def _session_bot_adapter(session: ReplaySession, cfg: Any) -> Any:
    return SimpleNamespace(
        id=int(session.id),
        user_id=int(session.user_id),
        symbol=session.symbol,
        interval=session.interval,
        algo_name=cfg.algo_name,
        trade_size=float(getattr(session, "trade_size", 0.0) or 0.0),
        quantity=float(getattr(session, "trade_size", 0.0) or 0.0),
        allow_short_selling=True,
        config_json=getattr(session, "config_json", None),
    )


def _clear_algo_state(live: Any, session_id: int) -> None:
    try:
        if hasattr(live, "_cleanup_exit_state"):
            live._cleanup_exit_state(int(session_id))
        for name in ("_PROB_PEAK", "_PEAK_PROB", "_PROFIT_PEAK", "_LAST_BAR_TS"):
            d = getattr(live, name, None)
            if isinstance(d, dict):
                d.pop(int(session_id), None)
    except Exception:
        pass


def clear_model_cache(session_id: int) -> None:
    """Called by orchestrator.py when a session ends."""
    for algo in ALLOWED_MM_ALGOS:
        try:
            _clear_algo_state(_live_module(algo), int(session_id))
        except Exception:
            pass


def _log_decision(
    live: Any,
    *,
    log_file: str,
    decision: str,
    prob_up: float,
    prob_down: float,
    data_len: int,
    bar_close_px: float,
    bar_open_px: float,
    open_trade: Any,
    model_path: str,
    symbol: str,
    interval: str,
    cfg: Any,
    reason: str,
    df: Optional[pd.DataFrame],
    X: Optional[Any],
    feat_names: Optional[List[str]],
    prob_up_avg: Optional[float],
    prob_up_avg_prev: Optional[float],
) -> None:
    try:
        live.log_trade_decision(
            log_file=log_file,
            decision=decision,
            prob_up=float(prob_up or 0.0),
            prob_down=float(prob_down or 0.0),
            data_len=int(data_len or 0),
            bar_close_px=float(bar_close_px or 0.0),
            bar_open_px=float(bar_open_px or 0.0),
            open_trade=_make_open_trade_adapter(open_trade, int(getattr(open_trade, "session_id", 0) or 0)) if open_trade else None,
            model_path=model_path or "",
            symbol=symbol or "",
            interval=interval or "",
            cfg=cfg,
            reason=reason or "-",
            df=df,
            X=X,
            feat_cols=feat_names,
            prob_up_avg=prob_up_avg,
            prob_up_avg_prev=prob_up_avg_prev,
        )
    except Exception as e:
        logger.warning("Replay production log failed: %s", e)



def _core_state_from_live(live: Any, session_id: int, fallback_conviction: float = 0.0) -> MMCoreState:
    prob_peak_map = getattr(live, "_PROB_PEAK", None)
    if not isinstance(prob_peak_map, dict):
        prob_peak_map = getattr(live, "_PEAK_PROB", None)
    profit_peak_map = getattr(live, "_PROFIT_PEAK", None)
    return MMCoreState(
        prob_peak=float(prob_peak_map.get(int(session_id), fallback_conviction) if isinstance(prob_peak_map, dict) else fallback_conviction),
        profit_peak=float(profit_peak_map.get(int(session_id), 0.0) if isinstance(profit_peak_map, dict) else 0.0),
    )


def _store_core_state(live: Any, session_id: int, state: MMCoreState) -> None:
    prob_peak_map = getattr(live, "_PROB_PEAK", None)
    alt_prob_peak_map = getattr(live, "_PEAK_PROB", None)
    profit_peak_map = getattr(live, "_PROFIT_PEAK", None)
    if isinstance(prob_peak_map, dict):
        prob_peak_map[int(session_id)] = float(state.prob_peak or 0.0)
    if isinstance(alt_prob_peak_map, dict):
        alt_prob_peak_map[int(session_id)] = float(state.prob_peak or 0.0)
    if isinstance(profit_peak_map, dict):
        profit_peak_map[int(session_id)] = float(state.profit_peak or 0.0)

def run_algoMM_replay_tick(
    session_id: int,
    bar_idx: int,
    provider: ReplayDataProvider,
    cfg: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Main entry point used by the existing replay orchestrator.
    Name stays run_algoMM_replay_tick for compatibility, but it now dispatches
    to Algo1_MM, Algo2_MM, or Algo3_MM based on ReplaySession.algo_name/config_json.
    """
    db: Session = SessionLocal()

    live = None
    decision = "NONE"
    reason = ""
    data_len = 0
    prob_up = 0.0
    prob_down = 0.0
    prob_up_avg = None
    prob_up_avg_prev = None
    bar_close_px = 0.0
    bar_open_px = 0.0
    model_path = ""
    feat_names: List[str] = []
    X = None
    df: Optional[pd.DataFrame] = None
    open_trade = None
    log_file = ""
    now_et = datetime.now(_ET)
    symbol = ""
    interval = ""

    result: Dict[str, Any] = {
        "status": "ok",
        "decision": decision,
        "reason": reason,
        "prob_up": prob_up,
        "prob_down": prob_down,
        "price": 0.0,
        "bar_idx": bar_idx,
        "bar_time": None,
    }

    def finish(status: str = "ok") -> Dict[str, Any]:
        try:
            if live is not None and log_file:
                _log_decision(
                    live,
                    log_file=log_file,
                    decision=decision,
                    prob_up=prob_up,
                    prob_down=prob_down,
                    data_len=data_len,
                    bar_close_px=bar_close_px,
                    bar_open_px=bar_open_px,
                    open_trade=open_trade,
                    model_path=model_path,
                    symbol=symbol,
                    interval=interval,
                    cfg=cfg,
                    reason=reason,
                    df=df,
                    X=X,
                    feat_names=feat_names,
                    prob_up_avg=prob_up_avg,
                    prob_up_avg_prev=prob_up_avg_prev,
                )
        except Exception as e:
            logger.warning("finish/log failed: %s", e)

        result.update(
            status=status,
            decision=decision,
            reason=reason,
            prob_up=float(prob_up or 0.0),
            prob_down=float(prob_down or 0.0),
            price=float(bar_close_px or 0.0),
            bar_time=now_et.replace(tzinfo=None).isoformat() if now_et else None,
        )
        return result

    try:
        session = db.query(ReplaySession).filter_by(id=session_id).first()
        if not session:
            decision = "SESSION_NOT_FOUND"
            reason = "SESSION_NOT_FOUND"
            result.update(status="error", decision=decision, reason=reason)
            return result

        live, cfg, js = _load_replay_config(session)
        symbol = session.symbol
        interval = session.interval or "1min"
        allow_short = _safe_bool(js.get("allow_short_selling", js.get("allow_short", True)), True)
        core_cfg = config_from_obj(cfg, allow_short=allow_short)

        bar_time = provider.bar_time(bar_idx)
        now_et = _as_et(bar_time)
        result["bar_time"] = now_et.replace(tzinfo=None).isoformat()

        log_dir = os.path.join(DATA_ROOT, str(session.user_id))
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f"replay_{session.id}_{session.symbol}_{cfg.algo_name}.log")
        model_path = _replay_model_path(session, cfg, interval)

        realized_pnl = float(get_session_pnl(db, session_id) or 0.0)
        daily_loss_limit = float(getattr(cfg, "daily_loss_limit_usd", 0.0) or 0.0)
        if daily_loss_limit > 0 and realized_pnl <= -abs(daily_loss_limit):
            decision = "DAILY_LOSS_LIMIT"
            reason = f"REALIZED_PNL_{realized_pnl:.2f}_LE_LIMIT_{-abs(daily_loss_limit):.2f}"
            return finish("blocked")

        visible_df = _build_replay_df(provider, bar_idx)
        if visible_df is None or visible_df.empty:
            decision = "NO_DATA"
            reason = "REPLAY_DF_EMPTY"
            return finish("ok")

        source_df = _build_replay_source_df(session=session, visible_df=visible_df, now_et=now_et, cfg=cfg)
        if source_df is None or source_df.empty:
            decision = "NO_SOURCE_DATA"
            reason = "WARMUP_AND_VISIBLE_SOURCE_EMPTY"
            return finish("ok")

        df = source_df.sort_index()
        data_len = len(df)
        bar_close_px = float(df["close"].iloc[-1])
        bar_open_px = float(df["open"].iloc[-1])
        result["price"] = bar_close_px

        # Same candle JSONL shape as production, but keyed by replay session id.
        try:
            if hasattr(live, "_log_candle_jsonl"):
                live._log_candle_jsonl(log_dir, _session_bot_adapter(session, cfg), interval, df)
        except Exception:
            logger.debug("Replay candle jsonl write skipped", exc_info=True)

        if bar_close_px <= 0 or not np.isfinite(bar_close_px):
            decision = "INVALID_PRICE"
            reason = "NON_POSITIVE_OR_NAN_PRICE"
            return finish("ok")

        open_trade = get_open_trade(db, session_id)
        if open_trade is not None:
            update_open_trade_mark_replay(db, open_trade, bar_close_px, commit=True)

        if cfg.algo_name in {"Algo_SMI", "Algo_MACD"}:
            if cfg.algo_name == "Algo_SMI":
                from app.scripts.stocks.bots.algo3_logic import determine_signals as _indicator_signals
                indicator_name = "SMI"
            else:
                from app.scripts.stocks.bots.algo5_logic import determine_signals as _indicator_signals
                indicator_name = "MACD"

            signals_df = _indicator_signals(df)
            if signals_df is None or signals_df.empty or len(signals_df) < 2:
                decision = "NO_INDICATOR_SIGNAL_ROWS"
                reason = f"{indicator_name}_SIGNALS_TOO_SHORT"
                return finish("ok")

            prev_bar = signals_df.iloc[-2]
            curr_bar = signals_df.iloc[-1]
            buy_signal = bool(prev_bar.get("Buy_Signal", False))
            sell_signal = bool(prev_bar.get("Sell_Signal", False))
            exec_price = float(curr_bar.get("open", bar_close_px) or bar_close_px)
            if exec_price <= 0 or not np.isfinite(exec_price):
                exec_price = bar_close_px

            if open_trade is not None and bool(getattr(cfg, "eod_close", True)) and now_et.time() >= datetime.strptime("15:50", "%H:%M").time():
                close_position_replay(
                    db=db,
                    trade=open_trade,
                    price=float(exec_price),
                    bar_time=now_et.replace(tzinfo=None),
                    reason="EOD_CLOSE",
                )
                db.commit()
                decision = "EXIT_EOD_CLOSE"
                reason = f"{indicator_name}_EOD_CLOSE"
                return finish("ok")

            if open_trade is not None:
                side = str(getattr(open_trade, "position_side", "") or "").lower()
                entry_price = float(getattr(open_trade, "entry_price", 0.0) or 0.0)
                qty = float(getattr(open_trade, "quantity", 0.0) or 0.0)
                if entry_price > 0 and qty > 0 and exec_price > 0:
                    pnl = (exec_price - entry_price) * qty if side == "long" else (entry_price - exec_price) * qty
                    basis = abs(entry_price * qty)
                    stop_loss_pct = max(0.0, float(getattr(cfg, "stop_loss_pct", getattr(cfg, "per_share_stop_pct", 0.0)) or 0.0))
                    trailing_profit_pct = max(
                        0.0,
                        float(getattr(cfg, "trailing_profit_pct", getattr(cfg, "per_share_trailing_profit_pct", 0.0)) or 0.0),
                    )
                    if stop_loss_pct > 0 and pnl <= -(basis * stop_loss_pct):
                        close_position_replay(
                            db=db,
                            trade=open_trade,
                            price=float(exec_price),
                            bar_time=now_et.replace(tzinfo=None),
                            reason="STOP_LOSS_PCT",
                        )
                        db.commit()
                        decision = "EXIT_STOP_LOSS_PCT"
                        reason = f"{indicator_name}_STOP_LOSS_PCT"
                        return finish("ok")
                    if trailing_profit_pct > 0:
                        core_state = _core_state_from_live(live, session_id, fallback_conviction=0.0)
                        profit_peak = max(float(getattr(core_state, "profit_peak", 0.0) or 0.0), pnl)
                        _store_core_state(live, session_id, MMCoreState(prob_peak=core_state.prob_peak, profit_peak=profit_peak))
                        trail_usd = basis * trailing_profit_pct
                        if profit_peak >= trail_usd and (profit_peak - pnl) >= trail_usd:
                            close_position_replay(
                                db=db,
                                trade=open_trade,
                                price=float(exec_price),
                                bar_time=now_et.replace(tzinfo=None),
                                reason="TRAILING_PROFIT_PCT",
                            )
                            db.commit()
                            _clear_algo_state(live, session_id)
                            decision = "EXIT_TRAILING_PROFIT_PCT"
                            reason = f"{indicator_name}_TRAILING_PROFIT_PCT"
                            return finish("ok")

                if side == "long" and sell_signal:
                    close_position_replay(
                        db=db,
                        trade=open_trade,
                        price=float(exec_price),
                        bar_time=now_et.replace(tzinfo=None),
                        reason=f"{indicator_name}_SELL_SIGNAL",
                    )
                    db.commit()
                    decision = f"EXIT_{indicator_name}_SELL_SIGNAL"
                    reason = "OPPOSITE_INDICATOR_SIGNAL"
                    return finish("ok")
                if side == "short" and buy_signal:
                    close_position_replay(
                        db=db,
                        trade=open_trade,
                        price=float(exec_price),
                        bar_time=now_et.replace(tzinfo=None),
                        reason=f"{indicator_name}_BUY_SIGNAL",
                    )
                    db.commit()
                    decision = f"EXIT_{indicator_name}_BUY_SIGNAL"
                    reason = "OPPOSITE_INDICATOR_SIGNAL"
                    return finish("ok")

                decision = "HOLD_POSITION"
                reason = f"{indicator_name}_NO_OPPOSITE_SIGNAL"
                return finish("ok")

            if buy_signal:
                qty = float(getattr(session, "trade_size", 0.0) or 1.0)
                open_position_replay(
                    db=db,
                    session=session,
                    position_side="long",
                    price=float(exec_price),
                    bar_time=now_et.replace(tzinfo=None),
                    quantity=qty,
                )
                db.commit()
                decision = f"OPEN_LONG_{indicator_name}"
                reason = f"{indicator_name}_BUY_SIGNAL"
                return finish("ok")

            if sell_signal and allow_short:
                qty = float(getattr(session, "trade_size", 0.0) or 1.0)
                open_position_replay(
                    db=db,
                    session=session,
                    position_side="short",
                    price=float(exec_price),
                    bar_time=now_et.replace(tzinfo=None),
                    quantity=qty,
                )
                db.commit()
                decision = f"OPEN_SHORT_{indicator_name}"
                reason = f"{indicator_name}_SELL_SIGNAL"
                return finish("ok")

            if sell_signal and not allow_short:
                decision = "SHORT_SIGNAL_BUT_SHORT_DISABLED"
                reason = f"{indicator_name}_SELL_SIGNAL"
                return finish("ok")

            decision = "NO_ENTRY_SIGNAL"
            reason = f"{indicator_name}_NO_SIGNAL"
            return finish("ok")

        mm2 = getattr(live, "mm2", None)
        if mm2 is None or not hasattr(mm2, "build_feature_matrix_from_df") or not hasattr(mm2, "build_training_features_from_df"):
            decision = "FEATURE_BUILDER_UNSAFE"
            reason = f"{cfg.algo_name} feature module missing live-safe builders"
            return finish("ok")

        infer_feat_df = mm2.build_feature_matrix_from_df(
            df=df,
            symbol=session.symbol,
            interval=interval,
            feature_set=cfg.feature_set,
        )
        train_feat_df = mm2.build_training_features_from_df(
            df=df,
            symbol=session.symbol,
            interval=interval,
            k_forward=cfg.k_forward,
            feature_set=cfg.feature_set,
        )

        if infer_feat_df is None or infer_feat_df.empty or len(infer_feat_df) < cfg.prob_smoothing_bars + 1:
            decision = "NO_INFER_FEATURES"
            reason = f"INFER_FEATURE_DF_TOO_SHORT rows={0 if infer_feat_df is None else len(infer_feat_df)}"
            return finish("ok")

        if train_feat_df is None or train_feat_df.empty or len(train_feat_df) < 30:
            decision = "NO_TRAIN_FEATURES"
            reason = f"TRAIN_FEATURE_DF_TOO_SHORT rows={0 if train_feat_df is None else len(train_feat_df)}"
            X = infer_feat_df
            feat_names = list(infer_feat_df.columns)
            return finish("ok")

        if "y" not in train_feat_df.columns or "w" not in train_feat_df.columns:
            decision = "BAD_TRAIN_FEATURES"
            reason = "MISSING_y_OR_w"
            X = infer_feat_df
            feat_names = list(infer_feat_df.columns)
            return finish("ok")

        min_train_rows = int(getattr(cfg, "replay_train_min_rows", 0) or 0)
        if min_train_rows > 0 and len(train_feat_df) < min_train_rows:
            decision = "WAIT_WARMUP"
            reason = f"TRAIN_ROWS_{len(train_feat_df)}_LT_MIN_{min_train_rows} source_bars={len(source_df)}"
            X = infer_feat_df
            feat_names = list(infer_feat_df.columns)
            return finish("ok")

        if train_feat_df["y"].nunique() < 2:
            decision = "ONE_CLASS_TRAINING"
            reason = "ONLY_ONE_CLASS_IN_TRAIN_FEATURES"
            X = infer_feat_df
            feat_names = list(infer_feat_df.columns)
            return finish("ok")

        if hasattr(mm2, "get_feature_columns"):
            default_feat_names = list(mm2.get_feature_columns(cfg.feature_set))
        else:
            default_feat_names = [c for c in train_feat_df.columns if c not in ("y", "w")]

        model, feat_names = live._load_or_train_model(
            model_path=model_path,
            symbol=session.symbol,
            interval=interval,
            feat_df=train_feat_df,
            feat_names=list(default_feat_names),
            cfg=cfg,
        )

        # Algo4_MM intentionally mimics the legacy production AlgoMM runner:
        # predict on the labeled training feature frame, then align those
        # probabilities back to the full price-data index.
        feature_source_df = train_feat_df if cfg.algo_name == "Algo4_MM" else infer_feat_df
        X = live._prepare_X(feature_source_df, feat_names)

        if cfg.algo_name != "Algo4_MM":
            try:
                latest_price_ts = pd.Timestamp(df.index[-1])
                latest_feat_ts = pd.Timestamp(X.index[-1])
                feature_lag_sec = (latest_price_ts - latest_feat_ts).total_seconds()
            except Exception:
                feature_lag_sec = None

            if feature_lag_sec is not None and abs(feature_lag_sec) > 60 * 10:
                decision = "STALE_FEATURES"
                reason = f"FEATURE_LAG_{int(feature_lag_sec)}s price_ts={df.index[-1]} feat_ts={X.index[-1]}"
                return finish("ok")

        probs = live.predict_probability(model, X, feat_names)
        min_prob_rows = 2 if cfg.algo_name == "Algo4_MM" else cfg.prob_smoothing_bars + 1
        if probs is None or len(probs) < min_prob_rows:
            decision = "NO_PROBS"
            reason = f"PREDICT_PROBS_TOO_SHORT rows={0 if probs is None else len(probs)}"
            return finish("ok")

        prob_raw = pd.Series(probs, index=X.index, name="prob_up").astype(float)
        if cfg.algo_name == "Algo4_MM":
            window = max(int(getattr(cfg, "k_forward", 3) or 3), 1)
            prob_series = prob_raw.rolling(window=window, min_periods=1).mean()
            prob_series_aligned = prob_series.reindex(df.index).ffill().bfill()
            if len(prob_series_aligned) < 2:
                decision = "PROB_ALIGN_SHORT"
                reason = "ALIGNED_PROB_SERIES_TOO_SHORT"
                return finish("ok")
            prob_up = float(prob_series_aligned.iloc[-2])
            prob_down = 1.0 - prob_up
            prob_up_avg = prob_up
            prob_up_avg_prev = float(prob_series_aligned.iloc[-3]) if len(prob_series_aligned) >= 3 else prob_up
            if not np.isfinite(prob_up):
                decision = "NO_VALID_PROB"
                reason = "PREV_PROB_NAN_OR_INF"
                return finish("ok")
        else:
            prob_avg = prob_raw.rolling(
                window=cfg.prob_smoothing_bars,
                min_periods=cfg.prob_smoothing_bars,
            ).mean().dropna()
            if len(prob_avg) < 2:
                decision = "NO_SMOOTHED_PROB"
                reason = "NEED_PREVIOUS_AND_CURRENT_PROB_AVG"
                return finish("ok")

            prob_up = float(prob_raw.iloc[-1])
            prob_down = 1.0 - prob_up
            prob_up_avg = float(prob_avg.iloc[-1])
            prob_up_avg_prev = float(prob_avg.iloc[-2])

            if not np.isfinite(prob_up_avg) or not np.isfinite(prob_up_avg_prev):
                decision = "NO_VALID_PROB"
                reason = "PROB_AVG_NAN_OR_INF"
                return finish("ok")

        open_trade = get_open_trade(db, session_id)
        if open_trade is not None:
            update_open_trade_mark_replay(db, open_trade, bar_close_px, commit=True)

        # Exact once-per-bar duplicate protection from production, keyed by replay session id.
        bar_key = str(df.index[-1])
        last_bar_map = getattr(live, "_LAST_BAR_TS", None)
        if isinstance(last_bar_map, dict):
            if bool(getattr(cfg, "once_per_bar", True)) and open_trade is None and last_bar_map.get(session.id) == bar_key:
                decision = "SAME_BAR_SKIP"
                reason = f"ALREADY_EVALUATED_BAR_{bar_key}"
                return finish("ok")
            last_bar_map[session.id] = bar_key

        if open_trade is None:
            entry_decision = evaluate_entry(
                prob_up_avg=float(prob_up_avg),
                prob_up_avg_prev=float(prob_up_avg_prev) if prob_up_avg_prev is not None else None,
                cfg=core_cfg,
            )
            should_enter = entry_decision.should_act
            enter_direction = entry_decision.action
            entry_reason = entry_decision.reason

            if not should_enter:
                decision = "NO_ENTRY_SIGNAL"
                reason = entry_reason or "ENTRY_CONDITIONS_NOT_MET"
            else:
                qty = float(getattr(session, "trade_size", 0.0) or 0.0)
                if qty <= 0:
                    qty = 1.0
                exec_price = live.get_smart_execution_price(df)
                position_side = "long" if enter_direction == "LONG" else "short"

                ot = open_position_replay(
                    db=db,
                    session=session,
                    position_side=position_side,
                    price=float(exec_price),
                    bar_time=now_et.replace(tzinfo=None),
                    quantity=qty,
                )
                db.commit()
                open_trade = get_open_trade(db, session_id)

                if ot is None and open_trade is None:
                    decision = "OPEN_FAILED"
                    reason = (
                        f"open_position_replay_returned_none_or_no_open_trade_created "
                        f"side={position_side} qty={qty} price={float(exec_price):.2f} "
                        f"config_json={getattr(session, 'config_json', None)}"
                    )
                else:
                    _clear_algo_state(live, session_id)
                    # Same production state initialization after opening.
                    prob_peak = getattr(live, "_PROB_PEAK", None)
                    profit_peak = getattr(live, "_PROFIT_PEAK", None)
                    if isinstance(prob_peak, dict):
                        conviction = prob_up_avg if enter_direction == "LONG" else 1.0 - prob_up_avg
                        prob_peak[int(session_id)] = float(conviction)
                    if isinstance(profit_peak, dict):
                        profit_peak[int(session_id)] = 0.0
                    decision = f"OPEN_{enter_direction}"
                    reason = entry_reason or f"{enter_direction}_CONDITIONS_MET"
                    open_trade = open_trade or ot

        else:
            exit_view = _make_open_trade_adapter(open_trade, session_id)
            side = str(getattr(exit_view, "position_side", "") or "").lower()
            fallback_conviction = float(prob_up_avg if side == "long" else 1.0 - prob_up_avg)
            core_state = _core_state_from_live(live, session_id, fallback_conviction=fallback_conviction)
            exit_decision = evaluate_exit(
                MMCorePosition(
                    side=side,
                    entry_price=float(getattr(exit_view, "entry_price", 0.0) or 0.0),
                    quantity=float(getattr(exit_view, "quantity", 0.0) or 0.0),
                ),
                current_price=bar_close_px,
                prob_up_avg=float(prob_up_avg),
                cfg=core_cfg,
                state=core_state,
                now_et=now_et,
            )
            _store_core_state(live, session_id, exit_decision.state or core_state)
            should_exit = exit_decision.should_act
            exit_reason = exit_decision.reason

            if should_exit:
                exec_price = live.get_smart_execution_price(df)
                close_position_replay(
                    db=db,
                    trade=open_trade,
                    price=float(exec_price),
                    bar_time=now_et.replace(tzinfo=None),
                    reason=exit_reason,
                )
                db.commit()
                _clear_algo_state(live, session_id)
                decision = f"EXIT_{exit_reason}"
                reason = exit_reason
                open_trade = None
            else:
                decision = "HOLD_POSITION"
                reason = "GUARDRAILS_NOT_HIT"

        logger.info(
            "session=%s algo=%s bar=%s time=%s decision=%s reason=%s raw_up=%.3f avg=%.3f prev=%.3f price=%.2f",
            session_id,
            cfg.algo_name,
            bar_idx,
            now_et,
            decision,
            reason,
            prob_up,
            prob_up_avg if prob_up_avg is not None else -1,
            prob_up_avg_prev if prob_up_avg_prev is not None else -1,
            bar_close_px,
        )

        return finish("ok")

    except Exception as e:
        db.rollback()
        logger.error("Replay tick error session=%s bar=%s: %s", session_id, bar_idx, e, exc_info=True)
        decision = f"ERROR: {type(e).__name__}()"
        reason = f"EXCEPTION: {str(e)[:160]}"
        return finish("error")

    finally:
        db.close()


# Optional explicit aliases; existing orchestrator can keep using run_algoMM_replay_tick.
def run_Algo1_MM_replay_tick(session_id: int, bar_idx: int, provider: ReplayDataProvider, cfg: Optional[Any] = None):
    return run_algoMM_replay_tick(session_id=session_id, bar_idx=bar_idx, provider=provider, cfg=cfg)


def run_Algo2_MM_replay_tick(session_id: int, bar_idx: int, provider: ReplayDataProvider, cfg: Optional[Any] = None):
    return run_algoMM_replay_tick(session_id=session_id, bar_idx=bar_idx, provider=provider, cfg=cfg)


def run_Algo3_MM_replay_tick(session_id: int, bar_idx: int, provider: ReplayDataProvider, cfg: Optional[Any] = None):
    return run_algoMM_replay_tick(session_id=session_id, bar_idx=bar_idx, provider=provider, cfg=cfg)


def run_Algo4_MM_replay_tick(session_id: int, bar_idx: int, provider: ReplayDataProvider, cfg: Optional[Any] = None):
    return run_algoMM_replay_tick(session_id=session_id, bar_idx=bar_idx, provider=provider, cfg=cfg)


def run_Algo5_MM_replay_tick(session_id: int, bar_idx: int, provider: ReplayDataProvider, cfg: Optional[Any] = None):
    return run_algoMM_replay_tick(session_id=session_id, bar_idx=bar_idx, provider=provider, cfg=cfg)


def run_Algo_SMI_replay_tick(session_id: int, bar_idx: int, provider: ReplayDataProvider, cfg: Optional[Any] = None):
    return run_algoMM_replay_tick(session_id=session_id, bar_idx=bar_idx, provider=provider, cfg=cfg)


def run_Algo_MACD_replay_tick(session_id: int, bar_idx: int, provider: ReplayDataProvider, cfg: Optional[Any] = None):
    return run_algoMM_replay_tick(session_id=session_id, bar_idx=bar_idx, provider=provider, cfg=cfg)
