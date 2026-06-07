#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/stock_algos/symbol_screener.py
"""
AlgoMM Symbol Screener v3 — WALK-FORWARD VALIDATION
====================================================

v2 was overfitting: trained and tested on the same data, so symbols
like TSLA showed 68% WR in backtest but 0% in live trading.

v3 fixes this with WALK-FORWARD VALIDATION:
  1. Split data into TRAIN (first 70%) and TEST (last 30%)
  2. Train the model ONLY on the train set
  3. Backtest ONLY on the test set (bars the model has never seen)
  4. Run 3 rolling windows for robustness — penalize inconsistency
  5. Skip first 15 min of each day (matches live OPEN_WINDOW)
  6. Per-share stop loss of $8 (prevents ARM-like -$18 blowups)

This produces realistic scores that match live trading performance.

Usage:
  python -m app.scripts.stock_algos.symbol_screener --all
  python -m app.scripts.stock_algos.symbol_screener --top 30
  python -m app.scripts.stock_algos.symbol_screener --symbols MU,AMD,TSLA,ADBE
"""

import os, sys, json, logging, argparse, warnings
from datetime import datetime, time as dtime
from typing import Optional, Dict, List, Tuple, Any
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", category=UserWarning)
os.environ["LOKY_MAX_CPU_COUNT"] = "4"

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

logger = logging.getLogger("SymbolScreener")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [Screener] %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

DATA_ROOT = "/var/www/stockwicks/data"
QQQ_CSV = "/var/www/stockwicks/app/scripts/stocks/qqq_list.csv"
RESULTS_FILE = os.path.join(DATA_ROOT, "symbol_screener_results.json")
LOG_FILE = os.path.join(DATA_ROOT, "symbol_screener.log")

# AlgoMM v2 defaults
LONG_THRESHOLD = 0.55
SHORT_THRESHOLD = 0.45
MIN_PROB_ADVANTAGE = 0.03
LONG_EXIT_THRESHOLD = 0.50
SHORT_EXIT_THRESHOLD = 0.50
COOLDOWN_BARS = 1
PER_SHARE_STOP = 8.0       # v3: per-share stop loss (prevents ARM-like blowups)
MAX_BARS_HELD = 24
INTERVAL = "5min"
K_FORWARD = 1
BUILDER_DAYS = 30
TRAIN_RATIO = 0.70          # 70% train, 30% test
N_ROLLING_WINDOWS = 3       # overlapping walk-forward splits


def _setup_file_logger():
    for h in logger.handlers:
        if hasattr(h, 'baseFilename'):
            return
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        fh = logging.FileHandler(LOG_FILE, mode='a')
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [Screener] %(message)s"))
        logger.addHandler(fh)
    except Exception:
        pass


def load_qqq_symbols(csv_path=QQQ_CSV, top_n=None):
    try:
        df = pd.read_csv(csv_path)
        df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
        symbols = []
        for _, row in df.iterrows():
            sym = str(row.get("symbol", "")).strip()
            if not sym or sym.lower() == "nan":
                continue
            alloc = 0.0
            for col in ["allocation_(%)", "allocation"]:
                if col in row.index:
                    try:
                        alloc = float(row[col])
                    except Exception:
                        pass
                    break
            symbols.append({"symbol": sym, "company": str(row.get("company", "")),
                            "rank": int(row.get("rank", 999)), "allocation_pct": alloc})
        symbols.sort(key=lambda x: x["rank"])
        if top_n:
            symbols = symbols[:top_n]
        logger.info(f"Loaded {len(symbols)} symbols from {csv_path}")
        return symbols
    except Exception as e:
        logger.error(f"Failed to load QQQ list: {e}")
        return []


# ============================================================================
# DATA FETCHING
# ============================================================================

def fetch_and_resample(symbol):
    try:
        from app.scripts.stock_algos.base_wiring import StockBaseRunner
        runner = StockBaseRunner()
        df_raw = runner.fetch_source_bars(symbol, lookback_days=BUILDER_DAYS)
        if df_raw is None or df_raw.empty:
            return None, None
        _, _, df = runner.resample_interval(df_raw, INTERVAL, symbol)
        if df is None or df.empty or len(df) < 60:
            return None, None
        logger.info(f"  {symbol}: {len(df_raw)} raw -> {len(df)} 5min bars")
        return df_raw, df
    except Exception as e:
        logger.error(f"  {symbol}: fetch failed: {e}")
        return None, None


