from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text

from app.database.connection import Base


class ReplaySession(Base):
    __tablename__ = "replay_sessions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)

    symbol = Column(String(50), index=True, nullable=False)
    algo_name = Column(String(100), default="AlgoMM", nullable=False)
    interval = Column(String(30), default="5min", nullable=False)

    # Replay range/config
    start_date = Column(String(20), nullable=True)
    end_date = Column(String(20), nullable=True)
    speed = Column(Float, default=1.0, nullable=True)
    trade_size = Column(Float, default=100.0, nullable=True)
    config_json = Column(Text, nullable=True)

    # Process/cursor state
    status = Column(String(50), default="CREATED", nullable=False)
    pid = Column(Integer, nullable=True)
    current_bar_idx = Column(Integer, default=0, nullable=True)
    current_bar_time = Column(DateTime, nullable=True)
    total_bars = Column(Integer, default=0, nullable=True)
    error_message = Column(Text, nullable=True)

    started_at = Column(DateTime, nullable=True)
    stopped_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, nullable=True)


class ReplayOpenTrade(Base):
    __tablename__ = "replay_open_trades"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("replay_sessions.id"), index=True, nullable=False)

    symbol = Column(String(50), index=True, nullable=False)
    position_side = Column(String(20), nullable=False)
    quantity = Column(Integer, default=1, nullable=False)

    entry_price = Column(Float, nullable=False)
    current_price = Column(Float, nullable=True)
    unrealized_pl = Column(Float, default=0.0, nullable=True)

    entry_time = Column(DateTime, default=datetime.utcnow, nullable=False)
    entry_reason = Column(Text, nullable=True)
    last_update_time = Column(DateTime, nullable=True)


class ReplayTradeHistory(Base):
    __tablename__ = "replay_trade_history"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("replay_sessions.id"), index=True, nullable=False)

    symbol = Column(String(50), index=True, nullable=False)
    position_side = Column(String(20), nullable=False)
    quantity = Column(Integer, default=1, nullable=False)

    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=False)
    profit_loss = Column(Float, default=0.0, nullable=True)

    entry_time = Column(DateTime, nullable=True)
    exit_time = Column(DateTime, default=datetime.utcnow, nullable=False)

    exit_reason = Column(Text, nullable=True)
    duration_bars = Column(Integer, nullable=True)
