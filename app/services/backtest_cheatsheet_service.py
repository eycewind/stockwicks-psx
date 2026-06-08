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


def _interval_tolerance(interval: str) -> pd.Timedelta:
    minutes = {
        "1min": 1,
        "5min": 5,
        "10min": 10,
        "15min": 15,
        "30min": 30,
    }.get((interval or "").lower())
    if minutes:
        return pd.Timedelta(minutes=max(minutes // 2, 1))
    return pd.Timedelta(hours=12)


def _with_utc_index(df: pd.DataFrame, naive_tz: str) -> pd.DataFrame:
    out = df.copy()
    idx = pd.to_datetime(out.index)
    if getattr(idx, "tz", None) is None:
        idx = idx.tz_localize(naive_tz)
    else:
        idx = idx.tz_convert("UTC")
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("UTC")
    out.index = idx
    return out.sort_index()


def _try_exact_align(
    price_full: pd.DataFrame,
    train_feat: pd.DataFrame,
    infer_feat: pd.DataFrame,
    naive_tz: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    price = _with_utc_index(price_full, naive_tz)
    train = _with_utc_index(train_feat, naive_tz)
    infer = _with_utc_index(infer_feat, naive_tz)
    common_idx = price.index.intersection(train.index).intersection(infer.index)
    return price.loc[common_idx].copy(), train.loc[common_idx].copy(), infer.loc[common_idx].copy()


def _asof_align(
    price_full: pd.DataFrame,
    train_feat: pd.DataFrame,
    infer_feat: pd.DataFrame,
    interval: str,
    naive_tz: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    price = _with_utc_index(price_full, naive_tz)
    train = _with_utc_index(train_feat, naive_tz)
    infer = _with_utc_index(infer_feat, naive_tz)

    price_base = price.reset_index()
    train_base = train.reset_index()
    infer_base = infer.reset_index()
    price_base = price_base.rename(columns={price_base.columns[0]: "bar_ts"}).sort_values("bar_ts")
    train_base = train_base.rename(columns={train_base.columns[0]: "feat_ts"}).sort_values("feat_ts")
    infer_base = infer_base.rename(columns={infer_base.columns[0]: "feat_ts"}).sort_values("feat_ts")
    tolerance = _interval_tolerance(interval)

    aligned_train = pd.merge_asof(
        price_base[["bar_ts"]],
        train_base,
        left_on="bar_ts",
        right_on="feat_ts",
        direction="nearest",
        tolerance=tolerance,
    ).drop(columns=["feat_ts"])
    aligned_infer = pd.merge_asof(
        price_base[["bar_ts"]],
        infer_base,
        left_on="bar_ts",
        right_on="feat_ts",
        direction="nearest",
        tolerance=tolerance,
    ).drop(columns=["feat_ts"])

    aligned_train = aligned_train.set_index("bar_ts")
    aligned_infer = aligned_infer.set_index("bar_ts")
    price = price.loc[aligned_train.index].copy()

    keep = aligned_train.notna().any(axis=1) & aligned_infer.notna().any(axis=1)
    if "y" in aligned_train.columns:
        keep = keep & aligned_train["y"].notna()
    if "w" in aligned_train.columns:
        keep = keep & aligned_train["w"].notna()

    return price.loc[keep].copy(), aligned_train.loc[keep].copy(), aligned_infer.loc[keep].copy()


def _align_price_and_features(
    price_full: pd.DataFrame,
    train_feat: pd.DataFrame,
    infer_feat: pd.DataFrame,
    interval: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, str]:
    candidates: list[tuple[str, pd.DataFrame, pd.DataFrame, pd.DataFrame]] = []
    for naive_tz in ("UTC", "America/New_York"):
        price, train, infer = _try_exact_align(price_full, train_feat, infer_feat, naive_tz)
        candidates.append((f"exact:{naive_tz}", price, train, infer))

        price, train, infer = _asof_align(price_full, train_feat, infer_feat, interval, naive_tz)
        candidates.append((f"nearest:{naive_tz}", price, train, infer))

    label, price_df, train_df, infer_df = max(candidates, key=lambda item: len(item[1]))
    return price_df, train_df, infer_df, label


def _param_grid(profile: str) -> list[dict[str, Any]]:
    if str(profile or "quick").lower() == "deep":
        long_entries = [0.56, 0.58, 0.60, 0.62, 0.65]
        short_entries = [0.44, 0.42, 0.40, 0.38, 0.35]
        prob_trail_drops = [0.03, 0.05, 0.08]
        hard_stops = [200.0, 300.0, 500.0]
        trail_activations = [50.0, 75.0, 100.0]
        trail_distances = [25.0, 35.0, 50.0]
    else:
        long_entries = [0.58, 0.60, 0.62]
        short_entries = [0.42, 0.40, 0.38]
        prob_trail_drops = [0.03, 0.05]
        hard_stops = [200.0, 300.0]
        trail_activations = [50.0, 75.0]
        trail_distances = [25.0, 35.0]

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


def _simulate_combo(
    *,
    price_df: pd.DataFrame,
    prob_up: pd.Series,
    params: dict[str, Any],
    trade_size: float,
    allow_short: bool,
    eod_close: bool,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    smoothing = max(1, int(params.get("prob_smoothing_bars", 3)))
    prob_avg = prob_up.rolling(window=smoothing, min_periods=smoothing).mean()

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

    for i in range(1, len(price_df)):
        ts = price_df.index[i]
        price = float(price_df["close"].iloc[i])
        current_prob = _safe_float(aligned.iloc[i], float("nan"))
        prev_prob = _safe_float(aligned.iloc[i - 1], float("nan"))
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
            crossed_up = prev_prob < long_entry <= current_prob
            crossed_down = prev_prob > short_entry >= current_prob
            if crossed_up:
                position_side = "long"
                entry_price = price
                entry_time = ts
                prob_peak = current_prob
                profit_peak = 0.0
            elif crossed_down and allow_short:
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

    params_grid = _param_grid(req.profile)
    all_rows: list[dict[str, Any]] = []
    errors: list[str] = []

    for interval in intervals:
        try:
            price_full = _fetch_price_frame(symbol, interval, req.builder_days)
        except Exception as exc:
            errors.append(f"{symbol} {interval}: {exc}")
            continue

        for algo_name, feature_set in ALGO_FEATURE_SETS.items():
            try:
                feature_module = _load_feature_module(feature_set)
            except ModuleNotFoundError:
                errors.append(f"{algo_name}: {feature_set} is not available in this build")
                continue
            except Exception as exc:
                errors.append(f"{algo_name}: failed loading {feature_set}: {exc}")
                continue

            try:
                train_feat = feature_module.build_training_features_from_df(
                    df=price_full,
                    symbol=symbol,
                    interval=interval,
                    k_forward=req.k_forward,
                    feature_set=feature_set,
                )
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

            price_df, train_feat, infer_feat, align_method = _align_price_and_features(
                price_full,
                train_feat,
                infer_feat,
                interval,
            )
            if len(price_df) < 80:
                errors.append(
                    f"{algo_name} {interval}: not enough aligned bars ({len(price_df)}) "
                    f"using {align_method}"
                )
                continue
            if "y" not in train_feat or "w" not in train_feat or train_feat["y"].nunique() < 2:
                errors.append(f"{algo_name} {interval}: training labels are not usable")
                continue

            split_at = int(len(price_df) * (1.0 - req.oos_fraction))
            split_at = max(30, min(split_at, len(price_df) - 20))
            X_train = _prepare_X(train_feat.iloc[:split_at], list(feat_names))
            y_train = train_feat.loc[X_train.index, "y"].astype(int).values
            w_train = train_feat.loc[X_train.index, "w"].astype(float).values
            if len(set(y_train)) < 2:
                errors.append(f"{algo_name} {interval}: OOS split left only one training class")
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

            sim_index = price_df.index[split_at:]
            sim_price = price_df.loc[sim_index]
            prob_series = pd.Series(probs, index=X_all.index, name="prob_up").loc[sim_index]

            for params in params_grid:
                trades, metrics = _simulate_combo(
                    price_df=sim_price,
                    prob_up=prob_series,
                    params=params,
                    trade_size=req.trade_size,
                    allow_short=req.allow_short,
                    eod_close=req.eod_close,
                )
                row = {
                    "symbol": symbol,
                    "interval": interval,
                    "algo_name": algo_name,
                    "feature_set": feature_set,
                    "score": _score(metrics),
                    "confidence": _confidence(metrics),
                    "first_test_bar": sim_price.index[0].isoformat() if not sim_price.empty else None,
                    "last_test_bar": sim_price.index[-1].isoformat() if not sim_price.empty else None,
                    **params,
                    **metrics,
                }
                all_rows.append(row)

    all_rows.sort(key=lambda r: (r["score"], r["total_profit"], r["win_rate"]), reverse=True)
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
        "tested_combinations": len(all_rows),
        "top": top_rows,
        "best_by_algo": best_by_algo,
        "errors": errors,
    }