# ============================================================================
# FEATURE BUILDING
# ============================================================================

def build_features(df_raw, df, symbol):
    try:
        from app.scripts.research.mm_features3_builder import (
            build_features_from_df, build_feature_matrix,
        )
        feat_labeled = None
        try:
            feat_labeled = build_features_from_df(
                df_raw, symbol=symbol, interval=INTERVAL,
                k_forward=K_FORWARD, history_days=BUILDER_DAYS,
            )
        except Exception as e:
            logger.warning(f"  {symbol}: labeled features failed: {e}")

        feat_current = None
        try:
            feat_current = build_feature_matrix(df, symbol=symbol, interval=INTERVAL)
        except Exception as e:
            logger.warning(f"  {symbol}: current features failed: {e}")

        lc = len(feat_labeled) if feat_labeled is not None else 0
        cc = len(feat_current) if feat_current is not None else 0
        logger.info(f"  {symbol}: {lc} labeled, {cc} current features")
        return feat_labeled, feat_current
    except Exception as e:
        logger.error(f"  {symbol}: feature build failed: {e}")
        return None, None


# ============================================================================
# WALK-FORWARD SPLIT
# ============================================================================

def split_train_test(feat_df, ratio=TRAIN_RATIO):
    """Split labeled features into train/test by time order."""
    if feat_df is None or feat_df.empty or "y" not in feat_df.columns:
        return None, None
    n = len(feat_df)
    split_idx = int(n * ratio)
    if split_idx < 30 or (n - split_idx) < 10:
        return None, None
    train = feat_df.iloc[:split_idx].copy()
    test = feat_df.iloc[split_idx:].copy()
    return train, test


def get_rolling_splits(feat_df, n_windows=N_ROLLING_WINDOWS):
    """Create overlapping walk-forward windows for robustness."""
    if feat_df is None or feat_df.empty or "y" not in feat_df.columns:
        return []
    n = len(feat_df)
    if n < 100:
        # Not enough data for multiple windows — single split
        pair = split_train_test(feat_df)
        return [pair] if pair[0] is not None else []

    splits = []
    window_size = n  # full dataset for each split
    step = int(n * 0.10)  # shift by 10% each window

    for i in range(n_windows):
        offset = i * step
        end = min(n, n - (n_windows - 1 - i) * step)
        start = max(0, end - window_size)
        if start >= end:
            continue
        subset = feat_df.iloc[start:end]
        pair = split_train_test(subset)
        if pair[0] is not None:
            splits.append(pair)

    if not splits:
        pair = split_train_test(feat_df)
        if pair[0] is not None:
            splits.append(pair)

    return splits


# ============================================================================
# MODEL TRAINING (exact same as algoMM_runner)
# ============================================================================

def train_model(train_df, symbol):
    from sklearn.ensemble import HistGradientBoostingClassifier
    feat_names = [c for c in train_df.columns if c not in ("y", "w")]
    if "y" not in train_df.columns:
        return None, feat_names, {}

    X = train_df[feat_names].replace([np.inf, -np.inf], np.nan).fillna(0.0).astype(float)
    y = train_df.loc[X.index, "y"].astype(int).values
    w = train_df.loc[X.index, "w"].astype(float).values

    stats = {"n_train": len(X), "n_up": int(y.sum()), "n_down": len(y) - int(y.sum()),
             "balance": float(y.mean()) if len(y) > 0 else 0}

    if len(X) < 20:
        return None, feat_names, stats

    try:
        model = HistGradientBoostingClassifier(
            max_depth=4, learning_rate=0.06, max_iter=300,
            early_stopping=True, validation_fraction=0.1,
            random_state=42, verbose=0,
        )
        model.fit(X, y, sample_weight=w)
        return model, feat_names, stats
    except Exception as e:
        logger.warning(f"  {symbol}: train failed: {e}")
        return None, feat_names, stats


# ============================================================================
# PREDICT ON TEST SET ONLY
# ============================================================================

