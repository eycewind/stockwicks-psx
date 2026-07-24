import json
from types import SimpleNamespace

from app.services.sparkie_weekly_service import (
    SYMBOL_OUTCOME_ANALYZED,
    SYMBOL_OUTCOME_BACKTEST_FAILURE,
    SYMBOL_OUTCOME_DATA_FAILURE,
    SYMBOL_OUTCOME_NO_RESULT,
    _best_match,
    _evaluate_result,
    _normalize_symbol_outcome,
    _rank_matches,
    _unique_symbol_alternatives,
)
from app.services.sparkie_risk_scoring import SPARKIE_ANALYSIS_MODEL_VERSION


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
        params_json=json.dumps({
            "sparkie_analysis_model_version": SPARKIE_ANALYSIS_MODEL_VERSION,
            "sparkie_execution_slippage_bps": 10.0,
            "sparkie_median_daily_dollar_volume": 10_000_000.0,
            "sparkie_max_position_pct_daily_dollar_volume": 0.01,
        }),
    )


def test_weekly_match_uses_full_cash_and_daily_risk_budget():
    match = _evaluate_result(
        _result(([125.0, 140.0, 130.0, 150.0, 110.0, -50.0] * 4)),
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
        _result(([125.0, 140.0, 130.0, 150.0, 110.0, -50.0] * 4), win_rate=0.489),
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
    assert match["gates"]["profitable_day_confidence"] is False


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


def test_weekly_ranking_prefers_conservative_success_over_raw_return_score():
    high_success = {
        "qualified": True,
        "symbol": "STEADY",
        "positive_day_rate_95pct_low": 0.65,
        "positive_day_rate": 0.80,
        "selection_score": 70.0,
        "return_to_risk": 2.0,
        "validation_return_pct": 5.0,
        "conservative_monthly_pnl": 500.0,
        "estimated_max_drawdown": 100.0,
    }
    high_return = {
        "qualified": True,
        "symbol": "FAST",
        "positive_day_rate_95pct_low": 0.55,
        "positive_day_rate": 0.75,
        "selection_score": 99.0,
        "return_to_risk": 10.0,
        "validation_return_pct": 20.0,
        "conservative_monthly_pnl": 5_000.0,
        "estimated_max_drawdown": 500.0,
    }

    ranked = _rank_matches([high_return, high_success])

    assert ranked[0]["symbol"] == "STEADY"


def test_weekly_match_rejects_legacy_cost_free_catalog_result():
    legacy = _result(([125.0, 140.0, 130.0, 150.0, 110.0, -50.0] * 4))
    legacy.params_json = "{}"

    match = _evaluate_result(
        legacy,
        account_equity=10_000.0,
        daily_risk_budget=1_000.0,
        confidence_level=0.60,
    )

    assert match is not None
    assert match["qualified"] is False
    assert match["gates"]["execution_cost_model_present"] is False
    assert match["gates"]["liquidity_capacity"] is False


def test_weekly_legacy_checkpoint_outcomes_are_separated():
    assert _normalize_symbol_outcome("completed") == SYMBOL_OUTCOME_ANALYZED
    assert (
        _normalize_symbol_outcome(
            "failed",
            "No validated result was produced for this symbol.",
        )
        == SYMBOL_OUTCOME_NO_RESULT
    )
    assert (
        _normalize_symbol_outcome(
            "failed",
            "ABC: data preparation skipped: Downloaded data has only 10 trading days.",
        )
        == SYMBOL_OUTCOME_DATA_FAILURE
    )
    assert (
        _normalize_symbol_outcome("failed", "5min: model training exploded")
        == SYMBOL_OUTCOME_BACKTEST_FAILURE
    )
