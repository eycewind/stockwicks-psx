from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.database.connection import DATABASE_URL, get_db
from app.models.user import User
from app.routes.auth import get_current_user
from app.utils.client_context import client_slug as current_client_slug


router = APIRouter(prefix="/admin/live-trades", tags=["admin-live-trades"])
templates = Jinja2Templates(directory="app/templates")

_ENGINE_CACHE: dict[str, Engine] = {}


def _require_admin(user: User = Depends(get_current_user)) -> User:
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _configured_client_slugs() -> list[str]:
    raw = os.getenv("ADMIN_CLIENT_SLUGS", "").strip()
    if raw:
        slugs = [s.strip() for s in raw.split(",") if s.strip()]
    else:
        slugs = [current_client_slug()]
    seen: set[str] = set()
    out: list[str] = []
    for slug in slugs:
        if slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


def _dsn_for_client(slug: str) -> str | None:
    urls_raw = os.getenv("ADMIN_CLIENT_DATABASE_URLS", "").strip()
    if urls_raw:
        try:
            urls = json.loads(urls_raw)
            if isinstance(urls, dict) and urls.get(slug):
                return str(urls[slug])
        except Exception:
            pass

    template = os.getenv("ADMIN_CLIENT_DATABASE_URL_TEMPLATE", "").strip()
    if template:
        return template.format(slug=slug)

    if slug == current_client_slug():
        return DATABASE_URL

    return None


def _engine_for_client(slug: str) -> Engine:
    dsn = _dsn_for_client(slug)
    if not dsn:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No database URL configured for client '{slug}'. "
                "Set ADMIN_CLIENT_DATABASE_URLS or ADMIN_CLIENT_DATABASE_URL_TEMPLATE."
            ),
        )
    cached = _ENGINE_CACHE.get(dsn)
    if cached is None:
        cached = create_engine(
            dsn,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={"connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "10"))},
            future=True,
        )
        _ENGINE_CACHE[dsn] = cached
    return cached


@contextmanager
def _client_connection(slug: str) -> Iterator[Any]:
    engine = _engine_for_client(slug)
    with engine.connect() as conn:
        yield conn


def _parse_day(value: str | None, default: date) -> date:
    if not value:
        return default
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date: {value}")


def _date_bounds(start: str | None, end: str | None) -> tuple[datetime, datetime]:
    today = date.today()
    start_day = _parse_day(start, today - timedelta(days=30))
    end_day = _parse_day(end, today)
    if start_day > end_day:
        raise HTTPException(status_code=400, detail="Start date must be before end date")
    return (
        datetime.combine(start_day, time.min),
        datetime.combine(end_day + timedelta(days=1), time.min),
    )


def _normalize_filter(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value or value.upper() == "ALL":
        return None
    return value


def _selected_clients(client: str | None) -> list[str]:
    slugs = _configured_client_slugs()
    client = _normalize_filter(client)
    if not client:
        return slugs
    if client not in slugs:
        raise HTTPException(status_code=400, detail=f"Client '{client}' is not configured for admin reporting")
    return [client]


def _fetch_client_rows(
    *,
    slug: str,
    start_dt: datetime,
    end_dt: datetime,
    symbol: str | None,
    user_id: int | None,
    limit: int,
) -> list[dict[str, Any]]:
    filters = [
        "m.exit_time >= :start_dt",
        "m.exit_time < :end_dt",
    ]
    params: dict[str, Any] = {
        "start_dt": start_dt,
        "end_dt": end_dt,
        "limit": limit,
    }
    if symbol:
        filters.append("upper(m.symbol) = :symbol")
        params["symbol"] = symbol.upper()
    if user_id is not None:
        filters.append("m.user_id = :user_id")
        params["user_id"] = user_id

    table_check = text("SELECT to_regclass('public.paper_stock_bot_live_mirror_history') IS NOT NULL")
    with _client_connection(slug) as conn:
        has_mirror_table = bool(conn.execute(table_check).scalar())
    if not has_mirror_table:
        return []

    sql = text(f"""
        SELECT
            m.id,
            m.user_id,
            u.username,
            u.email,
            m.bot_id,
            b.algo_name,
            m.history_trade_id,
            m.symbol,
            m.side,
            m.quantity,
            m.entry_price,
            m.exit_price,
            m.profit_loss,
            m.entry_time,
            m.exit_time,
            m.schwab_order_id,
            m.mirror_status,
            m.created_at
        FROM paper_stock_bot_live_mirror_history m
        LEFT JOIN users u ON u.id = m.user_id
        LEFT JOIN paper_stock_trade_bots b ON b.id = m.bot_id
        WHERE {" AND ".join(filters)}
        ORDER BY m.exit_time DESC NULLS LAST, m.id DESC
        LIMIT :limit
    """)

    with _client_connection(slug) as conn:
        rows = conn.execute(sql, params).mappings().all()
    payload: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["client"] = slug
        payload.append(item)
    return payload


def _summarize(rows: list[dict[str, Any]], selected_symbol: str | None) -> dict[str, Any]:
    pnl = sum(float(r.get("profit_loss") or 0.0) for r in rows)
    wins = sum(1 for r in rows if float(r.get("profit_loss") or 0.0) > 0)
    losses = sum(1 for r in rows if float(r.get("profit_loss") or 0.0) < 0)
    breakeven = len(rows) - wins - losses
    decided = wins + losses
    return {
        "symbol": selected_symbol.upper() if selected_symbol else "ALL",
        "total_trades": len(rows),
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "success_rate": round((wins / decided) * 100, 2) if decided else 0.0,
        "pnl": round(pnl, 2),
    }


def _client_summaries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("client") or "unknown"), []).append(row)
    return [
        {"client": slug, **_summarize(client_rows, None)}
        for slug, client_rows in sorted(grouped.items())
    ]


