# /var/www/stockwicks/app/utils/stock/indicators.py
import pandas as pd
import numpy as np

# -------------------------------
# Moving Averages
# -------------------------------
def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average"""
    return series.rolling(window=period, min_periods=1).mean()

def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average"""
    return series.ewm(span=period, adjust=False).mean()

# Internal MA helper for SMI (ema/sma)
def _ma(series: pd.Series, length: int, kind: str = "ema") -> pd.Series:
    k = (kind or "ema").lower()
    if k == "ema":
        return ema(series, length)
    if k == "sma":
        return sma(series, length)
    raise ValueError(f"Unknown MA kind: {kind}")

# -------------------------------
# Volatility
# -------------------------------
def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (ATR)"""
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()

    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.rolling(window=period, min_periods=1).mean()
    return atr

def compute_bollinger_bands(series: pd.Series, period: int = 20, std_dev: int = 2):
    """Bollinger Bands: returns (mid, upper, lower)"""
    mid = sma(series, period)
    std = series.rolling(window=period, min_periods=1).std()
    upper = mid + (std_dev * std)
    lower = mid - (std_dev * std)
    return mid, upper, lower

def compute_keltner_channels(df: pd.DataFrame, period: int = 20, multiplier: float = 2.0):
    """Keltner Channels: returns (mid, upper, lower)"""
    mid = ema(df['close'], period)
    atr = compute_atr(df, period)
    upper = mid + multiplier * atr
    lower = mid - multiplier * atr
    return mid, upper, lower

def compute_donchian_channels(df: pd.DataFrame, period: int = 20):
    """Donchian Channels: returns (upper, lower)"""
    upper = df['high'].rolling(window=period, min_periods=1).max()
    lower = df['low'].rolling(window=period, min_periods=1).min()
    return upper, lower

# -------------------------------
# Momentum
# -------------------------------
def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (RSI)"""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

# ---- Legacy SMI (kept for backward-compat with any old callers) ----
def compute_smi(df: pd.DataFrame, period: int = 14, smooth: int = 3, double_smooth: int = 3) -> pd.Series:
    """
    Stochastic Momentum Index (legacy/simple version).
    NOTE: For Yahoo/TradingView-style SMI use `compute_smi_blau(...)` below.
    """
    hl_mid = (df['high'].rolling(period).max() + df['low'].rolling(period).min()) / 2
    diff = df['close'] - hl_mid

    diff_smooth = diff.rolling(smooth).mean()
    hl_range = (df['high'].rolling(period).max() - df['low'].rolling(period).min()).rolling(smooth).mean()

    diff_double = diff_smooth.rolling(double_smooth).mean()
    hl_double = hl_range.rolling(double_smooth).mean()

    smi = 100 * (diff_double / (hl_double / 2))
    return smi

# ---- Yahoo/TradingView-style Blau SMI (returns %K and %D) ----
def compute_smi_blau(
    df: pd.DataFrame,
    length: int = 10,   # %K lookback (Yahoo default 10)
    r: int = 3,         # first smoothing (Yahoo default 3)
    s: int = 3,         # second smoothing (Yahoo default 3)
    sig: int = 10,      # %D length (Yahoo default 10)
    ma: str = "ema",    # smoothing type: "ema" or "sma" (Yahoo uses EMA)
):
    """
    William Blau Stochastic Momentum Index (Yahoo/TradingView style).
    Returns tuple: (smi_k, smi_d) where:
      - smi_k is %K (main line, range roughly [-100, +100])
      - smi_d is %D = MA(smi_k, sig, ma)  (signal line)

    Requires df columns: high, low, close.
    Index should be time-like, unique, and sorted.
    """
    if df is None or df.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float)

    for col in ("high", "low", "close"):
        if col not in df.columns:
            raise ValueError(f"compute_smi_blau: missing column '{col}'")

    h = df["high"].astype(float)
    l = df["low"].astype(float)
    c = df["close"].astype(float)

    hh = h.rolling(length, min_periods=length).max()
    ll = l.rolling(length, min_periods=length).min()

    m  = (hh + ll) / 2.0         # middle of range
    d  = c - m                   # distance of close from mid
    hl = hh - ll                 # total range

    d1  = _ma(d,  r, ma)
    d2  = _ma(d1, s, ma)
    hl1 = _ma(hl, r, ma)
    hl2 = _ma(hl1, s, ma)

    denom = (hl2 / 2.0).replace(0, np.nan)
    smi_k = 100.0 * (d2 / denom)
    smi_k = smi_k.replace([np.inf, -np.inf], np.nan)

    smi_d = _ma(smi_k, sig, ma) if (sig and sig > 0) else None
    return smi_k, smi_d

