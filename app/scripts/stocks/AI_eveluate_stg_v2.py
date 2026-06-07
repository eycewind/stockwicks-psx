# /var/www/stockwicks/app/scripts/stocks/AI_eveluate_stg_v2.py
# AI_eveluate_stg_v2.py
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
import glob, pickle, time
import pandas as pd
import concurrent.futures
import logging
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta

# ── Bootstrap ─────────────────────────────────────────────────────────
load_dotenv()
os.environ['PYTHONIOENCODING'] = 'utf-8'
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("AI_eval_algo3_style")

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_THIS_DIR, "../..")))

# App helpers (read-only)
from app.utils.stock.data_fetch import get_schwab_1min_history, process_interval
from app.utils.stock.indicators import compute_smi

# ── CLI (unchanged) ───────────────────────────────────────────────────
symbol = sys.argv[1].upper()
trade_size = float(sys.argv[2])
user_id = sys.argv[3]

# ── Paths (unchanged outputs) ─────────────────────────────────────────
DATA_DIR = os.getenv('DATA_DIR', '/var/www/stockwicks/data')
user_data_dir = os.path.join(DATA_DIR, str(user_id))
os.makedirs(user_data_dir, exist_ok=True)

intervals_to_test = ['1min', '5min', '15min', '1d']  # mimic Algo3

# ── Cleanup old files (pre-run) ───────────────────────────────────────
def _cleanup_old_outputs():
    pattern = os.path.join(user_data_dir, f"{user_id}_{symbol}_*")
    for path in glob.glob(pattern):
        try: os.remove(path)
        except Exception as e: log.warning(f"Could not remove {path}: {e}")

# ── Common cutoff & snapshot cache ────────────────────────────────────
_INTERVAL_SEC = {"1min": 60, "5min": 300, "15min": 900, "1d": 86400}
LATENCY_SEC = 30             # buffer so a bar just “closed” isn’t half-baked
CACHE_TTL_SEC = 300          # keep 1m snapshot fixed up to 5 minutes (prevents flip-flop)

def _now_utc():
    return datetime.utcnow().replace(tzinfo=timezone.utc)

def _last_closed_bar_time(now_utc, interval: str) -> pd.Timestamp:
    """Return the timestamp of the most recent fully closed bar for this interval."""
    sec = _INTERVAL_SEC.get(interval, 60)
    # subtract latency buffer then floor to interval
    t = now_utc - timedelta(seconds=LATENCY_SEC)
    epoch = int(t.timestamp())
    aligned = epoch - (epoch % sec)
    return pd.Timestamp(aligned, unit="s", tz="UTC")

def _common_cutoff(now_utc) -> pd.Timestamp:
    """Use the MIN of all last-closed times so every interval ends on the same candle time."""
    times = [_last_closed_bar_time(now_utc, iv) for iv in intervals_to_test]
    return min(times)

# ── Technical logic (Algo3 SMI) ───────────────────────────────────────
def get_trade_type(interval: str) -> str:
    return "Day Trade" if interval in {'1min','5min'} else "Swing Trade"

def _cross_up(series: pd.Series, level: float, lookback: int = 3):
    if series is None or len(series) < 2: return False, None
    lb = min(lookback, len(series)-1)
    for k in range(1, lb+1):
        pv, cv = float(series.iloc[-(k+1)]), float(series.iloc[-k])
        if pv < level and cv > level: return True, k
    return False, None

def _cross_down(series: pd.Series, level: float, lookback: int = 3):
    if series is None or len(series) < 2: return False, None
    lb = min(lookback, len(series)-1)
    for k in range(1, lb+1):
        pv, cv = float(series.iloc[-(k+1)]), float(series.iloc[-k])
        if pv > level and cv < level: return True, k
    return False, None

def _backtest_algo3_smi(resampled: pd.DataFrame, lookback: int = 3) -> dict:
    smi = compute_smi(resampled)
    if smi is None or len(smi) < 2:
        return {"trades":0,"wins":0,"total_return":0.0,"success_rate":0.0}

    closes = resampled["close"].astype(float)
    position, entry_px = None, None
    trades = wins = 0
    pnl_sum = 0.0

    for i in range(1, len(resampled)):
        smi_win = smi.iloc[:i+1]
        px = float(closes.iloc[i])

        long_entry,  _ = _cross_up(smi_win, -60.0, lookback)
        long_exit,   _ = _cross_down(smi_win, +60.0, lookback)
        short_entry, _ = _cross_down(smi_win, +60.0, lookback)
        short_exit,  _ = _cross_up(smi_win, -60.0, lookback)

        if position is None:
            if long_entry:  position, entry_px = "long",  px
            elif short_entry: position, entry_px = "short", px
            continue

        if position == "long" and long_exit:
            profit = (px - entry_px) * trade_size
            pnl_sum += profit; trades += 1; wins += (1 if profit>0 else 0)
            position, entry_px = None, None

        elif position == "short" and short_exit:
            profit = (entry_px - px) * trade_size
            pnl_sum += profit; trades += 1; wins += (1 if profit>0 else 0)
            position, entry_px = None, None

    if position is not None and entry_px is not None:
        last_px = float(closes.iloc[-1])
        profit = (last_px - entry_px) * trade_size if position=="long" else (entry_px - last_px) * trade_size
        pnl_sum += profit; trades += 1; wins += (1 if profit>0 else 0)

    sr = (wins / trades * 100.0) if trades>0 else 0.0
    return {"trades":trades,"wins":wins,"total_return":pnl_sum,"success_rate":sr}

