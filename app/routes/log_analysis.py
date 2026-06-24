# app/routes/log_analysis.py
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from app.services.log_analysis_service import (
    blocked_entries,
    chart_payload,
    feature_series,
    load_rows,
    summarize,
)

router = APIRouter(prefix="/analysis", tags=["log-analysis"])
templates = Jinja2Templates(directory="app/templates")


def project_base_dir() -> Path:
    # app/routes/log_analysis.py
    # parents[0] = routes
    # parents[1] = app
    # parents[2] = client root
    return Path(__file__).resolve().parents[2]


@router.get("/logs")
async def log_analysis_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="log_analysis/index.html",
        context={
            "title": "Bot Log Analysis",
        },
    )


@router.get("/api/logs/summary")
async def api_logs_summary(symbol: Optional[str] = Query(default=None)):
    rows = load_rows(project_base_dir(), symbol=symbol)
    return JSONResponse(summarize(rows))


@router.get("/api/logs/symbols")
async def api_logs_symbols():
    rows = load_rows(project_base_dir())
    symbols = sorted({
        str(row.get("symbol") or "").upper()
        for row in rows
        if row.get("symbol") and str(row.get("symbol")).upper() != "UNKNOWN"
    })
    return JSONResponse({"symbols": symbols})


@router.get("/api/logs/chart-data")
async def api_logs_chart_data(symbol: Optional[str] = Query(default=None)):
    rows = load_rows(project_base_dir(), symbol=symbol)
    return JSONResponse(chart_payload(rows))


@router.get("/api/logs/blocked-entries")
async def api_blocked_entries(symbol: Optional[str] = Query(default=None)):
    rows = load_rows(project_base_dir(), symbol=symbol)
    return JSONResponse(
        {
            "blocked_entries": blocked_entries(rows),
        }
    )


@router.get("/api/logs/features")
async def api_features(symbol: Optional[str] = Query(default=None)):
    rows = load_rows(project_base_dir(), symbol=symbol)
    return JSONResponse(feature_series(rows))
