# /var/stockwicks/clients/ashakil/app/services/replay_trade_service.py
"""
Replay Trade Service
====================

Parallel to app.services.paper_trade_service, but writes to replay_* tables
and uses SIMULATED bar times, not wall clock.

Used only by replay runner / orchestrator. Never touches:
  - paper_stock_bot_*
  - PaperAccount
  - Schwab live orders
  - EmailService / notifications
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Dict, Any

from sqlalchemy.orm import Session

from app.models.replay import (
    ReplaySession,
    ReplayOpenTrade,
    ReplayTradeHistory,
)

log = logging.getLogger("ReplayTradeSvc")
if not log.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [ReplayTradeSvc] %(message)s"
    ))
    log.addHandler(h)
    log.setLevel(logging.INFO)


# =============================================================================
# Helpers
# =============================================================================
def _model_keys(model_cls) -> set[str]:
    """
    Return valid SQLAlchemy mapped attribute names for a model.
    Prevents errors like:
      'user_id' is an invalid keyword argument for ReplayOpenTrade
    """
    try:
        return {p.key for p in model_cls.__mapper__.attrs}
    except Exception:
        return set()


def _filter_kwargs(model_cls, values: Dict[str, Any]) -> Dict[str, Any]:
    keys = _model_keys(model_cls)
    if not keys:
        return values

    kept = {k: v for k, v in values.items() if k in keys}
    skipped = sorted(set(values.keys()) - set(kept.keys()))
    if skipped:
        log.warning(
            "[MODEL_KWARGS] Skipping fields not present on %s: %s",
            model_cls.__name__,
            skipped,
        )
    return kept


def _get_attr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


# =============================================================================
# Open
# =============================================================================
def open_position_replay(
    db: Session,
    session: ReplaySession,
    position_side: str,     # 'long' | 'short'
    price: float,
    bar_time: datetime,     # simulated entry time
    quantity: Optional[float] = None,
) -> Optional[ReplayOpenTrade]:
    """
    Open a replay position. Returns the open trade row, or None on failure.
    """
    try:
        side = (position_side or "").lower().strip()
        if side not in {"long", "short"}:
            raise ValueError(f"Invalid position_side: {position_side}")

        qty = float(quantity) if quantity is not None else float(_get_attr(session, "trade_size", 0.0) or 0.0)
        if qty <= 0:
            raise ValueError(f"Invalid quantity: {qty}")

        existing = (
            db.query(ReplayOpenTrade)
              .filter_by(session_id=session.id, symbol=session.symbol)
              .first()
        )
        if existing:
            log.warning(
                "[OPEN] Session %s already has open %s for %s; refusing another",
                session.id,
                existing.position_side,
                session.symbol,
            )
            return None

        values = {
            "session_id": session.id,
            "user_id": _get_attr(session, "user_id", None),
            "symbol": session.symbol,
            "position_side": side,
            "trade_type": "BUY" if side == "long" else "SELL_SHORT",
            "quantity": qty,
            "entry_price": float(price),
            "entry_time": bar_time,
            "current_price": float(price),
            "unrealized_pl": 0.0,
            "unrealized_pnl": 0.0,
            "algo_name": _get_attr(session, "algo_name", "AlgoMM"),
        }

        ot = ReplayOpenTrade(**_filter_kwargs(ReplayOpenTrade, values))
        db.add(ot)
        db.commit()
        db.refresh(ot)

        log.info(
            "[OPEN] Session %s %s %s qty=%s @ %.2f bar_time=%s",
            session.id,
            side.upper(),
            session.symbol,
            qty,
            float(price),
            bar_time,
        )
        return ot

    except Exception as e:
        db.rollback()
        log.error("[OPEN] Failed for session %s: %s", getattr(session, "id", "?"), e, exc_info=True)
        return None


# =============================================================================
# Mark-to-market update
# =============================================================================
def update_open_trade_mark_replay(
    db: Session,
    trade: ReplayOpenTrade,
    price: float,
    bar_time: Optional[datetime] = None,
    commit: bool = True,
) -> Optional[ReplayOpenTrade]:
    """
    Update replay open trade current_price and unrealized P/L.

    Compatible with calls like:
      update_open_trade_mark_replay(db, trade, price, commit=True)
      update_open_trade_mark_replay(db, trade, price, bar_time=bar_time, commit=False)

    Safe for schemas that may have:
      current_price
      unrealized_pl
      unrealized_pnl
      updated_at
    """
    try:
        if trade is None:
            return None

        side = (_get_attr(trade, "position_side", "") or "").lower().strip()
        qty = float(_get_attr(trade, "quantity", 0.0) or 0.0)
        ep = float(_get_attr(trade, "entry_price", 0.0) or 0.0)
        px = float(price)

        if side == "long":
            pnl = (px - ep) * qty
        elif side == "short":
            pnl = (ep - px) * qty
        else:
            return trade

        if hasattr(trade, "current_price"):
            trade.current_price = px
        if hasattr(trade, "unrealized_pl"):
            trade.unrealized_pl = float(pnl)
        if hasattr(trade, "unrealized_pnl"):
            trade.unrealized_pnl = float(pnl)
        if bar_time is not None and hasattr(trade, "updated_at"):
            trade.updated_at = bar_time

        db.add(trade)

        if commit:
            db.commit()
            db.refresh(trade)
        else:
            db.flush()

        return trade

    except Exception as e:
        if commit:
            db.rollback()
        log.error(
            "[MARK] Failed for trade %s: %s",
            getattr(trade, "id", "?"),
            e,
            exc_info=True,
        )
        return trade


# =============================================================================
# Close
# =============================================================================
def close_position_replay(
    db: Session,
    trade: ReplayOpenTrade,
    price: float,
    bar_time: datetime,
    reason: Optional[str] = None,
) -> Optional[ReplayTradeHistory]:
    """
    Close a replay position, write a row to replay_trade_history,
    and delete the open trade.
    """
    try:
        if trade is None:
            raise ValueError("close_position_replay: trade is required")

        side = (trade.position_side or "").lower().strip()
        qty = float(trade.quantity or 0.0)
        ep = float(trade.entry_price)
        xp = float(price)

        if side not in {"long", "short"}:
            raise ValueError(f"Unknown position_side: {trade.position_side}")

        pnl = (xp - ep) * qty if side == "long" else (ep - xp) * qty

        values = {
            "session_id": trade.session_id,
            "user_id": _get_attr(trade, "user_id", None),
            "symbol": trade.symbol,
            "trade_type": "SELL" if side == "long" else "BUY_TO_COVER",
            "position_side": side,
            "quantity": qty,
            "entry_price": ep,
            "entry_time": trade.entry_time,
            "exit_price": xp,
            "exit_time": bar_time,
            "profit_loss": float(pnl),
            "pnl": float(pnl),
            "algo_name": _get_attr(trade, "algo_name", "AlgoMM"),
            "exit_reason": (reason or "")[:64] if reason else None,
        }

        hist = ReplayTradeHistory(**_filter_kwargs(ReplayTradeHistory, values))
        db.add(hist)
        db.delete(trade)
        db.commit()
        db.refresh(hist)

        log.info(
            "[CLOSE] Session %s %s %s qty=%s entry=%.2f exit=%.2f pnl=$%.2f reason=%s",
            trade.session_id,
            side.upper(),
            trade.symbol,
            qty,
            ep,
            xp,
            pnl,
            reason or "-",
        )
        return hist

    except Exception as e:
        db.rollback()
        log.error(
            "[CLOSE] Failed for trade %s: %s",
            getattr(trade, "id", "?"),
            e,
            exc_info=True,
        )
        return None


# =============================================================================
# Queries
# =============================================================================
def get_open_trade(db: Session, session_id: int) -> Optional[ReplayOpenTrade]:
    return (
        db.query(ReplayOpenTrade)
          .filter_by(session_id=session_id)
          .first()
    )


def get_session_pnl(db: Session, session_id: int) -> float:
    """
    Sum realized P&L across closed trades.
    Supports either profit_loss or pnl depending on your model/table.
    """
    try:
        if hasattr(ReplayTradeHistory, "profit_loss"):
            rows = (
                db.query(ReplayTradeHistory.profit_loss)
                  .filter_by(session_id=session_id)
                  .all()
            )
            return float(sum((r[0] or 0.0) for r in rows))

        if hasattr(ReplayTradeHistory, "pnl"):
            rows = (
                db.query(ReplayTradeHistory.pnl)
                  .filter_by(session_id=session_id)
                  .all()
            )
            return float(sum((r[0] or 0.0) for r in rows))

        return 0.0

    except Exception as e:
        log.error("[PNL] Failed for session %s: %s", session_id, e, exc_info=True)
        return 0.0


def get_last_trade_time(db: Session, session_id: int) -> Optional[datetime]:
    """
    Return the simulated exit_time of the most recent closed trade.
    Used by cooldown logic if any old replay path still asks for it.
    """
    row = (
        db.query(ReplayTradeHistory)
          .filter_by(session_id=session_id)
          .order_by(ReplayTradeHistory.id.desc())
          .first()
    )
    return row.exit_time if row else None