def predict_on_test(model, test_df, feat_names):
    if model is None or test_df is None or test_df.empty:
        return None
    feat_cols = [c for c in feat_names if c not in ("y", "w")]
    available = [c for c in feat_cols if c in test_df.columns]
    X = test_df[available].copy()
    for col in feat_cols:
        if col not in X.columns:
            X[col] = 0.0
    X = X.replace([np.inf, -np.inf], np.nan).fillna(0.0).astype(float)
    try:
        probs = model.predict_proba(X)
        result = test_df.copy()
        result["prob_up"] = probs[:, 1]
        result["prob_down"] = 1.0 - probs[:, 1]
        return result
    except Exception:
        return None


# ============================================================================
# SIMULATE TRADING (with per-share stop + opening filter)
# ============================================================================

@dataclass
class SimTrade:
    side: str
    entry_price: float
    entry_bar: int
    entry_time: str
    exit_price: float = 0.0
    exit_bar: int = 0
    exit_time: str = ""
    pnl: float = 0.0
    exit_reason: str = ""
    bars_held: int = 0


def simulate_trading(df, predictions, symbol):
    """
    Walk-forward backtest on OUT-OF-SAMPLE predictions only.
    Uses per-share stop ($8) and skips first 15 min.
    """
    trades, position, bars_since_close = [], None, 999
    n = min(len(df), len(predictions))

    # Align df index with predictions index
    pred_index = set(predictions.index)

    for i in range(n):
        idx = df.index[i]
        if idx not in pred_index:
            continue

        pred_row = predictions.loc[idx]
        pu = float(pred_row["prob_up"])
        pd_ = float(pred_row["prob_down"])
        price = float(df["close"].iloc[i])
        ts = str(idx)

        bt = None
        try:
            bt = idx.time() if hasattr(idx, 'time') else None
        except Exception:
            pass

        # v3: Skip first 15 min of each day (9:30-9:45)
        if bt and bt < dtime(9, 45):
            continue

        # EXIT LOGIC
        if position is not None:
            position.bars_held = i - position.entry_bar
            should_exit, reason = False, ""

            # Per-share stop (v3: replaces $300 hard stop)
            if position.side == "long":
                per_share_loss = position.entry_price - price
            else:
                per_share_loss = price - position.entry_price
            if per_share_loss >= PER_SHARE_STOP:
                should_exit, reason = True, "STOP"

            # Probability reversal
            if not should_exit:
                if position.side == "long" and pu < LONG_EXIT_THRESHOLD:
                    should_exit, reason = True, "PROB"
                elif position.side == "short" and pd_ < (1.0 - SHORT_EXIT_THRESHOLD):
                    should_exit, reason = True, "PROB"

            # Time exit
            if not should_exit and position.bars_held > MAX_BARS_HELD:
                should_exit, reason = True, "TIME"

            # EOD close
            if not should_exit and bt and bt >= dtime(15, 55):
                should_exit, reason = True, "EOD"

            if should_exit:
                position.pnl = (price - position.entry_price) if position.side == "long" else (position.entry_price - price)
                position.exit_price = price
                position.exit_bar = i
                position.exit_time = ts
                position.exit_reason = reason
                trades.append(position)
                position = None
                bars_since_close = 0
                continue

        # ENTRY LOGIC
        else:
            bars_since_close += 1
            if bars_since_close < COOLDOWN_BARS:
                continue
            if bt and bt >= dtime(15, 45):
                continue

            if pu >= LONG_THRESHOLD and pu > (pd_ + MIN_PROB_ADVANTAGE):
                position = SimTrade(side="long", entry_price=price, entry_bar=i, entry_time=ts)
            elif pd_ >= (1.0 - SHORT_THRESHOLD) and pd_ > (pu + MIN_PROB_ADVANTAGE):
                position = SimTrade(side="short", entry_price=price, entry_bar=i, entry_time=ts)

    # Close open position
    if position and n > 0:
        price = float(df["close"].iloc[-1])
        position.pnl = (price - position.entry_price) if position.side == "long" else (position.entry_price - price)
        position.exit_price = price
        position.exit_bar = n - 1
        position.exit_time = str(df.index[-1])
        position.exit_reason = "END"
        trades.append(position)

    # Stats
    tt = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gp = sum(t.pnl for t in wins)
    gl = abs(sum(t.pnl for t in losses))

    return trades, {
        "total_trades": tt,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / tt * 100, 1) if tt > 0 else 0,
        "total_pnl": round(sum(t.pnl for t in trades), 2),
        "avg_win": round(gp / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(t.pnl for t in losses) / len(losses), 2) if losses else 0,
        "biggest_win": round(max((t.pnl for t in trades), default=0), 2),
        "biggest_loss": round(min((t.pnl for t in trades), default=0), 2),
        "profit_factor": round(gp / gl, 2) if gl > 0 else (99.0 if gp > 0 else 0),
        "avg_bars_held": round(sum(t.bars_held for t in trades) / tt, 1) if tt > 0 else 0,
    }


