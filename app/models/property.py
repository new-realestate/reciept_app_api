import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, ForeignKeyConstraint, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.session import Base
from app.models.types import UUIDType

PROPERTY_TYPES = ("residential", "commercial", "mixed", "land", "other")
PROPERTY_STATUSES = ("active", "inactive", "archived")
UNIT_TYPES = ("apartment", "room", "office", "shop", "house", "other")
UNIT_STATUSES = ("active", "inactive", "archived")


class Property(Base):
    """A property owned by a business (never by an individual staff member)."""

    __tablename__ = "properties"
    # (tenant_id, id) is unique so child rows can carry a composite FK that pins them to the same business.
    __table_args__ = (UniqueConstraint("tenant_id", "id", name="uq_properties_tenant_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    property_type: Mapped[str] = mapped_column(String(16), nullable=False, default="residential")
    address: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    # active | inactive | archived. Properties are archived, never deleted, so history stays intact.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PropertyUnit(Base):
    """A rentable space within a property (apartment, room, office, ...)."""

    __tablename__ = "property_units"
    __table_args__ = (
        UniqueConstraint("property_id", "label", name="uq_units_property_label"),
        UniqueConstraint("tenant_id", "id", name="uq_units_tenant_id"),
        # Lets a tenancy's (property_id, unit_id) pair be checked by the database.
        UniqueConstraint("property_id", "id", name="uq_units_property_id"),
        ForeignKeyConstraint(
            ["tenant_id", "property_id"], ["properties.tenant_id", "properties.id"],
            name="fk_units_property", ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    property_id: Mapped[uuid.UUID] = mapped_column(UUIDType(), nullable=False, index=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    unit_type: Mapped[str] = mapped_column(String(16), nullable=False, default="apartment")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    default_rent_cents: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PropertyStaffAssignment(Base):
    """Which staff member may manage which property.

    Rows are never deleted: revoking sets revoked_at, so the history of who managed
    what (and who granted it) is kept. Only rows with revoked_at IS NULL grant access.
    """

    __tablename__ = "property_staff_assignments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "property_id"], ["properties.tenant_id", "properties.id"],
            name="fk_assignments_property", ondelete="CASCADE",
        ),
        Index(
            "uq_assignments_active", "property_id", "user_id", unique=True,
            postgresql_where=text("revoked_at IS NULL"), sqlite_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    property_id: Mapped[uuid.UUID] = mapped_column(UUIDType(), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assigned_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), ForeignKey("users.id", ondelete="SET NULL"))
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), ForeignKey("users.id", ondelete="SET NULL"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
