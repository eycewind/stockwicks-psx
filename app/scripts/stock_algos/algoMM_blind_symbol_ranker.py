#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/stock_algos/algoMM_blind_symbol_ranker.py
"""
AlgoMM Blind Walk-Forward Symbol Ranker

Purpose
-------
Scan a symbol CSV such as /var/www/stockwicks/app/scripts/stocks/qqq_list.csv
and rank which stocks work best for the current AlgoMM-style logic.

Blind / no-peek rules
---------------------
1) At signal bar t, prediction uses features available at or before t only.
2) The model is trained only on rows whose labels would already be known by t:
      train_idx <= t - k_forward bars
   This prevents training labels from using future bars inside the active test window.
3) Entry signal generated at close of bar t is filled at open of bar t+1.
4) Exit signal generated at close of bar t is filled at open of bar t+1.
5) By default, stops are close-based, not intrabar. This avoids pretending we know
   the exact high/low path inside a candle. Use --intrabar-stops only for a separate
   stress test, not for the main ranking.

Run examples
------------
cd /var/www/stockwicks
PYTHONPATH=/var/www/stockwicks python3 app/scripts/stock_algos/algoMM_blind_symbol_ranker.py \
  --csv /var/www/stockwicks/app/scripts/stocks/qqq_list.csv \
  --interval 5min --days 30 --test-bars 300 --top 30

Faster smoke test:
PYTHONPATH=/var/www/stockwicks python3 app/scripts/stock_algos/algoMM_blind_symbol_ranker.py \
  --symbols NVDA,AAPL,MSFT,AVGO,TSLA \
  --interval 5min --days 30 --test-bars 120 --top 10
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import traceback
from dataclasses import dataclass, asdict
from datetime import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Keep sklearn CPU use reasonable on VPS
os.environ.setdefault("LOKY_MAX_CPU_COUNT", "4")

from sklearn.ensemble import HistGradientBoostingClassifier

from app.scripts.stock_algos.base_wiring import StockBaseRunner, _ET
from app.scripts.research.mm_features2_builder import build_features

# Reuse the exact live entry helper / thresholds as much as possible.
# If you later tune algoMM_runner.py, this ranker stays close to live.
from app.scripts.stock_algos.algoMM_runner import (  # type: ignore
    BotConfig,
    DEFAULTS,
    _prepare_X,
    should_enter_trade,
)


DATA_ROOT = Path("/var/www/stockwicks/data")
OUT_DIR = DATA_ROOT / "backtests" / "algoMM_blind_ranker"


@dataclass
class SimTrade:
    symbol: str
    side: str                     # LONG or SHORT
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    shares: float
    pnl: float
    return_pct: float
    bars_held: int
    entry_reason: str
    exit_reason: str
    prob_entry: float
    prob_exit: float


@dataclass
class SymbolResult:
    symbol: str
    ok: bool
    error: str = ""
    bars: int = 0
    feature_rows: int = 0
    test_bars: int = 0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    median_pnl: float = 0.0
    avg_return_pct: float = 0.0
    max_drawdown: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    long_trades: int = 0
    short_trades: int = 0
    score: float = 0.0
    csv_path: str = ""


class FakeTrade:
    """Small object with the fields needed by our backtest exit logic."""
    def __init__(self, bot_id: int, position_side: str, entry_price: float, quantity: float):
        self.bot_id = bot_id
        self.position_side = position_side  # long / short
        self.entry_price = float(entry_price)
        self.quantity = float(quantity)


_PEAK_PROB_BT: Dict[str, float] = {}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Blind walk-forward scanner for AlgoMM symbols")
    p.add_argument("--csv", default="/var/www/stockwicks/app/scripts/stocks/qqq_list.csv", help="CSV with Symbol column")
    p.add_argument("--symbols", default="", help="Comma-separated symbols. Overrides --csv if provided.")
    p.add_argument("--interval", default="5min", help="1min, 5min, 10min, 15min, 30min, 1d")
    p.add_argument("--days", type=int, default=30, help="Feature builder days")
    p.add_argument("--k-forward", type=int, default=int(DEFAULTS.get("k_forward", 3)), help="Forward label horizon")
    p.add_argument("--test-bars", type=int, default=300, help="Number of final bars to blind-test")
    p.add_argument("--min-train-bars", type=int, default=400, help="Minimum labeled rows before first test signal")
    p.add_argument("--retrain-every", type=int, default=26, help="Retrain model every N bars during walk-forward")
    p.add_argument("--max-symbols", type=int, default=0, help="Limit symbols for quick tests. 0 = all")
    p.add_argument("--top", type=int, default=25, help="How many ranked symbols to print")
    p.add_argument("--capital-per-trade", type=float, default=10000.0, help="Backtest notional per trade")
    p.add_argument("--allow-short", action="store_true", help="Allow shorts. Default long-only unless enabled.")
    p.add_argument("--long-threshold", type=float, default=float(DEFAULTS.get("long_threshold", 0.60)))
    p.add_argument("--short-threshold", type=float, default=float(DEFAULTS.get("short_threshold", 0.40)))
    p.add_argument("--min-prob-advantage", type=float, default=float(DEFAULTS.get("min_prob_advantage", 0.03)))
    p.add_argument("--min-volume-multiplier", type=float, default=float(DEFAULTS.get("min_volume_multiplier", 0.2)))
    p.add_argument("--long-exit-threshold", type=float, default=float(DEFAULTS.get("long_exit_threshold", 0.55)))
    p.add_argument("--short-exit-threshold", type=float, default=float(DEFAULTS.get("short_exit_threshold", 0.45)))
    p.add_argument("--per-share-stop-pct", type=float, default=float(DEFAULTS.get("per_share_stop_pct", 0.01)))
    p.add_argument("--hard-stop-usd", type=float, default=float(DEFAULTS.get("hard_stop_usd", 300.0)))
    p.add_argument("--prob-trail-drop", type=float, default=float(DEFAULTS.get("prob_trail_drop", 0.02)))
    p.add_argument("--max-hold-bars", type=int, default=30, help="Force exit after this many bars. 0 disables.")
    p.add_argument("--intrabar-stops", action="store_true", help="Optional stress-test using OHLC high/low stops. Not used in score by default.")
    p.add_argument("--out-dir", default=str(OUT_DIR))
    return p.parse_args()


def load_symbols(args: argparse.Namespace) -> List[str]:
    if args.symbols.strip():
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        df = pd.read_csv(args.csv)
        if "Symbol" not in df.columns:
            raise ValueError(f"CSV must contain Symbol column: {args.csv}")
        symbols = [str(s).strip().upper() for s in df["Symbol"].dropna().tolist() if str(s).strip()]
    # Keep order, remove duplicates
    seen = set()
    out = []
    for s in symbols:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    if args.max_symbols and args.max_symbols > 0:
        out = out[: args.max_symbols]
    return out


def make_cfg(symbol: str, args: argparse.Namespace) -> BotConfig:
    cfg = BotConfig()
    cfg.symbol = symbol
    cfg.builder_days = args.days
    cfg.k_forward = max(1, int(args.k_forward))
    cfg.long_threshold = float(args.long_threshold)
    cfg.short_threshold = float(args.short_threshold)
    cfg.min_prob_advantage = float(args.min_prob_advantage)
    cfg.min_volume_multiplier = float(args.min_volume_multiplier)
    cfg.long_exit_threshold = float(args.long_exit_threshold)
    cfg.short_exit_threshold = float(args.short_exit_threshold)
    cfg.per_share_stop_pct = float(args.per_share_stop_pct)
    cfg.hard_stop_usd = float(args.hard_stop_usd)
    cfg.prob_trail_drop = float(args.prob_trail_drop)
    return cfg


def fetch_price_df(symbol: str, interval: str) -> pd.DataFrame:
    runner = StockBaseRunner()
    raw = runner.fetch_source_bars(symbol)
    if raw is None or raw.empty:
        raise ValueError("no raw bars returned")
    _, _, df = runner.resample_interval(raw, interval, symbol)
    if df is None or df.empty:
        raise ValueError("resample returned empty df")
    df = df.sort_index()
    # Normalize index tz/format but preserve timestamps
    df = df[["open", "high", "low", "close", "volume"]].copy()
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=["open", "high", "low", "close"])
    df = df[df["close"] > 0]
    return df


def build_and_align_features(symbol: str, interval: str, cfg: BotConfig, price_df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    feat_df = build_features(symbol=symbol, interval=interval, days=cfg.builder_days, k_forward=cfg.k_forward)
    if feat_df is None or feat_df.empty:
        raise ValueError("feature builder returned empty df")
    feat_df = feat_df.sort_index()
    if "y" not in feat_df.columns or "w" not in feat_df.columns:
        raise ValueError("feature df missing y/w columns")

    # Use only feature rows that have matching price bars. This also helps avoid alignment drift.
    common_idx = feat_df.index.intersection(price_df.index)
    if len(common_idx) < 100:
        # Fallback: keep original index if exact timestamps don't intersect enough.
        # Some builders may produce slightly different timezone metadata.
        feat_df = feat_df.loc[feat_df.index.isin(price_df.index)] if len(common_idx) else feat_df
    else:
        feat_df = feat_df.loc[common_idx]

    feature_names = [c for c in feat_df.columns if c not in ("y", "w")]
    if not feature_names:
        raise ValueError("no usable feature columns")
    return feat_df, feature_names


def train_model(train_df: pd.DataFrame, feature_names: List[str]) -> Tuple[HistGradientBoostingClassifier, List[str]]:
    X = _prepare_X(train_df, feature_names)
    y = train_df.loc[X.index, "y"].astype(int).values
    w = train_df.loc[X.index, "w"].astype(float).values

    if len(np.unique(y)) < 2:
        raise ValueError("training window has only one class")

    clf = HistGradientBoostingClassifier(
        max_depth=4,
        learning_rate=0.06,
        max_iter=250,
        l2_regularization=1.0,
        random_state=42,
    )
    clf.fit(X, y, sample_weight=w)
    return clf, feature_names


def predict_prob_up(model, row_df: pd.DataFrame, feature_names: List[str]) -> float:
    X = _prepare_X(row_df, feature_names)
    if hasattr(model, "predict_proba"):
        proba = model.predict_proba(X)
        # Class labels might be [0,1]; find class 1 column.
        classes = list(getattr(model, "classes_", [0, 1]))
        if 1 in classes:
            return float(proba[-1, classes.index(1)])
        return float(proba[-1, -1])
    # Fallback should rarely be used.
    pred = model.predict(X)[-1]
    return float(pred)


def should_exit_backtest(
    symbol: str,
    side: str,
    entry_price: float,
    shares: float,
    current_close: float,
    prob_up: float,
    prob_down: float,
    cfg: BotConfig,
    bars_held: int,
    max_hold_bars: int,
) -> Tuple[bool, str]:
    """Backtest-safe exit: signal at current close, fill next bar open."""
    position_side = "long" if side == "LONG" else "short"

    # Close-based per-share stop
    stop_per_share = entry_price * cfg.per_share_stop_pct
    per_share_loss = (entry_price - current_close) if position_side == "long" else (current_close - entry_price)
    if per_share_loss >= stop_per_share:
        _PEAK_PROB_BT.pop(symbol, None)
        return True, "PER_SHARE_STOP_CLOSE_BASED"

    # Hard USD stop, close-based
    loss_usd = per_share_loss * shares
    if loss_usd >= cfg.hard_stop_usd:
        _PEAK_PROB_BT.pop(symbol, None)
        return True, "HARD_STOP_CLOSE_BASED"

    # Prob trailing drop
    conviction = prob_up if position_side == "long" else prob_down
    peak = _PEAK_PROB_BT.get(symbol, conviction)
    peak = max(peak, conviction)
    _PEAK_PROB_BT[symbol] = peak
    if (peak - conviction) >= cfg.prob_trail_drop:
        _PEAK_PROB_BT.pop(symbol, None)
        return True, "PROB_TRAIL_DROP"

    # Probability floor
    if position_side == "long" and prob_up < cfg.long_exit_threshold:
        _PEAK_PROB_BT.pop(symbol, None)
        return True, "PROBABILITY_FLOOR"
    if position_side == "short" and prob_down < (1.0 - cfg.short_exit_threshold):
        _PEAK_PROB_BT.pop(symbol, None)
        return True, "PROBABILITY_FLOOR"

    if max_hold_bars and bars_held >= max_hold_bars:
        _PEAK_PROB_BT.pop(symbol, None)
        return True, "MAX_HOLD_BARS"

    return False, ""


def calc_drawdown(equity: List[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    max_dd = 0.0
    for x in equity:
        peak = max(peak, x)
        max_dd = min(max_dd, x - peak)
    return float(max_dd)


def score_symbol(result: SymbolResult) -> float:
    """
    Ranking score favors: positive P&L, win rate, profit factor, number of trades.
    Penalizes: drawdown and tiny trade samples.
    """
    if not result.ok or result.trades <= 0:
        return -999999.0
    sample_factor = min(1.0, result.trades / 8.0)
    pf_cap = min(result.profit_factor, 4.0) if math.isfinite(result.profit_factor) else 4.0
    dd_penalty = abs(result.max_drawdown) * 0.35
    return float((result.total_pnl + result.expectancy * result.trades + 100.0 * pf_cap + 250.0 * result.win_rate) * sample_factor - dd_penalty)


def run_symbol(symbol: str, args: argparse.Namespace) -> SymbolResult:
    cfg = make_cfg(symbol, args)
    trades: List[SimTrade] = []
    equity = [0.0]

    try:
        price_df = fetch_price_df(symbol, args.interval)
        feat_df, feature_names = build_and_align_features(symbol, args.interval, cfg, price_df)

        # Use only common timestamps after alignment.
        idx = feat_df.index.intersection(price_df.index)
        if len(idx) < args.min_train_bars + max(50, args.test_bars // 2):
            raise ValueError(f"not enough aligned bars: {len(idx)}")
        feat_df = feat_df.loc[idx].sort_index()
        price_df = price_df.loc[idx].sort_index()

        start_pos = max(args.min_train_bars, len(feat_df) - args.test_bars)
        # Need one next bar for fills; loop stops at len-2.
        end_pos = len(feat_df) - 2
        if start_pos >= end_pos:
            raise ValueError("test window too small after reserving next-bar fills")

        model = None
        model_features = feature_names
        last_train_cut_pos = -1

        open_pos = None  # dict

        for pos in range(start_pos, end_pos + 1):
            ts = feat_df.index[pos]
            next_ts = feat_df.index[pos + 1]

            # BLIND TRAIN CUTOFF:
            # y at row r needs future r+k_forward. At current signal row pos,
            # only labels through pos-k_forward are knowable.
            train_cut_pos = pos - cfg.k_forward
            if train_cut_pos < args.min_train_bars:
                continue

            needs_retrain = (
                model is None
                or last_train_cut_pos < 0
                or (pos - last_train_cut_pos) >= args.retrain_every
            )
            if needs_retrain:
                train_df = feat_df.iloc[: train_cut_pos + 1]
                # Drop any rows with missing y/w before training.
                train_df = train_df.dropna(subset=["y", "w"])
                if len(train_df) < args.min_train_bars:
                    continue
                model, model_features = train_model(train_df, feature_names)
                last_train_cut_pos = pos

            row_df = feat_df.iloc[[pos]]
            prob_up = predict_prob_up(model, row_df, model_features)
            if not np.isfinite(prob_up):
                continue
            prob_down = 1.0 - prob_up

            current_bar = price_df.iloc[pos]
            next_bar = price_df.iloc[pos + 1]
            current_close = float(current_bar["close"])
            next_open = float(next_bar["open"])
            if current_close <= 0 or next_open <= 0:
                continue

            if open_pos is None:
                should_enter, direction, reason = should_enter_trade(
                    prob_up=prob_up,
                    prob_down=prob_down,
                    df=price_df.iloc[: pos + 1],
                    current_index=pos,
                    cfg=cfg,
                    allow_short=bool(args.allow_short),
                )
                if should_enter:
                    entry_price = next_open  # signal at t, fill at t+1 open
                    shares = math.floor(args.capital_per_trade / entry_price)
                    if shares <= 0:
                        continue
                    open_pos = {
                        "side": direction,
                        "entry_pos": pos + 1,
                        "entry_signal_ts": ts,
                        "entry_time": next_ts,
                        "entry_price": entry_price,
                        "shares": float(shares),
                        "entry_reason": reason,
                        "prob_entry": prob_up if direction == "LONG" else prob_down,
                    }
                    _PEAK_PROB_BT[symbol] = open_pos["prob_entry"]
                continue

            # If position was just filled on this same next_open, don't exit before it exists.
            if pos <= int(open_pos["entry_pos"]):
                continue

            bars_held = pos - int(open_pos["entry_pos"])
            should_exit, exit_reason = should_exit_backtest(
                symbol=symbol,
                side=open_pos["side"],
                entry_price=float(open_pos["entry_price"]),
                shares=float(open_pos["shares"]),
                current_close=current_close,
                prob_up=prob_up,
                prob_down=prob_down,
                cfg=cfg,
                bars_held=bars_held,
                max_hold_bars=args.max_hold_bars,
            )

            if should_exit:
                exit_price = next_open  # exit signal at t, fill at t+1 open
                side = open_pos["side"]
                if side == "LONG":
                    pnl = (exit_price - float(open_pos["entry_price"])) * float(open_pos["shares"])
                    ret_pct = (exit_price / float(open_pos["entry_price"]) - 1.0) * 100.0
                    prob_exit = prob_up
                else:
                    pnl = (float(open_pos["entry_price"]) - exit_price) * float(open_pos["shares"])
                    ret_pct = (float(open_pos["entry_price"]) / exit_price - 1.0) * 100.0
                    prob_exit = prob_down
                trades.append(
                    SimTrade(
                        symbol=symbol,
                        side=side,
                        entry_time=str(open_pos["entry_time"]),
                        exit_time=str(next_ts),
                        entry_price=round(float(open_pos["entry_price"]), 4),
                        exit_price=round(float(exit_price), 4),
                        shares=float(open_pos["shares"]),
                        pnl=round(float(pnl), 2),
                        return_pct=round(float(ret_pct), 4),
                        bars_held=int(bars_held),
                        entry_reason=str(open_pos["entry_reason"]),
                        exit_reason=exit_reason,
                        prob_entry=round(float(open_pos["prob_entry"]), 4),
                        prob_exit=round(float(prob_exit), 4),
                    )
                )
                equity.append(equity[-1] + float(pnl))
                open_pos = None

        # Force close any remaining position at final close, for accounting only.
        if open_pos is not None:
            final_ts = price_df.index[-1]
            final_close = float(price_df["close"].iloc[-1])
            side = open_pos["side"]
            if side == "LONG":
                pnl = (final_close - float(open_pos["entry_price"])) * float(open_pos["shares"])
                ret_pct = (final_close / float(open_pos["entry_price"]) - 1.0) * 100.0
            else:
                pnl = (float(open_pos["entry_price"]) - final_close) * float(open_pos["shares"])
                ret_pct = (float(open_pos["entry_price"]) / final_close - 1.0) * 100.0
            trades.append(
                SimTrade(
                    symbol=symbol,
                    side=side,
                    entry_time=str(open_pos["entry_time"]),
                    exit_time=str(final_ts),
                    entry_price=round(float(open_pos["entry_price"]), 4),
                    exit_price=round(float(final_close), 4),
                    shares=float(open_pos["shares"]),
                    pnl=round(float(pnl), 2),
                    return_pct=round(float(ret_pct), 4),
                    bars_held=int(len(price_df) - 1 - int(open_pos["entry_pos"])),
                    entry_reason=str(open_pos["entry_reason"]),
                    exit_reason="FORCED_END_OF_TEST",
                    prob_entry=round(float(open_pos["prob_entry"]), 4),
                    prob_exit=0.0,
                )
            )
            equity.append(equity[-1] + float(pnl))
            _PEAK_PROB_BT.pop(symbol, None)

        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        trades_path = out_dir / f"{symbol}_{args.interval}_trades.csv"
        pd.DataFrame([asdict(t) for t in trades]).to_csv(trades_path, index=False)

        pnls = np.array([t.pnl for t in trades], dtype=float)
        wins = int((pnls > 0).sum()) if len(pnls) else 0
        losses = int((pnls < 0).sum()) if len(pnls) else 0
        gross_win = float(pnls[pnls > 0].sum()) if len(pnls) else 0.0
        gross_loss = float(-pnls[pnls < 0].sum()) if len(pnls) else 0.0
        profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)

        res = SymbolResult(
            symbol=symbol,
            ok=True,
            bars=len(price_df),
            feature_rows=len(feat_df),
            test_bars=end_pos - start_pos + 1,
            trades=len(trades),
            wins=wins,
            losses=losses,
            win_rate=round(wins / len(trades), 4) if trades else 0.0,
            total_pnl=round(float(pnls.sum()), 2) if len(pnls) else 0.0,
            avg_pnl=round(float(pnls.mean()), 2) if len(pnls) else 0.0,
            median_pnl=round(float(np.median(pnls)), 2) if len(pnls) else 0.0,
            avg_return_pct=round(float(np.mean([t.return_pct for t in trades])), 4) if trades else 0.0,
            max_drawdown=round(calc_drawdown(equity), 2),
            profit_factor=round(profit_factor, 4) if math.isfinite(profit_factor) else 999.0,
            expectancy=round(float(pnls.mean()), 2) if len(pnls) else 0.0,
            long_trades=sum(1 for t in trades if t.side == "LONG"),
            short_trades=sum(1 for t in trades if t.side == "SHORT"),
            csv_path=str(trades_path),
        )
        res.score = round(score_symbol(res), 2)
        return res

    except Exception as e:
        return SymbolResult(symbol=symbol, ok=False, error=f"{e}\n{traceback.format_exc(limit=2)}")


def main() -> int:
    args = parse_args()
    symbols = load_symbols(args)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[START] symbols={len(symbols)} interval={args.interval} days={args.days} test_bars={args.test_bars}")
    print(f"[BLIND] train rows stop at signal_pos - k_forward; signals fill next bar open")
    print(f"[OUT] {out_dir}")

    results: List[SymbolResult] = []
    for n, sym in enumerate(symbols, 1):
        print(f"\n[{n}/{len(symbols)}] {sym} ...", flush=True)
        res = run_symbol(sym, args)
        results.append(res)
        if res.ok:
            print(
                f"  OK trades={res.trades} pnl=${res.total_pnl:.2f} "
                f"win={res.win_rate:.1%} pf={res.profit_factor} dd=${res.max_drawdown:.2f} score={res.score:.2f}"
            )
        else:
            print(f"  SKIP/ERROR: {res.error.splitlines()[0] if res.error else 'unknown'}")

        # Save incremental results so a long run is not lost.
        pd.DataFrame([asdict(r) for r in results]).to_csv(out_dir / "symbol_rankings_partial.csv", index=False)

    rank_df = pd.DataFrame([asdict(r) for r in results])
    if not rank_df.empty:
        rank_df = rank_df.sort_values(["ok", "score", "total_pnl", "profit_factor"], ascending=[False, False, False, False])
    rankings_path = out_dir / f"symbol_rankings_{args.interval}.csv"
    rank_df.to_csv(rankings_path, index=False)

    ok_df = rank_df[rank_df["ok"] == True].copy() if not rank_df.empty else pd.DataFrame()
    print("\n========== TOP SYMBOLS ==========")
    if ok_df.empty:
        print("No successful symbols. Check errors in ranking CSV.")
    else:
        cols = [
            "symbol", "score", "trades", "win_rate", "total_pnl", "avg_pnl",
            "max_drawdown", "profit_factor", "long_trades", "short_trades", "csv_path"
        ]
        print(ok_df[cols].head(args.top).to_string(index=False))

    print(f"\nSaved ranking CSV: {rankings_path}")
    print("Saved per-symbol trade CSVs in same folder.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
