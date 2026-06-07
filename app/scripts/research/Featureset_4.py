#!/usr/bin/env python3
import os, sys, logging
import numpy as np
import pandas as pd
from datetime import time as dtime, datetime, timedelta, timezone
from dotenv import load_dotenv

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

load_dotenv()
logger = logging.getLogger("Featureset_4")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s (Featureset_4) %(message)s")

# --- Schwab auth helper (same one you already use) ---
from app.utils.stock.schwab_token import get_valid_access_token

# --- Supported intervals & Schwab request defaults ---
_SCHWAB_INTERVALS = {
    "1min":  ("day", 10, "minute", 1),
    "5min":  ("day", 10, "minute", 5),
    "10min": ("day", 10, "minute", 10),
    "15min": ("day", 10, "minute", 15),
    "30min": ("day", 10, "minute", 30),
    "1d":    ("year", 20, "daily", 1),  # Increased period for daily
}

ET_TZ = "America/New_York"

FEATURESET_4 = [
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

FEATURESETS = {
    "Featureset_4": FEATURESET_4,
    "FeatureSet_4": FEATURESET_4,
    "featureset_4": FEATURESET_4,
    "legacy_prod12": FEATURESET_4,
}


def get_feature_columns(feature_set: str = "Featureset_4") -> list[str]:
    name = str(feature_set or "Featureset_4").strip()
    return list(FEATURESETS.get(name, FEATURESET_4))

# -------------------- Raw OHLCV fetchers --------------------

def _fetch_price_history(symbol: str, interval: str) -> dict:
    """Single-call Schwab pricehistory (good for 1d or <=10 intraday days)."""
    import requests
    if interval not in _SCHWAB_INTERVALS:
        raise ValueError(f"Unsupported interval: {interval}")
    periodType, period, freqType, freq = _SCHWAB_INTERVALS[interval]

    token = get_valid_access_token()
    url = "https://api.schwabapi.com/marketdata/v1/pricehistory"
    params = {
        "symbol": symbol,
        "periodType": periodType,
        "period": period,
        "frequencyType": freqType,
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
    logger.info(f"PriceHistory [{symbol} {interval}] periodType={periodType} period={period} "
                f"freqType={freqType} freq={freq} status={status}")
    if status != 200:
        raise RuntimeError(f"PriceHistory error {status}: {r.text}")
    return j

def _fetch_price_history_range(symbol: str, interval: str, days: int) -> dict:
    """
    Fetch intraday beyond Schwab's period=10 cap by paging with startDate/endDate.
    Returns {"candles": [...]} merged & de-duplicated.
    """
    import requests
    if interval not in _SCHWAB_INTERVALS:
        raise ValueError(f"Unsupported interval: {interval}")
    _, _, freqType, freq = _SCHWAB_INTERVALS[interval]

    token = get_valid_access_token()
    url = "https://api.schwabapi.com/marketdata/v1/pricehistory"
    headers = {"Authorization": f"Bearer {token}"}

    # Use single call for daily or small intraday windows
    if interval == "1d" or days is None or days <= 10:
        return _fetch_price_history(symbol, interval)

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    step = timedelta(days=9)  # under 10 to avoid server truncation

    merged = []
    cur_start = start_dt
    while cur_start < end_dt:
        cur_end = min(cur_start + step, end_dt)
        params = {
            "symbol": symbol,
            "startDate": int(cur_start.timestamp() * 1000),
            "endDate": int(cur_end.timestamp() * 1000),
            "frequencyType": freqType,
            "frequency": freq,
            "needExtendedHoursData": "false",
        }
        r = requests.get(url, headers=headers, params=params, timeout=30)
        status = r.status_code
        try:
            j = r.json()
        except Exception:
            j = {}
        logger.info(f"PriceHistory range [{symbol} {interval}] {cur_start.date()} → {cur_end.date()} status={status}")
        if status == 200 and isinstance(j, dict) and "candles" in j and isinstance(j["candles"], list):
            merged.extend(j["candles"])
            logger.info(f"  → Got {len(j['candles'])} candles")
        else:
            logger.warning(f"Range fetch returned no candles for window {cur_start} → {cur_end}: {r.text}")
        cur_start = cur_end

    # Deduplicate by timestamp field
    if not merged:
        return {"candles": []}
    seen = set()
    dedup = []
    for c in merged:
        ts = c.get("datetime") or c.get("timestamp")
        if ts is None:
            continue
        if ts in seen:
            continue
        seen.add(ts)
        dedup.append(c)
    dedup.sort(key=lambda x: x.get("datetime", x.get("timestamp", 0)))
    return {"candles": dedup}

# -------------------- JSON → DataFrame --------------------

def _to_ohlcv_frame(resp_json: dict) -> pd.DataFrame:
    """
    Schwab response → DataFrame with columns: open, high, low, close, volume (UTC index).
    Accepts open/high/low/close or openPrice/highPrice/lowPrice/closePrice.
    """
    if not resp_json or "candles" not in resp_json:
        return pd.DataFrame()
    rows = resp_json.get("candles", [])
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
    l_col = first_existing("low",  "lowPrice")
    c_col = first_existing("close","closePrice")
    v_col = first_existing("volume", "totalVolume", "shareVolume")

    if not all([o_col, h_col, l_col, c_col]):
        return pd.DataFrame()

    out = pd.DataFrame({
        "ts":     pd.to_datetime(df[ts_col], unit="ms", utc=True),
        "open":   pd.to_numeric(df[o_col], errors="coerce"),
        "high":   pd.to_numeric(df[h_col], errors="coerce"),
        "low":    pd.to_numeric(df[l_col], errors="coerce"),
        "close":  pd.to_numeric(df[c_col], errors="coerce"),
        "volume": pd.to_numeric(df[v_col], errors="coerce") if v_col else 0.0,
    })
    out = out.dropna(subset=["open","high","low","close"])
    out["volume"] = out["volume"].fillna(0.0)
    out = out.set_index("ts").sort_index()
    if out.index.has_duplicates:
        out = out[~out.index.duplicated(keep="last")]
    
    logger.info(f"OHLCV frame: {len(out)} rows from {out.index[0]} to {out.index[-1]}")
    return out

# -------------------- Feature engineering --------------------

def _rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0.0)
    dn = -delta.clip(upper=0.0)
    roll_up = up.ewm(alpha=1/length, adjust=False).mean()
    roll_dn = dn.ewm(alpha=1/length, adjust=False).mean()
    rs = roll_up / roll_dn.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)