def compute_macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD line, Signal line, Histogram"""
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

def compute_stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3):
    """Stochastic Oscillator: returns (%K, %D)"""
    lowest_low = df['low'].rolling(window=k_period, min_periods=1).min()
    highest_high = df['high'].rolling(window=k_period, min_periods=1).max()
    k = 100 * (df['close'] - lowest_low) / (highest_high - lowest_low)
    d = k.rolling(window=d_period, min_periods=1).mean()
    return k, d

def compute_williams_r(df: pd.DataFrame, period: int = 14):
    """Williams %R"""
    highest_high = df['high'].rolling(window=period, min_periods=1).max()
    lowest_low = df['low'].rolling(window=period, min_periods=1).min()
    wr = -100 * (highest_high - df['close']) / (highest_high - lowest_low)
    return wr

def compute_cci(df: pd.DataFrame, period: int = 20):
    """Commodity Channel Index (CCI)"""
    tp = (df['high'] + df['low'] + df['close']) / 3
    ma_ = tp.rolling(period).mean()
    md = (tp - ma_).abs().rolling(period).mean()
    cci = (tp - ma_) / (0.015 * md)
    return cci

def compute_adx(df: pd.DataFrame, period: int = 14):
    """Average Directional Index (ADX)"""
    high, low, close = df['high'], df['low'], df['close']
    plus_dm = high.diff()
    minus_dm_raw = low.diff()

    # +DM and -DM components
    plus_dm = np.where((plus_dm > 0) & (plus_dm > (-minus_dm_raw)), plus_dm, 0.0)
    minus_dm = np.where((minus_dm_raw < 0) & ((-minus_dm_raw) > plus_dm), -minus_dm_raw, 0.0)

    tr = pd.concat([
        (high - low),
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)

    atr = tr.rolling(window=period, min_periods=1).mean()

    plus_di = 100 * (pd.Series(plus_dm, index=high.index).rolling(period).sum() / atr)
    minus_di = 100 * (pd.Series(minus_dm, index=high.index).rolling(period).sum() / atr)

    dx = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di))
    adx = dx.rolling(period).mean()
    return adx

# -------------------------------
# Volume-based
# -------------------------------
def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """Volume Weighted Average Price (VWAP)"""
    pv = (df['close'] * df['volume']).cumsum()
    vol = df['volume'].cumsum()
    vwap = pv / vol
    return vwap

def compute_obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume (OBV)"""
    obv = (np.sign(df['close'].diff()) * df['volume']).fillna(0).cumsum()
    return obv
def compute_smi_blau_smooth(df, n=13, m=25, smooth_k=5, smooth_d=3):
    """
    Blau's SMI with extra smoothing for less noise.
    """
    H = df['high'].rolling(n).max()
    L = df['low'].rolling(n).min()
    M = (H + L) / 2
    D = (H - L) / 2

    smi = 100 * (df['close'] - M) / D
    K = smi.ewm(span=m).mean()

    # Extra smoothing to reduce whipsaw
    K = K.ewm(span=smooth_k).mean()
    D = K.ewm(span=smooth_d).mean()

    return K, D

#### My Indicator ####
import pandas as pd
import numpy as np

def _ema(x: pd.Series, span: int) -> pd.Series:
    return x.ewm(span=span, adjust=False).mean()

def session_vwap(df: pd.DataFrame) -> pd.Series:
    """
    Intraday/session VWAP. Resets each trading day (by date of index).
    Requires columns: close, high, low, volume. Index must be tz-aware or tz-naive consistently.
    """
    # Typical price
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].fillna(0)

    # Group by calendar date (session) and compute running cumulative TP*V / V
    dates = pd.to_datetime(df.index).date
    grp = pd.Series(dates, index=df.index)

    tpv_cum = (tp * vol).groupby(grp).cumsum()
    v_cum   = vol.groupby(grp).cumsum().replace(0, np.nan)

    vwap = tpv_cum / v_cum
    return vwap.rename("vwap")

