from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from typing import Literal

from app.routes.auth import get_current_user
from app.trading.sparkie import (
    AgentLaunchRequest,
    GoalRequest,
    SparkiePerformanceRequest,
    assess_goal_feasibility,
    build_agent_launch_plan,
    build_performance_preview,
)


router = APIRouter(prefix="/api/sparkie", tags=["sparkie"])
page_router = APIRouter(tags=["sparkie"])
templates = Jinja2Templates(directory="app/templates")


class GoalFeasibilityRequest(BaseModel):
    account_equity: float = Field(..., gt=0)
    target_profit: float = Field(..., gt=0)
    target_period: Literal["daily", "weekly", "monthly"] = "daily"
    confidence_level: float = Field(0.60, ge=0.50, le=0.95)


class SparkieLaunchRequest(GoalFeasibilityRequest):
    requested_mode: Literal["paper", "live_mirror"] = "paper"
    acknowledged_live_risk: bool = False


class SparkiePerformanceApiRequest(GoalFeasibilityRequest):
    start_date: str | None = None
    end_date: str | None = None
    lookback_days: int | None = Field(None, gt=0)
    symbols: list[str] = Field(default_factory=list)
    intervals: list[str] = Field(default_factory=list)
    algos: list[str] = Field(default_factory=list)


@page_router.get("/auth/sparkie", response_class=HTMLResponse)
def sparkie_page(
    request: Request,
    user=Depends(get_current_user),
):
    return templates.TemplateResponse(
        request,
        "sparkie/index.html",
        {
            "request": request,
            "user": user,
            "title": "Sparkie Agent",
        },
    )


@router.post("/goal-feasibility")
def goal_feasibility(
    payload: GoalFeasibilityRequest,
    _user=Depends(get_current_user),
):
    try:
        result = assess_goal_feasibility(
            GoalRequest(
                account_equity=payload.account_equity,
                target_profit=payload.target_profit,
                target_period=payload.target_period,
                confidence_level=payload.confidence_level,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return result.to_dict()


@router.post("/performance-preview")
def performance_preview(
    payload: SparkiePerformanceApiRequest,
    _user=Depends(get_current_user),
):
    try:
        result = build_performance_preview(
            SparkiePerformanceRequest(
                account_equity=payload.account_equity,
                target_profit=payload.target_profit,
                target_period=payload.target_period,
                start_date=_parse_date(payload.start_date, "start_date"),
                end_date=_parse_date(payload.end_date, "end_date"),
                lookback_days=payload.lookback_days,
                symbols=tuple(payload.symbols or ()),
                intervals=tuple(payload.intervals or ()),
                algos=tuple(payload.algos or ()),
                confidence_level=payload.confidence_level,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return result.to_dict()


def _parse_date(value: str | None, field_name: str):
    if not value:
        return None
    try:
        from datetime import date

        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be YYYY-MM-DD") from exc


@router.post("/launch-plan")
def launch_plan(
    payload: SparkieLaunchRequest,
    _user=Depends(get_current_user),
):
    try:
        result = build_agent_launch_plan(
            AgentLaunchRequest(
                account_equity=payload.account_equity,
                target_profit=payload.target_profit,
                target_period=payload.target_period,
                requested_mode=payload.requested_mode,
                confidence_level=payload.confidence_level,
                acknowledged_live_risk=payload.acknowledged_live_risk,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return result.to_dict()
