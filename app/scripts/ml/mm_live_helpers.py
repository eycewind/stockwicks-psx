# /var/www/stockwicks/app/scripts/ml/mm_live_helpers.py
import os
import logging
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

logger = logging.getLogger("MM_LiveHelpers")

# Default feature set. Kept for backward compatibility with older scripts.
# AlgoMM live/replay usually passes the actual feat_names saved with the model.
DEFAULT_FEATURES = [
    "body_ratio", "u_ratio", "l_ratio", "candle_dying",
    "ATR14", "bb_width", "squeeze",
    "ret1", "rv5", "rv20", "rsi14", "z_close",
    "vol_z", "vol_imb", "vwap_dev",
    "min_from_open", "min_to_close", "is_open30", "is_close30",
]


def _prepare_X(df: pd.DataFrame, feat_names: list[str]) -> pd.DataFrame:
    """Align columns, clean infinities/NAs, and fill with medians/zeros."""
    X = df.reindex(columns=feat_names).copy()
    X = X.replace([np.inf, -np.inf], np.nan).ffill().bfill()
    med = X.median(numeric_only=True)
    X = X.fillna(med).fillna(0.0)
    return X


def _quick_train_and_save(
    symbol: str,
    interval: str,
    builder_days: int,
    k_forward: int,
    model_path: str,
    feature_names: list[str],
    history_days: int | None = None,
) -> None:
    """
    Minimal in-place trainer used by live when a model file is missing.
    Imports lazily to avoid circular deps.
    """
    from app.scripts.research.mm_features2_builder import build_features  # lazy import

    os.makedirs(os.path.dirname(model_path), exist_ok=True)

    feat = build_features(
        symbol,
        interval=interval,
        days=builder_days,
        k_forward=k_forward,
        history_days=history_days,
    )
    if feat is None or feat.empty or len(feat) < 80:
        raise RuntimeError(f"Insufficient data to auto-train {symbol} {interval}")

    X = _prepare_X(feat, feature_names).astype(float)
    y = feat.loc[X.index, "y"].astype(int).values
    w = feat.loc[X.index, "w"].astype(float).values

    clf = HistGradientBoostingClassifier(
        max_depth=4,
        learning_rate=0.06,
        max_iter=400,
        l2_regularization=1.0,
    )
    clf.fit(X, y, sample_weight=w)

    joblib.dump(
        {"model": clf, "features": feature_names, "interval": interval, "symbol": symbol},
        model_path,
    )
    logger.info("[MM_LiveHelpers] Saved model → %s", model_path)


def load_or_train_model(
    model_path: str,
    symbol: str | None = None,
    interval: str | None = None,
    *,
    builder_days: int | None = None,
    history_days: int | None = None,
    feature_names: list[str] | None = None,
):
    """
    Load model file; if missing and symbol/interval provided, auto-train it.

    Env fallbacks:
      ALGOMM_BUILDER_DAYS default 25
      ALGOMM_HISTORY_DAYS default = builder_days
    """
    feature_names = feature_names or DEFAULT_FEATURES

    if os.path.exists(model_path):
        try:
            pack = joblib.load(model_path)
            model = pack["model"]
            feats = pack.get("features") or feature_names
            return model, feats
        except Exception as e:
            logger.error("[MM_LiveHelpers] Failed to load model %s: %s", model_path, e, exc_info=True)

    if symbol and interval:
        bd = int(os.getenv("ALGOMM_BUILDER_DAYS", "25")) if builder_days is None else int(builder_days)
        hd = int(os.getenv("ALGOMM_HISTORY_DAYS", str(bd))) if history_days is None else int(history_days)
        try:
            _quick_train_and_save(symbol, interval, bd, 1, model_path, feature_names, hd)
            pack = joblib.load(model_path)
            model = pack["model"]
            feats = pack.get("features") or feature_names
            return model, feats
        except Exception as e:
            logger.error("[MM_LiveHelpers] Auto-train failed for %s %s: %s", symbol, interval, e, exc_info=True)

    raise FileNotFoundError(f"Model not found and could not train: {model_path}")


def predict_probability(model, df: pd.DataFrame, feat_names: list[str]) -> np.ndarray:
    """
    Return probability of UP move for each row.

    Label convention from mm_features2_builder:
      y = 1 means future return > 0  => UP
      y = 0 means future return <= 0 => DOWN

    This version does NOT blindly assume predict_proba[:, 1] is UP.
    It checks model.classes_ and selects the probability column for class 1.
    """
    try:
        X = _prepare_X(df, feat_names).astype(float)
        if len(X) == 0:
            return np.array([])

        if hasattr(model, "predict_proba"):
            proba = model.predict_proba(X)
            classes = list(getattr(model, "classes_", []))

            up_idx = None
            for target in (1, 1.0, True, "1", "UP", "up", "LONG", "long"):
                if target in classes:
                    up_idx = classes.index(target)
                    break

            if up_idx is None:
                # Backward-compatible fallback, but log loudly.
                up_idx = proba.shape[1] - 1
                logger.warning(
                    "[MM_LiveHelpers] Could not find UP class in classes_=%s; falling back to column %s",
                    classes,
                    up_idx,
                )

            logger.info(
                "[MM_LiveHelpers] predict_probability classes_=%s up_idx=%s proba_shape=%s",
                classes,
                up_idx,
                getattr(proba, "shape", None),
            )
            return proba[:, up_idx]

        # Fallback: normalize raw predictions into [0, 1]
        preds = model.predict(X).astype(float)
        rng = np.ptp(preds) if np.ptp(preds) != 0 else 1.0
        return (preds - np.min(preds)) / rng

    except Exception as e:
        logger.error("[MM_LiveHelpers] predict_probability failed: %s", e, exc_info=True)
        return np.zeros(len(df), dtype=float)
