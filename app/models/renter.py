"""Renters (shown as "Tenants" in the portal) and their tenancies.

"Tenant" in this codebase means the business account, so the person or company
renting a property is a Renter. A renter is always linked to exactly one Customer
of the same business, which holds the contact details and receives documents.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.session import Base
from app.models.types import UUIDType

RENTER_STATUSES = ("active", "former", "archived")
TENANCY_STATUSES = ("draft", "active", "ended", "terminated")
RENT_FREQUENCIES = ("once", "weekly", "monthly", "quarterly", "annually")


class Renter(Base):
    __tablename__ = "renters"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_renters_tenant_id"),
        # One renter profile per customer: a returning renter gets a new tenancy, not a second identity.
        UniqueConstraint("customer_id", name="uq_renters_customer"),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"],
            name="fk_renters_customer", ondelete="RESTRICT",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(UUIDType(), nullable=False)
    # active | former | archived
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    occupation: Mapped[str | None] = mapped_column(Text)
    employer: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Tenancy(Base):
    """One occupancy of a property (and optionally a unit) by a renter.

    Ending a tenancy keeps the row, so a renter's history and the documents that
    reference it stay intact.
    """

    __tablename__ = "tenancies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_tenancies_tenant_id"),
        UniqueConstraint("tenant_id", "reference", name="uq_tenancies_reference"),
        ForeignKeyConstraint(
            ["tenant_id", "renter_id"], ["renters.tenant_id", "renters.id"], name="fk_tenancies_renter", ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "property_id"], ["properties.tenant_id", "properties.id"],
            name="fk_tenancies_property", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["property_id", "unit_id"], ["property_units.property_id", "property_units.id"],
            name="fk_tenancies_unit", ondelete="RESTRICT",
        ),
        # A unit has at most one active tenancy at a time.
        Index(
            "uq_tenancies_active_unit", "unit_id", unique=True,
            postgresql_where=text("status = 'active' AND unit_id IS NOT NULL"),
            sqlite_where=text("status = 'active' AND unit_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    renter_id: Mapped[uuid.UUID] = mapped_column(UUIDType(), nullable=False, index=True)
    property_id: Mapped[uuid.UUID] = mapped_column(UUIDType(), nullable=False, index=True)
    unit_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), index=True)
    reference: Mapped[str] = mapped_column(Text, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date)
    rent_amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    rent_frequency: Mapped[str] = mapped_column(String(16), nullable=False, default="monthly")
    deposit_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # draft | active | ended | terminated
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), ForeignKey("users.id", ondelete="SET NULL"))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
