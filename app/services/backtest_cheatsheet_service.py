from __future__ import annotations

import importlib
import math
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


ALGO_FEATURE_SETS = {
    "Algo1_MM": "Featureset_1",
    "Algo2_MM": "Featureset_2",
    "Algo3_MM": "Featureset_3",
    "Algo4_MM": "Featureset_4",
    "Algo5_MM": "Featureset_5",
}

_ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class CheatSheetRequest:
    symbol: str
    intervals: tuple[str, ...]
    trade_size: float = 100.0
    builder_days: int = 30
    k_forward: int = 3
    profile: str = "quick"
    allow_short: bool = True
    eod_close: bool = True
    oos_fraction: float = 0.35


def _safe_float(value: Any, default: float) -> float:
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except Exception:
        return default


def _prepare_X(feat_df: pd.DataFrame, feat_names: list[str]) -> pd.DataFrame:
    X = feat_df.reindex(columns=feat_names).copy()
    X = X.replace([np.inf, -np.inf], np.nan).ffill().bfill()
    med = X.median(numeric_only=True)
    X = X.fillna(med)
    all_nan = [c for c in X.columns if X[c].isna().all()]
    if all_nan:
        X[all_nan] = 0.0
    return X.fillna(0.0).astype(float)


def _predict_probability(model, X: pd.DataFrame) -> np.ndarray:
    if not hasattr(model, "predict_proba"):
        preds = model.predict(X).astype(float)
        rng = np.ptp(preds) if np.ptp(preds) != 0 else 1.0
        return (preds - np.min(preds)) / rng

    proba = model.predict_proba(X)
    classes = list(getattr(model, "classes_", []))
    up_idx = None
    for target in (1, 1.0, True, "1", "UP", "up", "LONG", "long"):
        if target in classes:
            up_idx = classes.index(target)
            break
    if up_idx is None:
        up_idx = proba.shape[1] - 1
    return proba[:, up_idx].astype(float)


def _load_feature_module(feature_set: str):
    return importlib.import_module(f"app.scripts.research.{feature_set}")


def _fetch_price_frame(symbol: str, interval: str, builder_days: int) -> pd.DataFrame:
    from app.scripts.stock_algos.base_wiring import StockBaseRunner

    runner = StockBaseRunner()
    raw = runner.fetch_source_bars(symbol, interval=interval, lookback_days=builder_days)
    if raw is None or raw.empty:
        raise RuntimeError(f"No price data returned for {symbol} {interval}")

    _, _, frame = runner.resample_interval(raw, interval, symbol)
    if frame is None or frame.empty:
        raise RuntimeError(f"Could not resample price data for {symbol} {interval}")
    return frame.sort_index()


def _param_grid(profile: str, algo_name: str) -> list[dict[str, Any]]:
    is_algo4 = str(algo_name or "") == "Algo4_MM"
    if str(profile or "quick").lower() == "deep":
        long_entries = [0.56, 0.58, 0.60, 0.62, 0.65]
        short_entries = [0.44, 0.42, 0.40, 0.38, 0.35]
        prob_trail_drops = [0.05, 0.10, 0.20, 0.35, 0.50]
        hard_stops = [300.0, 500.0, 750.0, 1000.0, 1500.0]
        trail_activations = [50.0, 75.0, 100.0, 150.0, 250.0]
        trail_distances = [25.0, 50.0, 75.0, 100.0, 150.0]
    else:
        long_entries = [0.58, 0.60, 0.62]
        short_entries = [0.42, 0.40, 0.38]
        prob_trail_drops = [0.05, 0.10, 0.20, 0.35]
        hard_stops = [300.0, 500.0, 750.0, 1000.0]
        trail_activations = [50.0, 75.0, 100.0, 150.0]
        trail_distances = [25.0, 50.0, 75.0, 100.0]

    rows: list[dict[str, Any]] = []
    for long_entry in long_entries:
        for short_entry in short_entries:
            if short_entry >= long_entry:
                continue
            for prob_trail_drop in prob_trail_drops:
                for hard_stop_usd in hard_stops:
                    for trailing_stop_activation in trail_activations:
                        for trailing_stop_distance in trail_distances:
                            rows.append(
                                {
                                    "long_entry_prob": long_entry,
                                    "short_entry_prob": short_entry,
                                    "prob_trail_drop": prob_trail_drop,
                                    "hard_stop_usd": hard_stop_usd,
                                    "trailing_stop_activation": trailing_stop_activation,
                                    "trailing_stop_distance": trailing_stop_distance,
                                    "prob_exit_mode": "trailing",
                                    "long_fixed_exit_prob": 0.55,
                                    "short_fixed_exit_prob": 0.55,
                                    "prob_smoothing_bars": 3,
                                    "min_prob_advantage": 0.03 if is_algo4 else 0.0,
                                }
                            )
    return rows


