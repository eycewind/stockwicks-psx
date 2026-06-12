#!/usr/bin/env python3
"""
MACD-focused feature builder for Algo5_MM.

Algo5_MM uses the same model-only probability engine as the other commercial
MM algos, but this feature set intentionally limits the model inputs to MACD
state, MACD cross behavior, and MACD momentum/acceleration at multiple speeds.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd


logger = logging.getLogger("Featureset_5")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s (Featureset_5) %(message)s",
    )


ET_TZ = "America/New_York"

_SCHWAB_INTERVALS = {
    "1min": ("day", 10, "minute", 1),
    "5min": ("day", 10, "minute", 5),
    "10min": ("day", 10, "minute", 10),
    "15min": ("day", 10, "minute", 15),
    "30min": ("day", 10, "minute", 30),
    "1d": ("year", 20, "daily", 1),
}

FEATURESET_5 = [
    "macd_12_26_line_atr",
    "macd_12_26_signal_atr",
    "macd_12_26_hist_atr",
    "macd_12_26_hist_slope",
    "macd_12_26_hist_accel",
    "macd_12_26_line_slope",
    "macd_12_26_above_signal",
    "macd_12_26_above_zero",
    "macd_12_26_cross_up",
    "macd_12_26_cross_down",
    "macd_12_26_zero_cross_up",
    "macd_12_26_zero_cross_down",
    "macd_12_26_hist_z",
    "macd_12_26_hist_rank",
    "macd_6_13_hist_atr",
    "macd_6_13_hist_slope",
    "macd_6_13_above_signal",
    "macd_6_13_cross_up",
    "macd_6_13_cross_down",
    "macd_19_39_hist_atr",
    "macd_19_39_hist_slope",
    "macd_19_39_above_signal",
    "macd_multi_timeframe_score",
    "macd_hist_slope_alignment",
    "price_vs_ema12_atr",
    "price_vs_ema26_atr",
]

FEATURESETS = {
    "Featureset_5": FEATURESET_5,
    "FeatureSet_5": FEATURESET_5,
    "featureset_5": FEATURESET_5,
    "macd_probability": FEATURESET_5,
    "macd_only": FEATURESET_5,
}


def get_feature_columns(feature_set: str = "Featureset_5") -> list[str]:
    name = str(feature_set or "Featureset_5").strip()
    return list(FEATURESETS.get(name, FEATURESET_5))


def _fetch_price_history(symbol: str, interval: str) -> dict:
    import requests
    from app.utils.stock.schwab_token import get_valid_access_token

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
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"PriceHistory error {r.status_code}: {r.text}")
    return r.json()


def _fetch_price_history_range(symbol: str, interval: str, days: int) -> dict:
    import requests
    from app.utils.stock.schwab_token import get_valid_access_token

    if interval not in _SCHWAB_INTERVALS:
        raise ValueError(f"Unsupported interval: {interval}")
    if interval == "1d" or days is None or days <= 10:
        return _fetch_price_history(symbol, interval)

    _, _, freq_type, freq = _SCHWAB_INTERVALS[interval]
    token = get_valid_access_token()
    url = "https://api.schwabapi.com/marketdata/v1/pricehistory"
    headers = {"Authorization": f"Bearer {token}"}
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
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and isinstance(data.get("candles"), list):
                merged.extend(data["candles"])
        else:
            logger.warning("PriceHistory range returned %s for %s %s", r.status_code, symbol, interval)
        cur_start = cur_end

    seen = set()
    deduped = []
    for candle in merged:
        ts = candle.get("datetime") or candle.get("timestamp")
        if ts is None or ts in seen:
            continue
        seen.add(ts)
        deduped.append(candle)
    deduped.sort(key=lambda c: c.get("datetime", c.get("timestamp", 0)))
    return {"candles": deduped}


def _to_ohlcv_frame(resp_json: dict) -> pd.DataFrame:
    rows = (resp_json or {}).get("candles") or []
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    ts_col = "datetime" if "datetime" in df.columns else ("timestamp" if "timestamp" in df.columns else None)
    if ts_col is None:
        return pd.DataFrame()

    def first_existing(*names: str) -> str | None:
        for name in names:
            if name in df.columns:
                return name
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
        ts_col = next((c for c in ("ts", "datetime", "timestamp", "time") if c in out.columns), None)
        if ts_col is None:
            return pd.DataFrame()
        if np.issubdtype(out[ts_col].dtype, np.number):
            out.index = pd.to_datetime(out[ts_col], unit="ms", utc=True)
        else:
            out.index = pd.to_datetime(out[ts_col], utc=True)
    elif out.index.tz is None:
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
    for col in [*required, "volume"]:
        if col not in out.columns:
            out[col] = 0.0
        out[col] = pd.to_numeric(out[col], errors="coerce")

    out = out.dropna(subset=required)
    out["volume"] = out["volume"].fillna(0.0)
    out = out[["open", "high", "low", "close", "volume"]].sort_index()
    if out.index.has_duplicates:
        out = out[~out.index.duplicated(keep="last")]
    return out


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

    neutral = y.isna() & fwd.notna()
    y[neutral] = (fwd[neutral] > 0).astype(float)
    w = fwd.abs().clip(lower=min_move / 2.0).fillna(0.0)
    return y, w


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=1).mean()


def _true_range(df: pd.DataFrame) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close_prev = df["close"].astype(float).shift(1)
    return pd.concat(
        [
            high - low,
            (high - close_prev).abs(),
            (low - close_prev).abs(),
        ],
        axis=1,
    ).max(axis=1)


def _atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    return _true_range(df).ewm(alpha=1 / length, adjust=False, min_periods=1).mean()


def _safe_z(series: pd.Series, length: int = 50) -> pd.Series:
    mean = series.rolling(length, min_periods=5).mean()
    std = series.rolling(length, min_periods=5).std()
    return ((series - mean) / (std + 1e-8)).replace([np.inf, -np.inf], np.nan).fillna(0.0)


def _rolling_rank(series: pd.Series, length: int = 50) -> pd.Series:
    def pct_rank(values: np.ndarray) -> float:
        if len(values) == 0:
            return 0.5
        latest = values[-1]
        return float(np.mean(values <= latest))

    return (
        series.rolling(length, min_periods=5)
        .apply(pct_rank, raw=True)
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0.5)
    )


def _macd_pack(
    close: pd.Series,
    atr: pd.Series,
    *,
    fast: int,
    slow: int,
    signal_span: int,
    prefix: str,
) -> pd.DataFrame:
    ema_fast = _ema(close, fast)
    ema_slow = _ema(close, slow)
    line = ema_fast - ema_slow
    signal = _ema(line, signal_span)
    hist = line - signal
    atr_safe = atr.replace(0, np.nan).ffill().bfill().fillna(close.abs() * 0.001 + 1e-8)

    prev_hist = hist.shift(1)
    prev_line = line.shift(1)
    prev_signal = signal.shift(1)

    return pd.DataFrame(
        {
            f"{prefix}_line_atr": line / (atr_safe + 1e-8),
            f"{prefix}_signal_atr": signal / (atr_safe + 1e-8),
            f"{prefix}_hist_atr": hist / (atr_safe + 1e-8),
            f"{prefix}_hist_slope": hist.diff() / (atr_safe + 1e-8),
            f"{prefix}_hist_accel": hist.diff().diff() / (atr_safe + 1e-8),
            f"{prefix}_line_slope": line.diff() / (atr_safe + 1e-8),
            f"{prefix}_above_signal": (line > signal).astype(float),
            f"{prefix}_above_zero": (line > 0).astype(float),
            f"{prefix}_cross_up": ((prev_line <= prev_signal) & (line > signal)).astype(float),
            f"{prefix}_cross_down": ((prev_line >= prev_signal) & (line < signal)).astype(float),
            f"{prefix}_zero_cross_up": ((prev_line <= 0) & (line > 0)).astype(float),
            f"{prefix}_zero_cross_down": ((prev_line >= 0) & (line < 0)).astype(float),
            f"{prefix}_hist_z": _safe_z(hist, 50),
            f"{prefix}_hist_rank": _rolling_rank(hist, 50),
        },
        index=close.index,
    )


def _build_feature_table(df: pd.DataFrame, interval: str = "5min") -> pd.DataFrame:
    close = df["close"].astype(float)
    atr14 = _atr(df, 14)

    standard = _macd_pack(close, atr14, fast=12, slow=26, signal_span=9, prefix="macd_12_26")
    fast = _macd_pack(close, atr14, fast=6, slow=13, signal_span=5, prefix="macd_6_13")
    slow = _macd_pack(close, atr14, fast=19, slow=39, signal_span=9, prefix="macd_19_39")

    ema12 = _ema(close, 12)
    ema26 = _ema(close, 26)
    atr_safe = atr14.replace(0, np.nan).ffill().bfill().fillna(close.abs() * 0.001 + 1e-8)

    score = (
        standard["macd_12_26_above_signal"]
        + standard["macd_12_26_above_zero"]
        + fast["macd_6_13_above_signal"]
        + slow["macd_19_39_above_signal"]
    ) / 4.0
    slope_alignment = (
        np.sign(standard["macd_12_26_hist_slope"])
        + np.sign(fast["macd_6_13_hist_slope"])
        + np.sign(slow["macd_19_39_hist_slope"])
    ) / 3.0

    derived = pd.DataFrame(
        {
            "macd_multi_timeframe_score": score,
            "macd_hist_slope_alignment": slope_alignment,
            "price_vs_ema12_atr": (close - ema12) / (atr_safe + 1e-8),
            "price_vs_ema26_atr": (close - ema26) / (atr_safe + 1e-8),
        },
        index=df.index,
    )

    feat = pd.concat([standard, fast, slow, derived], axis=1)
    feat = feat.reindex(columns=FEATURESET_5)
    return feat.replace([np.inf, -np.inf], np.nan).ffill().bfill().fillna(0.0)


def build_feature_matrix_from_df(
    df: pd.DataFrame,
    symbol: str = "",
    interval: str = "5min",
    feature_set: str = "Featureset_5",
) -> pd.DataFrame:
    ohlcv = _normalize_ohlcv_input(df)
    if ohlcv.empty:
        return pd.DataFrame()

    feat = _build_feature_table(ohlcv, interval)
    cols = get_feature_columns(feature_set)
    return feat.reindex(columns=cols).replace([np.inf, -np.inf], np.nan).ffill().bfill().fillna(0.0)


def build_training_features_from_df(
    df: pd.DataFrame,
    symbol: str = "",
    interval: str = "5min",
    k_forward: int = 1,
    min_move: float = 0.003,
    feature_set: str = "Featureset_5",
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
    y, w = _label_forward_magnitude(ohlcv["close"], k_forward, ET_TZ, min_move=min_move)
    feat["y"] = y.reindex(feat.index)
    feat["w"] = w.reindex(feat.index).fillna(0.0)
    feat = feat.dropna(subset=["y"])

    for col in feat.columns:
        try:
            feat[col] = pd.to_numeric(feat[col])
        except (ValueError, TypeError):
            pass

    return feat.replace([np.inf, -np.inf], np.nan).ffill().bfill().fillna(0.0)


def build_features_from_df(
    df: pd.DataFrame,
    symbol: str = "",
    interval: str = "5min",
    k_forward: int = 1,
    min_move: float = 0.003,
    drop_unlabeled: bool = False,
    feature_set: str = "Featureset_5",
) -> pd.DataFrame:
    if drop_unlabeled:
        return build_training_features_from_df(
            df=df,
            symbol=symbol,
            interval=interval,
            k_forward=k_forward,
            min_move=min_move,
            feature_set=feature_set,
        )

    ohlcv = _normalize_ohlcv_input(df)
    if ohlcv.empty:
        return pd.DataFrame()
    feat = build_feature_matrix_from_df(
        df=ohlcv,
        symbol=symbol,
        interval=interval,
        feature_set=feature_set,
    )
    y, w = _label_forward_magnitude(ohlcv["close"], k_forward, ET_TZ, min_move=min_move)
    feat["y"] = y.reindex(feat.index)
    feat["w"] = w.reindex(feat.index).fillna(0.0)
    return feat.replace([np.inf, -np.inf], np.nan).ffill().bfill().fillna(0.0)


def build_features(
    symbol: str,
    interval: str,
    days: int = 7,
    k_forward: int = 1,
    history_days: int | None = None,
    feature_set: str = "Featureset_5",
) -> pd.DataFrame:
    hist_days = history_days if history_days is not None else days
    logger.info("Fetching %s days of %s data for %s using MACD Featureset_5", hist_days, interval, symbol)

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
        logger.error("Final MACD features empty for %s %s", symbol, interval)
        return pd.DataFrame()

    logger.info(
        "Final MACD feature set: %s rows, %s features, up labels=%s down labels=%s",
        len(feat),
        len(get_feature_columns(feature_set)),
        int((feat["y"] == 1).sum()),
        int((feat["y"] == 0).sum()),
    )
    return feat
