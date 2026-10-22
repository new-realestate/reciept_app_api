import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.session import Base
from app.models.types import JSONBType, UUIDType


class DocumentCounter(Base):
    """Per-tenant sequence for human-readable document numbers (INV, QUO)."""

    __tablename__ = "document_counters"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("tenant_id", "number", name="uq_invoices_tenant_number"),
        CheckConstraint("total_cents >= 0", name="chk_invoice_total_nonneg"),
        CheckConstraint("amount_paid_cents >= 0 AND amount_paid_cents <= total_cents", name="chk_invoice_paid_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # NULL while a draft; assigned on issue so drafts never burn numbers.
    number: Mapped[str | None] = mapped_column(Text)
    # draft | issued | cancelled. paid / partially_paid / overdue are derived (see services/invoices.py).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")

    title: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)

    subtotal_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    discount_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    discount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tax_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    amount_paid_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    notes: Mapped[str | None] = mapped_column(Text)
    payment_instructions: Mapped[str | None] = mapped_column(Text)

    public_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    token_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    quotation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType(), ForeignKey("quotations.id", ondelete="SET NULL"), unique=True
    )
    schedule_occurrence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType(), ForeignKey("schedule_occurrences.id", ondelete="SET NULL"), unique=True
    )

    reminders_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Optional per-invoice override of the tenant reminder settings.
    reminder_overrides: Mapped[dict | None] = mapped_column(JSONBType())

    # Frozen copy of business/customer/lines taken at issue time so the document
    # a customer received never changes afterwards.
    snapshot: Mapped[dict | None] = mapped_column(JSONBType())

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), ForeignKey("users.id", ondelete="SET NULL"))
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    line_items: Mapped[list["InvoiceLineItem"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="InvoiceLineItem.position",
        lazy="selectin",
    )


class InvoiceLineItem(Base):
    __tablename__ = "invoice_line_items"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    line_total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)

    invoice: Mapped[Invoice] = relationship(back_populates="line_items")


class InvoicePayment(Base):
    __tablename__ = "invoice_payments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_invoice_payments_idem"),
        CheckConstraint("amount_cents > 0", name="chk_invoice_payment_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    receipt_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("receipts.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    paid_at: Mapped[date] = mapped_column(Date, nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    reference: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    recorded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