# ============================================================================
# MODEL QUALITY METRICS
# ============================================================================

def compute_model_metrics(predictions):
    if predictions is None or predictions.empty or "prob_up" not in predictions.columns:
        return {"prob_spread": 0, "pct_actionable": 0, "pct_strong": 0, "prob_std": 0}
    p = predictions["prob_up"]
    act = ((p >= LONG_THRESHOLD) | (p <= SHORT_THRESHOLD)).sum()
    strong = ((p >= 0.65) | (p <= 0.35)).sum()
    return {
        "prob_spread": round(float(p.max() - p.min()), 4),
        "pct_actionable": round(float(act / len(p) * 100), 1),
        "pct_strong": round(float(strong / len(p) * 100), 1),
        "prob_std": round(float(p.std()), 4),
    }


# ============================================================================
# PRICE METRICS
# ============================================================================

def compute_price_metrics(df, symbol):
    r = {"price": 0, "atr_pct": 0, "dollar_move": 0, "avg_volume": 0,
         "dollar_vol_m": 0, "midday_ratio": 0, "trend_eff": 0}
    try:
        c, h, l, v = df["close"].astype(float), df["high"].astype(float), df["low"].astype(float), df["volume"].astype(float)
        price = float(c.iloc[-1])
        r["price"] = round(price, 2)
        if price <= 0:
            return r
        tr = [max(h.iloc[i] - l.iloc[i], abs(h.iloc[i] - c.iloc[i-1]), abs(l.iloc[i] - c.iloc[i-1])) for i in range(1, min(len(df), 200))]
        if len(tr) >= 14:
            atr = pd.Series(tr).rolling(14).mean().iloc[-1]
            r["atr_pct"] = round(float(atr / price * 100), 4)
            r["dollar_move"] = round(float(atr), 2)
        r["avg_volume"] = round(float(v.mean()), 0)
        r["dollar_vol_m"] = round(float((v * c).mean() / 1e6), 1)
        if hasattr(df.index, 'time'):
            mo = v[(df.index.time >= dtime(9, 30)) & (df.index.time <= dtime(10, 30))]
            mi = v[(df.index.time >= dtime(11, 0)) & (df.index.time <= dtime(14, 0))]
            if len(mo) > 0 and mo.mean() > 0:
                r["midday_ratio"] = round(float(mi.mean() / mo.mean()), 3)
        if hasattr(df.index, 'date'):
            effs = []
            for d, g in df.groupby(df.index.date):
                if len(g) < 10:
                    continue
                cc = g["close"].astype(float)
                net = abs(cc.iloc[-1] - cc.iloc[0])
                path = cc.diff().abs().sum()
                if path > 0:
                    effs.append(net / path)
            if effs:
                r["trend_eff"] = round(float(np.mean(effs)), 3)
    except Exception as e:
        logger.warning(f"  {symbol}: price metrics failed: {e}")
    return r


# ============================================================================
# COMPOSITE SCORE (v3 — penalizes inconsistency)
# ============================================================================

