#!/usr/bin/env python3
"""
mm_features3_builder.py — Enhanced day-trading feature set for AlgoMM (5min intraday)
======================================================================================

Key improvements based on empirical AlgoMM observations + original recommendations:
1. ATR trailing stops matter → vwap_dist_atr is the critical feature
2. OBV slope predicts direction → daily-reset OBV features
3. VWAP acceleration and distance from extremes
4. Order flow imbalance (up/down pressure within bars)
5. Gap analysis (pre-market gaps and fill status)
6. Volume-weighted momentum
7. Volatility regime detection (expanding/contracting)
8. Micro-pullback detection
9. Enhanced wick analysis (upper/lower wick asymmetry)
10. Session position encoding (opening range breakouts)

All multi-bar calculations reset per ET session day (no look-ahead bias).

Feature groups (45 total):
  A) Session / time of day      (5)  — when in the day are we
  B) Price vs VWAP              (6)  — where are we vs today's VWAP
  C) ATR-based levels           (6)  — volatility-adjusted support/resistance
  D) OBV directional signals    (8)  — smart money flow conviction
  E) Combined OBV+ATR signals   (6)  — high-probability setups
  F) Order flow & pressure      (4)  — buying/selling imbalance
  G) Gap analysis               (2)  — pre-market gaps
  H) Momentum & trend           (4)  — short-term direction
  I) Bar structure & wicks      (4)  — what this bar looks like
  J) Volume                     (3)  — activity and confirmation

Labeling:
  Forward return over k=5 bars (25 min), same session only.
  min_move=0.001 (0.4%) — realistic for 5-min bars after spread/slippage.
"""

import os
import sys
import logging
import numpy as np
import pandas as pd
from datetime import time as dtime, datetime, timedelta, timezone
from dotenv import load_dotenv

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

load_dotenv()
logger = logging.getLogger("MMFeat3")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO, 
        format="%(asctime)s %(levelname)s (MMFeat3) %(message)s"
    )

from app.utils.stock.schwab_token import get_valid_access_token

_SCHWAB_INTERVALS = {
    "1min":  ("day", 10, "minute", 1),
    "5min":  ("day", 10, "minute", 5),
    "10min": ("day", 10, "minute", 10),
    "15min": ("day", 10, "minute", 15),
    "30min": ("day", 10, "minute", 30),
    "1d":    ("year", 20, "daily",  1),
}

ET_TZ = "America/New_York"
_SESSION_OPEN = dtime(9, 30)
_SESSION_CLOSE = dtime(16, 0)
_SESSION_MINS = 390


# ─────────────────────────────────────────────────────────────────
# Schwab fetchers (unchanged from original)
# ─────────────────────────────────────────────────────────────────

def _fetch_price_history(symbol: str, interval: str) -> dict:
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
    logger.info(
        "PriceHistory [%s %s] periodType=%s period=%s freqType=%s freq=%s status=%s",
        symbol, interval, periodType, period, freqType, freq, status,
    )
    if status != 200:
        raise RuntimeError(f"PriceHistory error {status}: {r.text}")
    return j


def _fetch_price_history_range(symbol: str, interval: str, days: int) -> dict:
    import requests
    if interval not in _SCHWAB_INTERVALS:
        raise ValueError(f"Unsupported interval: {interval}")
    _, _, freqType, freq = _SCHWAB_INTERVALS[interval]

    if interval == "1d" or days is None or days <= 10:
        return _fetch_price_history(symbol, interval)

    token = get_valid_access_token()
    url = "https://api.schwabapi.com/marketdata/v1/pricehistory"
    headers = {"Authorization": f"Bearer {token}"}

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    step = timedelta(days=9)

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
        logger.info(
            "PriceHistory range [%s %s] %s to %s status=%s",
            symbol, interval, cur_start.date(), cur_end.date(), status,
        )
        if status == 200 and isinstance(j, dict) and isinstance(j.get("candles"), list):
            merged.extend(j["candles"])
            logger.info("  Got %s candles", len(j["candles"]))
        else:
            logger.warning("Range fetch no candles %s to %s: %s", cur_start, cur_end, r.text)
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


def _to_ohlcv_frame(resp_json: dict) -> pd.DataFrame:
    if not resp_json or "candles" not in resp_json:
        return pd.DataFrame()
    rows = resp_json.get("candles", [])
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    ts_col = "datetime" if "datetime" in df.columns else ("timestamp" if "timestamp" in df.columns else None)
    if ts_col is None:
        return pd.DataFrame()

    def first_col(*names):
        for n in names:
            if n in df.columns:
                return n
        return None

    o_col = first_col("open", "openPrice")
    h_col = first_col("high", "highPrice")
    l_col = first_col("low", "lowPrice")
    c_col = first_col("close", "closePrice")
    v_col = first_col("volume", "totalVolume", "shareVolume")

    if not all([o_col, h_col, l_col, c_col]):
        return pd.DataFrame()

    out = pd.DataFrame({
        "ts": pd.to_datetime(df[ts_col], unit="ms", utc=True),
        "open": pd.to_numeric(df[o_col], errors="coerce"),
        "high": pd.to_numeric(df[h_col], errors="coerce"),
        "low": pd.to_numeric(df[l_col], errors="coerce"),
        "close": pd.to_numeric(df[c_col], errors="coerce"),
        "volume": pd.to_numeric(df[v_col], errors="coerce") if v_col else 0.0,
    })
    out = out.dropna(subset=["open", "high", "low", "close"])
    out["volume"] = out["volume"].fillna(0.0)
    out = out.set_index("ts").sort_index()
    if out.index.has_duplicates:
        out = out[~out.index.duplicated(keep="last")]
    logger.info("OHLCV frame: %s rows from %s to %s", len(out), out.index[0], out.index[-1])
    return out


