from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from math import pow
from typing import Literal, Optional


GoalPeriod = Literal["daily", "weekly", "monthly"]
FeasibilityLabel = Literal["reasonable", "aggressive", "unreasonable"]
AgentMode = Literal["paper", "live_mirror"]
PerformanceStatus = Literal["criteria_ready", "blocked"]

MIN_SPARKIE_ACCOUNT_EQUITY = 5_000.0

TRADING_PERIODS_PER_YEAR: dict[GoalPeriod, int] = {
    "daily": 252,
    "weekly": 52,
    "monthly": 12,
}


@dataclass(frozen=True)
class GoalRequest:
    account_equity: float
    target_profit: float
    target_period: GoalPeriod = "daily"
    confidence_level: float = 0.60


@dataclass(frozen=True)
class RiskRails:
    risk_per_trade_pct: float
    max_open_risk_pct: float
    daily_stop_loss_pct: float
    weekly_stop_loss_pct: float
    soft_drawdown_pct: float
    hard_drawdown_pct: float
    min_cash_buffer_pct: float


@dataclass(frozen=True)
class GoalFeasibility:
    label: FeasibilityLabel
    account_equity: float
    target_profit: float
    target_period: GoalPeriod
    target_return_pct: float
    implied_annual_return_pct: float
    confidence_level: float
    probability_note: str
    risk_rails: RiskRails
    max_reasonable_target_profit: float
    max_aggressive_target_profit: float
    minimum_account_equity: float
    meets_minimum_equity: bool
    message: str

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["risk_rails"] = asdict(self.risk_rails)
        return payload


@dataclass(frozen=True)
class AgentLaunchRequest:
    account_equity: float
    target_profit: float
    target_period: GoalPeriod = "daily"
    requested_mode: AgentMode = "paper"
    confidence_level: float = 0.60
    acknowledged_live_risk: bool = False


@dataclass(frozen=True)
class AgentLaunchPlan:
    agent_name: str
    mode: AgentMode
    can_start: bool
    requires_restart_for_live: bool
    live_mirror_allowed: bool
    minimum_account_equity: float
    meets_minimum_equity: bool
    feasibility: GoalFeasibility
    account_equity_source: str
    message: str
    next_step: str

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["feasibility"] = self.feasibility.to_dict()
        return payload


@dataclass(frozen=True)
class SparkiePerformanceRequest:
    account_equity: float
    target_profit: float
    target_period: GoalPeriod = "daily"
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    lookback_days: Optional[int] = None
    symbols: tuple[str, ...] = ()
    intervals: tuple[str, ...] = ()
    algos: tuple[str, ...] = ()
    confidence_level: float = 0.60


@dataclass(frozen=True)
class SparkiePerformancePreview:
    agent_name: str
    status: PerformanceStatus
    replay_ready: bool
    account_equity: float
    minimum_account_equity: float
    meets_minimum_equity: bool
    start_date: str
    end_date: str
    calendar_days: int
    estimated_trading_days: int
    target_profit_for_window: float
    target_return_for_window_pct: float
    feasibility: GoalFeasibility
    symbols: tuple[str, ...]
    intervals: tuple[str, ...]
    algos: tuple[str, ...]
    message: str
    next_step: str

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["feasibility"] = self.feasibility.to_dict()
        payload["symbols"] = list(self.symbols)
        payload["intervals"] = list(self.intervals)
        payload["algos"] = list(self.algos)
        return payload


def assess_goal_feasibility(request: GoalRequest) -> GoalFeasibility:
    _validate_request(request)

    target_return = request.target_profit / request.account_equity
    periods = TRADING_PERIODS_PER_YEAR[request.target_period]
    implied_annual = pow(1.0 + target_return, periods) - 1.0

    reasonable_pct, aggressive_pct = _period_thresholds(request.target_period)
    label = _label_for_target(target_return, reasonable_pct, aggressive_pct)
    rails = _rails_for_label(label)

    max_reasonable_target = request.account_equity * reasonable_pct
    max_aggressive_target = request.account_equity * aggressive_pct

    return GoalFeasibility(
        label=label,
        account_equity=round(request.account_equity, 2),
        target_profit=round(request.target_profit, 2),
        target_period=request.target_period,
        target_return_pct=round(target_return * 100.0, 4),
        implied_annual_return_pct=round(implied_annual * 100.0, 2),
        confidence_level=request.confidence_level,
        probability_note=_probability_note(label, request.confidence_level),
        risk_rails=rails,
        max_reasonable_target_profit=round(max_reasonable_target, 2),
        max_aggressive_target_profit=round(max_aggressive_target, 2),
        minimum_account_equity=MIN_SPARKIE_ACCOUNT_EQUITY,
        meets_minimum_equity=_meets_minimum_equity(request.account_equity),
        message=_message(label, request.target_period),
    )