def compute_composite_score(window_results, price_metrics):
    """
    Score from multiple walk-forward windows.
    Penalizes symbols where results vary wildly across windows.
    """
    if not window_results:
        return 0.0, {"backtest": 0, "model": 0, "volatility": 0, "volume": 0, "consistency": 0}

    # Average metrics across windows
    avg_wr = np.mean([w["stats"]["win_rate"] for w in window_results])
    avg_pf = np.mean([w["stats"]["profit_factor"] for w in window_results])
    avg_trades = np.mean([w["stats"]["total_trades"] for w in window_results])
    avg_pnl = np.mean([w["stats"]["total_pnl"] for w in window_results])
    avg_spread = np.mean([w["model"]["prob_spread"] for w in window_results])
    avg_actionable = np.mean([w["model"]["pct_actionable"] for w in window_results])

    # Consistency: std of win rates across windows
    if len(window_results) > 1:
        wr_std = np.std([w["stats"]["win_rate"] for w in window_results])
        pnl_signs = [1 if w["stats"]["total_pnl"] > 0 else 0 for w in window_results]
        pct_profitable_windows = sum(pnl_signs) / len(pnl_signs) * 100
    else:
        wr_std = 0
        pct_profitable_windows = 100 if avg_pnl > 0 else 0

    components = {}

    # Backtest (0-100) — based on OUT-OF-SAMPLE results
    bp = 0.0
    if avg_trades > 0:
        bp += max(0, min(35, (avg_wr - 50) * 1.75))    # WR: 50%=0, 70%=35
        bp += max(0, min(25, (avg_pf - 1.0) * 12.5))   # PF: 1.0=0, 3.0=25
        bp += min(20, avg_trades * 0.5)                  # Trades: 40=20
        if avg_pnl > 0:
            bp += 10                                      # Profitable bonus
        if avg_pnl < 0:
            bp *= 0.5                                     # Harsh penalty for losing
    components["backtest"] = round(bp, 1)

    # Model quality (0-100)
    mq = 0.0
    mq += min(30, avg_spread * 50)
    mq += min(30, avg_actionable * 0.6)
    mq += min(20, np.mean([w["model"]["pct_strong"] for w in window_results]) * 1.5)
    mq += min(20, np.mean([w["model"]["prob_std"] for w in window_results]) * 100)
    if avg_spread < 0.05:
        mq = 0
    components["model"] = round(mq, 1)

    # Volatility (0-100)
    vol = 0.0
    atr = price_metrics.get("atr_pct", 0)
    dm = price_metrics.get("dollar_move", 0)
    vol += min(50, atr * 120)
    vol += min(30, dm * 8)
    components["volatility"] = round(vol, 1)

    # Volume/execution (0-100)
    ve = 0.0
    ve += min(40, price_metrics.get("midday_ratio", 0) * 80)
    ve += min(30, price_metrics.get("dollar_vol_m", 0) / 10)
    ve += min(30, price_metrics.get("avg_volume", 0) / 100000)
    components["volume"] = round(ve, 1)

    # Consistency bonus/penalty (0-100)
    cons = 50.0  # base
    cons += min(30, pct_profitable_windows * 0.3)    # all windows profitable = +30
    cons -= min(30, wr_std * 2)                       # high WR variance = -30
    if avg_pnl > 0 and pct_profitable_windows >= 66:
        cons += 20                                     # reliable profit = +20
    components["consistency"] = round(max(0, cons), 1)

    # Weighted total
    weights = {"backtest": 0.35, "model": 0.20, "volatility": 0.15,
               "volume": 0.10, "consistency": 0.20}
    total = sum(components[k] * weights[k] for k in weights)

    return round(total, 1), components


# ============================================================================
# SCREEN ONE SYMBOL
# ============================================================================

