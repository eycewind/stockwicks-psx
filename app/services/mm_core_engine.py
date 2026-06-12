from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import Optional

import numpy as np


@dataclass
class MMCoreConfig:
    long_entry_prob: float = 0.60
    short_entry_prob: float = 0.40
    prob_smoothing_bars: int = 3
    stop_loss_usd: float = 300.0
    trailing_profit_usd: float = 75.0
    prob_trail_drop: float = 0.05
    prob_exit_mode: str = "trailing"
    long_fixed_exit_prob: float = 0.55
    short_fixed_exit_prob: float = 0.55
    allow_short: bool = True
    eod_close: bool = True


@dataclass
class MMCorePosition:
    side: str
    entry_price: float
    quantity: float


@dataclass
class MMCoreState:
    prob_peak: float = 0.0
    profit_peak: float = 0.0


@dataclass
class MMCoreDecision:
    should_act: bool
    action: str = ""
    reason: str = ""
    state: Optional[MMCoreState] = None


def _finite_float(value, default: float) -> float:
    try:
        out = float(value)
        return out if np.isfinite(out) else float(default)
    except Exception:
        return float(default)


def _bool_value(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"1", "true", "yes", "y", "on", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "disabled"}:
            return False
    return bool(default)


def config_from_obj(obj, *, allow_short: bool | None = None) -> MMCoreConfig:
    stop_loss = _finite_float(
        getattr(obj, "stop_loss_usd", getattr(obj, "hard_stop_usd", 300.0)),
        300.0,
    )
    trailing_profit = _finite_float(
        getattr(
            obj,
            "trailing_profit_usd",
            getattr(obj, "trailing_stop_distance", getattr(obj, "trailing_stop_activation", 75.0)),
        ),
        75.0,
    )
    mode = str(getattr(obj, "prob_exit_mode", "trailing") or "trailing").strip().lower()
    if mode not in {"trailing", "fixed"}:
        mode = "trailing"

    return MMCoreConfig(
        long_entry_prob=_finite_float(getattr(obj, "long_entry_prob", 0.60), 0.60),
        short_entry_prob=_finite_float(getattr(obj, "short_entry_prob", 0.40), 0.40),
        prob_smoothing_bars=max(1, int(_finite_float(getattr(obj, "prob_smoothing_bars", 3), 3))),
        stop_loss_usd=stop_loss,
        trailing_profit_usd=trailing_profit,
        prob_trail_drop=_finite_float(getattr(obj, "prob_trail_drop", 0.05), 0.05),
        prob_exit_mode=mode,
        long_fixed_exit_prob=_finite_float(getattr(obj, "long_fixed_exit_prob", 0.55), 0.55),
        short_fixed_exit_prob=_finite_float(getattr(obj, "short_fixed_exit_prob", 0.55), 0.55),
        allow_short=_bool_value(allow_short, True) if allow_short is not None else _bool_value(getattr(obj, "allow_short", True), True),
        eod_close=_bool_value(getattr(obj, "eod_close", True), True),
    )


def evaluate_entry(
    prob_up_avg: float,
    prob_up_avg_prev: float | None,
    cfg: MMCoreConfig,
) -> MMCoreDecision:
    if not np.isfinite(prob_up_avg):
        return MMCoreDecision(False, reason="NO_VALID_PROB_AVG")
    if prob_up_avg_prev is None or not np.isfinite(prob_up_avg_prev):
        return MMCoreDecision(False, reason="NO_PREVIOUS_PROB_AVG_FOR_CROSS")

    crossed_up = prob_up_avg_prev < cfg.long_entry_prob <= prob_up_avg
    crossed_down = prob_up_avg_prev > cfg.short_entry_prob >= prob_up_avg

    if crossed_up:
        return MMCoreDecision(True, action="LONG", reason=f"PROB_AVG_CROSS_ABOVE_LONG_ENTRY_{cfg.long_entry_prob:.2f}")
    if crossed_down and cfg.allow_short:
        return MMCoreDecision(True, action="SHORT", reason=f"PROB_AVG_CROSS_BELOW_SHORT_ENTRY_{cfg.short_entry_prob:.2f}")
    if crossed_down and not cfg.allow_short:
        return MMCoreDecision(False, reason="SHORT_SIGNAL_BUT_SHORT_DISABLED")
    if prob_up_avg >= cfg.long_entry_prob:
        return MMCoreDecision(False, reason="BULLISH_BUT_NO_NEW_LONG_CROSS")
    if prob_up_avg <= cfg.short_entry_prob:
        return MMCoreDecision(False, reason="BEARISH_BUT_NO_NEW_SHORT_CROSS")
    return MMCoreDecision(False, reason=f"NO_ENTRY_PROB_AVG_{prob_up_avg:.4f}_BETWEEN_{cfg.short_entry_prob:.2f}_{cfg.long_entry_prob:.2f}")


def position_pnl(position: MMCorePosition, current_price: float) -> float:
    if position.side.lower() == "long":
        return (current_price - position.entry_price) * position.quantity
    return (position.entry_price - current_price) * position.quantity


def evaluate_exit(
    position: MMCorePosition,
    current_price: float,
    prob_up_avg: float,
    cfg: MMCoreConfig,
    state: MMCoreState,
    *,
    now_et: datetime | None = None,
) -> MMCoreDecision:
    if position.entry_price <= 0 or position.quantity <= 0 or current_price <= 0:
        return MMCoreDecision(False, reason="BAD_EXIT_INPUT", state=state)

    pnl_usd = position_pnl(position, current_price)
    next_state = MMCoreState(prob_peak=state.prob_peak, profit_peak=max(state.profit_peak, pnl_usd))

    if cfg.stop_loss_usd > 0 and pnl_usd <= -abs(cfg.stop_loss_usd):
        return MMCoreDecision(True, action="EXIT", reason="STOP_LOSS", state=MMCoreState())

    if cfg.trailing_profit_usd > 0:
        giveback = next_state.profit_peak - pnl_usd
        if next_state.profit_peak >= cfg.trailing_profit_usd and giveback >= cfg.trailing_profit_usd:
            return MMCoreDecision(True, action="EXIT", reason="TRAILING_PROFIT", state=MMCoreState())

    side = position.side.lower()
    conviction = prob_up_avg if side == "long" else 1.0 - prob_up_avg
    if np.isfinite(conviction):
        if cfg.prob_exit_mode == "fixed":
            fixed_exit = cfg.long_fixed_exit_prob if side == "long" else cfg.short_fixed_exit_prob
            if fixed_exit > 0 and conviction <= fixed_exit:
                return MMCoreDecision(True, action="EXIT", reason="PROB_FIXED_EXIT", state=MMCoreState())
        else:
            next_state.prob_peak = max(next_state.prob_peak, conviction)
            drop = next_state.prob_peak - conviction
            if cfg.prob_trail_drop > 0 and drop >= cfg.prob_trail_drop:
                return MMCoreDecision(True, action="EXIT", reason="PROB_TRAIL_DROP", state=MMCoreState())

    if cfg.eod_close:
        ts = now_et or datetime.now()
        if ts.time() >= dtime(15, 50):
            return MMCoreDecision(True, action="EXIT", reason="EOD_CLOSE", state=MMCoreState())

    return MMCoreDecision(False, reason="GUARDRAILS_NOT_HIT", state=next_state)