def build_agent_launch_plan(request: AgentLaunchRequest) -> AgentLaunchPlan:
    feasibility = assess_goal_feasibility(
        GoalRequest(
            account_equity=request.account_equity,
            target_profit=request.target_profit,
            target_period=request.target_period,
            confidence_level=request.confidence_level,
        )
    )
    meets_minimum = _meets_minimum_equity(request.account_equity)

    if not meets_minimum:
        return AgentLaunchPlan(
            agent_name="Sparkie",
            mode=request.requested_mode,
            can_start=False,
            requires_restart_for_live=True,
            live_mirror_allowed=False,
            minimum_account_equity=MIN_SPARKIE_ACCOUNT_EQUITY,
            meets_minimum_equity=False,
            feasibility=feasibility,
            account_equity_source="paper_balance"
            if request.requested_mode == "paper"
            else "broker_account",
            message=(
                "Sparkie requires at least $5,000 account equity before it can run "
                "in paper or Live Mirror mode."
            ),
            next_step="Increase the simulated or broker account amount to at least $5,000.",
        )

    if request.requested_mode == "paper":
        return AgentLaunchPlan(
            agent_name="Sparkie",
            mode="paper",
            can_start=True,
            requires_restart_for_live=True,
            live_mirror_allowed=False,
            minimum_account_equity=MIN_SPARKIE_ACCOUNT_EQUITY,
            meets_minimum_equity=True,
            feasibility=feasibility,
            account_equity_source="paper_balance",
            message="Sparkie will run with paper money sized to the user's stated account amount.",
            next_step="Review paper trades, drawdown, win/loss streaks, and goal hit rate before restarting with Live Mirror.",
        )

    live_allowed = request.acknowledged_live_risk and feasibility.label != "unreasonable"
    return AgentLaunchPlan(
        agent_name="Sparkie",
        mode="live_mirror",
        can_start=live_allowed,
        requires_restart_for_live=True,
        live_mirror_allowed=live_allowed,
        minimum_account_equity=MIN_SPARKIE_ACCOUNT_EQUITY,
        meets_minimum_equity=True,
        feasibility=feasibility,
        account_equity_source="broker_account",
        message=_live_mirror_message(feasibility, request.acknowledged_live_risk),
        next_step=_live_mirror_next_step(live_allowed),
    )


def build_performance_preview(
    request: SparkiePerformanceRequest,
) -> SparkiePerformancePreview:
    start, end = _resolve_performance_window(
        request.start_date,
        request.end_date,
        request.lookback_days,
    )
    if end < start:
        raise ValueError("end_date must be greater than or equal to start_date")

    feasibility = assess_goal_feasibility(
        GoalRequest(
            account_equity=request.account_equity,
            target_profit=request.target_profit,
            target_period=request.target_period,
            confidence_level=request.confidence_level,
        )
    )
    meets_minimum = _meets_minimum_equity(request.account_equity)
    calendar_days = (end - start).days + 1
    trading_days = _estimated_trading_days(start, end)
    target_periods = _target_periods_in_window(
        request.target_period,
        calendar_days,
        trading_days,
    )
    target_profit_for_window = request.target_profit * target_periods
    replay_ready = meets_minimum

    return SparkiePerformancePreview(
        agent_name="Sparkie",
        status="criteria_ready" if replay_ready else "blocked",
        replay_ready=replay_ready,
        account_equity=round(request.account_equity, 2),
        minimum_account_equity=MIN_SPARKIE_ACCOUNT_EQUITY,
        meets_minimum_equity=meets_minimum,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        calendar_days=calendar_days,
        estimated_trading_days=trading_days,
        target_profit_for_window=round(target_profit_for_window, 2),
        target_return_for_window_pct=round(
            (target_profit_for_window / request.account_equity) * 100.0,
            4,
        ),
        feasibility=feasibility,
        symbols=tuple(_normalize_text_items(request.symbols)),
        intervals=tuple(_normalize_text_items(request.intervals)),
        algos=tuple(_normalize_text_items(request.algos)),
        message=_performance_message(replay_ready),
        next_step=_performance_next_step(replay_ready),
    )


def _validate_request(request: GoalRequest) -> None:
    if request.account_equity <= 0:
        raise ValueError("account_equity must be greater than zero")
    if request.target_profit <= 0:
        raise ValueError("target_profit must be greater than zero")
    if request.target_period not in TRADING_PERIODS_PER_YEAR:
        raise ValueError("target_period must be daily, weekly, or monthly")
    if not 0.4 <= request.confidence_level <= 0.95:
        raise ValueError("confidence_level must be between 0.40 and 0.95")


def _meets_minimum_equity(account_equity: float) -> bool:
    return account_equity >= MIN_SPARKIE_ACCOUNT_EQUITY