def screen_symbol(symbol):
    logger.info(f"\n{'='*60}\n  SCREENING: {symbol}\n{'='*60}")

    df_raw, df = fetch_and_resample(symbol)
    if df is None:
        return None

    feat_labeled, feat_current = build_features(df_raw, df, symbol)
    if feat_labeled is None or feat_labeled.empty or "y" not in feat_labeled.columns:
        logger.warning(f"  {symbol}: no labeled features")
        return None

    # Get rolling walk-forward splits
    splits = get_rolling_splits(feat_labeled)
    if not splits:
        logger.warning(f"  {symbol}: could not create train/test splits")
        return None

    window_results = []
    for wi, (train_df, test_df) in enumerate(splits):
        logger.info(f"  {symbol}: window {wi+1}/{len(splits)} — train={len(train_df)}, test={len(test_df)}")

        model, feat_names, train_stats = train_model(train_df, symbol)
        if model is None:
            continue

        # Predict on TEST set only (out-of-sample)
        predictions = predict_on_test(model, test_df, feat_names)
        if predictions is None or predictions.empty:
            continue

        # Get model metrics on test predictions
        mm = compute_model_metrics(predictions)

        # Find the matching bars in df for simulation
        # predictions index should align with df
        common_idx = df.index.intersection(predictions.index)
        if len(common_idx) < 10:
            continue

        df_test = df.loc[common_idx]
        pred_test = predictions.loc[common_idx]

        trades, stats = simulate_trading(df_test, pred_test, symbol)

        logger.info(
            f"  {symbol}: window {wi+1} OOS — {stats['total_trades']}T "
            f"{stats['win_rate']}%WR ${stats['total_pnl']:+.2f} PF={stats['profit_factor']:.2f}"
        )

        window_results.append({"stats": stats, "model": mm, "train_stats": train_stats})

    if not window_results:
        logger.warning(f"  {symbol}: no valid walk-forward windows")
        return None

    # Price metrics
    pm = compute_price_metrics(df, symbol)

    # Composite score
    total_score, components = compute_composite_score(window_results, pm)

    # Average stats across windows for display
    avg_stats = {}
    for key in window_results[0]["stats"]:
        vals = [w["stats"][key] for w in window_results]
        avg_stats[key] = round(np.mean(vals), 2) if vals else 0

    avg_model = {}
    for key in window_results[0]["model"]:
        vals = [w["model"][key] for w in window_results]
        avg_model[key] = round(np.mean(vals), 4) if vals else 0

    logger.info(
        f"\n  {symbol}: SCORE = {total_score:.1f}/100 (OOS)\n"
        f"    Backtest:    {components['backtest']:>5.1f} — {avg_stats['total_trades']:.0f}T "
        f"{avg_stats['win_rate']:.0f}%WR ${avg_stats['total_pnl']:+.2f} PF={avg_stats['profit_factor']:.2f}\n"
        f"    Model:       {components['model']:>5.1f} — spread={avg_model.get('prob_spread',0):.3f} "
        f"act={avg_model.get('pct_actionable',0):.0f}%\n"
        f"    Volatility:  {components['volatility']:>5.1f} — ATR={pm['atr_pct']:.3f}% ${pm['dollar_move']:.2f}/bar\n"
        f"    Volume:      {components['volume']:>5.1f} — midday={pm['midday_ratio']:.2f}\n"
        f"    Consistency: {components['consistency']:>5.1f}\n"
    )

    return {
        "symbol": symbol,
        "total_score": total_score,
        "components": components,
        "backtest": avg_stats,
        "model": avg_model,
        "price": pm,
        "n_windows": len(window_results),
        "screened_at": datetime.now().isoformat(),
    }


# ============================================================================
# RUN SCREENER
# ============================================================================

