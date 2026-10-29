import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.session import Base
from app.models.types import CIText, UUIDType


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        CheckConstraint("email IS NOT NULL OR phone IS NOT NULL", name="chk_customer_has_contact"),
        # Lets renters (and other business-scoped rows) reference a customer with a composite FK.
        UniqueConstraint("tenant_id", "id", name="uq_customers_tenant_id"),
        Index("idx_customers_tenant_phone_norm", "tenant_id", "phone_normalized"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(CIText())
    phone: Mapped[str | None] = mapped_column(Text)
    # E.164 form of `phone` used for duplicate matching; NULL when the number can't be parsed.
    phone_normalized: Mapped[str | None] = mapped_column(Text)
    customer_type: Mapped[str] = mapped_column(String(16), nullable=False, default="individual", server_default="individual")
    legal_name: Mapped[str | None] = mapped_column(Text)
    billing_address: Mapped[str | None] = mapped_column(Text)
    tax_identification_number: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    contact_preference: Mapped[str | None] = mapped_column(String(16))
    # active | archived. Customers are archived, never deleted, so their documents keep their recipient.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