def compute_vwmi(df: pd.DataFrame, n: int = 30, m: int = 5, s: int = 5):
    """
    Robust VWMI: SMI-style oscillator around session VWAP.
    Uses floor on RS and a warm-up mask.
    Returns (vwmi, signal, vwap)
    """
    import numpy as np
    import pandas as pd

    def _ema(x, span): return x.ewm(span=span, adjust=False).mean()

    # ----- session VWAP
    tp  = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].fillna(0)
    dates = pd.to_datetime(df.index).date
    grp = pd.Series(dates, index=df.index)
    vwap = ((tp * vol).groupby(grp).cumsum() / vol.replace(0, np.nan).groupby(grp).cumsum()).rename("vwap")

    # ----- rolling extremes for half-range
    Hn = df["high"].rolling(n).max()
    Ln = df["low"].rolling(n).min()
    upper = (Hn - vwap).clip(lower=0)
    lower = (vwap - Ln).clip(lower=0)
    half_range = (upper + lower) / 2.0

    # ----- distances & double smoothing
    dist = df["close"] - vwap
    ds = _ema(_ema(dist, m), m)

    # floor the denominator: eps tied to price scale
    price_eps = max(1e-6, float(df["close"].median()) * 1e-4)  # ~1 bp of price
    rs_raw = half_range.replace(0, np.nan)
    rs = _ema(_ema(rs_raw, m), m).clip(lower=price_eps)

    vwmi = 100.0 * (ds / rs)
    signal = _ema(vwmi, s)

    # warm-up mask: require enough bars for rolling + smoothing
    warmup = n + 2*m
    vwmi.iloc[:warmup] = np.nan
    signal.iloc[:warmup] = np.nan

    return vwmi.rename("vwmi"), signal.rename("vwmi_signal"), vwap

