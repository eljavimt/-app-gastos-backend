from sqlalchemy import Column, String, Numeric, DateTime, ForeignKey, Date, Text, func, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from app.database import Base

class AccountingEntry(Base):
    __tablename__ = "accounting_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4())
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    category_id = Column(UUID(as_uuid=True), ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False)
    
    # Orígenes opcionales para reconciliación
    bank_transaction_id = Column(UUID(as_uuid=True), ForeignKey("bank_transactions.id", ondelete="SET NULL"), nullable=True)
    invoice_id = Column(UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True)
    amazon_order_id = Column(UUID(as_uuid=True), ForeignKey("amazon_orders.id", ondelete="SET NULL"), nullable=True)
    amazon_item_id = Column(UUID(as_uuid=True), ForeignKey("amazon_items.id", ondelete="SET NULL"), nullable=True)

    entry_date = Column(Date, nullable=False, index=True)
    amount = Column(Numeric(15, 4), nullable=False) # Negativo para gastos, positivo para ingresos
    description = Column(Text, nullable=False)
    reconciliation_type = Column(String(50), nullable=False) # 'DIRECT_BANK', 'MATCHED_INVOICE', 'SPLIT_AMAZON', 'MANUAL'
    confidence_score = Column(Numeric(3, 2), default=1.00)
    metadata_json = Column("metadata", JSONB) # Mapeado al campo postgres 'metadata'
    created_at = Column(DateTime(timezone=True), server_default=func.current_timestamp())
    updated_at = Column(DateTime(timezone=True), server_default=func.current_timestamp(), onupdate=func.current_timestamp())

    # Relaciones
    user = relationship("User", back_populates="accounting_entries")
    category = relationship("Category", back_populates="accounting_entries")
    bank_transaction = relationship("BankTransaction", back_populates="accounting_entries")
    invoice = relationship("Invoice", back_populates="accounting_entries")
    amazon_order = relationship("AmazonOrder", back_populates="accounting_entries")
    amazon_item = relationship("AmazonItem", back_populates="accounting_entries")


class Budget(Base):
    __tablename__ = "budgets"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4())
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    category_id = Column(UUID(as_uuid=True), ForeignKey("categories.id", ondelete="CASCADE"), nullable=False)
    limit_amount = Column(Numeric(15, 4), nullable=False)
    period = Column(String(20), default="MONTHLY", nullable=False) # 'MONTHLY', 'ANNUAL'
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.current_timestamp())
    updated_at = Column(DateTime(timezone=True), server_default=func.current_timestamp(), onupdate=func.current_timestamp())

    # Relaciones
    user = relationship("User", back_populates="budgets")
    category = relationship("Category", back_populates="budgets")

    __table_args__ = (
        UniqueConstraint('user_id', 'category_id', 'period', 'start_date', name='unique_user_category_period'),
    )
