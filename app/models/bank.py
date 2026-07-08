from sqlalchemy import Column, String, Numeric, DateTime, Boolean, ForeignKey, Date, Text, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from app.database import Base

class BankAccount(Base):
    __tablename__ = "bank_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4())
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider_name = Column(String(100), nullable=False)
    account_number_masked = Column(String(50), nullable=False)
    account_type = Column(String(50), nullable=False)
    balance = Column(Numeric(15, 4), default=0.0000, nullable=False)
    currency = Column(String(3), default="EUR", nullable=False)
    last_synced_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.current_timestamp())
    updated_at = Column(DateTime(timezone=True), server_default=func.current_timestamp(), onupdate=func.current_timestamp())

    # Relaciones
    user = relationship("User", back_populates="bank_accounts")
    transactions = relationship("BankTransaction", back_populates="bank_account", cascade="all, delete-orphan")


class BankTransaction(Base):
    __tablename__ = "bank_transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4())
    bank_account_id = Column(UUID(as_uuid=True), ForeignKey("bank_accounts.id", ondelete="CASCADE"), nullable=False)
    transaction_date = Column(Date, nullable=False, index=True)
    value_date = Column(Date, nullable=False)
    raw_description = Column(Text, nullable=False)
    cleaned_merchant = Column(String(255))
    amount = Column(Numeric(15, 4), nullable=False, index=True) # Negativo para cargos, positivo para abonos
    currency = Column(String(3), nullable=False)
    balance_snapshot = Column(Numeric(15, 4))
    is_reconciled = Column(Boolean, default=False, nullable=False)
    import_source = Column(String(50), nullable=False) # 'API_PSD2', 'CSV_MANUAL', 'PDF_OCR'
    raw_payload = Column(JSONB)
    created_at = Column(DateTime(timezone=True), server_default=func.current_timestamp())

    # Relaciones
    bank_account = relationship("BankAccount", back_populates="transactions")
    accounting_entries = relationship("AccountingEntry", back_populates="bank_transaction")