def _true_range(h, l, c_prev):
    return np.maximum(h - l, np.maximum((h - c_prev).abs(), (l - c_prev).abs()))

def _atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    c_prev = df["close"].shift(1)
    tr = _true_range(df["high"], df["low"], c_prev)
    return pd.Series(tr).ewm(alpha=1/length, adjust=False).mean()

def _bollinger_width(x: pd.Series, length: int = 20, mult: float = 2.0) -> pd.Series:
    ma = x.rolling(length).mean()
    sd = x.rolling(length).std()
    upper = ma + mult*sd
    lower = ma - mult*sd
    width = (upper - lower) / ma.replace(0, np.nan)
    return width.fillna(0.0)

def _intraday_session_features(idx_utc: pd.DatetimeIndex) -> pd.DataFrame:
    """Minutes from open/close and open/close windows, ET-aware."""
    et = idx_utc.tz_convert(ET_TZ)
    open_t = dtime(9, 30)
    close_t = dtime(16, 0)

    mins_from_open, mins_to_close, open30, close30 = [], [], [], []
    for ts in et:
        o_dt = ts.replace(hour=open_t.hour, minute=open_t.minute, second=0, microsecond=0)
        c_dt = ts.replace(hour=close_t.hour, minute=close_t.minute, second=0, microsecond=0)
        mfo = max(0, int((ts - o_dt).total_seconds() // 60))
        mtc = max(0, int((c_dt - ts).total_seconds() // 60))
        mins_from_open.append(mfo)
        mins_to_close.append(mtc)
        open30.append(1.0 if 0 <= mfo <= 30 else 0.0)
        close30.append(1.0 if 0 <= mtc <= 30 else 0.0)

    return pd.DataFrame({
        "min_from_open": mins_from_open,
        "min_to_close": mins_to_close,
        "is_open30": open30,
        "is_close30": close30,
    }, index=idx_utc)

def _compute_vwap_dev(df: pd.DataFrame) -> pd.Series:
    """Rolling intraday VWAP per ET day; deviation of close from VWAP (pct)."""
    et = df.index.tz_convert(ET_TZ)
    day = et.date
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    cum_pv = tp * df["volume"]
    out = []
    cum_pv_sum = 0.0
    cum_v_sum = 0.0
    last_day = None
    for i, d in enumerate(day):
        if last_day is None or d != last_day:
            cum_pv_sum = 0.0
            cum_v_sum = 0.0
            last_day = d
        cum_pv_sum += cum_pv.iloc[i]
        cum_v_sum += df["volume"].iloc[i]
        vwap = cum_pv_sum / (cum_v_sum if cum_v_sum != 0 else 1.0)
        out.append((df["close"].iloc[i] - vwap) / (vwap if vwap != 0 else 1.0))
    return pd.Series(out, index=df.index).fillna(0.0)

def _safe_z(x: pd.Series, length: int = 20) -> pd.Series:
    m = x.rolling(length).mean()
    s = x.rolling(length).std()
    z = (x - m) / s.replace(0, np.nan)
    return z.replace([np.inf, -np.inf], 0.0).fillna(0.0)

def _volume_imbalance(df: pd.DataFrame) -> pd.Series:
    """Crude up/down volume imbalance normalized by rolling mean volume."""
    sign = np.sign(df["close"].diff().fillna(0.0))
    uv = (sign.clip(lower=0) * df["volume"])
    dv = ((-sign).clip(lower=0) * df["volume"])
    imb = (uv - dv)
    norm = df["volume"].rolling(20).mean().replace(0, np.nan)
    return (imb / norm).replace([np.inf, -np.inf], 0.0).fillna(0.0)

def _build_feature_table(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    # Core price-based features (most important)
    body = (df["close"] - df["open"]).abs()
    rng = (df["high"] - df["low"]).replace(0, 1.0)
    body_ratio = (body / rng).fillna(0.0)
    
    # Volume features
    vol_z = _safe_z(df["volume"].replace(0, np.nan).ffill(), 20)
    
    # Key technical indicators
    atr14 = _atr(df, 14).fillna(0.0)
    rsi14 = _rsi(df["close"], 14)
    bb_width = _bollinger_width(df["close"], 20, 2.0)
    
    # Returns and volatility
    ret1 = df["close"].pct_change().fillna(0.0)
    rv5 = df["close"].pct_change().rolling(5).std().fillna(0.0)
    
    # Volume imbalance
    vol_imb = _volume_imbalance(df)
    
    # VWAP deviation for intraday
    vwap_dev = _compute_vwap_dev(df) if interval != "1d" else pd.Series(0.0, index=df.index)
    
    # Session features for intraday
    if interval != "1d":
        sess = _intraday_session_features(df.index)
    else:
        sess = pd.DataFrame({
            "min_from_open": np.zeros(len(df)),
            "min_to_close": np.zeros(len(df)),
            "is_open30": np.zeros(len(df)),
            "is_close30": np.zeros(len(df)),
        }, index=df.index)

    # Reduced feature set - only the most predictive ones
    feat = pd.DataFrame({
        # Core price action
        "body_ratio": body_ratio,
        "ATR14": atr14,
        "rsi14": rsi14,
        
        # Volatility and momentum
        "ret1": ret1,
        "rv5": rv5,
        "bb_width": bb_width,
        
        # Volume dynamics
        "vol_z": vol_z,
        "vol_imb": vol_imb,
        
        # Intraday specific
        "vwap_dev": vwap_dev,
    }, index=df.index)

    # Add session features
    feat = pd.concat([feat, sess], axis=1)
    feat = feat.replace([np.inf, -np.inf], np.nan).ffill().bfill().fillna(0.0)
    
    logger.info(f"Feature table: {len(feat)} rows with {len(feat.columns)} features")
    return feat


# -------------------- Trend / Volume / Reversal additions --------------------

def _trend_strength_features(df: pd.DataFrame, short_span: int = 5, long_span: int = 20) -> pd.DataFrame:
    """Recent-vs-longer trend, crossover, and position-in-range features."""
    close = df["close"].astype(float)

    sma_short = close.rolling(short_span, min_periods=1).mean()
    sma_long  = close.rolling(long_span,  min_periods=1).mean()

    recent_ret = close.pct_change(periods=short_span)
    long_ret   = close.pct_change(periods=long_span)

    momentum_accel = recent_ret - long_ret.shift(short_span)

    golden_cross = (sma_short > sma_long).astype(float)
    death_cross  = (sma_short < sma_long).astype(float)

    long_abs = long_ret.abs().replace(0, np.nan)
    recent_trend_strength = recent_ret.abs() / (long_abs + 1e-8)

    recent_high = close.rolling(short_span, min_periods=1).max()
    recent_low  = close.rolling(short_span, min_periods=1).min()
    pr = (recent_high - recent_low).replace(0, np.nan)
    price_position = (close - recent_low) / (pr + 1e-8)

    sma_distance = (sma_short - sma_long) / (sma_long.abs() + 1e-8)
    trend_dir = np.sign(sma_distance).fillna(0.0)

    return pd.DataFrame(
        {
            "trend_accel": momentum_accel.fillna(0.0),
            "golden_cross": golden_cross.fillna(0.0),
            "death_cross": death_cross.fillna(0.0),
            "recent_trend_strength": recent_trend_strength.fillna(0.0),
            "price_position": price_position.fillna(0.5),
            "sma_distance": sma_distance.fillna(0.0),
            "trend_dir": trend_dir.astype(float),
        },
        index=df.index,
    )

def _volume_trend_confirmation(df: pd.DataFrame, span: int = 5) -> pd.DataFrame:
    """Volume confirming price moves (ratio, corr, spikes)."""
    close = df["close"].astype(float)
    open_ = df["open"].astype(float)
    volume = df["volume"].astype(float).replace(0, np.nan).ffill().fillna(1.0)

    price_up = (close > open_).astype(float)
    vol_change = volume.pct_change().fillna(0.0)

    vol_sma = volume.rolling(span, min_periods=1).mean()
    vol_ratio = volume / (vol_sma + 1e-8)

    price_change = close.pct_change().fillna(0.0)

    # Safer rolling corr (avoid NaNs/zero-std)
    vol_price_corr = pd.Series(0.0, index=df.index)
    for i in range(span - 1, len(df)):
        pw = price_change.iloc[i - span + 1 : i + 1]
        vw = vol_change.iloc[i - span + 1 : i + 1]
        if len(pw) >= 2 and pw.std() > 0 and vw.std() > 0:
            corr = pw.corr(vw)
            vol_price_corr.iloc[i] = 0.0 if pd.isna(corr) else float(corr)

    strong_up = (price_change > price_change.rolling(span, min_periods=1).mean()).astype(float)
    high_vol_up = (vol_ratio * strong_up).fillna(0.0)

    return pd.DataFrame(
        {
            "vol_price_corr": vol_price_corr.fillna(0.0),
            "vol_ratio": vol_ratio.fillna(1.0),
            "high_vol_up": high_vol_up.fillna(0.0),
            "vol_confirmation": (price_up * vol_ratio).fillna(0.0),
            "vol_spike": (vol_ratio > 2.0).astype(float),
        },
        index=df.index,
    )

def _detect_reversal_patterns(df: pd.DataFrame, lookback: int = 10) -> pd.DataFrame:
    """Reversal-ish signals: breaks + momentum shift + RSI extremes."""
    close = df["close"].astype(float)
    high  = df["high"].astype(float)
    low   = df["low"].astype(float)
    volume = df["volume"].astype(float).replace(0, np.nan).ffill().fillna(1.0)

    recent_high = high.rolling(lookback, min_periods=1).max()
    recent_low  = low.rolling(lookback, min_periods=1).min()

    from_high = (recent_high - close) / (recent_high.abs() + 1e-8)
    from_low  = (close - recent_low) / (close.abs() + 1e-8)

    broke_high = (close > recent_high.shift(1)).astype(float)
    broke_low  = (close < recent_low.shift(1)).astype(float)

    mom_short = close.pct_change(3).fillna(0.0)
    mom_med   = close.pct_change(8).fillna(0.0)
    momentum_shift = (mom_short - mom_med).fillna(0.0)

    vol_avg = volume.rolling(lookback, min_periods=1).mean()
    vol_spike = (volume / (vol_avg + 1e-8)) > 1.5

    reversal_signal = (((broke_high > 0) | (broke_low > 0)) & vol_spike).astype(float)

    # RSI extremes (use existing rsi14 if present elsewhere; otherwise compute quick RSI)
    if "rsi14" in df.columns:
        rsi = df["rsi14"]
    else:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14, min_periods=1).mean()
        loss = (-delta).clip(lower=0).rolling(14, min_periods=1).mean()
        rs = gain / (loss + 1e-8)
        rsi = 100 - (100 / (1 + rs))
    rsi_extreme = ((rsi < 30) | (rsi > 70)).astype(float)

    return pd.DataFrame(
        {
            "from_recent_high": from_high.fillna(0.0),
            "from_recent_low": from_low.fillna(0.0),
            "broke_high": broke_high.fillna(0.0),
            "broke_low": broke_low.fillna(0.0),
            "momentum_shift": momentum_shift.fillna(0.0),
            "reversal_signal": reversal_signal.fillna(0.0),
            "rsi_extreme": rsi_extreme.fillna(0.0),
        },
        index=df.index,
    )

def add_trend_volume_reversal_features(feat: pd.DataFrame, df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Safest integration: call near the end of _build_feature_table, after base+session features exist."""
    try:
        trend = _trend_strength_features(df)
        vol   = _volume_trend_confirmation(df)
        rev   = _detect_reversal_patterns(df)
        out = pd.concat([feat, trend, vol, rev], axis=1)
        out = out.replace([np.inf, -np.inf], np.nan).ffill().bfill().fillna(0.0)
        return out
    except Exception as e:
        logger.warning(f"add_trend_volume_reversal_features failed: {e}", exc_info=True)
        return feat


def _build_feature_table_slim(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Slim 10-12 feature set for AlgoMM (avoids weak session/correlated fields)."""
    close = df["close"].astype(float)
    high  = df["high"].astype(float)
    low   = df["low"].astype(float)
    open_ = df["open"].astype(float)
    volume = df["volume"].astype(float).replace(0, np.nan).ffill().fillna(1.0)

    # ---- helpers ----
    def _ema(s: pd.Series, span: int) -> pd.Series:
        return s.ewm(span=span, adjust=False, min_periods=1).mean()

    # 1) Momentum (short-term returns)
    momentum = close.pct_change(3).fillna(0.0)

    # 2) MA slope (trend strength proxy)
    sma20 = close.rolling(20, min_periods=1).mean()
    ma_slope = sma20.diff().fillna(0.0) / (close.abs() + 1e-8)

    # 3) RSI 14
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14, min_periods=1).mean()
    loss = (-delta).clip(lower=0).rolling(14, min_periods=1).mean()
    rs = gain / (loss + 1e-8)
    rsi14 = (100 - (100 / (1 + rs))).fillna(50.0)

    # 4) MACD histogram (12/26/9)
    ema12 = _ema(close, 12)
    ema26 = _ema(close, 26)
    macd = ema12 - ema26
    macd_signal = _ema(macd, 9)
    macd_hist = (macd - macd_signal).fillna(0.0)

    # 5) Vol expansion (BB width)
    std20 = close.rolling(20, min_periods=1).std().fillna(0.0)
    bb_width = (4.0 * std20 / (close.abs() + 1e-8)).fillna(0.0)

    # 6) Vol ratio (current vs avg)
    vol_sma20 = volume.rolling(20, min_periods=1).mean()
    vol_ratio = (volume / (vol_sma20 + 1e-8)).fillna(1.0)

    # 7/8) Distance to resistance/support (rolling high/low)
    rh = high.rolling(40, min_periods=1).max()
    rl = low.rolling(40, min_periods=1).min()
    dist_to_resistance = ((rh - close) / (close.abs() + 1e-8)).fillna(0.0)
    dist_to_support = ((close - rl) / (close.abs() + 1e-8)).fillna(0.0)

    # 9) Market regime (simple trend regime: sign of sma20-sma50)
    sma50 = close.rolling(50, min_periods=1).mean()
    market_regime = np.sign((sma20 - sma50).fillna(0.0))  # -1 / 0 / +1

    # 10) Chop entropy (entropy of up/down returns in a window)
    ret_sign = (close.pct_change().fillna(0.0) > 0).astype(int)
    p_up = ret_sign.rolling(30, min_periods=5).mean().clip(1e-6, 1 - 1e-6)
    chop_entropy = (-(p_up * np.log(p_up) + (1 - p_up) * np.log(1 - p_up))).fillna(0.0)

    # 11) Price position in recent range
    price_position = ((close - rl) / ((rh - rl).abs() + 1e-8)).fillna(0.5)

    # 12) VWAP deviation (session-style cumulative VWAP)
    typical = (high + low + close) / 3.0
    cum_pv = (typical * volume).cumsum()
    cum_v = volume.cumsum()
    vwap = (cum_pv / (cum_v + 1e-8)).fillna(close)
    vwap_dev = ((close - vwap) / (close.abs() + 1e-8)).fillna(0.0)

    out = pd.DataFrame(
        {
            "ma_slope": ma_slope,
            "momentum": momentum,
            "rsi14": rsi14 / 100.0,  # scale 0-1
            "macd_hist": macd_hist,
            "vol_expansion": bb_width,
            "vol_ratio": vol_ratio,
            "dist_to_resistance": dist_to_resistance,
            "dist_to_support": dist_to_support,
            "market_regime": market_regime.astype(float),
            "chop_entropy": chop_entropy,
            "price_position": price_position,
            "vwap_dev": vwap_dev,
        },
        index=df.index,
    )

    out = out.replace([np.inf, -np.inf], np.nan).ffill().bfill().fillna(0.0)
    return out

# -------------------- Labeling --------------------

def _label_forward_same_session(close: pd.Series, k: int, tz: str) -> tuple[pd.Series, pd.Series]:
    """
    Forward return/label restricted to same ET session (no overnight).
    Works even when the DatetimeIndex has no fixed frequency.
    """
    # Ensure tz-aware index
    if not isinstance(close.index, pd.DatetimeIndex):
        raise ValueError("close index must be a DatetimeIndex")
    idx_utc = close.index if close.index.tz is not None else close.index.tz_localize("UTC")
    et = idx_utc.tz_convert(tz)

    # Use a Series to shift safely (DatetimeIndex.shift requires a freq)
    et_s = pd.Series(et, index=close.index)
    same_day_next = et_s.dt.date.eq(et_s.shift(-k).dt.date)

    fwd = (close.shift(-k) / close - 1.0).where(same_day_next)
    y = (fwd > 0).astype(float)          # keep NaN where crossing sessions
    w = fwd.abs()
    return y, w

def _label_forward_magnitude(
    close: pd.Series,
    k: int,
    tz: str,
    min_move: float = 0.003,   # 0.3%
) -> tuple[pd.Series, pd.Series]:
    """
    Forward-magnitude labeling restricted to same ET session (no overnight).
    Drops noisy bars by setting y=NaN when abs(fwd_return) < min_move.
    Returns:
      y: {0,1} with NaN for noise
      w: abs(fwd_return) as sample weight
    """
    if not isinstance(close.index, pd.DatetimeIndex):
        raise ValueError("close index must be a DatetimeIndex")
    idx_utc = close.index if close.index.tz is not None else close.index.tz_localize("UTC")
    et = idx_utc.tz_convert(tz)

    et_s = pd.Series(et, index=close.index)
    same_day_next = et_s.dt.date.eq(et_s.shift(-k).dt.date)

    fwd = (close.shift(-k) / close - 1.0).where(same_day_next)

    w = fwd.abs()
    y = (fwd > 0).astype(float)

    # drop noise
    y = y.where(w >= float(min_move))
    return y, w


# -------------------- Public builder --------------------

def build_features(
    symbol: str,
    interval: str,
    days: int = 7,
    k_forward: int = 1,
    history_days: int | None = None
) -> pd.DataFrame:
    """
    Returns DataFrame indexed by UTC with columns:
      [features..., 'y', 'w']  (PURE OHLCV, NO OPTIONS FIELDS)
    - days:      number of ET market days to keep for *feature window*
    - history_days: intraday OHLCV depth to pull (paged when >10); defaults to `days`
    """
    hist_days = history_days if history_days is not None else days

    logger.info(f"Fetching {hist_days} days of {interval} data for {symbol}")
    
    if interval != "1d" and hist_days and hist_days > 10:
        raw = _fetch_price_history_range(symbol, interval, hist_days)
    else:
        raw = _fetch_price_history(symbol, interval)

    if not raw or "candles" not in raw or not raw.get("candles"):
        logger.error(f"PriceHistory returned no candles for {symbol} {interval}")
        return pd.DataFrame()

    logger.info(f"PriceHistory candles count: {len(raw['candles'])}")
    ohlcv = _to_ohlcv_frame(raw)
    if ohlcv is None or ohlcv.empty:
        logger.error(f"OHLCV frame is empty after parsing for {symbol} {interval}")
        return pd.DataFrame()

    # Count actual trading days
    if interval != "1d":
        et = ohlcv.index.tz_convert(ET_TZ)
        dates = pd.Series(et.date, index=ohlcv.index)
        unique_days = sorted(pd.unique(dates))
        logger.info(f"Available trading days in data: {len(unique_days)} from {unique_days[0]} to {unique_days[-1]}")

    # Trim to last N ET market days for intraday feature window (`days`)
    if interval != "1d" and days is not None and days > 0:
        et = ohlcv.index.tz_convert(ET_TZ)
        dates = pd.Series(et.date, index=ohlcv.index)
        unique_days = sorted(pd.unique(dates))
        if len(unique_days) <= days:
            # Use all available days if we don't have enough
            logger.info(f"Using all {len(unique_days)} available trading days")
            keep_days = unique_days
        else:
            keep_days = unique_days[-days:]
            logger.info(f"Keeping last {len(keep_days)} trading days from {keep_days[0]} to {keep_days[-1]}")
        
        ohlcv = ohlcv[dates.isin(keep_days)]
        if ohlcv.empty:
            logger.error(f"No intraday rows after day-slicing for {symbol} {interval}")
            return pd.DataFrame()

    feat = _build_feature_table_slim(ohlcv, interval)
    if feat.empty:
        logger.error(f"Feature table built empty for {symbol} {interval}")
        return pd.DataFrame()

    y, w = _label_forward_magnitude(ohlcv["close"], k_forward, ET_TZ, min_move=0.003)
    feat["y"] = y.reindex(feat.index)
    feat["w"] = w.reindex(feat.index).fillna(0.0)

    feat = feat.dropna(subset=["y"])
    
    # Fix for FutureWarning
    for c in feat.columns:
        try:
            feat[c] = pd.to_numeric(feat[c])
        except (ValueError, TypeError):
            pass  # Keep as-is if conversion fails

    if feat.empty:
        logger.error(f"Final features empty after labeling for {symbol} {interval}")
        return pd.DataFrame()

    logger.info(f"Final feature set: {len(feat)} rows, {sum(feat['y'] == 1)} up labels, {sum(feat['y'] == 0)} down labels")
    return feat


def _normalize_ohlcv_input(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy().sort_index()
    needed = ["open", "high", "low", "close", "volume"]
    for col in needed:
        if col not in out.columns:
            return pd.DataFrame()
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out[needed].dropna(subset=["open", "high", "low", "close"]).sort_index()
    out["volume"] = out["volume"].fillna(0.0)
    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index, utc=True, errors="coerce")
    if out.index.tz is None:
        out.index = out.index.tz_localize("UTC")
    if out.index.has_duplicates:
        out = out[~out.index.duplicated(keep="last")]
    return out


def build_feature_matrix_from_df(
    df: pd.DataFrame,
    symbol: str = "",
    interval: str = "5min",
    feature_set: str = "Featureset_4",
) -> pd.DataFrame:
    """Inference builder for replay/diagnostics; keeps the latest candle."""
    ohlcv = _normalize_ohlcv_input(df)
    if ohlcv.empty:
        return pd.DataFrame()
    feat = _build_feature_table_slim(ohlcv, interval)
    cols = get_feature_columns(feature_set)
    feat = feat.reindex(columns=cols)
    return feat.replace([np.inf, -np.inf], np.nan).ffill().bfill().fillna(0.0)


def build_training_features_from_df(
    df: pd.DataFrame,
    symbol: str = "",
    interval: str = "5min",
    k_forward: int = 1,
    min_move: float = 0.003,
    feature_set: str = "Featureset_4",
) -> pd.DataFrame:
    """Training builder matching production labels, without fetching Schwab."""
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
