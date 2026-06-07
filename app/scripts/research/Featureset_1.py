#!/usr/bin/env python3
"""
StockWicks AlgoMM feature-set builder.

Commercial version:
- Featureset_1: original compact 12-feature AlgoMM set.
- Featureset_2: Featureset_1 + OBV / VWAP behavior features.
- Live-safe inference builder keeps the latest candle.
- Training builder adds forward labels and drops only unlabeled rows.

Install target:
  /var/stockwicks/clients/ashakil/app/scripts/research/Featureset_1.py / Featureset_2.py
"""

import os
import sys
import logging
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from dotenv import load_dotenv

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

load_dotenv()

logger = logging.getLogger("AlgoMM_FeatureSets")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s (AlgoMM_FeatureSets) %(message)s",
    )

from app.utils.stock.schwab_token import get_valid_access_token  # noqa: E402


ET_TZ = "America/New_York"

_SCHWAB_INTERVALS = {
    "1min": ("day", 10, "minute", 1),
    "5min": ("day", 10, "minute", 5),
    "10min": ("day", 10, "minute", 10),
    "15min": ("day", 10, "minute", 15),
    "30min": ("day", 10, "minute", 30),
    "1d": ("year", 20, "daily", 1),
}

FEATURESET_1 = [
    "ma_slope",
    "momentum",
    "rsi14",
    "macd_hist",
    "vol_expansion",
    "vol_ratio",
    "dist_to_resistance",
    "dist_to_support",
    "market_regime",
    "chop_entropy",
    "price_position",
    "vwap_dev",
]

FEATURESET_2 = [
    *FEATURESET_1,
    "obv_delta",
    "obv_slope",
    "obv_ratio",
    "obv_direction",
    "vwap_dist",
    "vwap_dist_abs",
    "vwap_dist_delta",
    "vwap_slope",
    "above_vwap",
    "below_vwap",
    "moving_toward_vwap",
    "moving_away_from_vwap",
    "vwap_reclaim",
    "vwap_reject",
]

FEATURESETS = {
    "Featureset_1": FEATURESET_1,
    # Backwards-compatible aliases:
    "FeatureSet_1": FEATURESET_1,
    "featureset_1": FEATURESET_1,
    "prod12": FEATURESET_1,
}


def get_feature_columns(feature_set: str = "Featureset_1") -> list[str]:
    """Return the selected model feature column names."""
    name = str(feature_set or "Featureset_1").strip()
    return list(FEATURESETS.get(name, FEATURESET_1))


# -------------------- Schwab fetchers --------------------

def _fetch_price_history(symbol: str, interval: str) -> dict:
    import requests

    if interval not in _SCHWAB_INTERVALS:
        raise ValueError(f"Unsupported interval: {interval}")

    period_type, period, freq_type, freq = _SCHWAB_INTERVALS[interval]
    token = get_valid_access_token()

    url = "https://api.schwabapi.com/marketdata/v1/pricehistory"
    params = {
        "symbol": symbol,
        "periodType": period_type,
        "period": period,
        "frequencyType": freq_type,
        "frequency": freq,
        "needExtendedHoursData": "false",
    }
    headers = {"Authorization": f"Bearer {token}"}

    r = requests.get(url, headers=headers, params=params, timeout=30)
    status = r.status_code
    try:
        j = r.json()
    except Exception:
        j = {}

    logger.info(
        "PriceHistory [%s %s] periodType=%s period=%s freqType=%s freq=%s status=%s",
        symbol, interval, period_type, period, freq_type, freq, status,
    )

    if status != 200:
        raise RuntimeError(f"PriceHistory error {status}: {r.text}")
    return j


