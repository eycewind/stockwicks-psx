from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.routes.auth import get_current_user
from app.services.barchart_symbols import barchart_ranked_rows


router = APIRouter(tags=["research"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/auth/research", response_class=HTMLResponse)
def research_page(request: Request, user=Depends(get_current_user)):
    return templates.TemplateResponse(
        request,
        "research/index.html",
        {
            "request": request,
            "user": user,
            "title": "Market Research",
        },
    )


@router.get("/api/research/stocks/{sentiment}")
def research_rankings(
    sentiment: Literal["bullish", "bearish"],
    refresh: bool = False,
    _user=Depends(get_current_user),
):
    try:
        rows = barchart_ranked_rows(sentiment, force_refresh=refresh)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"The {sentiment} stock rankings are temporarily unavailable: {exc}",
        ) from exc

    rank_field = "currentRankUsTop100" if sentiment == "bullish" else "currentRankUsBottom100"
    return {
        "ok": True,
        "sentiment": sentiment,
        "label": "Stock Bullish" if sentiment == "bullish" else "Stock Bearish",
        "count": len(rows),
        "fetched_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "rows": [
            {
                "symbol": row.get("symbol"),
                "name": row.get("symbolName"),
                "weighted_alpha": row.get("weightedAlpha"),
                "rank": row.get(rank_field),
                "previous_rank": row.get("previousRank"),
                "latest": row.get("lastPrice"),
                "change": row.get("priceChange"),
                "percent_change": row.get("percentChange"),
                "high_52w": row.get("highPrice1y"),
                "low_52w": row.get("lowPrice1y"),
                "percent_change_52w": row.get("percentChange1y"),
                "time": row.get("tradeTime"),
            }
            for row in rows
        ],
    }
