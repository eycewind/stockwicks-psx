from __future__ import annotations

import math
from typing import Any, Iterable


SPARKIE_ANALYSIS_MODEL_VERSION = "success_risk_liquidity_v2"


def wilson_lower_bound(successes: int, trials: int, z_score: float = 1.96) -> float:
    """Return the conservative 95% lower bound for a binomial success rate."""
    if trials <= 0:
        return 0.0
    observed = max(0.0, min(1.0, float(successes) / float(trials)))
    z_squared = z_score * z_score
    denominator = 1.0 + z_squared / trials
    center = observed + z_squared / (2.0 * trials)
    spread = z_score * math.sqrt(
        (observed * (1.0 - observed) + z_squared / (4.0 * trials)) / trials
    )
    return max(0.0, min(1.0, (center - spread) / denominator))


def risk_adjusted_selection_metrics(
    *,
    daily_profits: Iterable[float],
    account_equity: float,
    daily_risk_budget: float,
    conservative_monthly_pnl: float,
    typical_monthly_pnl: float,
    max_drawdown: float,
    validation_profit_loss: float,
    confidence_preference: float,
) -> dict[str, Any]:
    """
    Build a bounded, risk-sensitive ranking score.

    Raw historical P/L is deliberately capped inside the score so an extreme
    backtest cannot overwhelm consistency, downside, drawdown, and holdout
    validation. Dollar values remain untouched in the user-facing evidence.
    """
    profits = [float(value) for value in daily_profits]
    equity = max(float(account_equity), 1.0)
    risk_budget = max(float(daily_risk_budget), 1.0)
    risk_tolerance = max(0.0, min(1.0, risk_budget / equity))

    positive_days = sum(value > 0 for value in profits)
    observed_positive_rate = positive_days / len(profits) if profits else 0.0
    confidence_low = wilson_lower_bound(positive_days, len(profits))
    preference = max(0.01, min(0.99, float(confidence_preference)))
    confidence_attainment = min(confidence_low / preference, 1.0)

    losses = [abs(value) for value in profits if value < 0]
    downside_deviation = (
        math.sqrt(sum(value * value for value in losses) / len(profits))
        if profits and losses
        else 0.0
    )
    worst_daily_loss = abs(min(min(profits), 0.0)) if profits else 0.0
    drawdown = abs(float(max_drawdown))

    conservative_return = float(conservative_monthly_pnl) / equity
    typical_return = float(typical_monthly_pnl) / equity
    drawdown_pct = drawdown / equity
    worst_loss_pct = worst_daily_loss / equity
    downside_deviation_pct = downside_deviation / equity
    validation_return = float(validation_profit_loss) / equity

    # Cap extraordinary historical returns before scoring. The original P/L
    # remains visible, but a 100% backtest month is not treated as ten times
    # more trustworthy than a 10% month.
    return_quality = (
        0.60 * min(max(conservative_return, 0.0) / 0.20, 1.0)
        + 0.40 * min(max(typical_return, 0.0) / 0.25, 1.0)
    )
    validation_quality = min(max(validation_return, 0.0) / 0.10, 1.0)
    confidence_quality = 0.50 * confidence_low + 0.50 * confidence_attainment

    drawdown_safety = 1.0 - min(drawdown / risk_budget, 1.0)
    worst_day_safety = 1.0 - min(worst_daily_loss / risk_budget, 1.0)
    downside_safety = 1.0 - min(downside_deviation / risk_budget, 1.0)
    risk_quality = (
        0.45 * drawdown_safety
        + 0.35 * worst_day_safety
        + 0.20 * downside_safety
    )

    binding_risk = max(drawdown, worst_daily_loss, downside_deviation, equity * 0.01)
    return_to_risk = max(float(conservative_monthly_pnl), 0.0) / binding_risk
    efficiency_quality = min(return_to_risk / 3.0, 1.0)

    # A client accepting more daily risk can place more weight on return, but
    # consistency and downside never disappear from the ranking.
    return_weight = 0.30 + 0.25 * risk_tolerance
    confidence_weight = 0.30 - 0.15 * risk_tolerance
    risk_weight = 0.25 - 0.10 * risk_tolerance
    validation_weight = 0.10
    efficiency_weight = 0.05
    selection_score = 100.0 * (
        return_weight * return_quality
        + confidence_weight * confidence_quality
        + risk_weight * risk_quality
        + validation_weight * validation_quality
        + efficiency_weight * efficiency_quality
    )

    return {
        "selection_score": round(selection_score, 3),
        "positive_day_rate": round(observed_positive_rate, 4),
        "positive_day_rate_95pct_low": round(confidence_low, 4),
        "confidence_preference": round(preference, 4),
        "confidence_preference_met": confidence_low >= preference,
        "risk_budget_utilization_pct": round(worst_daily_loss / risk_budget * 100.0, 2),
        "drawdown_budget_utilization_pct": round(drawdown / risk_budget * 100.0, 2),
        "worst_daily_loss_pct": round(worst_loss_pct * 100.0, 2),
        "max_drawdown_pct": round(drawdown_pct * 100.0, 2),
        "downside_deviation": round(downside_deviation, 2),
        "downside_deviation_pct": round(downside_deviation_pct * 100.0, 2),
        "validation_return_pct": round(validation_return * 100.0, 2),
        "return_to_risk": round(return_to_risk, 3),
        "ranking_components": {
            "return_quality": round(return_quality, 4),
            "confidence_quality": round(confidence_quality, 4),
            "risk_quality": round(risk_quality, 4),
            "validation_quality": round(validation_quality, 4),
            "efficiency_quality": round(efficiency_quality, 4),
        },
    }
