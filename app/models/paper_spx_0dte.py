# app/models/paper_spx_0dte.py
"""
Paper SPX 0DTE Bot Tables

Tables:
  - paper_spx_picks: last recommendations/picks per bot run
  - paper_spx_open_trade: currently open (paper) SPX option position(s)
  - paper_spx_trade_history: closed trades (paper)

Notes:
  - Price/PnL are stored in "option price points" (e.g. 7.30). USD PnL = points * 100 * qty.
  - qty is enforced as 1 by default for 0DTE bot.
  - This file is standalone and does NOT depend on the generic option bot models.
"""

from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Date, DateTime, Text, ForeignKey, Index
)
from sqlalchemy.orm import relationship

from app.database.connection import Base


class PaperSPXPick(Base):
    __tablename__ = "paper_spx_picks"

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    bot_name = Column(String(64), default="SPX_0DTE_BOT", index=True)

    # request context
    run_date = Column(Date, index=True, nullable=False)  # ET date used for the pick
    mode = Column(String(16), default="both")           # credit | debit | both
    strategy = Column(String(32), default="auto")
    max_risk = Column(Float, default=100.0)

    # recommended contract details (single-leg)
    underlying_symbol = Column(String(16), default="SPX", index=True)
    occ_symbol = Column(String(64), index=True)         # option OCC symbol
    put_call = Column(String(8))                        # CALL | PUT
    position_side = Column(String(8))                   # BUY | SELL
    strike = Column(Float)
    expiration = Column(Date)

    entry_price = Column(Float)                         # points
    target1 = Column(Float)
    target2 = Column(Float)
    stop = Column(Float)

    confidence = Column(Float)
    confidence_level = Column(String(16))
    score = Column(Float)

    # extra blob for UI/debugging
    details_json = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    user = relationship("User", lazy="joined")

    __table_args__ = (
        Index("ix_spx_picks_user_date", "user_id", "run_date"),
    )


class PaperSPXOpenTrade(Base):
    __tablename__ = "paper_spx_open_trade"

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)

    # links
    pick_id = Column(Integer, ForeignKey("paper_spx_picks.id"), index=True, nullable=True)

    # contract
    underlying_symbol = Column(String(16), default="SPX", index=True)
    occ_symbol = Column(String(64), index=True, nullable=False)
    put_call = Column(String(8), nullable=False)           # CALL | PUT
    position_side = Column(String(8), nullable=False)      # BUY | SELL

    strike = Column(Float, nullable=False)
    expiration = Column(Date, nullable=False)

    quantity = Column(Integer, default=1)                  # enforced by bot

    # pricing (points)
    entry_price = Column(Float, nullable=False)
    current_mark_price = Column(Float, nullable=True)

    planned_take_profit_1 = Column(Float, nullable=True)
    planned_take_profit_2 = Column(Float, nullable=True)
    planned_stop_loss = Column(Float, nullable=True)

    # lifecycle
    status = Column(String(16), default="OPEN", index=True)  # OPEN | CLOSED
    opened_at = Column(DateTime, default=datetime.utcnow, index=True)

    # optional bookkeeping
    notes = Column(Text, nullable=True)

    user = relationship("User", lazy="joined")
    pick = relationship("PaperSPXPick", lazy="joined")

    __table_args__ = (
        Index("ix_spx_open_user_status", "user_id", "status"),
        Index("ix_spx_open_occ_exp", "occ_symbol", "expiration"),
    )


class PaperSPXTradeHistory(Base):
    __tablename__ = "paper_spx_trade_history"

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)

    # contract
    underlying_symbol = Column(String(16), default="SPX", index=True)
    occ_symbol = Column(String(64), index=True, nullable=False)
    put_call = Column(String(8), nullable=False)           # CALL | PUT
    position_side = Column(String(8), nullable=False)      # BUY | SELL
    strike = Column(Float, nullable=False)
    expiration = Column(Date, nullable=False)
    quantity = Column(Integer, default=1)

    # prices (points)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=False)

    # times
    opened_at = Column(DateTime, nullable=False)
    closed_at = Column(DateTime, default=datetime.utcnow, index=True)

    close_reason = Column(String(32), default="MANUAL")     # TP1 | TP2 | SL | TIME | EOD | MANUAL
    pnl_points = Column(Float, nullable=False)
    pnl_usd = Column(Float, nullable=False)

    pick_id = Column(Integer, nullable=True)
    details_json = Column(Text, nullable=True)

    user = relationship("User", lazy="joined")

    __table_args__ = (
        Index("ix_spx_hist_user_closed", "user_id", "closed_at"),
    )
