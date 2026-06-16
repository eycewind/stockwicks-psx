from __future__ import annotations

import importlib
import math
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from app.services.mm_core_engine import (
    MMCorePosition,
    MMCoreState,
    config_from_obj,
    evaluate_entry,
    evaluate_exit,
)


ALGO_FEATURE_SETS = {
    "Algo1_MM": "Featureset_1",
    "Algo2_MM": "Featureset_2",
    "Algo3_MM": "Featureset_3",
    "Algo4_MM": "Featureset_4",
    "Algo5_MM": "Featureset_5",
    "Algo_SMI": "SMI",
    "Algo_MACD": "MACD",
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
    long_entries = [0.50, 0.55, 0.60, 0.65]
    short_entries = [0.30, 0.35, 0.40, 0.45]
    prob_trail_drops = [round(x / 100.0, 2) for x in range(5, 66, 5)]
    stop_loss_pcts = [0.01, 0.02, 0.03]
    trailing_profit_pcts = [0.005, 0.01]
    prob_exit_modes = ["trailing", "fixed"]
    long_fixed_exit_probs = [0.40]
    short_fixed_exit_probs = [0.60]

    rows: list[dict[str, Any]] = []
    for long_entry in long_entries:
        for short_entry in short_entries:
            if short_entry >= long_entry:
                continue
            for prob_trail_drop in prob_trail_drops:
                for stop_loss_pct in stop_loss_pcts:
                    for trailing_profit_pct in trailing_profit_pcts:
                        for prob_exit_mode in prob_exit_modes:
                            for long_fixed_exit_prob in long_fixed_exit_probs:
                                for short_fixed_exit_prob in short_fixed_exit_probs:
                                    rows.append(
                                        {
                                            "long_entry_prob": long_entry,
                                            "short_entry_prob": short_entry,
                                            "prob_trail_drop": prob_trail_drop,
                                            "hard_stop_usd": 0.0,
                                            "stop_loss_usd": 0.0,
                                            "trailing_profit_usd": 0.0,
                                            "stop_loss_pct": stop_loss_pct,
                                            "trailing_profit_pct": trailing_profit_pct,
                                            "per_share_stop_pct": stop_loss_pct,
                                            "per_share_trailing_profit_pct": trailing_profit_pct,
                                            "prob_exit_mode": prob_exit_mode,
                                            "long_fixed_exit_prob": long_fixed_exit_prob,
                                            "short_fixed_exit_prob": short_fixed_exit_prob,
                                            "prob_smoothing_bars": 3,
                                            "min_prob_advantage": 0.03 if is_algo4 else 0.0,
                                        }
                                    )
    return rows


def _indicator_param_grid() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for stop_loss_pct in (0.01, 0.02, 0.03):
        for trailing_profit_pct in (0.005, 0.01):
            rows.append(
                {
                    "hard_stop_usd": 0.0,
                    "stop_loss_usd": 0.0,
                    "trailing_profit_usd": 0.0,
                    "stop_loss_pct": stop_loss_pct,
                    "trailing_profit_pct": trailing_profit_pct,
                    "per_share_stop_pct": stop_loss_pct,
                    "per_share_trailing_profit_pct": trailing_profit_pct,
                    "prob_exit_mode": "indicator_only",
                    "long_entry_prob": None,
                    "short_entry_prob": None,
                    "prob_trail_drop": None,
                    "long_fixed_exit_prob": None,
                    "short_fixed_exit_prob": None,
                    "prob_smoothing_bars": None,
                    "min_prob_advantage": None,
                }
            )
    return rows


def _holdout_candidate_limit(profile: str) -> int:
    return 50 if str(profile or "quick").lower() == "deep" else 20


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
    core_state = MMCoreState()
    trades: list[dict[str, Any]] = []
    core_cfg = config_from_obj(type("ScanConfig", (), params)(), allow_short=allow_short)
    core_cfg.eod_close = bool(eod_close)

    def close_trade(ts, price: float, reason: str):
        nonlocal position_side, entry_price, entry_time, core_state
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
        core_state = MMCoreState()

    aligned = prob_avg.reindex(price_df.index).ffill()

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
            exit_decision = evaluate_exit(
                MMCorePosition(side=position_side, entry_price=entry_price, quantity=trade_size),
                price,
                current_prob,
                core_cfg,
                core_state,
                now_et=ts.astimezone(_ET).to_pydatetime() if hasattr(ts, "astimezone") else None,
            )
            core_state = exit_decision.state or core_state
            if exit_decision.should_act:
                close_trade(ts, price, exit_decision.reason)
                just_closed = True

        if position_side is None and not just_closed:
            enter_decision = evaluate_entry(current_prob, prev_prob, core_cfg)

            if enter_decision.should_act and enter_decision.action == "LONG":
                position_side = "long"
                entry_price = price
                entry_time = ts
                core_state = MMCoreState(prob_peak=current_prob, profit_peak=0.0)
            elif enter_decision.should_act and enter_decision.action == "SHORT":
                position_side = "short"
                entry_price = price
                entry_time = ts
                core_state = MMCoreState(prob_peak=1.0 - current_prob, profit_peak=0.0)

    if position_side:
        close_trade(price_df.index[-1], float(price_df["close"].iloc[-1]), "FINAL_BAR_CLOSE")

    return trades, _trade_metrics(trades)


def _simulate_indicator_algo(
    *,
    algo_name: str,
    price_df: pd.DataFrame,
    trade_size: float,
    allow_short: bool,
    eod_close: bool,
    params: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    if str(algo_name or "") == "Algo_SMI":
        from app.scripts.stocks.bots.algo3_logic import determine_signals
        indicator_name = "SMI"
    elif str(algo_name or "") == "Algo_MACD":
        from app.scripts.stocks.bots.algo5_logic import determine_signals
        indicator_name = "MACD"
    else:
        raise ValueError(f"Unsupported indicator-only algo: {algo_name}")

    signals_df = determine_signals(price_df)
    position_side: str | None = None
    entry_price = 0.0
    entry_time = None
    profit_peak = 0.0
    trades: list[dict[str, Any]] = []
    params = params or {}
    stop_loss_pct = max(0.0, _safe_float(params.get("stop_loss_pct", params.get("per_share_stop_pct", 0.0)), 0.0))
    trailing_profit_pct = max(
        0.0,
        _safe_float(params.get("trailing_profit_pct", params.get("per_share_trailing_profit_pct", 0.0)), 0.0),
    )

    def close_trade(ts, price: float, reason: str):
        nonlocal position_side, entry_price, entry_time, profit_peak
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
        profit_peak = 0.0

    for i in range(1, len(signals_df)):
        ts = signals_df.index[i]
        prev = signals_df.iloc[i - 1]
        curr = signals_df.iloc[i]
        price = _safe_float(curr.get("open", curr.get("close", 0.0)), 0.0)
        if price <= 0:
            continue

        buy_signal = bool(prev.get("Buy_Signal", False))
        sell_signal = bool(prev.get("Sell_Signal", False))
        just_closed = False
        if position_side:
            pnl = (price - entry_price) * trade_size if position_side == "long" else (entry_price - price) * trade_size
            profit_peak = max(profit_peak, pnl)
            basis = abs(entry_price * trade_size)
            stop_loss_usd = basis * stop_loss_pct
            trailing_profit_usd = basis * trailing_profit_pct

            if stop_loss_usd > 0 and pnl <= -stop_loss_usd:
                close_trade(ts, price, "STOP_LOSS_PCT")
                just_closed = True
            elif trailing_profit_usd > 0 and profit_peak >= trailing_profit_usd and (profit_peak - pnl) >= trailing_profit_usd:
                close_trade(ts, price, "TRAILING_PROFIT_PCT")
                just_closed = True

        if not just_closed and position_side == "long" and sell_signal:
            close_trade(ts, price, f"{indicator_name}_SELL_SIGNAL")
            just_closed = True
        elif not just_closed and position_side == "short" and buy_signal:
            close_trade(ts, price, f"{indicator_name}_BUY_SIGNAL")
            just_closed = True

        if eod_close and position_side is not None:
            ts_et = ts.astimezone(_ET) if hasattr(ts, "astimezone") else None
            if ts_et is not None and ts_et.hour >= 15 and (ts_et.hour > 15 or ts_et.minute >= 58):
                close_trade(ts, price, "EOD_CLOSE")
                just_closed = True

        if position_side is None and not just_closed:
            if buy_signal:
                position_side = "long"
                entry_price = price
                entry_time = ts
                profit_peak = 0.0
            elif sell_signal and allow_short:
                position_side = "short"
                entry_price = price
                entry_time = ts
                profit_peak = 0.0

    if position_side:
        close_trade(signals_df.index[-1], float(signals_df["close"].iloc[-1]), "FINAL_BAR_CLOSE")

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


def _deployment_score(
    *,
    validation_score: float,
    validation_total_profit: float,
    validation_num_trades: float,
    validation_win_rate: float,
    holdout_metrics: dict[str, float],
) -> float:
    holdout_score = _score(holdout_metrics)
    score = min(float(validation_score), float(holdout_score))

    holdout_trades = float(holdout_metrics["num_trades"])
    holdout_profit = float(holdout_metrics["total_profit"])
    holdout_win_rate = float(holdout_metrics["win_rate"])

    if validation_total_profit <= 0:
        score -= 5000.0 + abs(float(validation_total_profit))
    if holdout_profit <= 0:
        score -= 10000.0 + abs(holdout_profit)
    if validation_num_trades < 5:
        score -= (5.0 - float(validation_num_trades)) * 500.0
    if holdout_trades < 8:
        score -= (8.0 - holdout_trades) * 750.0
    if validation_win_rate < 0.50:
        score -= (0.50 - float(validation_win_rate)) * 2000.0
    if holdout_win_rate < 0.50:
        score -= (0.50 - holdout_win_rate) * 3000.0
    if validation_win_rate - holdout_win_rate > 0.25:
        score -= (float(validation_win_rate) - holdout_win_rate) * 2000.0
    return float(score)


def _confidence(metrics: dict[str, float], validation_metrics: dict[str, float] | None = None) -> str:
    if metrics["num_trades"] < 8:
        return "Low"
    validation_ok = True
    if validation_metrics is not None:
        validation_ok = (
            validation_metrics["total_profit"] > 0
            and validation_metrics["num_trades"] >= 5
            and validation_metrics["win_rate"] >= 0.50
        )
    if validation_ok and metrics["total_profit"] > 0 and metrics["win_rate"] >= 0.55 and metrics["profit_factor"] >= 1.25:
        return "High"
    if validation_ok and metrics["total_profit"] > 0 and metrics["win_rate"] >= 0.50:
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
    tested_combinations = 0

    for interval in intervals:
        try:
            price_full = _fetch_price_frame(symbol, interval, req.builder_days)
        except Exception as exc:
            errors.append(f"{symbol} {interval}: {exc}")
            continue

        for algo_name, feature_set in ALGO_FEATURE_SETS.items():
            if algo_name in {"Algo_SMI", "Algo_MACD"}:
                n = len(price_full)
                test_start = int(max(1, n * (1.0 - req.oos_fraction)))
                test_price = price_full.iloc[test_start:].copy()
                if len(test_price) < 20:
                    errors.append(f"{algo_name} {interval}: not enough holdout bars for indicator test ({len(test_price)})")
                    continue

                for params in _indicator_param_grid():
                    trades, metrics = _simulate_indicator_algo(
                        algo_name=algo_name,
                        price_df=test_price,
                        trade_size=req.trade_size,
                        allow_short=req.allow_short,
                        eod_close=req.eod_close,
                        params=params,
                    )
                    tested_combinations += 1
                    validation_score = _score(metrics)
                    deployment_score = _deployment_score(
                        validation_score=validation_score,
                        validation_total_profit=metrics["total_profit"],
                        validation_num_trades=metrics["num_trades"],
                        validation_win_rate=metrics["win_rate"],
                        holdout_metrics=metrics,
                    )
                    row = {
                        "symbol": symbol,
                        "interval": interval,
                        "algo_name": algo_name,
                        "feature_set": feature_set,
                        "score": deployment_score,
                        "selection_score": validation_score,
                        "validation_score": validation_score,
                        "holdout_score": validation_score,
                        "confidence": _confidence(metrics, metrics),
                        "backtest_method": "indicator_only_holdout",
                        "backtest_method_label": "Indicator-only holdout scan",
                        "backtest_notes": (
                            f"{algo_name} uses direct {'SMI' if algo_name == 'Algo_SMI' else 'MACD'} "
                            "Buy_Signal/Sell_Signal logic plus percent stop/trailing guardrails. "
                            "No model training, no probability thresholds."
                        ),
                        "history_bars": int(n),
                        "train_bars": 0,
                        "validation_bars": 0,
                        "test_bars": int(len(test_price)),
                        "first_train_bar": None,
                        "last_train_bar": None,
                        "first_validation_bar": None,
                        "last_validation_bar": None,
                        "first_test_bar": _ts_iso(test_price.index, 0),
                        "last_test_bar": _ts_iso(test_price.index, -1),
                        "oos_fraction": float(req.oos_fraction),
                        "validation_total_profit": metrics["total_profit"],
                        "validation_num_trades": metrics["num_trades"],
                        "validation_win_rate": metrics["win_rate"],
                        "holdout_num_trades": metrics["num_trades"],
                        **params,
                        **metrics,
                    }
                    all_rows.append(row)
                continue

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

            validation_candidates: list[dict[str, Any]] = []
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
                tested_combinations += 1
                validation_score = _score(validation_metrics)
                selection_score = _selection_score(validation_metrics)
                validation_candidates.append(
                    {
                        "score": selection_score,
                        "selection_score": selection_score,
                        "validation_score": validation_score,
                        "validation_total_profit": validation_metrics["total_profit"],
                        "validation_num_trades": validation_metrics["num_trades"],
                        "validation_win_rate": validation_metrics["win_rate"],
                        **params,
                    }
                )

            validation_candidates.sort(
                key=lambda r: (
                    r["score"],
                    r["validation_total_profit"],
                    r["validation_win_rate"],
                    -r.get("hard_stop_usd", 0.0),
                ),
                reverse=True,
            )

            for candidate in validation_candidates[: _holdout_candidate_limit(req.profile)]:
                params = {
                    key: candidate[key]
                    for key in (
                        "long_entry_prob",
                        "short_entry_prob",
                        "prob_trail_drop",
                        "hard_stop_usd",
                        "stop_loss_usd",
                        "trailing_profit_usd",
                        "stop_loss_pct",
                        "trailing_profit_pct",
                        "per_share_stop_pct",
                        "per_share_trailing_profit_pct",
                        "prob_exit_mode",
                        "long_fixed_exit_prob",
                        "short_fixed_exit_prob",
                        "prob_smoothing_bars",
                        "min_prob_advantage",
                    )
                    if key in candidate
                }
                holdout_trades, holdout_metrics = _simulate_combo(
                    algo_name=algo_name,
                    price_df=holdout_price,
                    prob_up=holdout_prob,
                    params=params,
                    trade_size=req.trade_size,
                    allow_short=req.allow_short,
                    eod_close=req.eod_close,
                )
                holdout_score = _score(holdout_metrics)
                deployment_score = _deployment_score(
                    validation_score=candidate["validation_score"],
                    validation_total_profit=candidate["validation_total_profit"],
                    validation_num_trades=candidate["validation_num_trades"],
                    validation_win_rate=candidate["validation_win_rate"],
                    holdout_metrics=holdout_metrics,
                )
                validation_metrics_for_confidence = {
                    "total_profit": candidate["validation_total_profit"],
                    "num_trades": candidate["validation_num_trades"],
                    "win_rate": candidate["validation_win_rate"],
                }
                row = {
                    "symbol": symbol,
                    "interval": interval,
                    "algo_name": algo_name,
                    "feature_set": feature_set,
                    "score": deployment_score,
                    "selection_score": candidate["selection_score"],
                    "validation_score": candidate["validation_score"],
                    "holdout_score": holdout_score,
                    "confidence": _confidence(holdout_metrics, validation_metrics_for_confidence),
                    "backtest_method": "validation_picked_holdout",
                    "backtest_method_label": "Validation-picked holdout scan",
                    "backtest_notes": (
                        "Trains one model on pre-split history, selects parameters "
                        "on validation bars, then reports P/L on later holdout bars. "
                        "Training labels are rebuilt from pre-split candles only. "
                        "The parameter sweep matches replay-supported exits: percent SL, "
                        "percent trailing profit, probability trail drop, and long/short "
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
                    "validation_total_profit": candidate["validation_total_profit"],
                    "validation_num_trades": candidate["validation_num_trades"],
                    "validation_win_rate": candidate["validation_win_rate"],
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
            "runs later holdout bars for the strongest validation candidates, then ranks by a deployment score that requires "
            "both validation and holdout confirmation. The displayed P/L and win rate are holdout results. "
            "Training labels are rebuilt from the pre-split "
            "frame so they cannot use future validation candles. The parameter "
            "grid is limited to replay-supported exits, with percent stop loss, "
            "percent trailing profit, and probability trail values for volatile moves."
        ),
        "oos_fraction": float(req.oos_fraction),
        "tested_combinations": tested_combinations,
        "top": top_rows,
        "best_by_algo": best_by_algo,
        "errors": errors,
    }
