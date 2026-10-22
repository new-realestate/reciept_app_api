import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.session import Base
from app.models.types import JSONBType, UUIDType


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    # NULL for platform-level events (e.g. a super admin logging in) that belong to no business.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType())
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONBType())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
