from __future__ import annotations

import json
from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func

from app.database.connection import SessionLocal
from app.models.paper_trading_bot import (
    PaperStockBotOpenTrade,
    PaperStockBotTradeHistory,
    PaperStockTradeBot,
)


_ET = ZoneInfo("America/New_York")


def entry_risk_gate(bot_id: int, now_et: datetime | None = None) -> dict[str, Any]:
    """Persist and enforce the Sparkie daily lock before a new entry."""
    db = SessionLocal()
    try:
        bot = db.get(PaperStockTradeBot, int(bot_id))
        if not bot:
            return {"allowed": False, "reason": "BOT_NOT_FOUND"}
        if db.query(PaperStockBotOpenTrade.id).filter_by(bot_id=int(bot_id)).first():
            return {"allowed": True, "reason": "OPEN_POSITION_NEEDS_MANAGEMENT"}
        state = evaluate_daily_state(db, bot, unrealized_pnl=0.0, now_et=now_et)
        db.commit()
        return {"allowed": not state["locked"], **state}
    except Exception:
        db.rollback()
        # A risk-control database failure must fail closed for new entries.
        return {"allowed": False, "reason": "DAILY_RISK_CHECK_FAILED", "locked": True}
    finally:
        db.close()


def evaluate_daily_state(
    db,
    bot: PaperStockTradeBot,
    *,
    unrealized_pnl: float = 0.0,
    now_et: datetime | None = None,
) -> dict[str, Any]:
    now_et = _as_et(now_et)
    trading_date = now_et.date().isoformat()
    cfg = _config(bot)
    target = _float(cfg.get("daily_profit_target_usd"))
    loss_limit = abs(_float(cfg.get("daily_loss_limit_usd")))
    enabled = bool(
        cfg.get("sparkie_v2")
        or cfg.get("stop_trading_after_daily_target")
        or cfg.get("stop_trading_after_daily_loss")
    ) and (target > 0 or loss_limit > 0)
    if not enabled:
        return {
            "locked": False,
            "reason": "DAILY_POLICY_DISABLED",
            "realized_pnl": 0.0,
            "total_pnl": float(unrealized_pnl or 0.0),
            "target": target,
            "loss_limit": loss_limit,
        }

    if cfg.get("daily_lock_date") and cfg.get("daily_lock_date") != trading_date:
        cfg.pop("daily_lock_date", None)
        cfg.pop("daily_lock_reason", None)
        cfg.pop("daily_lock_pnl", None)
        bot.config_json = json.dumps(cfg, separators=(",", ":"), sort_keys=True)

    realized = realized_pnl_for_date(db, int(bot.id), now_et)
    total = realized + float(unrealized_pnl or 0.0)
    reason = str(cfg.get("daily_lock_reason") or "") if cfg.get("daily_lock_date") == trading_date else ""
    if not reason and target > 0 and total >= target:
        reason = "DAILY_PROFIT_TARGET"
    if not reason and loss_limit > 0 and total <= -loss_limit:
        reason = "DAILY_LOSS_LIMIT"
    if reason:
        cfg["daily_lock_date"] = trading_date
        cfg["daily_lock_reason"] = reason
        cfg["daily_lock_pnl"] = round(total, 2)
        bot.config_json = json.dumps(cfg, separators=(",", ":"), sort_keys=True)

    return {
        "locked": bool(reason),
        "reason": reason or "DAILY_LIMITS_CLEAR",
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(float(unrealized_pnl or 0.0), 2),
        "total_pnl": round(total, 2),
        "target": target,
        "loss_limit": loss_limit,
        "trading_date": trading_date,
    }


def realized_pnl_for_date(db, bot_id: int, now_et: datetime | None = None) -> float:
    now_et = _as_et(now_et)
    start_et = datetime.combine(now_et.date(), time.min, tzinfo=_ET)
    end_et = datetime.combine(now_et.date(), time.max, tzinfo=_ET)
    # History timestamps are stored as naive UTC by the current models.
    start_utc = start_et.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_et.astimezone(timezone.utc).replace(tzinfo=None)
    value = (
        db.query(func.coalesce(func.sum(PaperStockBotTradeHistory.profit_loss), 0.0))
        .filter(PaperStockBotTradeHistory.bot_id == int(bot_id))
        .filter(PaperStockBotTradeHistory.exit_time >= start_utc)
        .filter(PaperStockBotTradeHistory.exit_time <= end_utc)
        .scalar()
    )
    return float(value or 0.0)


def _config(bot: PaperStockTradeBot) -> dict[str, Any]:
    try:
        value = json.loads(bot.config_json or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _as_et(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(_ET)
    if value.tzinfo is None:
        return value.replace(tzinfo=_ET)
    return value.astimezone(_ET)
