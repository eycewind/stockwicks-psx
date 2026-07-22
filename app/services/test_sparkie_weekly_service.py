import json
from types import SimpleNamespace

from app.services.sparkie_weekly_service import DAILY_LOSS_MULTIPLIER, _evaluate_result


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


def test_weekly_match_uses_full_cash_and_five_x_loss_cap():
    match = _evaluate_result(
        _result([125.0, 140.0, 130.0, 150.0, -50.0, -50.0]),
        account_equity=10_000.0,
        daily_target=100.0,
        confidence_level=0.60,
    )

    assert match is not None
    assert match["qualified"] is True
    assert match["shares"] == 100
    assert match["estimated_notional"] == 10_000.0
    assert match["daily_loss_limit"] == 100.0 * DAILY_LOSS_MULTIPLIER
    assert match["target_hit_days"] == 4
    assert match["target_hit_rate"] == 0.6667


def test_weekly_match_rejects_target_with_insufficient_hit_rate():
    match = _evaluate_result(
        _result([125.0, 20.0, 10.0, -700.0, 0.0, 0.0]),
        account_equity=10_000.0,
        daily_target=100.0,
        confidence_level=0.60,
    )

    assert match is not None
    assert match["qualified"] is False
    assert match["daily_loss_limit"] == 500.0
    assert match["gates"]["target_confidence"] is False
