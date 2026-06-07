import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database.connection import get_db
from app.models.paper_trading_bot import PaperStockTradeBot
from app.routes.auth import get_current_user

router = APIRouter(prefix="/auth/papertradebot", tags=["Paper Bot"])
templates = Jinja2Templates(directory="app/templates")


def _url_prefix(request: Request) -> str:
    return request.headers.get("x-forwarded-prefix", "") or ""


def _read_jsonl(path: Path, limit: int = 500):
    if not path.exists():
        return []

    rows = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []

    return rows[-limit:]


def _bot_log_payload(bot: PaperStockTradeBot):
    data_dir = os.getenv("DATA_DIR", "/var/stockwicks/clients/ashakil/data")

    user_id = int(bot.user_id)
    bot_id = int(bot.id)
    symbol = str(bot.symbol).upper()
    interval = str(bot.interval)
    algo_name = str(bot.algo_name)

    base_dir = Path(data_dir) / str(user_id)

    candles_path = base_dir / f"bot_{bot_id}_{symbol}_{interval}_candles.jsonl"
    decisions_path = base_dir / f"bot_{bot_id}_{symbol}_{algo_name}_decisions.jsonl"
    events_path = base_dir / f"bot_{bot_id}_{symbol}_{algo_name}_events.jsonl"

    candles = _read_jsonl(candles_path, limit=500)
    decisions = _read_jsonl(decisions_path, limit=100)
    events = _read_jsonl(events_path, limit=100)

    return {
        "bot": {
            "id": bot_id,
            "user_id": user_id,
            "symbol": symbol,
            "interval": interval,
            "algo_name": algo_name,
            "status": bot.status,
        },
        "paths": {
            "candles": str(candles_path),
            "decisions": str(decisions_path),
            "events": str(events_path),
        },
        "candles": candles,
        "decisions": decisions,
        "events": events,
    }



@router.get("")
@router.get("/")
def redirect_paper_bot_to_production(request: Request):
    return RedirectResponse(url=f"{_url_prefix(request)}/auth/papertradebot", status_code=303)

@router.get("", response_class=HTMLResponse, name="paper_bot")
@router.get("/", response_class=HTMLResponse, name="paper_bot_slash")
def paper_bot_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    bots = (
        db.query(PaperStockTradeBot)
        .filter(PaperStockTradeBot.user_id == current_user.id)
        .order_by(PaperStockTradeBot.id.desc())
        .all()
    )

    return templates.TemplateResponse(
        request,
        "paper_bot/index.html",
        {
            "request": request,
            "user": current_user,
            "bots": bots,
            "url_prefix": _url_prefix(request),
        },
    )


@router.post("/create", name="paper_bot_create")
def create_paper_bot(
    request: Request,
    symbol: str = Form("SPY"),
    interval: str = Form("5m"),
    algo_name: str = Form("mvp_demo"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    bot = PaperStockTradeBot(
        user_id=current_user.id,
        symbol=symbol.upper().strip(),
        interval=interval.strip(),
        algo_name=algo_name.strip(),
        status="stopped",
    )
    db.add(bot)
    db.commit()

    return RedirectResponse(url=f"{_url_prefix(request)}/auth/papertradebot", status_code=303)


@router.post("/{bot_id}/start", name="paper_bot_start")
def start_paper_bot(
    bot_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    bot = (
        db.query(PaperStockTradeBot)
        .filter(
            PaperStockTradeBot.id == bot_id,
            PaperStockTradeBot.user_id == current_user.id,
        )
        .first()
    )

    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    bot.status = "running"
    db.commit()

    return RedirectResponse(url=f"{_url_prefix(request)}/auth/papertradebot", status_code=303)


@router.post("/{bot_id}/stop", name="paper_bot_stop")
def stop_paper_bot(
    bot_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    bot = (
        db.query(PaperStockTradeBot)
        .filter(
            PaperStockTradeBot.id == bot_id,
            PaperStockTradeBot.user_id == current_user.id,
        )
        .first()
    )

    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    bot.status = "stopped"
    db.commit()

    return RedirectResponse(url=f"{_url_prefix(request)}/auth/papertradebot", status_code=303)


@router.get("/{bot_id}/chart-data", name="paper_bot_chart_data")
def paper_bot_chart_data(
    bot_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    bot = (
        db.query(PaperStockTradeBot)
        .filter(
            PaperStockTradeBot.id == bot_id,
            PaperStockTradeBot.user_id == current_user.id,
        )
        .first()
    )

    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    return JSONResponse(_bot_log_payload(bot))
