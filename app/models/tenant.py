import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.session import Base
from app.models.enums import PlanTier
from app.models.types import PortableEnum, UUIDType

plan_tier_enum = PortableEnum(PlanTier, name="plan_tier")


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    business_name: Mapped[str] = mapped_column(Text, nullable=False)
    logo_url: Mapped[str | None] = mapped_column(Text)
    brand_color: Mapped[str | None] = mapped_column(Text)
    po_box: Mapped[str | None] = mapped_column(Text)
    physical_address: Mapped[str | None] = mapped_column(Text)
    contact_phone: Mapped[str | None] = mapped_column(Text)
    contact_email: Mapped[str | None] = mapped_column(Text)
    bank_name: Mapped[str | None] = mapped_column(Text)
    bank_account_number: Mapped[str | None] = mapped_column(Text)
    plan_tier: Mapped[PlanTier] = mapped_column(plan_tier_enum, nullable=False, default=PlanTier.free_trial)
    default_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="UGX")
    # Which receipt PDF/hosted-view layout to use; see app/services/pdf.py RECEIPT_TEMPLATES.
    receipt_template: Mapped[str] = mapped_column(Text, nullable=False, default="classic", server_default="classic")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
