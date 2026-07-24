import json
from types import SimpleNamespace

from app.services.sparkie_weekly_service import (
    _best_match,
    _evaluate_result,
    _unique_symbol_alternatives,
)


def _result(daily_values, *, win_rate=0.65, symbol="TEST", max_drawdown=100.0):
    return SimpleNamespace(
        reference_price=100.0,
        baseline_shares=100,
        daily_pnl_json=json.dumps(
            [
                {"date": f"2026-07-{index + 1:02d}", "profit_loss": value, "trades": 1}
                for index, value in enumerate(daily_values)
            ]
        ),
        validation_profit_loss=500.0,
        validation_trades=10,
        trades=20,
        win_rate=win_rate,
        max_drawdown=max_drawdown,
        score=5.0,
        symbol=symbol,
        interval="5min",
        algo_name="Algo1_MM",
        params_json="{}",
    )


def test_weekly_match_uses_full_cash_and_daily_risk_budget():
    match = _evaluate_result(
        _result(([125.0, 140.0, 130.0, 150.0, -50.0, -50.0] * 4)),
        account_equity=10_000.0,
        daily_risk_budget=1_000.0,
        confidence_level=0.60,
    )

    assert match is not None
    assert match["qualified"] is True
    assert match["shares"] == 100
    assert match["estimated_notional"] == 10_000.0
    assert match["daily_risk_budget"] == 1_000.0
    assert match["max_loss_days"] == 0
    assert match["conservative_monthly_pnl"] > 0


def test_weekly_match_rejects_a_daily_risk_budget_breach():
    match = _evaluate_result(
        _result(([125.0, 20.0, 10.0, -700.0, 0.0, 0.0] * 4)),
        account_equity=10_000.0,
        daily_risk_budget=500.0,
        confidence_level=0.60,
    )

    assert match is not None
    assert match["qualified"] is False
    assert match["daily_risk_budget"] == 500.0
    assert match["gates"]["daily_loss_limit_respected"] is False


def test_weekly_match_does_not_reject_positive_strategy_only_for_sub_fifty_win_rate():
    match = _evaluate_result(
        _result(([125.0, 140.0, 130.0, 150.0, -50.0, -50.0] * 4), win_rate=0.489),
        account_equity=10_000.0,
        daily_risk_budget=1_000.0,
        confidence_level=0.60,
    )

    assert match is not None
    assert match["qualified"] is True
    assert match["gates"]["trade_evidence"] is True


def test_weekly_match_uses_confidence_and_risk_adjusted_metrics():
    match = _evaluate_result(
        _result(([125.0, 140.0, 130.0, 150.0, -50.0, -50.0] * 4)),
        account_equity=10_000.0,
        daily_risk_budget=1_000.0,
        confidence_level=0.60,
    )

    assert match is not None
    assert 0.0 <= match["selection_score"] <= 100.0
    assert match["confidence_preference"] == 0.60
    assert match["positive_day_rate_95pct_low"] < match["positive_day_rate"]
    assert match["risk_budget_utilization_pct"] == 5.0


def test_weekly_match_changes_when_daily_loss_budget_becomes_binding():
    safe = _result(([100.0, 100.0, 100.0, 100.0, 100.0, -50.0] * 4), symbol="SAFE")
    aggressive = _result(
        ([700.0, 700.0, 700.0, 700.0, 700.0, -1_500.0] * 4),
        symbol="FAST",
        max_drawdown=1_500.0,
    )

    low_risk = _best_match(
        [safe, aggressive],
        account_equity=10_000.0,
        daily_risk_budget=1_000.0,
        confidence_level=0.60,
    )
    high_risk = _best_match(
        [safe, aggressive],
        account_equity=10_000.0,
        daily_risk_budget=5_000.0,
        confidence_level=0.60,
    )

    assert low_risk is not None
    assert low_risk["symbol"] == "SAFE"
    assert high_risk is not None
    assert high_risk["symbol"] == "FAST"


def test_weekly_alternatives_show_different_symbols():
    matches = [
        {"symbol": "CODX", "selection_score": 90},
        {"symbol": "CODX", "selection_score": 89},
        {"symbol": "NVDA", "selection_score": 88},
        {"symbol": "AAPL", "selection_score": 87},
    ]

    alternatives = _unique_symbol_alternatives(matches, exclude_symbol="CODX", limit=4)

    assert [match["symbol"] for match in alternatives] == ["NVDA", "AAPL"]