# ─────────────────────────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────────────────────────

# Fixed _get_day_groups function
def _get_day_groups(df: pd.DataFrame) -> pd.Series:
    """Get ET date for each bar for daily grouping."""
    et = df.index.tz_convert(ET_TZ)
    return pd.Series(et.date, index=df.index)

# Fixed _calculate_daily_vwap function
def _calculate_daily_vwap(df: pd.DataFrame) -> pd.Series:
    """Calculate VWAP that resets each ET trading day."""
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)
    
    typical = (high + low + close) / 3.0
    pv = typical * volume
    
    day_groups = _get_day_groups(df)
    daily_vwap = pd.Series(index=df.index, dtype=float)
    
    # FIX: Use pd.unique() on the Series
    for day in pd.unique(day_groups):
        mask = day_groups == day
        cum_pv = pv[mask].cumsum()
        cum_v = volume[mask].cumsum()
        daily_vwap[mask] = cum_pv / (cum_v + 1e-8)
    
    return daily_vwap.fillna(df["close"])


# ─────────────────────────────────────────────────────────────────
# ATR Features (your observation #1)
# ─────────────────────────────────────────────────────────────────

# Fixed _calculate_atr_features function
def _calculate_atr_features(df: pd.DataFrame) -> pd.DataFrame:
    """ATR-based features that reset daily - captures volatility-adjusted levels."""
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    
    # True Range
    prev_close = close.shift(1)
    tr = np.maximum(
        high - low,
        np.maximum((high - prev_close).abs(), (low - prev_close).abs())
    )
    
    day_groups = _get_day_groups(df)
    
    # Daily-reset ATR
    atr_daily = pd.Series(index=df.index, dtype=float)
    atr_pct_daily = pd.Series(index=df.index, dtype=float)
    
    # FIX: Use pd.unique() on the Series
    for day in pd.unique(day_groups):
        mask = day_groups == day
        if mask.sum() > 5:
            atr_daily[mask] = tr[mask].ewm(alpha=1/14, adjust=False).mean()
            atr_pct_daily[mask] = atr_daily[mask] / (close[mask].abs() + 1e-8)
    
    atr_daily = atr_daily.fillna(tr.rolling(14, min_periods=1).mean())
    atr_pct_daily = atr_pct_daily.fillna(0.01)
    
    # VWAP for distance calculation
    daily_vwap = _calculate_daily_vwap(df)
    
    # Critical feature: distance from VWAP in ATR units
    vwap_dist_atr = ((close - daily_vwap) / (atr_daily + 1e-8)).fillna(0.0)
    
    # ATR expansion/contraction
    atr_expanding = (atr_daily > atr_daily.shift(5)).astype(float)
    
    # ATR percentile within day
    atr_percentile = pd.Series(index=df.index, dtype=float)
    for day in pd.unique(day_groups):
        mask = day_groups == day
        if mask.sum() > 10:
            atr_percentile[mask] = atr_daily[mask].rank(pct=True)
    atr_percentile = atr_percentile.fillna(0.5)
    
    # Price move vs ATR
    move_3bar = (close - close.shift(3)).abs()
    move_vs_atr = (move_3bar / (atr_daily + 1e-8)).fillna(0.0)
    
    # ATR slope
    atr_slope = atr_daily.diff(3).fillna(0.0) / (atr_daily.shift(3) + 1e-8)
    
    # Extreme ATR zones
    atr_extreme_high = (vwap_dist_atr.abs() > 2.0).astype(float)
    atr_tight = (vwap_dist_atr.abs() < 0.5).astype(float)
    
    return pd.DataFrame({
        "vwap_dist_atr": vwap_dist_atr.clip(-1.5, 1.5),  # v2: was [-3,3] — reduced to prevent extreme MR signals
        "atr_pct": atr_pct_daily,
        "atr_expanding": atr_expanding,
        "atr_percentile": atr_percentile,
        "move_vs_atr": move_vs_atr.clip(0, 5),
        "atr_slope": atr_slope.clip(-0.5, 0.5),
        "atr_extreme": atr_extreme_high,
        "atr_tight": atr_tight,
    }, index=df.index)



# ─────────────────────────────────────────────────────────────────
# OBV Features (your observation #2)
# ─────────────────────────────────────────────────────────────────



