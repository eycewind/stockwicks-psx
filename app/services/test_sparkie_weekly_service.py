import json
from types import SimpleNamespace

from app.services.sparkie_weekly_service import _evaluate_result


def _result(daily_values):
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
        win_rate=0.65,
        max_drawdown=100.0,
        score=5.0,
        symbol="TEST",
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
