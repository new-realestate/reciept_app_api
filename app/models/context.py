"""Optional property context on documents (receipts, invoices, quotations, schedules).

All three columns are NULL for general business transactions. When set, composite
foreign keys pin them to rows of the same business.
"""

import uuid

from sqlalchemy import ForeignKeyConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.types import UUIDType


class PropertyContextMixin:
    property_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), nullable=True, index=True)
    unit_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), nullable=True)
    tenancy_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), nullable=True, index=True)


def property_context_fks(table: str) -> tuple[ForeignKeyConstraint, ...]:
    return (
        ForeignKeyConstraint(
            ["tenant_id", "property_id"], ["properties.tenant_id", "properties.id"], name=f"fk_{table}_property"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "unit_id"], ["property_units.tenant_id", "property_units.id"], name=f"fk_{table}_unit"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "tenancy_id"], ["tenancies.tenant_id", "tenancies.id"], name=f"fk_{table}_tenancy"
        ),
    )
