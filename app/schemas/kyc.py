import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

IdDocumentType = Literal["national_id", "passport", "driving_permit", "refugee_id", "other"]
DocumentKind = Literal["id_front", "id_back", "passport_page", "photo", "proof_of_address", "other"]
Decision = Literal["approved", "rejected", "requires_correction"]


class KycProfileUpdate(BaseModel):
    legal_name: str | None = Field(default=None, max_length=300)
    date_of_birth: date | None = None
    residential_address: str | None = Field(default=None, max_length=1000)
    id_document_type: IdDocumentType | None = None
    # Write-only: stored encrypted; responses only ever carry the masked form.
    id_number: str | None = Field(default=None, min_length=3, max_length=64)
    id_expiry_date: date | None = None
    emergency_contact_name: str | None = Field(default=None, max_length=200)
    emergency_contact_phone: str | None = Field(default=None, max_length=40)
    emergency_contact_relationship: str | None = Field(default=None, max_length=100)
    staff_reference: str | None = Field(default=None, max_length=100)


class KycReviewRequest(BaseModel):
    decision: Decision
    note: str | None = Field(default=None, max_length=1000)


class KycDocumentOut(BaseModel):
    id: uuid.UUID
    kind: str
    content_type: str
    size_bytes: int
    original_filename: str | None
    uploaded_at: datetime
    uploaded_by_user_id: uuid.UUID | None

    model_config = {"from_attributes": True}


class KycReviewOut(BaseModel):
    id: uuid.UUID
    decision: str
    note: str | None
    reviewer_user_id: uuid.UUID | None
    reviewer_name: str | None = None
    created_at: datetime


class KycProfileOut(BaseModel):
    id: uuid.UUID
    subject_type: Literal["staff", "renter"]
    user_id: uuid.UUID | None
    renter_id: uuid.UUID | None
    subject_name: str | None = None
    legal_name: str | None
    date_of_birth: date | None
    residential_address: str | None
    id_document_type: str | None
    id_number_masked: str | None
    id_expiry_date: date | None
    emergency_contact_name: str | None
    emergency_contact_phone: str | None
    emergency_contact_relationship: str | None
    staff_reference: str | None
    # not_submitted | pending_review | verified | rejected | requires_correction | expired
    status: str
    verification_method: str
    submitted_at: datetime | None
    submitted_by_user_id: uuid.UUID | None
    reviewed_at: datetime | None
    reviewed_by_user_id: uuid.UUID | None
    review_note: str | None
    documents: list[KycDocumentOut] = []
    reviews: list[KycReviewOut] = []
    can_edit: bool = False
    can_submit: bool = False
    can_review: bool = False
    can_view_documents: bool = False


class KycQueueItem(BaseModel):
    profile_id: uuid.UUID
    subject_type: Literal["staff", "renter"]
    subject_name: str | None
    user_id: uuid.UUID | None
    renter_id: uuid.UUID | None
    status: str
    submitted_at: datetime | None


class DownloadLinkOut(BaseModel):
    url: str
    expires_at: datetime


class RevealedIdNumber(BaseModel):
    id_number: str