def compute_vwmi_atr(df: pd.DataFrame, n: int = 30, m: int = 5, s: int = 5):
    """
    VWMI using ATR/2 as denominator (very stable).
    Returns (vwmi, signal, vwap)
    """
    import numpy as np
    import pandas as pd

    def _ema(x, span): return x.ewm(span=span, adjust=False).mean()

    # session VWAP
    tp  = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].fillna(0)
    dates = pd.to_datetime(df.index).date
    grp = pd.Series(dates, index=df.index)
    vwap = ((tp * vol).groupby(grp).cumsum() / vol.replace(0, np.nan).groupby(grp).cumsum()).rename("vwap")

    # ATR
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(),
                    (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    atr = _ema(tr, n)

    dist = close - vwap
    ds = _ema(_ema(dist, m), m)

    price_eps = max(1e-6, float(df["close"].median()) * 1e-4)
    rs = _ema(_ema(atr / 2.0, m), m).clip(lower=price_eps)

    vwmi = 100.0 * (ds / rs)
    signal = _ema(vwmi, s)

    warmup = n + 2*m
    vwmi.iloc[:warmup] = np.nan
    signal.iloc[:warmup] = np.nan

    return vwmi.rename("vwmi"), signal.rename("vwmi_signal"), vwap

def _wilders_ma(series: pd.Series, period: int) -> pd.Series:
    """
    Wilder's Moving Average (RMA) with alpha = 1/period.
    This matches Thinkorswim AverageType.WILDERS.
    """
    # Use ewm with alpha=1/period (adjust=False to match Wilder recursion)
    return series.ewm(alpha=1.0/period, adjust=False, min_periods=1).mean()

def _tos_modified_true_range(df: pd.DataFrame, atr_period: int) -> pd.Series:
    """
    Thinkorswim 'modified' trueRange from their ATR trailing stop study.
    Replicates the ToS script:

        HiLo = Min(high - low, 1.5 * Average(high - low, ATRPeriod));
        HRef = if low <= high[1]
                 then high - close[1]
               else (high - close[1]) - 0.5 * (low - high[1]);
        LRef = if high >= low[1]
                 then close[1] - low
               else (close[1] - low) - 0.5 * (low[1] - high);
        trueRange = Max(HiLo, Max(HRef, LRef));
    """
    h  = df["high"].astype(float)
    l  = df["low"].astype(float)
    c1 = df["close"].shift(1).astype(float)
    h1 = h.shift(1)
    l1 = l.shift(1)

    # Average(high - low, ATRPeriod) — ToS "Average" is default SMA here
    hl = (h - l)
    avg_hl = hl.rolling(window=atr_period, min_periods=1).mean()

    hilo = np.minimum(hl, 1.5 * avg_hl)

    # HRef
    cond_href = (l <= h1)
    href_then  = (h - c1)
    href_else  = (h - c1) - 0.5 * (l - h1)
    href = np.where(cond_href, href_then, href_else)

    # LRef
    cond_lref = (h >= l1)
    lref_then = (c1 - l)
    lref_else = (c1 - l) - 0.5 * (l1 - h)
    lref = np.where(cond_lref, lref_then, lref_else)

    # trueRange = max(HiLo, max(HRef, LRef))
    tr = np.maximum(hilo, np.maximum(href, lref))
    tr = pd.Series(tr, index=df.index)

    # Guard against negatives from numerical issues
    tr = tr.clip(lower=0)
    return tr

def compute_atr_tos(
    df: pd.DataFrame,
    period: int = 14,
    trail_type: str = "modified",   # "modified" or "unmodified"
    average_type: str = "wilders",  # must be "wilders" to match ToS
) -> pd.Series:
    """
    ToS-compatible ATR:
      - trail_type = "modified": uses ToS modified TR (as in their trailing stop)
      - trail_type = "unmodified": classic TR = max(H-L, |H-C[1]|, |L-C[1]|)
      - average_type = "wilders": Wilder RMA smoothing (ToS AverageType.WILDERS)
    """
    if trail_type not in {"modified", "unmodified"}:
        raise ValueError("trail_type must be 'modified' or 'unmodified'")

    if average_type.lower() != "wilders":
        raise ValueError("To match ToS, use average_type='wilders'")

    if trail_type == "modified":
        tr = _tos_modified_true_range(df, period)
    else:
        # classic TR
        h  = df["high"].astype(float)
        l  = df["low"].astype(float)
        c1 = df["close"].shift(1).astype(float)
        tr = pd.concat([(h - l).abs(), (h - c1).abs(), (l - c1).abs()], axis=1).max(axis=1)

    atr = _wilders_ma(tr, period)
    atr.name = "atr_tos"
    return atr


def atr_trailing_stop_tos(
    df: pd.DataFrame,
    atr_period: int = 5,
    atr_factor: float = 3.5,
    trail_type: str = "modified",    # "modified" or "unmodified"
) -> pd.Series:
    """
    Reproduces the ToS ATR trailing stop study behavior:
      - trueRange per 'trail_type'
      - Wilder smoothing
      - state machine that flips when price crosses prior trail
    Returns a pd.Series 'atr_trail' aligned to df.index.
    """
    close = df["close"].astype(float)
    atr   = compute_atr_tos(df, period=atr_period, trail_type=trail_type, average_type="wilders")
    loss  = atr_factor * atr

    trail = np.full(len(close), np.nan, dtype=float)
    state = np.full(len(close), 0, dtype=int)  # 0=init, 1=long, 2=short

    for i in range(len(close)):
        if i == 0 or np.isnan(loss.iat[i]):
            continue

        if state[i-1] == 0:
            state[i] = 1
            trail[i] = close.iat[i] - loss.iat[i]
            continue

        prev_state = state[i-1]
        prev_trail = trail[i-1]

        if prev_state == 1:  # long
            if close.iat[i] > prev_trail:
                state[i] = 1
                trail[i] = max(prev_trail, close.iat[i] - loss.iat[i])
            else:
                state[i] = 2
                trail[i] = close.iat[i] + loss.iat[i]
        else:  # short
            if close.iat[i] < prev_trail:
                state[i] = 2
                trail[i] = min(prev_trail, close.iat[i] + loss.iat[i])
            else:
                state[i] = 1
                trail[i] = close.iat[i] - loss.iat[i]

    s = pd.Series(trail, index=df.index, name="atr_trail_tos")
    return s
