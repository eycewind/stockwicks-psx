from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from app.database.base import Base


class BrokerConnection(Base):
    __tablename__ = "broker_connections"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)

    broker_name = Column(String(50), default="Schwab", nullable=False)
    connected = Column(Boolean, default=False, nullable=False)

    access_token_encrypted = Column(Text, nullable=True)
    refresh_token_encrypted = Column(Text, nullable=True)
    access_expires_at = Column(DateTime, nullable=True)
    refresh_expires_at = Column(DateTime, nullable=True)

    account_hash_exists = Column(Boolean, default=False, nullable=False)
    reauth_required = Column(Boolean, default=True, nullable=False)

    last_refresh_status = Column(String(100), nullable=True)
    last_refresh_error = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class SchwabAccount(Base):
    __tablename__ = "schwab_accounts"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)

    account_hash = Column(String(255), nullable=True)
    account_number_masked = Column(String(50), nullable=True)
    account_type = Column(String(100), nullable=True)
    is_default = Column(Boolean, default=True, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