def _trade_metrics(trades: list[dict[str, Any]]) -> dict[str, float]:
    if not trades:
        return {
            "total_profit": 0.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "avg_trade": 0.0,
            "num_trades": 0.0,
        }

    pnl = np.array([float(t["profit"]) for t in trades], dtype=float)
    equity = pnl.cumsum()
    running_max = np.maximum.accumulate(equity)
    drawdown = running_max - equity
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    gross_profit = float(wins.sum()) if len(wins) else 0.0
    gross_loss = abs(float(losses.sum())) if len(losses) else 0.0
    return {
        "total_profit": float(pnl.sum()),
        "max_drawdown": float(drawdown.max()) if len(drawdown) else 0.0,
        "win_rate": float(len(wins) / len(pnl)),
        "profit_factor": float(gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0),
        "avg_trade": float(pnl.mean()),
        "num_trades": float(len(pnl)),
    }


def _ts_iso(index: pd.Index, pos: int) -> str | None:
    if len(index) == 0:
        return None
    try:
        return index[pos].isoformat()
    except Exception:
        return str(index[pos])


def _simulate_combo(
    *,
    algo_name: str,
    price_df: pd.DataFrame,
    prob_up: pd.Series,
    params: dict[str, Any],
    trade_size: float,
    allow_short: bool,
    eod_close: bool,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    is_algo4 = str(algo_name or "") == "Algo4_MM"
    smoothing = max(1, int(params.get("prob_smoothing_bars", 3)))
    # Algo4_MM live/replay uses the previous aligned probability from a
    # rolling k-forward average with min_periods=1. Algo1/2/3/5 use the
    # stricter smoothed-cross behavior.
    min_periods = 1 if is_algo4 else smoothing
    prob_avg = prob_up.rolling(window=smoothing, min_periods=min_periods).mean()

    position_side: str | None = None
    entry_price = 0.0
    entry_time = None
    prob_peak = 0.0
    profit_peak = 0.0
    trades: list[dict[str, Any]] = []

    def close_trade(ts, price: float, reason: str):
        nonlocal position_side, entry_price, entry_time, prob_peak, profit_peak
        if not position_side:
            return
        profit = (price - entry_price) * trade_size if position_side == "long" else (entry_price - price) * trade_size
        trades.append(
            {
                "side": position_side,
                "entry_time": entry_time,
                "exit_time": ts,
                "entry_price": entry_price,
                "exit_price": price,
                "profit": float(profit),
                "exit_reason": reason,
            }
        )
        position_side = None
        entry_price = 0.0
        entry_time = None
        prob_peak = 0.0
        profit_peak = 0.0

    aligned = prob_avg.reindex(price_df.index).ffill()
    long_entry = float(params["long_entry_prob"])
    short_entry = float(params["short_entry_prob"])
    hard_stop = float(params["hard_stop_usd"])
    trail_activation = float(params["trailing_stop_activation"])
    trail_distance = float(params["trailing_stop_distance"])
    prob_trail_drop = float(params["prob_trail_drop"])
    min_prob_advantage = float(params.get("min_prob_advantage", 0.0) or 0.0)

    for i in range(1, len(price_df)):
        ts = price_df.index[i]
        price = float(price_df["close"].iloc[i])
        # Algo4 avoids using the current candle's own probability for the
        # decision. This mirrors algoMM_replay_runner's prev_prob behavior.
        prob_pos = i - 1 if is_algo4 else i
        prev_pos = max(0, prob_pos - 1)
        current_prob = _safe_float(aligned.iloc[prob_pos], float("nan"))
        prev_prob = _safe_float(aligned.iloc[prev_pos], float("nan"))
        if not math.isfinite(current_prob) or not math.isfinite(prev_prob):
            continue

        just_closed = False
        if position_side:
            pnl = (price - entry_price) * trade_size if position_side == "long" else (entry_price - price) * trade_size
            if hard_stop > 0 and pnl <= -hard_stop:
                close_trade(ts, price, "HARD_STOP")
                just_closed = True
            else:
                profit_peak = max(profit_peak, pnl)
                if trail_activation > 0 and trail_distance > 0 and profit_peak >= trail_activation:
                    if profit_peak - pnl >= trail_distance:
                        close_trade(ts, price, "TRAILING_PROFIT_STOP")
                        just_closed = True

            if position_side and not just_closed:
                conviction = current_prob if position_side == "long" else 1.0 - current_prob
                prob_peak = max(prob_peak, conviction)
                if prob_trail_drop > 0 and prob_peak - conviction >= prob_trail_drop:
                    close_trade(ts, price, "PROB_TRAIL_DROP")
                    just_closed = True

            if position_side and not just_closed and eod_close:
                cur_time = ts.astimezone(_ET).time() if getattr(ts, "tzinfo", None) else ts.time()
                if cur_time.hour > 15 or (cur_time.hour == 15 and cur_time.minute >= 50):
                    close_trade(ts, price, "EOD_CLOSE")
                    just_closed = True

        if position_side is None and not just_closed:
            if is_algo4:
                prob_down = 1.0 - current_prob
                enter_long = current_prob >= long_entry and current_prob > (prob_down + min_prob_advantage)
                enter_short = allow_short and prob_down >= (1.0 - short_entry) and prob_down > (current_prob + min_prob_advantage)
            else:
                enter_long = prev_prob < long_entry <= current_prob
                enter_short = allow_short and prev_prob > short_entry >= current_prob

            if enter_long:
                position_side = "long"
                entry_price = price
                entry_time = ts
                prob_peak = current_prob
                profit_peak = 0.0
            elif enter_short:
                position_side = "short"
                entry_price = price
                entry_time = ts
                prob_peak = 1.0 - current_prob
                profit_peak = 0.0

    if position_side:
        close_trade(price_df.index[-1], float(price_df["close"].iloc[-1]), "FINAL_BAR_CLOSE")

    return trades, _trade_metrics(trades)


def _score(metrics: dict[str, float]) -> float:
    trades = metrics["num_trades"]
    if trades < 2:
        return -100000.0 + metrics["total_profit"]
    return (
        metrics["total_profit"]
        - (0.50 * metrics["max_drawdown"])
        + (metrics["win_rate"] * 100.0)
        + min(trades, 12.0)
        + (metrics["avg_trade"] * 0.25)
    )


def _selection_score(validation_metrics: dict[str, float]) -> float:
    validation_score = _score(validation_metrics)
    trade_penalty = 250.0 if validation_metrics["num_trades"] < 2 else 0.0
    return validation_score - trade_penalty


def _confidence(metrics: dict[str, float]) -> str:
    if metrics["num_trades"] < 3:
        return "Low"
    if metrics["total_profit"] > 0 and metrics["win_rate"] >= 0.55 and metrics["profit_factor"] >= 1.25:
        return "High"
    if metrics["total_profit"] > 0 and metrics["win_rate"] >= 0.45:
        return "Medium"
    return "Low"


def run_cheatsheet(req: CheatSheetRequest) -> dict[str, Any]:
    symbol = req.symbol.upper().strip()
    if not symbol:
        raise ValueError("Symbol is required")

    intervals = tuple(i.strip().lower() for i in req.intervals if i.strip())
    if not intervals:
        raise ValueError("At least one interval is required")

    all_rows: list[dict[str, Any]] = []
    errors: list[str] = []

    for interval in intervals:
        try:
            price_full = _fetch_price_frame(symbol, interval, req.builder_days)
        except Exception as exc:
            errors.append(f"{symbol} {interval}: {exc}")
            continue

        for algo_name, feature_set in ALGO_FEATURE_SETS.items():
            params_grid = _param_grid(req.profile, algo_name)
            try:
                feature_module = _load_feature_module(feature_set)
            except ModuleNotFoundError:
                errors.append(f"{algo_name}: {feature_set} is not available in this build")
                continue
            except Exception as exc:
                errors.append(f"{algo_name}: failed loading {feature_set}: {exc}")
                continue

            try:
                infer_feat = feature_module.build_feature_matrix_from_df(
                    df=price_full,
                    symbol=symbol,
                    interval=interval,
                    feature_set=feature_set,
                )
                feat_names = feature_module.get_feature_columns(feature_set)
            except Exception as exc:
                errors.append(f"{algo_name} {interval}: feature build failed: {exc}")
                continue

            common_idx = infer_feat.index.intersection(price_full.index)
            infer_feat = infer_feat.loc[common_idx].copy()
            price_df = price_full.loc[common_idx].copy()
            if len(common_idx) < 80:
                errors.append(f"{algo_name} {interval}: not enough aligned bars ({len(common_idx)})")
                continue

            n = len(common_idx)
            min_validation_bars = 20
            min_holdout_bars = 20
            train_end = int(n * (1.0 - req.oos_fraction))
            train_end = max(30, min(train_end, n - min_validation_bars - min_holdout_bars))
            if train_end < 30 or n - train_end < min_validation_bars + min_holdout_bars:
                errors.append(f"{algo_name} {interval}: not enough bars for train/validation/holdout split ({n})")
                continue

            oos_count = n - train_end
            validation_count = max(min_validation_bars, oos_count // 2)
            validation_end = min(train_end + validation_count, n - min_holdout_bars)

            train_index = common_idx[:train_end]
            validation_index = common_idx[train_end:validation_end]
            holdout_index = common_idx[validation_end:]
            if len(validation_index) < min_validation_bars or len(holdout_index) < min_holdout_bars:
                errors.append(f"{algo_name} {interval}: validation/holdout split too small")
                continue

            # Build labels from the pre-split price frame only. Building labels
            # on the full frame would let the last training rows see forward
            # validation candles through y/w.
            train_price_source = price_df.loc[train_index].copy()
            train_feat_safe = feature_module.build_training_features_from_df(
                df=train_price_source,
                symbol=symbol,
                interval=interval,
                k_forward=req.k_forward,
                feature_set=feature_set,
            )
            if train_feat_safe is None or train_feat_safe.empty:
                errors.append(f"{algo_name} {interval}: safe training labels are empty")
                continue
            train_feat_safe = train_feat_safe.loc[train_feat_safe.index.intersection(train_index)].copy()
            if "y" not in train_feat_safe or "w" not in train_feat_safe or train_feat_safe["y"].nunique() < 2:
                errors.append(f"{algo_name} {interval}: safe training labels are not usable")
                continue

            X_train = _prepare_X(train_feat_safe, list(feat_names))
            y_train = train_feat_safe.loc[X_train.index, "y"].astype(int).values
            w_train = train_feat_safe.loc[X_train.index, "w"].astype(float).values
            if len(set(y_train)) < 2:
                errors.append(f"{algo_name} {interval}: safe train split left only one training class")
                continue

            try:
                from sklearn.ensemble import HistGradientBoostingClassifier

                model = HistGradientBoostingClassifier(
                    max_depth=4,
                    learning_rate=0.06,
                    max_iter=250,
                    l2_regularization=1.0,
                )
                model.fit(X_train, y_train, sample_weight=w_train)
                X_all = _prepare_X(infer_feat, list(feat_names))
                probs = _predict_probability(model, X_all)
            except Exception as exc:
                errors.append(f"{algo_name} {interval}: model training failed: {exc}")
                continue

            validation_price = price_df.loc[validation_index]
            holdout_price = price_df.loc[holdout_index]
            prob_all = pd.Series(probs, index=X_all.index, name="prob_up")
            validation_prob = prob_all.loc[validation_index]
            holdout_prob = prob_all.loc[holdout_index]

            for params in params_grid:
                validation_trades, validation_metrics = _simulate_combo(
                    algo_name=algo_name,
                    price_df=validation_price,
                    prob_up=validation_prob,
                    params=params,
                    trade_size=req.trade_size,
                    allow_short=req.allow_short,
                    eod_close=req.eod_close,
                )
                holdout_trades, holdout_metrics = _simulate_combo(
                    algo_name=algo_name,
                    price_df=holdout_price,
                    prob_up=holdout_prob,
                    params=params,
                    trade_size=req.trade_size,
                    allow_short=req.allow_short,
                    eod_close=req.eod_close,
                )
                validation_score = _score(validation_metrics)
                holdout_score = _score(holdout_metrics)
                selection_score = _selection_score(validation_metrics)
                row = {
                    "symbol": symbol,
                    "interval": interval,
                    "algo_name": algo_name,
                    "feature_set": feature_set,
                    "score": selection_score,
                    "selection_score": selection_score,
                    "validation_score": validation_score,
                    "holdout_score": holdout_score,
                    "confidence": _confidence(holdout_metrics),
                    "backtest_method": "validation_picked_holdout",
                    "backtest_method_label": "Validation-picked holdout scan",
                    "backtest_notes": (
                        "Trains one model on pre-split history, selects parameters "
                        "on validation bars, then reports P/L on later holdout bars. "
                        "Training labels are rebuilt from pre-split candles only. "
                        "The parameter sweep matches replay-supported exits: SL, "
                        "trailing stop, probability trail drop, and long/short "
                        "probability thresholds."
                    ),
                    "history_bars": int(len(common_idx)),
                    "train_bars": int(len(X_train)),
                    "validation_bars": int(len(validation_index)),
                    "test_bars": int(len(holdout_index)),
                    "first_train_bar": _ts_iso(train_index, 0),
                    "last_train_bar": _ts_iso(train_index, -1),
                    "first_validation_bar": _ts_iso(validation_index, 0),
                    "last_validation_bar": _ts_iso(validation_index, -1),
                    "first_test_bar": _ts_iso(holdout_price.index, 0),
                    "last_test_bar": _ts_iso(holdout_price.index, -1),
                    "oos_fraction": float(req.oos_fraction),
                    "validation_total_profit": validation_metrics["total_profit"],
                    "validation_num_trades": validation_metrics["num_trades"],
                    "validation_win_rate": validation_metrics["win_rate"],
                    "holdout_num_trades": holdout_metrics["num_trades"],
                    **params,
                    **holdout_metrics,
                }
                all_rows.append(row)

    all_rows.sort(
        key=lambda r: (
            r["score"],
            r["validation_total_profit"],
            r["validation_win_rate"],
            -r["max_drawdown"],
        ),
        reverse=True,
    )
    top_rows = all_rows[:20]
    best_by_algo = []
    seen: set[tuple[str, str]] = set()
    for row in all_rows:
        key = (row["algo_name"], row["interval"])
        if key in seen:
            continue
        seen.add(key)
        best_by_algo.append(row)

    return {
        "symbol": symbol,
        "intervals": intervals,
        "profile": req.profile,
        "backtest_method": "validation_picked_holdout",
        "backtest_method_label": "Validation-picked holdout scan",
        "backtest_explanation": (
            "The optimizer fetches recent history, trains one model per algo/interval "
            "using only pre-split candles, tests parameters on validation bars and "
            "later holdout bars, then ranks only by validation performance. The "
            "displayed P/L and win rate are holdout results, so the holdout is not "
            "used to pick winners. Training labels are rebuilt from the pre-split "
            "frame so they cannot use future validation candles. The parameter "
            "grid is limited to replay-supported exits, with wider SL, trailing "
            "stop, and probability trail values for volatile 5-minute moves."
        ),
        "oos_fraction": float(req.oos_fraction),
        "tested_combinations": len(all_rows),
        "top": top_rows,
        "best_by_algo": best_by_algo,
        "errors": errors,
    }