def _resolve_performance_window(
    start_date: Optional[date],
    end_date: Optional[date],
    lookback_days: Optional[int],
) -> tuple[date, date]:
    if lookback_days is not None:
        if lookback_days <= 0:
            raise ValueError("lookback_days must be greater than zero")
        end = end_date or date.today()
        return end - timedelta(days=lookback_days - 1), end

    if start_date is None or end_date is None:
        raise ValueError("Provide either lookback_days or both start_date and end_date")

    return start_date, end_date


def _estimated_trading_days(start_date: date, end_date: date) -> int:
    days = 0
    cursor = start_date
    while cursor <= end_date:
        if cursor.weekday() < 5:
            days += 1
        cursor += timedelta(days=1)
    return days


def _target_periods_in_window(
    period: GoalPeriod,
    calendar_days: int,
    trading_days: int,
) -> float:
    if period == "daily":
        return float(trading_days)
    if period == "weekly":
        return max(calendar_days / 7.0, 1.0)
    return max(calendar_days / 30.0, 1.0)


def _normalize_text_items(items: tuple[str, ...]) -> list[str]:
    normalized: list[str] = []
    for item in items or ():
        value = str(item or "").strip()
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def _performance_message(replay_ready: bool) -> str:
    if replay_ready:
        return "Sparkie criteria are valid and ready to be submitted to the replay/backtest runner."
    return "Sparkie performance preview is blocked because account equity is below the $5,000 minimum."


def _performance_next_step(replay_ready: bool) -> str:
    if replay_ready:
        return "Run the replay adapter for this date window, symbols, intervals, and algos, then compare actual P/L to the target."
    return "Use at least $5,000 paper balance before checking Sparkie performance."


def _live_mirror_message(
    feasibility: GoalFeasibility,
    acknowledged_live_risk: bool,
) -> str:
    if not acknowledged_live_risk:
        return "Live Mirror requires explicit user acknowledgement of live trading risk."
    if feasibility.label == "unreasonable":
        return "Live Mirror is blocked because the requested target is unreasonable for the account size."
    return "Sparkie can restart in Live Mirror mode with the same risk rails used during paper evaluation."


def _live_mirror_next_step(live_allowed: bool) -> str:
    if live_allowed:
        return "Start a new Sparkie run in Live Mirror mode and keep paper/live results paired for audit."
    return "Start or continue paper mode until the target and drawdown profile are acceptable."


def _period_thresholds(period: GoalPeriod) -> tuple[float, float]:
    if period == "daily":
        return 0.0025, 0.01
    if period == "weekly":
        return 0.01, 0.04
    return 0.03, 0.12


def _label_for_target(
    target_return: float,
    reasonable_pct: float,
    aggressive_pct: float,
) -> FeasibilityLabel:
    if target_return <= reasonable_pct:
        return "reasonable"
    if target_return <= aggressive_pct:
        return "aggressive"
    return "unreasonable"


def _rails_for_label(label: FeasibilityLabel) -> RiskRails:
    if label == "reasonable":
        return RiskRails(
            risk_per_trade_pct=0.25,
            max_open_risk_pct=0.75,
            daily_stop_loss_pct=1.0,
            weekly_stop_loss_pct=3.0,
            soft_drawdown_pct=4.0,
            hard_drawdown_pct=7.0,
            min_cash_buffer_pct=10.0,
        )
    if label == "aggressive":
        return RiskRails(
            risk_per_trade_pct=0.15,
            max_open_risk_pct=0.50,
            daily_stop_loss_pct=0.75,
            weekly_stop_loss_pct=2.0,
            soft_drawdown_pct=3.0,
            hard_drawdown_pct=5.0,
            min_cash_buffer_pct=15.0,
        )
    return RiskRails(
        risk_per_trade_pct=0.10,
        max_open_risk_pct=0.25,
        daily_stop_loss_pct=0.50,
        weekly_stop_loss_pct=1.5,
        soft_drawdown_pct=2.0,
        hard_drawdown_pct=4.0,
        min_cash_buffer_pct=25.0,
    )


def _probability_note(label: FeasibilityLabel, confidence_level: float) -> str:
    confidence_pct = round(confidence_level * 100)
    if label == "reasonable":
        return f"Target can be evaluated against strategy history at about {confidence_pct}% confidence."
    if label == "aggressive":
        return f"Target should require strong recent backtest evidence before being offered at {confidence_pct}% confidence."
    return "Target should not be promised; agent should reduce the suggested target or stay in paper mode."


def _message(label: FeasibilityLabel, period: GoalPeriod) -> str:
    if label == "reasonable":
        return f"This {period} target is within the starter risk model."
    if label == "aggressive":
        return f"This {period} target is possible only with higher variance and tighter loss limits."
    return f"This {period} target is too high for the account size under the starter risk model."