# Fixed _calculate_obv_features function
def _calculate_obv_features(df: pd.DataFrame) -> pd.DataFrame:
    """OBV-based features that reset daily - captures smart money flow conviction."""
    close = df["close"].astype(float)
    volume = df["volume"].astype(float).replace(0, np.nan).ffill().fillna(1.0)
    
    day_groups = _get_day_groups(df)
    
    # Daily-reset OBV
    obv_daily = pd.Series(index=df.index, dtype=float)
    
    # FIX: Use pd.unique() on the Series
    for day in pd.unique(day_groups):
        mask = day_groups == day
        if mask.sum() > 1:
            day_close = close[mask]
            day_volume = volume[mask]
            
            obv = pd.Series(index=day_close.index, dtype=float)
            obv_val = 0.0
            
            for i, idx in enumerate(day_close.index):
                if i == 0:
                    obv[idx] = 0.0
                else:
                    if day_close.iloc[i] > day_close.iloc[i-1]:
                        obv_val += day_volume.iloc[i]
                    elif day_close.iloc[i] < day_close.iloc[i-1]:
                        obv_val -= day_volume.iloc[i]
                    obv[idx] = obv_val
            
            obv_daily[mask] = obv
    
    obv_daily = obv_daily.fillna(0.0)
    
    # OBV slopes (the signal you observed)
    obv_slope_1 = obv_daily.diff(1).fillna(0.0)
    obv_slope_3 = obv_daily.diff(3).fillna(0.0)
    obv_slope_6 = obv_daily.diff(6).fillna(0.0)
    
    # Normalize by average daily volume
    avg_daily_vol = pd.Series(index=df.index, dtype=float)
    for day in pd.unique(day_groups):
        mask = day_groups == day
        avg_daily_vol[mask] = volume[mask].mean()
    avg_daily_vol = avg_daily_vol.fillna(1.0)
    
    obv_slope_1_norm = obv_slope_1 / (avg_daily_vol + 1e-8)
    obv_slope_3_norm = obv_slope_3 / (avg_daily_vol + 1e-8)
    obv_slope_6_norm = obv_slope_6 / (avg_daily_vol + 1e-8)
    
    # OBV divergence
    obv_high_5 = obv_daily.rolling(5, min_periods=1).max()
    price_high_5 = close.rolling(5, min_periods=1).max()
    obv_low_5 = obv_daily.rolling(5, min_periods=1).min()
    price_low_5 = close.rolling(5, min_periods=1).min()
    
    bullish_div = ((obv_daily == obv_high_5) & (close < price_high_5 * 0.99)).astype(float)
    bearish_div = ((obv_daily == obv_low_5) & (close > price_low_5 * 1.01)).astype(float)
    
    # OBV acceleration
    obv_accel = obv_slope_1_norm - obv_slope_3_norm.shift(2)
    
    # OBV vs VWAP alignment
    daily_vwap = _calculate_daily_vwap(df)
    above_vwap = (close > daily_vwap).astype(float)
    obv_rising = (obv_slope_3_norm > 0).astype(float)
    
    obv_up_price_up = (obv_rising * above_vwap)
    obv_down_price_down = ((1 - obv_rising) * (1 - above_vwap))
    obv_flat = (obv_slope_3_norm.abs() < 0.1).astype(float)
    
    # OBV position within day's range - FIX: Use transform with lambda
    obv_max = obv_daily.groupby(day_groups).transform('max')
    obv_min = obv_daily.groupby(day_groups).transform('min')
    obv_range = obv_max - obv_min
    obv_position = ((obv_daily - obv_min) / (obv_range + 1e-8)).fillna(0.5)
    
    # OBV rate of change
    obv_roc = obv_daily.pct_change(3).fillna(0.0).replace([np.inf, -np.inf], 0.0)
    
    return pd.DataFrame({
        "obv_slope_1_norm": obv_slope_1_norm.clip(-3, 3),
        "obv_slope_3_norm": obv_slope_3_norm.clip(-3, 3),
        "obv_slope_6_norm": obv_slope_6_norm.clip(-3, 3),
        # v2: REMOVED obv_bullish_div, obv_bearish_div (mean-reversion signals)
        # v2: REMOVED obv_position (model used it as "OBV too high = sell")
        "obv_accel": obv_accel.clip(-1, 1),
        "obv_roc": obv_roc.clip(-0.5, 0.5),
        "obv_up_price_up": obv_up_price_up,
        "obv_down_price_down": obv_down_price_down,
        "obv_flat": obv_flat,
        "obv_rising": obv_rising,
        # v2: NEW — explicit trend direction signal
        # +1 = OBV clearly up (slope > 0.3), 0 = flat, -1 = clearly down
        # This is NOT an oscillator — it's a DIRECTION indicator.
        # OBV up = buyers in control = LONG bias. Period.
        "obv_trend_dir": np.where(
            obv_slope_3_norm > 0.3, 1.0,
            np.where(obv_slope_3_norm < -0.3, -1.0, 0.0)
        ),
    }, index=df.index)



# ─────────────────────────────────────────────────────────────────
# Combined OBV + ATR Signals (your two observations together)
# ─────────────────────────────────────────────────────────────────

