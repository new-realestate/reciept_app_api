import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.session import Base
from app.models.types import JSONBType, UUIDType


class NotificationEvent(Base):
    """A persisted, idempotent notification (one channel, one recipient, one occasion).

    `dedupe_key` is unique, so planning the same reminder twice is a no-op.
    """

    __tablename__ = "notification_events"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # invoice_issued | quotation_sent | reminder_upcoming | reminder_due | reminder_overdue |
    # reminder_manual | payment_recorded | schedule_upcoming
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)  # invoice | quotation | occurrence
    subject_id: Mapped[uuid.UUID] = mapped_column(UUIDType(), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(8), nullable=False)  # email | sms
    recipient_party: Mapped[str] = mapped_column(String(16), nullable=False, default="customer")  # customer | business
    recipient: Mapped[str | None] = mapped_column(Text)

    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    # scheduled | sending | sent | failed | skipped | cancelled
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="scheduled", index=True)
    skip_reason: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    payload: Mapped[dict | None] = mapped_column(JSONBType())

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class NotificationAttempt(Base):
    __tablename__ = "notification_attempts"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("notification_events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # sent | failed | skipped
    provider: Mapped[str | None] = mapped_column(String(16))
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    response: Mapped[dict | None] = mapped_column(JSONBType())
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
