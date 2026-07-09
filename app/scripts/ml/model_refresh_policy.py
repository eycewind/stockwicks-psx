from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

import joblib
import pandas as pd


DEFAULT_MODEL_REFRESH_MODE = "adaptive"
DEFAULT_MODEL_MAX_AGE_MINUTES = 30.0
DEFAULT_MIN_NEW_BARS_BEFORE_RETRAIN = 30

VALID_MODEL_REFRESH_MODES = {"fixed", "scheduled", "adaptive", "every_bar"}


def _is_writable_dir(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".write-test-", dir=path, delete=True):
            pass
        return True
    except Exception:
        return False


def _fallback_model_path(model_path: str) -> str:
    filename = os.path.basename(model_path)
    fallback_dir = os.getenv("MODEL_FALLBACK_DIR", "").strip()
    if not fallback_dir:
        data_dir = os.getenv("DATA_DIR", "").strip()
        if data_dir:
            fallback_dir = os.path.join(data_dir, "models")
        else:
            requested_dir = Path(model_path).expanduser().resolve().parent
            client_root = requested_dir.parent if requested_dir.name == "models" else requested_dir
            fallback_dir = str(client_root / "data" / "models")
    return os.path.join(fallback_dir, filename)


def resolve_writable_model_path(model_path: str, logger: Any = None) -> str:
    requested_dir = os.path.dirname(model_path) or "."
    if _is_writable_dir(requested_dir):
        return model_path

    fallback_path = _fallback_model_path(model_path)
    fallback_dir = os.path.dirname(fallback_path) or "."
    if _is_writable_dir(fallback_dir):
        if logger:
            logger.warning(
                "[AlgoMM] MODEL_DIR is not writable (%s). Using fallback model path %s",
                requested_dir,
                fallback_path,
            )
        return fallback_path

    raise PermissionError(
        f"Neither MODEL_DIR ({requested_dir}) nor fallback model directory "
        f"({fallback_dir}) is writable"
    )


def normalize_model_refresh_mode(value: Any, default: str = DEFAULT_MODEL_REFRESH_MODE) -> str:
    mode = str(value or default).strip().lower()
    aliases = {
        "reuse": "fixed",
        "hold": "fixed",
        "once": "fixed",
        "time": "scheduled",
        "time_based": "scheduled",
        "every_tick": "every_bar",
        "force": "every_bar",
        "force_retrain": "every_bar",
    }
    mode = aliases.get(mode, mode)
    return mode if mode in VALID_MODEL_REFRESH_MODES else default


def _safe_float(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int) -> int:
    try:
        if value is None or value == "":
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def model_max_age_minutes(cfg: Any) -> float:
    explicit = getattr(cfg, "model_max_age_minutes", None)
    if explicit is not None:
        return max(0.0, _safe_float(explicit, DEFAULT_MODEL_MAX_AGE_MINUTES))
    hours = getattr(cfg, "model_max_age_hours", None)
    if hours is not None:
        return max(0.0, _safe_float(hours, DEFAULT_MODEL_MAX_AGE_MINUTES / 60.0) * 60.0)
    return DEFAULT_MODEL_MAX_AGE_MINUTES


def min_new_bars_before_retrain(cfg: Any) -> int:
    return max(
        0,
        _safe_int(
            getattr(cfg, "min_new_bars_before_retrain", DEFAULT_MIN_NEW_BARS_BEFORE_RETRAIN),
            DEFAULT_MIN_NEW_BARS_BEFORE_RETRAIN,
        ),
    )


def _timestamp(value: Any) -> pd.Timestamp | None:
    if value is None:
        return None
    try:
        return pd.Timestamp(value)
    except Exception:
        return None


def _minutes_between(start: Any, end: Any) -> float | None:
    start_ts = _timestamp(start)
    end_ts = _timestamp(end)
    if start_ts is None or end_ts is None:
        return None
    try:
        if start_ts.tzinfo is not None and end_ts.tzinfo is None:
            end_ts = end_ts.tz_localize(start_ts.tz)
        elif start_ts.tzinfo is None and end_ts.tzinfo is not None:
            end_ts = end_ts.tz_convert(None)
        elif start_ts.tzinfo is not None and end_ts.tzinfo is not None:
            end_ts = end_ts.tz_convert(start_ts.tz)
        return max(0.0, float((end_ts - start_ts).total_seconds()) / 60.0)
    except Exception:
        return None


def _latest_index_value(feat_df: pd.DataFrame) -> Any:
    try:
        if feat_df is not None and not feat_df.empty:
            return feat_df.index[-1]
    except Exception:
        pass
    return None