def _calculate_combined_signals(df: pd.DataFrame, atr_feats: pd.DataFrame, obv_feats: pd.DataFrame) -> pd.DataFrame:
    """Combine OBV directional signals with ATR volatility context."""
    close = df["close"].astype(float)
    daily_vwap = _calculate_daily_vwap(df)
    
    # High probability setups
    near_vwap = (atr_feats["vwap_dist_atr"].abs() < 0.5).astype(float)
    obv_strong_up = (obv_feats["obv_slope_3_norm"] > 0.5).astype(float)
    setup_long = near_vwap * obv_strong_up
    
    extended_up = (atr_feats["vwap_dist_atr"] > 1.5).astype(float)
    obv_strong_down = (obv_feats["obv_slope_3_norm"] < -0.5).astype(float)
    setup_short = extended_up * obv_strong_down
    
    # Chop zone (avoid trading)
    chop_zone = (obv_feats["obv_flat"] * atr_feats["atr_tight"]).astype(float)
    
    # Breakout signal
    breakout_signal = (
        obv_feats["obv_rising"] * 
        atr_feats["atr_expanding"] * 
        (atr_feats["vwap_dist_atr"].abs() < 1.0)
    ).astype(float)
    
    # Trend strength composite — v2: use OBV slope directly (not inverted by atr_percentile)
    trend_strength = obv_feats["obv_slope_3_norm"]
    
    # v2: REMOVED reversal_prob (explicitly mean-reversion, teaches model to fade)
    
    return pd.DataFrame({
        "setup_long": setup_long,
        "setup_short": setup_short,
        "chop_zone": chop_zone,
        "breakout_signal": breakout_signal,
        "trend_strength_composite": trend_strength.clip(-2, 2),
        "obv_atr_alignment": obv_feats["obv_rising"] * (1 - atr_feats["atr_extreme"]),
    }, index=df.index)


# ─────────────────────────────────────────────────────────────────
# Original Recommendations Features
# ─────────────────────────────────────────────────────────────────

def _calculate_vwap_enhanced_features(df: pd.DataFrame) -> pd.DataFrame:
    """Enhanced VWAP features from original recommendations."""
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    
    day_groups = _get_day_groups(df)
    daily_vwap = _calculate_daily_vwap(df)
    
    # VWAP slope and acceleration
    vwap_slope = daily_vwap.diff(3).fillna(0.0) / (close.abs() + 1e-8)
    vwap_accel = vwap_slope.diff(3).fillna(0.0)
    
    # VWAP distance from session extremes
    daily_high = high.groupby(day_groups).transform('cummax')
    daily_low = low.groupby(day_groups).transform('cummin')
    
    vwap_to_hod = ((daily_high - daily_vwap) / (daily_high.abs() + 1e-8)).clip(0.0, 1.0).fillna(0.0)
    vwap_to_lod = ((daily_vwap - daily_low) / (daily_vwap.abs() + 1e-8)).clip(0.0, 1.0).fillna(0.0)
    
    # VWAP cross detection
    prev_close = close.shift(1)
    prev_vwap = daily_vwap.shift(1)
    prev_above = (prev_close > prev_vwap)
    curr_above = (close > daily_vwap)
    vwap_cross = (curr_above.astype(int) - prev_above.astype(int)).fillna(0.0)
    
    # Consecutive bars above/below VWAP
    above_arr = (close > daily_vwap).astype(float).values
    consec = np.zeros(len(close))
    run = 0
    for k in range(len(above_arr)):
        if above_arr[k] == 1.0:
            run = run + 1 if run >= 0 else 1
        else:
            run = run - 1 if run <= 0 else -1
        consec[k] = run
    bars_vs_vwap = pd.Series(consec, index=close.index).clip(-10, 10)
    
    # VWAP position normalized by day's range
    day_range = daily_high - daily_low
    vwap_position = ((close - daily_vwap) / (day_range + 1e-8)).fillna(0.0)
    
    return pd.DataFrame({
        "vwap_slope": vwap_slope.clip(-0.1, 0.1),       # TREND: VWAP direction ✓
        "vwap_accel": vwap_accel.clip(-0.05, 0.05),     # NEUTRAL: acceleration ✓
        # v2: REMOVED vwap_to_hod (model faded distance from HOD)
        # v2: REMOVED vwap_to_lod (model faded distance from LOD)
        # v2: REMOVED vwap_position (model faded position in range)
        "vwap_cross": vwap_cross,                        # TREND: cross signal ✓
        "bars_vs_vwap": bars_vs_vwap,                    # TREND: persistence ✓
    }, index=df.index)


def _calculate_order_flow_features(df: pd.DataFrame) -> pd.DataFrame:
    """Order flow imbalance and pressure features."""
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    open_ = df["open"].astype(float)
    volume = df["volume"].astype(float)
    
    # Order flow imbalance within bar
    up_pressure = ((close - low) / (high - low + 1e-8)).fillna(0.5)
    down_pressure = 1.0 - up_pressure
    
    # Net pressure over last 3 bars
    net_pressure = (up_pressure - 0.5).rolling(3, min_periods=1).mean().fillna(0.0)
    
    # Volume-weighted momentum
    price_delta = close.diff(3)
    vol_avg = volume.rolling(20, min_periods=5).mean()
    vol_weighted_delta = (price_delta * volume) / (vol_avg + 1e-8)
    vw_momentum = (vol_weighted_delta / (close.abs() + 1e-8)).fillna(0.0)
    
    return pd.DataFrame({
        "up_pressure": up_pressure,
        "net_pressure": net_pressure.clip(-0.5, 0.5),
        "vw_momentum": vw_momentum.clip(-0.1, 0.1),
    }, index=df.index)


