from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from app.database.base import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(150), unique=True, index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)

    is_active = Column(Boolean, default=True, nullable=False)
    is_verified = Column(Boolean, default=False, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)

    first_name = Column(String(150), nullable=True)
    last_name = Column(String(150), nullable=True)

    # Kept nullable for compatibility with old cleaned auth code.
    stripe_customer_id = Column(String(255), nullable=True)
    subscription_id = Column(String(255), nullable=True)
    plan = Column(String(100), default="mvp", nullable=True)

    schwab_allowed = Column(String(5), default="N", nullable=True)
    verification_token = Column(String(255), nullable=True, index=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