def _fetch_price_history_range(symbol: str, interval: str, days: int) -> dict:
    import requests

    if interval not in _SCHWAB_INTERVALS:
        raise ValueError(f"Unsupported interval: {interval}")

    _, _, freq_type, freq = _SCHWAB_INTERVALS[interval]
    token = get_valid_access_token()

    url = "https://api.schwabapi.com/marketdata/v1/pricehistory"
    headers = {"Authorization": f"Bearer {token}"}

    if interval == "1d" or days is None or days <= 10:
        return _fetch_price_history(symbol, interval)

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=int(days))
    step = timedelta(days=9)

    merged: list[dict] = []
    cur_start = start_dt
    while cur_start < end_dt:
        cur_end = min(cur_start + step, end_dt)
        params = {
            "symbol": symbol,
            "startDate": int(cur_start.timestamp() * 1000),
            "endDate": int(cur_end.timestamp() * 1000),
            "frequencyType": freq_type,
            "frequency": freq,
            "needExtendedHoursData": "false",
        }
        r = requests.get(url, headers=headers, params=params, timeout=30)
        try:
            j = r.json()
        except Exception:
            j = {}

        logger.info(
            "PriceHistory range [%s %s] %s -> %s status=%s",
            symbol, interval, cur_start.date(), cur_end.date(), r.status_code,
        )

        if r.status_code == 200 and isinstance(j, dict) and isinstance(j.get("candles"), list):
            merged.extend(j["candles"])
        else:
            logger.warning("Range fetch returned no candles: %s", r.text)

        cur_start = cur_end

    if not merged:
        return {"candles": []}

    seen = set()
    dedup = []
    for c in merged:
        ts = c.get("datetime") or c.get("timestamp")
        if ts is None or ts in seen:
            continue
        seen.add(ts)
        dedup.append(c)

    dedup.sort(key=lambda x: x.get("datetime", x.get("timestamp", 0)))
    return {"candles": dedup}


# -------------------- OHLCV normalization --------------------

def _to_ohlcv_frame(resp_json: dict) -> pd.DataFrame:
    if not resp_json or "candles" not in resp_json:
        return pd.DataFrame()

    rows = resp_json.get("candles") or []
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    ts_col = "datetime" if "datetime" in df.columns else ("timestamp" if "timestamp" in df.columns else None)
    if ts_col is None:
        return pd.DataFrame()

    def first_existing(*names):
        for n in names:
            if n in df.columns:
                return n
        return None

    o_col = first_existing("open", "openPrice")
    h_col = first_existing("high", "highPrice")
    l_col = first_existing("low", "lowPrice")
    c_col = first_existing("close", "closePrice")
    v_col = first_existing("volume", "totalVolume", "shareVolume")

    if not all([o_col, h_col, l_col, c_col]):
        return pd.DataFrame()

    out = pd.DataFrame(
        {
            "ts": pd.to_datetime(df[ts_col], unit="ms", utc=True),
            "open": pd.to_numeric(df[o_col], errors="coerce"),
            "high": pd.to_numeric(df[h_col], errors="coerce"),
            "low": pd.to_numeric(df[l_col], errors="coerce"),
            "close": pd.to_numeric(df[c_col], errors="coerce"),
            "volume": pd.to_numeric(df[v_col], errors="coerce") if v_col else 0.0,
        }
    )
    out = out.dropna(subset=["open", "high", "low", "close"])
    out["volume"] = out["volume"].fillna(0.0)
    out = out.set_index("ts").sort_index()
    if out.index.has_duplicates:
        out = out[~out.index.duplicated(keep="last")]
    return out


def _normalize_ohlcv_input(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    if not isinstance(out.index, pd.DatetimeIndex):
        ts_col = None
        for c in ("ts", "datetime", "timestamp", "time"):
            if c in out.columns:
                ts_col = c
                break
        if ts_col is None:
            return pd.DataFrame()

        if np.issubdtype(out[ts_col].dtype, np.number):
            out.index = pd.to_datetime(out[ts_col], unit="ms", utc=True)
        else:
            out.index = pd.to_datetime(out[ts_col], utc=True)
    else:
        if out.index.tz is None:
            out.index = out.index.tz_localize("UTC")
        else:
            out.index = out.index.tz_convert("UTC")

    rename_map = {
        "openPrice": "open",
        "highPrice": "high",
        "lowPrice": "low",
        "closePrice": "close",
        "totalVolume": "volume",
        "shareVolume": "volume",
    }
    out = out.rename(columns={k: v for k, v in rename_map.items() if k in out.columns})

    required = ["open", "high", "low", "close"]
    if not all(c in out.columns for c in required):
        return pd.DataFrame()

    for c in ["open", "high", "low", "close", "volume"]:
        if c not in out.columns:
            out[c] = 0.0
        out[c] = pd.to_numeric(out[c], errors="coerce")

    out = out.dropna(subset=["open", "high", "low", "close"])
    out["volume"] = out["volume"].fillna(0.0)
    out = out[["open", "high", "low", "close", "volume"]].sort_index()

    if out.index.has_duplicates:
        out = out[~out.index.duplicated(keep="last")]
    return out


# -------------------- Feature helpers --------------------

def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False, min_periods=1).mean()