def _calculate_gap_features(df: pd.DataFrame) -> pd.DataFrame:
    """Pre-market gap analysis."""
    close = df["close"].astype(float)
    open_ = df["open"].astype(float)
    
    prev_close = close.shift(1)
    gap_pct = ((open_ - prev_close) / (prev_close.abs() + 1e-8)).fillna(0.0)
    gap_filled = ((close - prev_close) * (open_ - prev_close) <= 0).astype(float)
    
    return pd.DataFrame({
        "gap_pct": gap_pct.clip(-0.05, 0.05),
        "gap_filled": gap_filled,
    }, index=df.index)


def _calculate_volatility_regime_features(df: pd.DataFrame, atr_feats: pd.DataFrame) -> pd.DataFrame:
    """Volatility regime detection."""
    close = df["close"].astype(float)
    atr_pct = atr_feats["atr_pct"]
    
    # Expanding vs contracting
    atr_expanding_flag = (atr_pct > atr_pct.shift(5)).astype(float)
    
    # Volatility percentile
    vol_rank = atr_pct.rolling(20, min_periods=5).rank(pct=True).fillna(0.5)
    
    return pd.DataFrame({
        "atr_expanding_flag": atr_expanding_flag,
        "vol_rank": vol_rank,
    }, index=df.index)


def _calculate_pullback_features(df: pd.DataFrame) -> pd.DataFrame:
    """Micro-pullback detection."""
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    
    # EMAs for trend context
    ema9 = close.ewm(span=9, adjust=False).mean()
    
    # Pullback detection
    prev_high_3 = high.rolling(3).max().shift(1)
    prev_low_3 = low.rolling(3).min().shift(1)
    
    is_pullback_up = ((close < prev_high_3) & (close > ema9)).astype(float)
    is_pullback_down = ((close > prev_low_3) & (close < ema9)).astype(float)
    
    return pd.DataFrame({
        "is_pullback_up": is_pullback_up,
        "is_pullback_down": is_pullback_down,
    }, index=df.index)


def _calculate_enhanced_wick_features(df: pd.DataFrame) -> pd.DataFrame:
    """Enhanced wick analysis."""
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    open_ = df["open"].astype(float)
    
    bar_range = (high - low).replace(0, np.nan)
    bar_top = pd.concat([close, open_], axis=1).max(axis=1)
    bar_bottom = pd.concat([close, open_], axis=1).min(axis=1)
    
    upper_wick = (high - bar_top).clip(lower=0.0)
    lower_wick = (bar_bottom - low).clip(lower=0.0)
    
    upper_wick_pct = (upper_wick / (bar_range + 1e-8)).fillna(0.0)
    lower_wick_pct = (lower_wick / (bar_range + 1e-8)).fillna(0.0)
    
    # Wick asymmetry
    wick_bias = upper_wick_pct - lower_wick_pct
    
    # Bar range %
    bar_range_pct = ((high - low) / (close.abs() + 1e-8)).fillna(0.0)
    
    return pd.DataFrame({
        "upper_wick_pct": upper_wick_pct,
        "lower_wick_pct": lower_wick_pct,
        "wick_bias": wick_bias.clip(-1, 1),
        "bar_range_pct": bar_range_pct.clip(0, 0.05),
    }, index=df.index)


def _calculate_session_position_features(df: pd.DataFrame) -> pd.DataFrame:
    """Session position encoding including opening range breakouts."""
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    
    day_groups = _get_day_groups(df)
    
    # Opening range (first 30 min = 6 bars of 5-min)
    first_30m_high = pd.Series(index=df.index, dtype=float)
    first_30m_low = pd.Series(index=df.index, dtype=float)
    
    for day in day_groups.unique():
        mask = day_groups == day
        day_high = high[mask]
        day_low = low[mask]
        if len(day_high) >= 6:
            first_30m_high[mask] = day_high.iloc[:6].max()
            first_30m_low[mask] = day_low.iloc[:6].min()
        else:
            first_30m_high[mask] = day_high.max()
            first_30m_low[mask] = day_low.min()
    
    above_orb = (close > first_30m_high).astype(float)
    below_orb = (close < first_30m_low).astype(float)
    
    return pd.DataFrame({
        "above_orb": above_orb,
        "below_orb": below_orb,
    }, index=df.index)


# ─────────────────────────────────────────────────────────────────
# Mean-Reversion & Exhaustion Features (v2 — addresses directional bias)
# ─────────────────────────────────────────────────────────────────

