from sqlalchemy import Column, String, Numeric, DateTime, ForeignKey, Date, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base

class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4())
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    emitter_name = Column(String(255), nullable=False)
    emitter_tax_id = Column(String(50))
    issue_date = Column(Date, nullable=False, index=True)
    base_amount = Column(Numeric(15, 4), nullable=False)
    tax_rate = Column(Numeric(5, 2), nullable=False)
    tax_amount = Column(Numeric(15, 4), nullable=False)
    total_amount = Column(Numeric(15, 4), nullable=False, index=True)
    currency = Column(String(3), default="EUR", nullable=False)
    source_email_id = Column(String(255))
    file_storage_path = Column(String(512))
    parsed_confidence = Column(Numeric(3, 2))
    created_at = Column(DateTime(timezone=True), server_default=func.current_timestamp())
    updated_at = Column(DateTime(timezone=True), server_default=func.current_timestamp(), onupdate=func.current_timestamp())

    # Relaciones
    user = relationship("User", back_populates="invoices")
    items = relationship("InvoiceItem", back_populates="invoice", cascade="all, delete-orphan")
    accounting_entries = relationship("AccountingEntry", back_populates="invoice")


class InvoiceItem(Base):
    __tablename__ = "invoice_items"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4())
    invoice_id = Column(UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False)
    description = Column(Text, nullable=False)
    quantity = Column(Numeric(10, 2), default=1.00)
    unit_price = Column(Numeric(15, 4), nullable=False)
    total_amount = Column(Numeric(15, 4), nullable=False)

    # Relaciones
    invoice = relationship("Invoice", back_populates="items")
