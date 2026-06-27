# app/models/spx_0dte_alert_subscription.py
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import relationship

from app.database.connection import Base


class SPX0DTEAlertSubscription(Base):
    __tablename__ = "spx_0dte_alert_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    email = Column(String(255), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    user = relationship("User", lazy="joined")

    __table_args__ = (
        Index("ix_spx0dte_alert_subscriptions_active", "is_active"),
    )