def _session_vwap(df: pd.DataFrame, interval: str) -> pd.Series:
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float).replace(0, np.nan).ffill().fillna(1.0)
    typical = (high + low + close) / 3.0

    if interval != "1d" and isinstance(df.index, pd.DatetimeIndex):
        idx = df.index if df.index.tz is not None else df.index.tz_localize("UTC")
        session_key = pd.Series(idx.tz_convert(ET_TZ).date, index=df.index)
        cum_pv = (typical * volume).groupby(session_key).cumsum()
        cum_v = volume.groupby(session_key).cumsum()
    else:
        cum_pv = (typical * volume).cumsum()
        cum_v = volume.cumsum()

    return (cum_pv / (cum_v + 1e-8)).replace([np.inf, -np.inf], np.nan).ffill().bfill().fillna(close)


def _obv_features(df: pd.DataFrame, lookback: int = 6) -> pd.DataFrame:
    close = df["close"].astype(float)
    volume = df["volume"].astype(float).replace(0, np.nan).ffill().fillna(1.0)

    direction = np.sign(close.diff().fillna(0.0))
    signed_volume = direction * volume
    obv = signed_volume.cumsum()

    obv_delta = obv.diff().fillna(0.0)
    obv_slope_raw = obv.diff(lookback).fillna(0.0)
    avg_vol = volume.rolling(lookback, min_periods=1).mean().replace(0, np.nan)
    obv_ratio = (obv_slope_raw / (avg_vol * lookback + 1e-8)).clip(-3.0, 3.0).fillna(0.0)
    obv_slope = obv_ratio
    obv_direction = np.sign(obv_ratio).astype(float)

    return pd.DataFrame(
        {
            "obv_delta": (obv_delta / (avg_vol + 1e-8)).clip(-5.0, 5.0).fillna(0.0),
            "obv_slope": obv_slope,
            "obv_ratio": obv_ratio,
            "obv_direction": obv_direction,
        },
        index=df.index,
    )