# ── Shared snapshot (with cache) ──────────────────────────────────────
_df_1m_snapshot = None
_df_1m_cached_at = 0.0

def _get_snapshot_1m(symbol: str) -> pd.DataFrame:
    global _df_1m_snapshot, _df_1m_cached_at
    now = time.time()
    if _df_1m_snapshot is not None and (now - _df_1m_cached_at) < CACHE_TTL_SEC:
        return _df_1m_snapshot
    df = get_schwab_1min_history(symbol)
    _df_1m_snapshot, _df_1m_cached_at = df, now
    return df

# ── Evaluation per interval (uses common cutoff) ──────────────────────
_COMMON_CUTOFF = None

def evaluate_interval(interval: str):
    try:
        global _COMMON_CUTOFF
        df1 = _get_snapshot_1m(symbol)
        if df1 is None or df1.empty:
            log.warning(f"[{symbol}] No 1min data.")
            return None

        # use a single common cutoff for *all* intervals
        if _COMMON_CUTOFF is None:
            _COMMON_CUTOFF = _common_cutoff(_now_utc())

        _, _, resampled = process_interval(df1, interval, symbol)
        if resampled is None or resampled.empty:
            log.warning(f"[{symbol}] Resample empty for {interval}")
            return None

        # trim to the common cutoff so every interval ends on the same timestamp
        resampled = resampled[resampled.index <= _COMMON_CUTOFF]
        if resampled is None or len(resampled) < 5:
            log.warning(f"[{symbol}] Not enough closed bars for {interval} after cutoff.")
            return None

        stats = _backtest_algo3_smi(resampled, lookback=3)
        # round to cents to avoid invisible microscopic ties changing selection
        total_return = float(round(stats["total_return"], 2))
        success_rate = float(round(stats["success_rate"], 4))  # 0.01% precision

        return {
            "interval": interval,
            "total_return": total_return,
            "success_rate": success_rate,
            "trades": int(stats["trades"]),
            "trade_span": get_trade_type(interval),
        }

    except Exception as ex:
        log.error(f"Error in evaluate_interval({interval}): {ex}", exc_info=True)
        return None

# ── Parallel evaluation (unchanged structure) ─────────────────────────
def evaluate_all_intervals():
    results = []
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = {executor.submit(evaluate_interval, iv): iv for iv in intervals_to_test}
        for fut in concurrent.futures.as_completed(futures):
            r = fut.result()
            if r: results.append(r)
    return results

# ── Stable selection & output (unchanged filename) ────────────────────
def select_best_interval(results):
    if not results: return None
    df = pd.DataFrame(results)
    # deterministic, stable tie-breakers
    df = df.sort_values(
        by=['success_rate', 'total_return', 'trades', 'interval'],
        ascending=[False, False, False, True]
    )
    return df.iloc[0]

def write_ai_recommendation(best):
    output_path = os.path.join(user_data_dir, f"{user_id}_{symbol}_AI_recommend.csv")
    output_df = pd.DataFrame([{
        "Symbol": symbol,
        "AI Recommended Interval": best.get('interval'),
        "Trade Span": best['trade_span'],
        "Success Rate": f"{best['success_rate']:.2f}%",
        "Total Return": f"${best['total_return']:.2f}",
    }])
    output_df.to_csv(output_path, index=False)
    log.info(f"✅ Final AI recommendation saved to {output_path}")
    return output_path

# ── Main ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _cleanup_old_outputs()
    log.info(f"🔍 Evaluating {symbol} across {intervals_to_test} with common cutoff and snapshot cache.")
    results = evaluate_all_intervals()
    if not results:
        log.error("❌ No valid interval results to evaluate."); sys.exit(1)
    best = select_best_interval(results)
    if best is None:
        log.error("❌ Could not determine best interval."); sys.exit(1)
    write_ai_recommendation(best)
