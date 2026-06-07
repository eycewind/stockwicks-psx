from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from app.database.connection import Base

class StockAlert(Base):
    # __tablename__ = 'stock_alerts'
    __table_args__ = {'extend_existing': True}  # ✅ Fix: Allow existing table reuse

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    symbol = Column(String, nullable=False)
    target_price = Column(Float, nullable=False)
    created_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="alerts")