def _vwap_behavior_features(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    close = df["close"].astype(float)
    vwap = _session_vwap(df, interval)

    vwap_dist = ((close - vwap) / (vwap.abs() + 1e-8)).fillna(0.0)
    vwap_dist_abs = vwap_dist.abs()
    vwap_dist_delta = vwap_dist.diff().fillna(0.0)
    vwap_slope = (vwap.diff() / (vwap.abs() + 1e-8)).fillna(0.0)

    above_vwap = (vwap_dist > 0).astype(float)
    below_vwap = (vwap_dist < 0).astype(float)
    moving_toward_vwap = (vwap_dist_abs < vwap_dist_abs.shift(1)).astype(float).fillna(0.0)
    moving_away_from_vwap = (vwap_dist_abs > vwap_dist_abs.shift(1)).astype(float).fillna(0.0)

    prev_dist = vwap_dist.shift(1).fillna(0.0)
    vwap_reclaim = ((prev_dist <= 0) & (vwap_dist > 0)).astype(float)
    vwap_reject = ((prev_dist >= 0) & (vwap_dist < 0)).astype(float)

    return pd.DataFrame(
        {
            "vwap_dist": vwap_dist,
            "vwap_dist_abs": vwap_dist_abs,
            "vwap_dist_delta": vwap_dist_delta,
            "vwap_slope": vwap_slope,
            "above_vwap": above_vwap,
            "below_vwap": below_vwap,
            "moving_toward_vwap": moving_toward_vwap,
            "moving_away_from_vwap": moving_away_from_vwap,
            "vwap_reclaim": vwap_reclaim,
            "vwap_reject": vwap_reject,
        },
        index=df.index,
    )


def _build_feature_table(df: pd.DataFrame, interval: str = "5min") -> pd.DataFrame:
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float).replace(0, np.nan).ffill().fillna(1.0)

    momentum = close.pct_change(3).fillna(0.0)

    sma20 = close.rolling(20, min_periods=1).mean()
    sma50 = close.rolling(50, min_periods=1).mean()
    ma_slope = (sma20.diff() / (close.abs() + 1e-8)).fillna(0.0)

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14, min_periods=1).mean()
    loss = (-delta).clip(lower=0).rolling(14, min_periods=1).mean()
    rs = gain / (loss + 1e-8)
    rsi14 = (100 - (100 / (1 + rs))).fillna(50.0) / 100.0

    ema12 = _ema(close, 12)
    ema26 = _ema(close, 26)
    macd = ema12 - ema26
    macd_signal = _ema(macd, 9)
    macd_hist = (macd - macd_signal).fillna(0.0)

    std20 = close.rolling(20, min_periods=1).std().fillna(0.0)
    vol_expansion = (4.0 * std20 / (close.abs() + 1e-8)).fillna(0.0)

    vol_sma20 = volume.rolling(20, min_periods=1).mean()
    vol_ratio = (volume / (vol_sma20 + 1e-8)).fillna(1.0)

    recent_high = high.rolling(40, min_periods=1).max()
    recent_low = low.rolling(40, min_periods=1).min()
    dist_to_resistance = ((recent_high - close) / (close.abs() + 1e-8)).fillna(0.0)
    dist_to_support = ((close - recent_low) / (close.abs() + 1e-8)).fillna(0.0)

    market_regime = np.sign((sma20 - sma50).fillna(0.0)).astype(float)

    ret_sign = (close.pct_change().fillna(0.0) > 0).astype(int)
    p_up = ret_sign.rolling(30, min_periods=5).mean().clip(1e-6, 1 - 1e-6)
    chop_entropy = (-(p_up * np.log(p_up) + (1 - p_up) * np.log(1 - p_up))).fillna(0.0)

    price_position = ((close - recent_low) / ((recent_high - recent_low).abs() + 1e-8)).fillna(0.5)

    vwap = _session_vwap(df, interval)
    vwap_dev = ((close - vwap) / (close.abs() + 1e-8)).fillna(0.0)

    base = pd.DataFrame(
        {
            "ma_slope": ma_slope,
            "momentum": momentum,
            "rsi14": rsi14,
            "macd_hist": macd_hist,
            "vol_expansion": vol_expansion,
            "vol_ratio": vol_ratio,
            "dist_to_resistance": dist_to_resistance,
            "dist_to_support": dist_to_support,
            "market_regime": market_regime,
            "chop_entropy": chop_entropy,
            "price_position": price_position,
            "vwap_dev": vwap_dev,
        },
        index=df.index,
    )

    feat = pd.concat(
        [
            base,
            _obv_features(df),
            _vwap_behavior_features(df, interval),
        ],
        axis=1,
    )

    feat = feat.replace([np.inf, -np.inf], np.nan).ffill().bfill().fillna(0.0)
    return feat


# Backwards-compatible name used by older code.
_build_feature_table_slim = _build_feature_table


# -------------------- Labeling --------------------

def _label_forward_magnitude(
    close: pd.Series,
    k: int,
    tz: str = ET_TZ,
    min_move: float = 0.003,
) -> tuple[pd.Series, pd.Series]:
    if close is None or close.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float)

    close = close.astype(float)
    idx_utc = close.index if close.index.tz is not None else close.index.tz_localize("UTC")
    et = idx_utc.tz_convert(tz)
    et_s = pd.Series(et, index=close.index)

    same_day_next = et_s.dt.date.eq(et_s.shift(-k).dt.date)
    fwd = (close.shift(-k) / close - 1.0).where(same_day_next)

    y = pd.Series(np.nan, index=close.index, dtype=float)
    y[fwd > min_move] = 1.0
    y[fwd < -min_move] = 0.0

    # Small moves are still labeled by direction, but with small weight.
    neutral = y.isna() & fwd.notna()
    y[neutral] = (fwd[neutral] > 0).astype(float)

    w = fwd.abs().clip(lower=min_move / 2.0).fillna(0.0)
    return y, w