def _format_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for key in ("entry_time", "exit_time", "created_at"):
            value = item.get(key)
            if isinstance(value, datetime):
                item[key] = value.strftime("%Y-%m-%d %H:%M:%S")
        item["profit_loss"] = round(float(item.get("profit_loss") or 0.0), 2)
        out.append(item)
    return out


@router.get("")
def admin_live_trades_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(_require_admin),
):
    today = date.today()
    return templates.TemplateResponse(
        request,
        "admin/live_trades.html",
        {
            "request": request,
            "user": user,
            "clients": _configured_client_slugs(),
            "default_start": (today - timedelta(days=30)).isoformat(),
            "default_end": today.isoformat(),
        },
    )


@router.get("/json")
def admin_live_trades_json(
    client: str = Query("ALL"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    symbol: str = Query("ALL"),
    user_id: int | None = Query(None),
    limit: int = Query(1000, ge=1, le=10000),
    _: User = Depends(_require_admin),
):
    start_dt, end_dt = _date_bounds(start, end)
    selected_symbol = _normalize_filter(symbol)
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    clients = _selected_clients(client)

    per_client_limit = max(1, limit)
    for slug in clients:
        try:
            rows.extend(
                _fetch_client_rows(
                    slug=slug,
                    start_dt=start_dt,
                    end_dt=end_dt,
                    symbol=selected_symbol,
                    user_id=user_id,
                    limit=per_client_limit,
                )
            )
        except Exception as exc:
            errors.append({"client": slug, "error": str(exc)})

    rows.sort(key=lambda r: (r.get("exit_time") or datetime.min, r.get("id") or 0), reverse=True)
    rows = rows[:limit]

    return JSONResponse(
        {
            "filters": {
                "client": client,
                "clients": clients,
                "start": start_dt.date().isoformat(),
                "end": (end_dt.date() - timedelta(days=1)).isoformat(),
                "symbol": selected_symbol or "ALL",
                "user_id": user_id,
            },
            "summary": _summarize(rows, selected_symbol),
            "client_summaries": _client_summaries(rows),
            "trades": _format_rows(rows),
            "errors": errors,
        }
    )
