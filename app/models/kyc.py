"""Know-your-customer records for staff members and renters.

Verification is manual: a reviewer looks at the submitted details and documents
and records a decision. Nothing here claims a document is authentic just because
it was uploaded.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.session import Base
from app.models.types import UUIDType

KYC_STATUSES = ("not_submitted", "pending_review", "verified", "rejected", "requires_correction")
ID_DOCUMENT_TYPES = ("national_id", "passport", "driving_permit", "refugee_id", "other")
KYC_DOCUMENT_KINDS = ("id_front", "id_back", "passport_page", "photo", "proof_of_address", "other")
KYC_DECISIONS = ("approved", "rejected", "requires_correction")


class KycProfile(Base):
    __tablename__ = "kyc_profiles"
    __table_args__ = (
        CheckConstraint("(user_id IS NULL) <> (renter_id IS NULL)", name="chk_kyc_one_subject"),
        UniqueConstraint("user_id", name="uq_kyc_user"),
        UniqueConstraint("renter_id", name="uq_kyc_renter"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Exactly one subject: a staff member (user) or a renter.
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), ForeignKey("users.id", ondelete="CASCADE"))
    renter_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), ForeignKey("renters.id", ondelete="CASCADE"))

    legal_name: Mapped[str | None] = mapped_column(Text)
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    residential_address: Mapped[str | None] = mapped_column(Text)
    id_document_type: Mapped[str | None] = mapped_column(String(24))
    # Encrypted at rest (services/crypto.py); only the last 4 characters are stored in clear for masking.
    id_number_encrypted: Mapped[str | None] = mapped_column(Text)
    id_number_last4: Mapped[str | None] = mapped_column(String(8))
    id_expiry_date: Mapped[date | None] = mapped_column(Date)
    emergency_contact_name: Mapped[str | None] = mapped_column(Text)
    emergency_contact_phone: Mapped[str | None] = mapped_column(Text)
    emergency_contact_relationship: Mapped[str | None] = mapped_column(Text)
    staff_reference: Mapped[str | None] = mapped_column(Text)

    # not_submitted | pending_review | verified | rejected | requires_correction ("expired" is derived on read).
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="not_submitted")
    verification_method: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), ForeignKey("users.id", ondelete="SET NULL"))
    review_note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class KycDocument(Base):
    """An uploaded identity document. The file lives under the private storage prefix
    and is only reachable through a short-lived, authorized download link."""

    __tablename__ = "kyc_documents"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("kyc_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(Text)
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), ForeignKey("users.id", ondelete="SET NULL"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # The file is removed from storage on delete; this row is kept as a record that it existed.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), ForeignKey("users.id", ondelete="SET NULL"))


class KycReview(Base):
    """Append-only history of review decisions on a KYC profile."""

    __tablename__ = "kyc_reviews"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("kyc_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    reviewer_user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