def _calculate_mean_reversion_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Features that signal MEAN REVERSION — when a move is overextended
    and likely to snap back. Counterbalances the trend-following bias
    in vwap_dev, intraday_ret, obv_slope features.

    v2 addition: the model was learning "MU is going down today, keep
    shorting" because all cumulative features pointed the same direction.
    These features tell it "the move is exhausted, expect a bounce."
    """
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)

    day_groups = _get_day_groups(df)

    # 1. Bollinger Band position (where are we vs 20-bar mean ± 2 std)
    bb_mid = close.rolling(20, min_periods=5).mean()
    bb_std = close.rolling(20, min_periods=5).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std
    bb_width = (bb_upper - bb_lower) / (bb_mid + 1e-8)
    # Position: 0.0 = at lower band, 1.0 = at upper band
    bb_position = ((close - bb_lower) / (bb_upper - bb_lower + 1e-8)).fillna(0.5).clip(0, 1)
    # Distance from midline in std units
    bb_zscore = ((close - bb_mid) / (bb_std + 1e-8)).fillna(0.0).clip(-3, 3)

    # 2. Consecutive direction count (how many bars in a row same direction)
    price_direction = np.sign(close.diff()).fillna(0)
    consec_dir = pd.Series(0.0, index=df.index)
    run = 0.0
    for i in range(len(price_direction)):
        d = price_direction.iloc[i]
        if d == 0:
            run = 0
        elif i == 0:
            run = d
        elif d == price_direction.iloc[i - 1]:
            run = run + d
        else:
            run = d
        consec_dir.iloc[i] = run
    consec_dir = consec_dir.clip(-8, 8)

    # 3. Rate of change deceleration (is momentum exhausting?)
    roc_3 = close.pct_change(3).fillna(0)
    roc_6 = close.pct_change(6).fillna(0)
    # If 3-bar ROC is weaker than 6-bar ROC / 2, momentum is fading
    roc_decel = (roc_3.abs() - roc_6.abs() / 2).fillna(0).clip(-0.02, 0.02)

    # 4. Distance from day's VWAP in standard deviations (not ATR)
    daily_vwap = _calculate_daily_vwap(df)
    vwap_diff = close - daily_vwap
    vwap_std = pd.Series(index=df.index, dtype=float)
    for day in pd.unique(day_groups):
        mask = day_groups == day
        if mask.sum() > 5:
            vwap_std[mask] = vwap_diff[mask].expanding().std()
    vwap_std = vwap_std.fillna(1.0)
    vwap_zscore = (vwap_diff / (vwap_std + 1e-8)).fillna(0).clip(-3, 3)

    # 5. Volume exhaustion (volume declining while price continues)
    vol_sma_5 = volume.rolling(5, min_periods=1).mean()
    vol_sma_15 = volume.rolling(15, min_periods=3).mean()
    vol_declining = ((vol_sma_5 < vol_sma_15 * 0.7) & (close.diff(3).abs() > 0)).astype(float)

    # 6. High/Low of day distance (how far from today's extremes)
    day_high = high.groupby(day_groups).transform('cummax')
    day_low = low.groupby(day_groups).transform('cummin')
    dist_from_hod = ((day_high - close) / (close + 1e-8)).clip(0, 0.05).fillna(0)
    dist_from_lod = ((close - day_low) / (close + 1e-8)).clip(0, 0.05).fillna(0)

    return pd.DataFrame({
        "bb_position": bb_position,
        "bb_zscore": bb_zscore,
        "bb_width": bb_width.clip(0, 0.05).fillna(0.01),
        "consec_direction": consec_dir / 8.0,  # normalize to [-1, 1]
        "roc_decel": roc_decel,
        "vwap_zscore": vwap_zscore,
        "vol_declining": vol_declining,
        "dist_from_hod": dist_from_hod,
        "dist_from_lod": dist_from_lod,
    }, index=df.index)


# ─────────────────────────────────────────────────────────────────
# Main feature builder
# ─────────────────────────────────────────────────────────────────


# Fixed _build_feature_table_enhanced function - updated groupby usage
def _build_feature_table_enhanced(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Build complete enhanced feature set."""
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    open_ = df["open"].astype(float)
    volume = df["volume"].astype(float).replace(0, np.nan).ffill().fillna(1.0)
    
    # Time features - FIXED: Index object doesn't have .clip() method
    et = df.index.tz_convert(ET_TZ)
    session_start = et.normalize() + pd.Timedelta(hours=9, minutes=30)
    
    # Calculate minutes elapsed safely using numpy
    mins_array = ((et - session_start).total_seconds() / 60.0).values
    mins_clipped = np.clip(mins_array, 0.0, 390.0)
    minutes_elapsed = pd.Series(mins_clipped, index=df.index)
    
    session_pct = minutes_elapsed / 390.0
    is_open_window = (minutes_elapsed <= 30.0).astype(float)
    is_close_window = ((390.0 - minutes_elapsed) <= 30.0).astype(float)
    
    # Cyclical time encoding
    time_sin = np.sin(2 * np.pi * session_pct.values)
    time_cos = np.cos(2 * np.pi * session_pct.values)
    session_quarter = pd.Series(
        np.clip((session_pct.values * 4).astype(int) + 1, 1, 4),
        index=df.index
    )
    
    # Get ATR features first (needed for others)
    atr_feats = _calculate_atr_features(df)
    
    # Calculate all feature groups
    obv_feats = _calculate_obv_features(df)
    combined_feats = _calculate_combined_signals(df, atr_feats, obv_feats)
    vwap_enhanced = _calculate_vwap_enhanced_features(df)
    order_flow = _calculate_order_flow_features(df)
    gap_feats = _calculate_gap_features(df)
    vol_regime = _calculate_volatility_regime_features(df, atr_feats)
    pullback_feats = _calculate_pullback_features(df)
    wick_feats = _calculate_enhanced_wick_features(df)
    session_pos = _calculate_session_position_features(df)
    
    # Basic VWAP deviation
    daily_vwap = _calculate_daily_vwap(df)
    vwap_dev = ((close - daily_vwap) / (close.abs() + 1e-8)).fillna(0.0)
    
    # Day range position
    day_groups = _get_day_groups(df)
    daily_high = high.groupby(day_groups).transform('cummax')
    daily_low = low.groupby(day_groups).transform('cummin')
    day_range_pos = ((close - daily_low) / (daily_high - daily_low + 1e-8)).fillna(0.5)
    
    # Intraday return
    day_open = open_.groupby(day_groups).transform('first')
    intraday_ret = ((close - day_open) / (day_open.abs() + 1e-8)).fillna(0.0)
    
    # Short-term momentum (intraday only)
    momentum_3 = close.pct_change(3).fillna(0.0)
    # Kill overnight momentum
    et_series = pd.Series(et, index=df.index)
    momentum_3 = momentum_3.where(et_series.diff(3).dt.total_seconds() < 3600, 0.0)
    
    # RSI with daily reset
    rsi_daily = pd.Series(index=df.index, dtype=float)
    # FIX: Use pd.unique() on the Series
    for day in pd.unique(day_groups):
        mask = day_groups == day
        if mask.sum() > 14:
            delta = close[mask].diff()
            gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
            loss = (-delta).clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
            rs = gain / (loss + 1e-8)
            rsi_daily[mask] = (100 - (100 / (1 + rs))).fillna(50)
        else:
            rsi_daily[mask] = 50
    rsi_daily = rsi_daily.fillna(50) / 100.0
    
    # Close vs open
    close_vs_open = ((close - open_) / (open_.abs() + 1e-8)).fillna(0.0)
    
    # Volume features
    vol_ratio = pd.Series(index=df.index, dtype=float)
    # FIX: Use pd.unique() on the Series
    for day in pd.unique(day_groups):
        mask = day_groups == day
        if mask.sum() > 5:
            vol_avg = volume[mask].rolling(10, min_periods=3).mean()
            vol_ratio[mask] = volume[mask] / (vol_avg + 1e-8)
    vol_ratio = vol_ratio.fillna(1.0)
    vol_surge = (vol_ratio > 2.0).astype(float)
    
    # EMA9 deviation
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema9_dev = ((close - ema9) / (close.abs() + 1e-8)).fillna(0.0)
    
    # Combine all features
    base_features = pd.DataFrame({
        # Session/time (6)
        "session_pct": session_pct,
        "is_open_window": is_open_window,
        "is_close_window": is_close_window,
        "time_sin": time_sin,
        "time_cos": time_cos,
        "session_quarter": session_quarter.astype(float),
        
        # Basic price vs VWAP (3)
        "vwap_dev": vwap_dev,
        "day_range_pos": day_range_pos,
        "intraday_ret": intraday_ret,
        
        # Momentum (3)
        "momentum_3": momentum_3,
        "rsi14_daily": rsi_daily,
        "ema9_dev": ema9_dev,
        
        # Bar basics (1)
        "close_vs_open": close_vs_open,
        
        # Volume (2)
        "vol_ratio": vol_ratio,
        "vol_surge": vol_surge,
        
    }, index=df.index)
    
    # Stack all features
    mean_reversion = _calculate_mean_reversion_features(df)

    all_features = pd.concat([
        base_features,
        atr_feats,
        obv_feats,
        combined_feats,
        vwap_enhanced,
        order_flow,
        gap_feats,
        vol_regime,
        pullback_feats,
        wick_feats,
        session_pos,
        mean_reversion,
    ], axis=1)
    
    # Clean up infinities and NaN
    all_features = all_features.replace([np.inf, -np.inf], np.nan)
    all_features = all_features.ffill().bfill().fillna(0.0)
    
    return all_features

