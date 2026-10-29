"""KYC workflow, access rules and document handling.

Who may do what:
- Staff KYC: the member edits and submits their own profile; owners/admins may also edit
  it. Only an owner or admin reviews it (an admin's profile only by an owner, an owner's
  only by another owner), and never the subject themselves.
- Renter KYC: owners/admins, or staff with renters.manage on one of the renter's
  properties, may edit, upload and submit. Owners/admins review; staff may review only
  with renters.kyc_review on one of the renter's properties, and never a submission
  they made themselves.
- Documents: downloadable by managers, by the subject (their own staff profile) and by
  staff reviewers in scope. Uploading does not imply being able to download.

Verification is a manual review record; it is kept separate from account activation
and tenancy status.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException, status
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessScope, Perm
from app.core.audit import record_audit_event
from app.core.config import get_settings
from app.models.customer import Customer
from app.models.enums import UserRole
from app.models.kyc import KycDocument, KycProfile, KycReview
from app.models.renter import Renter, Tenancy
from app.models.user import User
from app.services import crypto

IDENTITY_FIELDS = ("legal_name", "date_of_birth", "id_document_type", "id_number", "id_expiry_date")
ID_DOCUMENT_KINDS = ("id_front", "id_back", "passport_page")
EDITABLE_STATUSES = ("not_submitted", "rejected", "requires_correction", "verified", "pending_review")
SUBMITTABLE_STATUSES = ("not_submitted", "rejected", "requires_correction")
DECISION_STATUS = {"approved": "verified", "rejected": "rejected", "requires_correction": "requires_correction"}

# Magic-byte signatures; the client-supplied content type is not trusted.
_SIGNATURES: list[tuple[bytes, str, str]] = [
    (b"%PDF-", "application/pdf", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
]

DOWNLOAD_TOKEN_TYPE = "kyc_document"


def _role(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def effective_status(profile: KycProfile | None, today: date | None = None) -> str:
    if profile is None:
        return "not_submitted"
    today = today or date.today()
    if profile.status == "verified" and profile.id_expiry_date is not None and profile.id_expiry_date < today:
        return "expired"
    return profile.status


async def statuses_for_users(db: AsyncSession, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not user_ids:
        return {}
    profiles = (await db.execute(select(KycProfile).where(KycProfile.user_id.in_(user_ids)))).scalars().all()
    found = {p.user_id: effective_status(p) for p in profiles}
    return {uid: found.get(uid, "not_submitted") for uid in user_ids}


async def statuses_for_renters(db: AsyncSession, renter_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not renter_ids:
        return {}
    profiles = (await db.execute(select(KycProfile).where(KycProfile.renter_id.in_(renter_ids)))).scalars().all()
    found = {p.renter_id: effective_status(p) for p in profiles}
    return {rid: found.get(rid, "not_submitted") for rid in renter_ids}


# ------------------------------------------------------------------ loading


async def profile_for_user(db: AsyncSession, tenant_id: uuid.UUID, user: User) -> KycProfile:
    profile = (await db.execute(select(KycProfile).where(KycProfile.user_id == user.id))).scalar_one_or_none()
    if profile is None:
        profile = KycProfile(tenant_id=tenant_id, user_id=user.id, legal_name=user.full_name)
        db.add(profile)
        await db.flush()
    return profile


async def profile_for_renter(db: AsyncSession, renter: Renter) -> KycProfile:
    profile = (await db.execute(select(KycProfile).where(KycProfile.renter_id == renter.id))).scalar_one_or_none()
    if profile is None:
        profile = KycProfile(tenant_id=renter.tenant_id, renter_id=renter.id)
        db.add(profile)
        await db.flush()
    return profile


async def _renter_property_ids(db: AsyncSession, renter_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await db.execute(select(Tenancy.property_id).where(Tenancy.renter_id == renter_id))
    return set(rows.scalars().all())


class ProfileAccess:
    """What the caller may do with one KYC profile."""

    def __init__(self, *, view: bool, edit: bool, review: bool, documents: bool):
        self.view, self.edit, self.review, self.documents = view, edit, review, documents


async def access_for(db: AsyncSession, scope: AccessScope, profile: KycProfile) -> ProfileAccess:
    if profile.tenant_id != scope.tenant_id:
        return ProfileAccess(view=False, edit=False, review=False, documents=False)

    if profile.user_id is not None:
        is_self = profile.user_id == scope.id
        subject = await db.get(User, profile.user_id)
        subject_role = _role(subject.role) if subject else UserRole.staff.value
        caller_role = _role(scope.role)
        if is_self:
            can_review = False
        elif subject_role == UserRole.owner.value:
            can_review = caller_role == UserRole.owner.value
        elif subject_role == UserRole.admin.value:
            can_review = caller_role == UserRole.owner.value
        else:
            can_review = scope.is_manager
        view = is_self or scope.is_manager
        return ProfileAccess(view=view, edit=view, review=can_review, documents=is_self or can_review)

    # Renter profile
    if scope.is_manager:
        in_scope = True
    else:
        in_scope = bool(await _renter_property_ids(db, profile.renter_id) & scope.property_ids)
    manage = in_scope and scope.can(Perm.renters_manage)
    review_perm = scope.is_manager or (in_scope and scope.can(Perm.renters_kyc_review))
    own_submission = profile.submitted_by_user_id == scope.id and not scope.is_manager
    return ProfileAccess(
        view=manage or review_perm,
        edit=manage,
        review=review_perm and not own_submission,
        documents=review_perm,
    )


async def require_access(db: AsyncSession, scope: AccessScope, profile: KycProfile | None, need: str) -> ProfileAccess:
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="KYC profile not found")
    access = await access_for(db, scope, profile)
    if not access.view:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="KYC profile not found")
    if not getattr(access, need):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to do this")
    return access


async def subject_name(db: AsyncSession, profile: KycProfile) -> str | None:
    if profile.user_id is not None:
        user = await db.get(User, profile.user_id)
        return user.full_name if user else None
    renter = await db.get(Renter, profile.renter_id)
    if renter is None:
        return None
    customer = await db.get(Customer, renter.customer_id)
    return customer.name if customer else None


# ------------------------------------------------------------------ workflow


async def update_profile(db: AsyncSession, scope: AccessScope, profile: KycProfile, data: dict) -> None:
    if profile.status == "pending_review" and not scope.is_manager and any(k in data for k in IDENTITY_FIELDS):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This profile is awaiting review and can't be changed now")
    identity_changed = False
    if "id_number" in data:
        raw = (data.pop("id_number") or "").strip()
        if raw:
            profile.id_number_encrypted = crypto.encrypt_text(raw)
            profile.id_number_last4 = crypto.last4(raw)
        else:
            profile.id_number_encrypted = None
            profile.id_number_last4 = None
        identity_changed = True
    for field, value in data.items():
        if field in IDENTITY_FIELDS and getattr(profile, field) != value:
            identity_changed = True
        setattr(profile, field, value)
    if identity_changed and profile.status in ("verified", "pending_review"):
        # Changed identity details need a fresh review.
        profile.status = "not_submitted"
    await record_audit_event(
        db, tenant_id=profile.tenant_id, actor_user_id=scope.id, action="kyc.updated", entity_type="kyc_profile",
        entity_id=profile.id, metadata={"fields": sorted(set(data) | ({"id_number"} if identity_changed else set()))},
    )


async def submit(db: AsyncSession, scope: AccessScope, profile: KycProfile) -> None:
    if profile.status not in SUBMITTABLE_STATUSES:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"This profile is {profile.status} and can't be submitted")
    missing = [f for f in ("legal_name", "id_document_type", "id_number_encrypted") if not getattr(profile, f)]
    if missing:
        names = ", ".join("id_number" if f == "id_number_encrypted" else f for f in missing)
        raise HTTPException(status_code=422, detail=f"Complete these before submitting: {names}")
    has_id_doc = (
        await db.execute(
            select(KycDocument.id).where(
                KycDocument.profile_id == profile.id, KycDocument.deleted_at.is_(None), KycDocument.kind.in_(ID_DOCUMENT_KINDS)
            )
        )
    ).first()
    if has_id_doc is None:
        raise HTTPException(status_code=422, detail="Upload a copy of the identity document before submitting")
    profile.status = "pending_review"
    profile.submitted_at = datetime.now(timezone.utc)
    profile.submitted_by_user_id = scope.id
    profile.review_note = None
    await record_audit_event(
        db, tenant_id=profile.tenant_id, actor_user_id=scope.id, action="kyc.submitted", entity_type="kyc_profile", entity_id=profile.id,
    )


async def review(db: AsyncSession, scope: AccessScope, profile: KycProfile, decision: str, note: str | None) -> None:
    if profile.status != "pending_review":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only profiles awaiting review can be reviewed")
    if decision != "approved" and not (note and note.strip()):
        raise HTTPException(status_code=422, detail="Explain what needs to change (note)")
    profile.status = DECISION_STATUS[decision]
    profile.reviewed_at = datetime.now(timezone.utc)
    profile.reviewed_by_user_id = scope.id
    profile.review_note = note
    db.add(KycReview(tenant_id=profile.tenant_id, profile_id=profile.id, decision=decision, note=note, reviewer_user_id=scope.id))
    action = {"approved": "kyc.approved", "rejected": "kyc.rejected", "requires_correction": "kyc.correction_requested"}[decision]
    await record_audit_event(
        db, tenant_id=profile.tenant_id, actor_user_id=scope.id, action=action, entity_type="kyc_profile", entity_id=profile.id,
    )


# ------------------------------------------------------------------ documents


def sniff(data: bytes) -> tuple[str, str] | None:
    for signature, content_type, ext in _SIGNATURES:
        if data.startswith(signature):
            return content_type, ext
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def safe_filename(name: str | None) -> str | None:
    if not name:
        return None
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
    return name[:120] or None


def document_key(profile: KycProfile, ext: str) -> str:
    return f"private/kyc/{profile.tenant_id}/{profile.id}/{uuid.uuid4().hex}.{ext}"


def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download_token(document: KycDocument, user_id: uuid.UUID) -> tuple[str, datetime]:
    settings = get_settings()
    expires = datetime.now(timezone.utc) + timedelta(seconds=settings.kyc_download_link_seconds)
    token = jwt.encode(
        {"typ": DOWNLOAD_TOKEN_TYPE, "doc": str(document.id), "sub": str(user_id), "exp": expires},
        settings.jwt_secret, algorithm=settings.jwt_algorithm,
    )
    return token, expires


def read_download_token(token: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None
    if payload.get("typ") != DOWNLOAD_TOKEN_TYPE:
        return None
    try:
        return uuid.UUID(payload["doc"]), uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        return None