def build_feature_matrix_from_df(
    df: pd.DataFrame,
    symbol: str = "",
    interval: str = "5min",
    feature_set: str = "Featureset_1",
) -> pd.DataFrame:
    """Inference builder: no labels, no future-row dropping."""
    ohlcv = _normalize_ohlcv_input(df)
    if ohlcv.empty:
        return pd.DataFrame()

    feat = _build_feature_table(ohlcv, interval)
    cols = get_feature_columns(feature_set)
    feat = feat.reindex(columns=cols)
    return feat.replace([np.inf, -np.inf], np.nan).ffill().bfill().fillna(0.0)


def add_forward_labels(
    feat: pd.DataFrame,
    close: pd.Series,
    k_forward: int = 1,
    min_move: float = 0.003,
    drop_unlabeled: bool = True,
) -> pd.DataFrame:
    if feat is None or feat.empty:
        return pd.DataFrame()

    y, w = _label_forward_magnitude(close.reindex(feat.index), k_forward, ET_TZ, min_move=min_move)

    out = feat.copy()
    out["y"] = y.reindex(out.index)
    out["w"] = w.reindex(out.index)

    if drop_unlabeled:
        out = out.dropna(subset=["y", "w"])

    for c in out.columns:
        try:
            out[c] = pd.to_numeric(out[c])
        except (ValueError, TypeError):
            pass

    return out.replace([np.inf, -np.inf], np.nan).ffill().bfill().fillna(0.0)


def build_features_from_df(
    df: pd.DataFrame,
    symbol: str = "",
    interval: str = "5min",
    k_forward: int = 1,
    min_move: float = 0.003,
    drop_unlabeled: bool = False,
    feature_set: str = "Featureset_1",
) -> pd.DataFrame:
    ohlcv = _normalize_ohlcv_input(df)
    if ohlcv.empty:
        return pd.DataFrame()

    feat = build_feature_matrix_from_df(
        df=ohlcv,
        symbol=symbol,
        interval=interval,
        feature_set=feature_set,
    )
    return add_forward_labels(
        feat=feat,
        close=ohlcv["close"],
        k_forward=k_forward,
        min_move=min_move,
        drop_unlabeled=drop_unlabeled,
    )


def build_training_features_from_df(
    df: pd.DataFrame,
    symbol: str = "",
    interval: str = "5min",
    k_forward: int = 1,
    min_move: float = 0.003,
    feature_set: str = "Featureset_1",
) -> pd.DataFrame:
    return build_features_from_df(
        df=df,
        symbol=symbol,
        interval=interval,
        k_forward=k_forward,
        min_move=min_move,
        drop_unlabeled=True,
        feature_set=feature_set,
    )


def build_features(
    symbol: str,
    interval: str,
    days: int = 7,
    k_forward: int = 1,
    history_days: int | None = None,
    feature_set: str = "Featureset_1",
) -> pd.DataFrame:
    hist_days = history_days if history_days is not None else days

    logger.info(
        "Fetching %s days of %s data for %s using %s",
        hist_days, interval, symbol, feature_set,
    )

    if interval != "1d" and hist_days and hist_days > 10:
        raw = _fetch_price_history_range(symbol, interval, int(hist_days))
    else:
        raw = _fetch_price_history(symbol, interval)

    ohlcv = _to_ohlcv_frame(raw)
    if ohlcv.empty:
        logger.error("OHLCV frame is empty for %s %s", symbol, interval)
        return pd.DataFrame()

    if interval != "1d" and days is not None and days > 0:
        et = ohlcv.index.tz_convert(ET_TZ)
        dates = pd.Series(et.date, index=ohlcv.index)
        unique_days = sorted(pd.unique(dates))
        keep_days = unique_days[-int(days):]
        ohlcv = ohlcv[dates.isin(keep_days)]

    feat = build_training_features_from_df(
        df=ohlcv,
        symbol=symbol,
        interval=interval,
        k_forward=k_forward,
        feature_set=feature_set,
    )

    if feat.empty:
        logger.error("Final features empty for %s %s", symbol, interval)
        return pd.DataFrame()

    logger.info(
        "Final feature set %s: %s rows, %s features, up labels=%s down labels=%s",
        feature_set,
        len(feat),
        len(get_feature_columns(feature_set)),
        int((feat["y"] == 1).sum()),
        int((feat["y"] == 0).sum()),
    )
    return feat
