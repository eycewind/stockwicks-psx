from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint

from app.database.base import Base


class TradeNotificationLog(Base):
    __tablename__ = "trade_notification_log"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)
    trade_id = Column(String(100), nullable=False)
    trade_type = Column(String(50), nullable=False)
    notification_type = Column(String(50), nullable=False)
    sent_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "trade_id",
            "trade_type",
            "notification_type",
            name="uq_trade_notification_log_dedupe",
        ),
    )
