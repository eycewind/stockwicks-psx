from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text

from app.database.base import Base


class PaperStockTradeBot(Base):
    __tablename__ = "paper_stock_trade_bots"


    trade_size = Column(Float, default=1.0, nullable=False)
    notify_email = Column(Boolean, default=False, nullable=False)
    allow_short_selling = Column(Boolean, default=False, nullable=False)
    eod_auto_close = Column(Boolean, default=False, nullable=False)
    mirror_live = Column(Boolean, default=False, nullable=False)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)

    symbol = Column(String(50), index=True, nullable=False)
    algo_name = Column(String(100), default="AlgoMM", nullable=False)
    interval = Column(String(30), default="5min", nullable=False)
    config_json = Column(Text, nullable=True)

    is_active = Column(Boolean, default=False, nullable=False)
    status = Column(String(50), default="STOPPED", nullable=False)

    quantity = Column(Integer, default=1, nullable=False)
    allow_short_selling = Column(Boolean, default=False, nullable=False)
    mirror_live = Column(Boolean, default=False, nullable=False)

    stop_loss_usd = Column(Float, nullable=True)
    take_profit_usd = Column(Float, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class PaperStockBotOpenTrade(Base):
    __tablename__ = "paper_stock_bot_open_trades"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    bot_id = Column(Integer, ForeignKey("paper_stock_trade_bots.id"), index=True, nullable=False)

    symbol = Column(String(50), index=True, nullable=False)
    position_side = Column(String(20), nullable=False)
    quantity = Column(Integer, default=1, nullable=False)

    entry_price = Column(Float, nullable=False)
    current_price = Column(Float, nullable=True)
    unrealized_pl = Column(Float, default=0.0, nullable=True)

    entry_time = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class PaperStockBotTradeHistory(Base):
    __tablename__ = "paper_stock_bot_trade_history"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    bot_id = Column(Integer, ForeignKey("paper_stock_trade_bots.id"), index=True, nullable=False)

    symbol = Column(String(50), index=True, nullable=False)
    position_side = Column(String(20), nullable=False)
    quantity = Column(Integer, default=1, nullable=False)

    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=False)
    profit_loss = Column(Float, default=0.0, nullable=True)
    realized_pl = Column(Float, default=0.0, nullable=True)

    entry_time = Column(DateTime, nullable=True)
    exit_time = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