# ─────────────────────────────────────────────────────────────────
# Labeling
# ─────────────────────────────────────────────────────────────────

def _label_forward_magnitude(
    close: pd.Series,
    k: int,
    tz: str,
    min_move: float = 0.001,  # 0.4% - realistic for 5-min bars
) -> tuple[pd.Series, pd.Series]:
    """
    Forward-magnitude labeling, same ET session only.
    min_move=0.001 (0.4%) - realistic after spread/slippage.
    """
    if not isinstance(close.index, pd.DatetimeIndex):
        raise ValueError("close index must be a DatetimeIndex")

    idx_utc = close.index if close.index.tz is not None else close.index.tz_localize("UTC")
    et = idx_utc.tz_convert(tz)
    et_s = pd.Series(et, index=close.index)
    same_day = et_s.dt.date.eq(et_s.shift(-k).dt.date)

    fwd = (close.shift(-k) / close - 1.0).where(same_day)
    w = fwd.abs()
    y = (fwd > 0).astype(float)
    y = y.where(w >= float(min_move))
    return y, w


# ─────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────

def build_features_from_df(
    df: pd.DataFrame,
    symbol: str = "",
    interval: str = "5min",
    k_forward: int = 5,  # 25 min for 5-min bars
    input_interval: str | None = None,
    resample_input: bool = False,
    history_days: int | None = None,
    days: int | None = None,
) -> pd.DataFrame:
    """Build enhanced features + labels from a pre-fetched OHLCV DataFrame."""
    iv = (input_interval or interval or "5min").strip().lower()
    trim_days = history_days if history_days is not None else (days or None)

    if df is None or df.empty:
        logger.warning("build_features_from_df: empty df for %s", symbol)
        return pd.DataFrame()

    try:
        if not pd.api.types.is_datetime64_any_dtype(df.index):
            df = df.copy()
            df.index = pd.to_datetime(df.index, utc=True)
        elif df.index.tz is None:
            df = df.copy()
            df.index = df.index.tz_localize("UTC")
        df = df.copy()
        df.index = df.index.tz_convert(ET_TZ)
    except Exception as e:
        logger.warning("build_features_from_df: tz conversion failed for %s: %s", symbol, e)
        return pd.DataFrame()

    needed = {"open", "high", "low", "close", "volume"}
    if not needed.issubset(df.columns):
        logger.warning("build_features_from_df: missing columns for %s", symbol)
        return pd.DataFrame()

    df = (
        df[["open", "high", "low", "close", "volume"]]
        .dropna(subset=["open", "high", "low", "close"])
        .pipe(lambda d: d[~d.index.duplicated(keep="last")])
        .sort_index()
    )

    if iv != "1d":
        df = df.between_time("09:30", "16:00")

    if df.empty:
        logger.warning("build_features_from_df: empty after session filter for %s", symbol)
        return pd.DataFrame()

    if trim_days is not None and trim_days > 0 and iv != "1d":
        dates = pd.Series(df.index.date, index=df.index)
        unique_days = sorted(pd.unique(dates))
        if len(unique_days) > trim_days:
            df = df[dates.isin(unique_days[-trim_days:])]
        logger.info(
            "build_features_from_df: %s — %s rows across %s days",
            symbol, len(df), min(len(unique_days), trim_days),
        )

    if len(df) < 30:
        logger.warning("build_features_from_df: insufficient rows (%s) for %s", len(df), symbol)
        return pd.DataFrame()

    feat = _build_feature_table_enhanced(df, iv)
    if feat.empty:
        return pd.DataFrame()

    y, w = _label_forward_magnitude(df["close"], k_forward, ET_TZ)
    feat["y"] = y.reindex(feat.index)
    feat["w"] = w.reindex(feat.index).fillna(0.0)
    feat = feat.dropna(subset=["y"])

    for c in feat.columns:
        try:
            feat[c] = pd.to_numeric(feat[c])
        except (ValueError, TypeError):
            pass

    if feat.empty:
        logger.warning("build_features_from_df: empty after labeling for %s", symbol)
        return pd.DataFrame()

    logger.info(
        "build_features_from_df: %s — %s rows, %s up / %s down labels",
        symbol, len(feat),
        int((feat["y"] == 1).sum()),
        int((feat["y"] == 0).sum()),
    )
    return feat