def run_screener(symbols=None, top_n=None, output_path=RESULTS_FILE):
    _setup_file_logger()
    if symbols:
        sym_list = [{"symbol": s.strip().upper(), "rank": i+1, "company": ""}
                    for i, s in enumerate(symbols)]
    else:
        sym_list = load_qqq_symbols(top_n=top_n)
    if not sym_list:
        return []

    # Filter out known bad symbols (leveraged ETFs, etc)
    bad_symbols = {"SOXS", "SOXL", "TQQQ", "SQQQ", "UVXY", "VXX", "SPXS", "SPXL"}
    sym_list = [s for s in sym_list if s["symbol"] not in bad_symbols]

    logger.info(f"\nScreening {len(sym_list)} symbols — WALK-FORWARD validation (v3)")
    logger.info(f"Train/Test split: {TRAIN_RATIO:.0%} / {1-TRAIN_RATIO:.0%}, {N_ROLLING_WINDOWS} windows")

    results = []
    for i, entry in enumerate(sym_list):
        sym = entry["symbol"]
        logger.info(f"\n[{i+1}/{len(sym_list)}] {sym} ({entry.get('company', '')})")
        try:
            r = screen_symbol(sym)
            if r:
                r["qqq_rank"] = entry.get("rank", 999)
                r["company"] = entry.get("company", "")
                results.append(r)
        except Exception as e:
            logger.error(f"  {sym}: FAILED: {e}")

    results.sort(key=lambda x: x["total_score"], reverse=True)
    for i, r in enumerate(results):
        r["screener_rank"] = i + 1

    # Save
    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as f:
            json.dump({
                "screened_at": datetime.now().isoformat(),
                "mode": "walk_forward_v3",
                "train_ratio": TRAIN_RATIO,
                "n_windows": N_ROLLING_WINDOWS,
                "symbols_screened": len(results),
                "results": results,
            }, f, indent=2, default=str)
    except Exception:
        pass

    # Print table
    print(f"\n{'='*130}")
    print(f"  AlgoMM SCREENER v3 — WALK-FORWARD (out-of-sample) | {len(results)} symbols | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Train: {TRAIN_RATIO:.0%} | Test: {1-TRAIN_RATIO:.0%} | {N_ROLLING_WINDOWS} rolling windows | Per-share stop: ${PER_SHARE_STOP}")
    print(f"{'='*130}")
    print(
        f"{'Rk':>3} {'Sym':<6} {'SCORE':>6} | "
        f"{'BkTst':>5} {'Model':>5} {'Volat':>5} {'Vol':>5} {'Cons':>5} | "
        f"{'Trades':>6} {'WR%':>5} {'P&L':>9} {'PF':>5} | "
        f"{'Spread':>6} {'Act%':>5} | "
        f"{'Price':>8} {'ATR%':>6} {'$/bar':>6} {'Mid%':>5}"
    )
    print(f"{'─'*3:>3} {'─'*6:<6} {'─'*6:>6} | "
          f"{'─'*5:>5} {'─'*5:>5} {'─'*5:>5} {'─'*5:>5} {'─'*5:>5} | "
          f"{'─'*6:>6} {'─'*5:>5} {'─'*9:>9} {'─'*5:>5} | "
          f"{'─'*6:>6} {'─'*5:>5} | "
          f"{'─'*8:>8} {'─'*6:>6} {'─'*6:>6} {'─'*5:>5}")

    for r in results:
        c, b, m, p = r["components"], r["backtest"], r["model"], r["price"]
        flag = "⭐" if r["screener_rank"] <= 8 else "  "
        print(
            f"{flag}{r['screener_rank']:>1} {r['symbol']:<6} {r['total_score']:>6.1f} | "
            f"{c['backtest']:>5.1f} {c['model']:>5.1f} {c['volatility']:>5.1f} "
            f"{c['volume']:>5.1f} {c['consistency']:>5.1f} | "
            f"{b['total_trades']:>6.0f} {b['win_rate']:>5.1f} "
            f"${b['total_pnl']:>+8.2f} {b['profit_factor']:>5.2f} | "
            f"{m.get('prob_spread', 0):>6.3f} {m.get('pct_actionable', 0):>5.1f} | "
            f"${p.get('price', 0):>7.2f} {p.get('atr_pct', 0):>5.3f}% "
            f"${p.get('dollar_move', 0):>5.2f} {p.get('midday_ratio', 0):>5.2f}"
        )

    top = results[:8]
    if top:
        print(f"\n{'='*70}")
        print(f"  TOP PICKS (out-of-sample validated)")
        print(f"{'='*70}")
        for r in top:
            b = r["backtest"]
            print(
                f"  #{r['screener_rank']} {r['symbol']:<6} Score={r['total_score']:.1f} | "
                f"{b['total_trades']:.0f}T {b['win_rate']:.0f}%WR "
                f"${b['total_pnl']:+.2f} PF={b['profit_factor']:.1f} | "
                f"{r['n_windows']} windows"
            )

    print(f"\nLog: {LOG_FILE}")
    print(f"JSON: {output_path}")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AlgoMM Screener v3 — walk-forward")
    parser.add_argument("--symbols", type=str, default="")
    parser.add_argument("--top", type=int, default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--output", type=str, default=RESULTS_FILE)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    if args.debug:
        logger.setLevel(logging.DEBUG)
    symbols, top_n = None, None
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.all:
        top_n = None
    elif args.top:
        top_n = args.top
    else:
        top_n = 30
    run_screener(symbols=symbols, top_n=top_n, output_path=args.output)