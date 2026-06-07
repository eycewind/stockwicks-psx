from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String

from app.database.base import Base


class PaperAccount(Base):
    __tablename__ = "paper_accounts"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    current_balance = Column(Float, default=100000.0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PaperTrade(Base):
    __tablename__ = "paper_trades"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    symbol = Column(String(50), index=True, nullable=False)
    quantity = Column(Integer, default=0, nullable=False)
    entry_price = Column(Float, nullable=True)
    exit_price = Column(Float, nullable=True)
    position_side = Column(String(20), default="long", nullable=True)
    status = Column(String(30), default="OPEN", nullable=False)
    profit_loss = Column(Float, default=0.0, nullable=True)
    opened_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    closed_at = Column(DateTime, nullable=True)


class PaperOrder(Base):
    __tablename__ = "paper_orders"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    symbol = Column(String(50), index=True, nullable=False)
    quantity = Column(Integer, default=0, nullable=False)
    order_type = Column(String(30), default="limit", nullable=True)
    limit_price = Column(Float, nullable=True)
    position_side = Column(String(20), default="long", nullable=True)
    status = Column(String(30), default="WORKING", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    cancelled_at = Column(DateTime, nullable=True)
