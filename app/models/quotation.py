import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.session import Base
from app.models.context import PropertyContextMixin, property_context_fks
from app.models.types import JSONBType, UUIDType


class Quotation(PropertyContextMixin, Base):
    __tablename__ = "quotations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "number", name="uq_quotations_tenant_number"),
        CheckConstraint("total_cents >= 0", name="chk_quotation_total_nonneg"),
        *property_context_fks("quotations"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    number: Mapped[str | None] = mapped_column(Text)
    # draft | sent | viewed | accepted | rejected | expired | cancelled
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")

    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    valid_until: Mapped[date] = mapped_column(Date, nullable=False)

    subtotal_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    discount_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    discount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tax_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    terms: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    # Never rendered on public pages or PDFs.
    internal_notes: Mapped[str | None] = mapped_column(Text)

    public_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    token_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_name: Mapped[str | None] = mapped_column(Text)
    response_comment: Mapped[str | None] = mapped_column(Text)

    # Frozen at send time; on acceptance it is the record of what was accepted.
    snapshot: Mapped[dict | None] = mapped_column(JSONBType())

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), ForeignKey("users.id", ondelete="SET NULL"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    line_items: Mapped[list["QuotationLineItem"]] = relationship(
        back_populates="quotation",
        cascade="all, delete-orphan",
        order_by="QuotationLineItem.position",
        lazy="selectin",
    )


class QuotationLineItem(Base):
    __tablename__ = "quotation_line_items"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    quotation_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("quotations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    line_total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)

    quotation: Mapped[Quotation] = relationship(back_populates="line_items")
