from sqlalchemy import Column, String, Numeric, DateTime, ForeignKey, Date, Text, Integer, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base

class AmazonOrder(Base):
    __tablename__ = "amazon_orders"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4())
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    amazon_order_id = Column(String(100), unique=True, nullable=False, index=True)
    order_date = Column(Date, nullable=False, index=True)
    total_amount = Column(Numeric(15, 4), nullable=False, index=True)
    currency = Column(String(3), default="EUR", nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.current_timestamp())

    # Relaciones
    user = relationship("User", back_populates="amazon_orders")
    items = relationship("AmazonItem", back_populates="order", cascade="all, delete-orphan")
    accounting_entries = relationship("AccountingEntry", back_populates="amazon_order")


class AmazonItem(Base):
    __tablename__ = "amazon_items"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4())
    amazon_order_id = Column(UUID(as_uuid=True), ForeignKey("amazon_orders.id", ondelete="CASCADE"), nullable=False)
    product_title = Column(Text, nullable=False)
    amazon_category = Column(String(150))
    seller_name = Column(String(255))
    quantity = Column(Integer, default=1, nullable=False)
    unit_price = Column(Numeric(15, 4), nullable=False)
    total_price = Column(Numeric(15, 4), nullable=False)

    # Relaciones
    order = relationship("AmazonOrder", back_populates="items")
    accounting_entries = relationship("AccountingEntry", back_populates="amazon_item")