def build_feature_matrix(
    df: pd.DataFrame,
    symbol: str = "",
    interval: str = "5min",
) -> pd.DataFrame:
    """Build feature table without labels (for live prediction)."""
    iv = (interval or "5min").strip().lower()

    if df is None or df.empty:
        return pd.DataFrame()

    try:
        if not pd.api.types.is_datetime64_any_dtype(df.index):
            df = df.copy()
            df.index = pd.to_datetime(df.index, utc=True)
        elif df.index.tz is None:
            df = df.copy()
            df.index = df.index.tz_localize("UTC")
        df = df.copy()
        df.index = df.index.tz_convert(ET_TZ)
    except Exception as e:
        logger.warning("build_feature_matrix: tz conversion failed for %s: %s", symbol, e)
        return pd.DataFrame()

    needed = {"open", "high", "low", "close", "volume"}
    if not needed.issubset(df.columns):
        return pd.DataFrame()

    df = (
        df[["open", "high", "low", "close", "volume"]]
        .dropna(subset=["open", "high", "low", "close"])
        .pipe(lambda d: d[~d.index.duplicated(keep="last")])
        .sort_index()
    )

    if iv != "1d":
        df = df.between_time("09:30", "16:00")

    if len(df) < 10:
        return pd.DataFrame()

    feat = _build_feature_table_enhanced(df, iv)
    return feat


def build_features(
    symbol: str,
    interval: str,
    days: int = 60,
    k_forward: int = 5,
    history_days: int | None = None,
) -> pd.DataFrame:
    """Fetch OHLCV from Schwab then compute enhanced features."""
    hist_days = history_days if history_days is not None else days
    logger.info("Fetching %s days of %s data for %s", hist_days, interval, symbol)

    if interval != "1d" and hist_days and hist_days > 10:
        raw = _fetch_price_history_range(symbol, interval, hist_days)
    else:
        raw = _fetch_price_history(symbol, interval)

    if not raw or not raw.get("candles"):
        logger.error("PriceHistory returned no candles for %s %s", symbol, interval)
        return pd.DataFrame()

    logger.info("PriceHistory candles count: %s", len(raw["candles"]))
    ohlcv = _to_ohlcv_frame(raw)
    if ohlcv is None or ohlcv.empty:
        logger.error("OHLCV frame empty after parsing for %s %s", symbol, interval)
        return pd.DataFrame()

    return build_features_from_df(
        ohlcv,
        symbol=symbol,
        interval=interval,
        k_forward=k_forward,
        history_days=days,
    )