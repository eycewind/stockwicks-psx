from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.paper_trading_bot import (
    PaperStockTradeBot,
    PaperStockBotOpenTrade,
    PaperStockBotTradeHistory,
)


def order_by_best_ts(query, model, fields=("entry_time", "created_at", "updated_at", "id")):
    for field in fields:
        if hasattr(model, field):
            return query.order_by(getattr(model, field).desc())
    return query


def hist_pl_value(row: Any) -> float:
    for field in ("profit_loss", "realized_pl", "pnl", "pl"):
        value = getattr(row, field, None)
        if value is not None:
            try:
                return float(value)
            except Exception:
                return 0.0
    return 0.0


def row_pl_value(row: Any) -> Decimal:
    value = getattr(row, "profit_loss", None)
    if value is None:
        value = getattr(row, "realized_pl", None)
    if value is None:
        value = getattr(row, "unrealized_pl", None)
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal("0")


def summarize_rows(rows: list[Any]) -> dict:
    total_pl = sum(row_pl_value(row) for row in rows)
    wins = sum(1 for row in rows if row_pl_value(row) > 0)
    losses = sum(1 for row in rows if row_pl_value(row) < 0)
    total = len(rows)

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round((wins / total) * 100, 2) if total else 0,
        "total_pl": float(total_pl),
    }


def get_bot_dashboard_data(db: Session, user_id: int) -> dict:
    bots = (
        db.query(PaperStockTradeBot)
        .filter(PaperStockTradeBot.user_id == user_id)
        .order_by(PaperStockTradeBot.created_at.desc())
        .all()
    )

    open_trades_query = db.query(PaperStockBotOpenTrade).filter(
        PaperStockBotOpenTrade.user_id == user_id
    )
    open_trades = order_by_best_ts(open_trades_query, PaperStockBotOpenTrade).all()

    history = (
        db.query(PaperStockBotTradeHistory)
        .filter(PaperStockBotTradeHistory.user_id == user_id)
        .order_by(PaperStockBotTradeHistory.id.desc())
        .limit(100)
        .all()
    )

    total_pl = sum(hist_pl_value(row) for row in history)

    return {
        "bots": bots,
        "open_trades": open_trades,
        "history": history,
        "summary": summarize_rows(history),
        "paper_account": {
            "initial_balance": 100000,
            "current_balance": 100000 + total_pl,
        },
    }