def _should_retrain(pack: dict[str, Any], cfg: Any, feat_df: pd.DataFrame, now: Any, force_retrain: bool) -> tuple[bool, str]:
    if force_retrain:
        return True, "force_retrain"

    mode = normalize_model_refresh_mode(getattr(cfg, "model_refresh_mode", None))
    if mode == "every_bar":
        return True, "mode_every_bar"
    if mode == "fixed":
        return False, "mode_fixed"

    min_bars = min_new_bars_before_retrain(cfg)
    trained_rows = int(pack.get("trained_rows") or 0)
    current_rows = int(len(feat_df) if feat_df is not None else 0)
    new_rows = max(0, current_rows - trained_rows)

    max_age = model_max_age_minutes(cfg)
    trained_at_bar_time = pack.get("trained_at_bar_time") or pack.get("trained_at")
    age_minutes = _minutes_between(trained_at_bar_time, now or _latest_index_value(feat_df))

    if mode == "scheduled":
        if max_age > 0 and age_minutes is not None and age_minutes >= max_age:
            return True, f"age_minutes={age_minutes:.1f}"
        if min_bars > 0 and new_rows >= min_bars:
            return True, f"new_rows={new_rows}"
        return False, f"fresh age_minutes={age_minutes} new_rows={new_rows}"

    # Adaptive starts with the scheduled guardrails. Regime-change triggers can
    # be added here without touching each algo runner.
    if max_age > 0 and age_minutes is not None and age_minutes >= max_age:
        return True, f"adaptive_age_minutes={age_minutes:.1f}"
    if min_bars > 0 and new_rows >= min_bars:
        return True, f"adaptive_new_rows={new_rows}"
    return False, f"adaptive_fresh age_minutes={age_minutes} new_rows={new_rows}"


def load_or_train_model_with_policy(
    *,
    model_path: str,
    symbol: str,
    interval: str,
    feat_df: pd.DataFrame,
    feat_names: Iterable[str],
    cfg: Any,
    prepare_X: Callable[[pd.DataFrame, list[str]], pd.DataFrame],
    logger: Any,
    force_retrain: bool = False,
    now: Any = None,
):
    feat_names = list(feat_names)
    mode = normalize_model_refresh_mode(getattr(cfg, "model_refresh_mode", None))
    requested_model_path = model_path
    model_path = resolve_writable_model_path(model_path, logger)

    pack: dict[str, Any] | None = None
    if os.path.exists(model_path):
        try:
            loaded = joblib.load(model_path)
            if isinstance(loaded, dict) and "model" in loaded:
                pack = loaded
            else:
                pack = {"model": loaded, "features": feat_names}
        except Exception as exc:
            if logger:
                logger.warning(
                    "[AlgoMM] Failed to load existing model %s (%s). Will retrain.",
                    model_path,
                    exc,
                    exc_info=True,
                )
            pack = None

    if pack is not None:
        retrain, reason = _should_retrain(pack, cfg, feat_df, now, force_retrain)
        if not retrain:
            if logger:
                logger.info("[AlgoMM] Loaded model from %s refresh_mode=%s reason=%s", model_path, mode, reason)
            return pack["model"], pack.get("features", feat_names)
        if logger:
            logger.info("[AlgoMM] Retraining model %s refresh_mode=%s reason=%s", model_path, mode, reason)
    elif logger:
        logger.info("[AlgoMM] Training new model for %s %s refresh_mode=%s", symbol, interval, mode)
        if model_path != requested_model_path:
            logger.info("[AlgoMM] Requested model path %s redirected to %s", requested_model_path, model_path)

    from sklearn.ensemble import HistGradientBoostingClassifier

    X = prepare_X(feat_df, feat_names)
    y = feat_df.loc[X.index, "y"].astype(int).values
    w = feat_df.loc[X.index, "w"].astype(float).values

    clf = HistGradientBoostingClassifier(
        max_depth=4,
        learning_rate=0.06,
        max_iter=250,
        l2_regularization=1.0,
    )
    clf.fit(X, y, sample_weight=w)

    os.makedirs(os.path.dirname(model_path), exist_ok=True)
    trained_bar_time = now or _latest_index_value(feat_df)
    joblib.dump(
        {
            "model": clf,
            "features": feat_names,
            "interval": interval,
            "symbol": symbol,
            "algo_name": getattr(cfg, "algo_name", None),
            "feature_set": getattr(cfg, "feature_set", None),
            "trained_at": datetime.now().isoformat(),
            "trained_at_bar_time": str(trained_bar_time) if trained_bar_time is not None else None,
            "trained_rows": int(len(feat_df)),
            "model_refresh_mode": mode,
            "model_max_age_minutes": model_max_age_minutes(cfg),
            "min_new_bars_before_retrain": min_new_bars_before_retrain(cfg),
        },
        model_path,
    )
    if logger:
        logger.info("[AlgoMM] Trained and saved model to %s refresh_mode=%s", model_path, mode)
    return clf, feat_names
