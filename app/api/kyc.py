"""KYC profiles, documents and reviews for staff members and renters.

Identity document files are stored under the private storage prefix and are only
reachable through a short-lived signed link minted for an authorized caller. Each
link use re-checks that the caller still has access, and every access is audited.
"""

import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessScope, Perm, build_scope, get_scope, require_manager_scope
from app.core.audit import record_audit_event
from app.core.config import get_settings
from app.core.deps import CurrentUser
from app.db.session import get_db
from app.models.kyc import KycDocument, KycProfile, KycReview
from app.models.renter import Tenancy
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.kyc import (
    DocumentKind,
    DownloadLinkOut,
    KycDocumentOut,
    KycProfileOut,
    KycProfileUpdate,
    KycQueueItem,
    KycReviewOut,
    KycReviewRequest,
    RevealedIdNumber,
)
from app.services import crypto, storage
from app.services import kyc as svc
from app.services.renters import get_renter

router = APIRouter(tags=["kyc"])


async def _out(db: AsyncSession, scope: AccessScope, profile: KycProfile) -> KycProfileOut:
    access = await svc.access_for(db, scope, profile)
    docs = (
        await db.execute(
            select(KycDocument).where(KycDocument.profile_id == profile.id, KycDocument.deleted_at.is_(None))
            .order_by(KycDocument.uploaded_at)
        )
    ).scalars().all()
    review_rows = (
        await db.execute(
            select(KycReview, User.full_name).outerjoin(User, User.id == KycReview.reviewer_user_id)
            .where(KycReview.profile_id == profile.id).order_by(KycReview.created_at.desc())
        )
    ).all()
    return KycProfileOut(
        id=profile.id,
        subject_type="staff" if profile.user_id else "renter",
        user_id=profile.user_id,
        renter_id=profile.renter_id,
        subject_name=await svc.subject_name(db, profile),
        legal_name=profile.legal_name,
        date_of_birth=profile.date_of_birth,
        residential_address=profile.residential_address,
        id_document_type=profile.id_document_type,
        id_number_masked=crypto.mask_identifier(profile.id_number_last4),
        id_expiry_date=profile.id_expiry_date,
        emergency_contact_name=profile.emergency_contact_name,
        emergency_contact_phone=profile.emergency_contact_phone,
        emergency_contact_relationship=profile.emergency_contact_relationship,
        staff_reference=profile.staff_reference,
        status=svc.effective_status(profile),
        verification_method=profile.verification_method,
        submitted_at=profile.submitted_at,
        submitted_by_user_id=profile.submitted_by_user_id,
        reviewed_at=profile.reviewed_at,
        reviewed_by_user_id=profile.reviewed_by_user_id,
        review_note=profile.review_note,
        documents=[KycDocumentOut.model_validate(d) for d in docs],
        reviews=[
            KycReviewOut(id=r.id, decision=r.decision, note=r.note, reviewer_user_id=r.reviewer_user_id,
                         reviewer_name=name, created_at=r.created_at)
            for r, name in review_rows
        ],
        can_edit=access.edit,
        can_submit=access.edit and profile.status in svc.SUBMITTABLE_STATUSES,
        can_review=access.review and profile.status == "pending_review",
        can_view_documents=access.documents,
    )


async def _profile(db: AsyncSession, scope: AccessScope, profile_id: uuid.UUID, *, lock: bool = False) -> KycProfile:
    stmt = select(KycProfile).where(KycProfile.id == profile_id, KycProfile.tenant_id == scope.tenant_id)
    if lock and db.get_bind().dialect.name != "sqlite":
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


# ------------------------------------------------------------------ profiles


@router.get("/kyc/me", response_model=KycProfileOut)
async def my_profile(current_user: AccessScope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """The signed-in member's own KYC profile (created on first access)."""
    if current_user.impersonator_id is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not available while acting as a business")
    profile = await svc.profile_for_user(db, current_user.tenant_id, current_user.user)
    await db.commit()
    return await _out(db, current_user, profile)


@router.get("/team/members/{user_id}/kyc", response_model=KycProfileOut)
async def member_profile(user_id: uuid.UUID, current_user: AccessScope = Depends(require_manager_scope), db: AsyncSession = Depends(get_db)):
    user = (
        await db.execute(select(User).where(User.id == user_id, User.tenant_id == current_user.tenant_id, User.is_superadmin.is_(False)))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team member not found")
    profile = await svc.profile_for_user(db, current_user.tenant_id, user)
    await db.commit()
    return await _out(db, current_user, profile)


@router.get("/renters/{renter_id}/kyc", response_model=KycProfileOut)
async def renter_profile(renter_id: uuid.UUID, current_user: AccessScope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    renter = await get_renter(db, current_user, renter_id)
    profile = await svc.profile_for_renter(db, renter)
    await svc.require_access(db, current_user, profile, "view")
    await db.commit()
    return await _out(db, current_user, profile)


@router.get("/kyc/profiles/{profile_id}", response_model=KycProfileOut)
async def get_profile(profile_id: uuid.UUID, current_user: AccessScope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    profile = await _profile(db, current_user, profile_id)
    await svc.require_access(db, current_user, profile, "view")
    return await _out(db, current_user, profile)


@router.put("/kyc/profiles/{profile_id}", response_model=KycProfileOut)
async def update_profile(
    profile_id: uuid.UUID, payload: KycProfileUpdate, current_user: AccessScope = Depends(get_scope), db: AsyncSession = Depends(get_db)
):
    profile = await _profile(db, current_user, profile_id, lock=True)
    await svc.require_access(db, current_user, profile, "edit")
    await svc.update_profile(db, current_user, profile, payload.model_dump(exclude_unset=True))
    await db.commit()
    await db.refresh(profile)
    return await _out(db, current_user, profile)


@router.post("/kyc/profiles/{profile_id}/submit", response_model=KycProfileOut)
async def submit_profile(profile_id: uuid.UUID, current_user: AccessScope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    profile = await _profile(db, current_user, profile_id, lock=True)
    await svc.require_access(db, current_user, profile, "edit")
    await svc.submit(db, current_user, profile)
    await db.commit()
    await db.refresh(profile)
    return await _out(db, current_user, profile)


@router.post("/kyc/profiles/{profile_id}/review", response_model=KycProfileOut)
async def review_profile(
    profile_id: uuid.UUID, payload: KycReviewRequest, current_user: AccessScope = Depends(get_scope), db: AsyncSession = Depends(get_db)
):
    profile = await _profile(db, current_user, profile_id, lock=True)
    await svc.require_access(db, current_user, profile, "review")
    await svc.review(db, current_user, profile, payload.decision, payload.note)
    await db.commit()
    await db.refresh(profile)
    return await _out(db, current_user, profile)


@router.post("/kyc/profiles/{profile_id}/reveal-id-number", response_model=RevealedIdNumber)
async def reveal_id_number(profile_id: uuid.UUID, current_user: AccessScope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """The full identity number, for a reviewer comparing it with the document. Audited."""
    profile = await _profile(db, current_user, profile_id)
    access = await svc.require_access(db, current_user, profile, "view")
    if not (access.review or profile.user_id == current_user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to do this")
    if not profile.id_number_encrypted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No identity number on file")
    value = crypto.decrypt_text(profile.id_number_encrypted)
    await record_audit_event(
        db, tenant_id=profile.tenant_id, actor_user_id=current_user.id, action="kyc.id_number_revealed",
        entity_type="kyc_profile", entity_id=profile.id,
    )
    await db.commit()
    return RevealedIdNumber(id_number=value)


@router.get("/kyc/review-queue", response_model=list[KycQueueItem])
async def review_queue(current_user: AccessScope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Profiles awaiting a decision that the caller is allowed to review."""
    if not (current_user.is_manager or current_user.can(Perm.renters_kyc_review)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to review KYC")
    stmt = select(KycProfile).where(KycProfile.tenant_id == current_user.tenant_id, KycProfile.status == "pending_review")
    if not current_user.is_manager:
        stmt = stmt.where(KycProfile.renter_id.in_(
            select(Tenancy.renter_id).where(Tenancy.property_id.in_(current_user.property_ids or [uuid.UUID(int=0)]))
        ))
    items = []
    for profile in (await db.execute(stmt.order_by(KycProfile.submitted_at))).scalars():
        if (await svc.access_for(db, current_user, profile)).review:
            items.append(KycQueueItem(
                profile_id=profile.id, subject_type="staff" if profile.user_id else "renter",
                subject_name=await svc.subject_name(db, profile), user_id=profile.user_id, renter_id=profile.renter_id,
                status=profile.status, submitted_at=profile.submitted_at,
            ))
    return items


# ------------------------------------------------------------------ documents


@router.post("/kyc/profiles/{profile_id}/documents", response_model=KycDocumentOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    profile_id: uuid.UUID,
    kind: DocumentKind = Form(...),
    file: UploadFile = File(...),
    current_user: AccessScope = Depends(get_scope),
    db: AsyncSession = Depends(get_db),
):
    profile = await _profile(db, current_user, profile_id)
    await svc.require_access(db, current_user, profile, "edit")
    if profile.status == "pending_review" and not current_user.is_manager:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This profile is awaiting review and can't be changed now")

    max_bytes = get_settings().kyc_max_document_bytes
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=f"File too large (max {max_bytes // (1024 * 1024)}MB)")
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The file is empty")
    detected = svc.sniff(data)
    if detected is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Upload a PDF, PNG, JPEG or WebP file")
    content_type, ext = detected

    key = svc.document_key(profile, ext)
    await asyncio.to_thread(storage.put, key, data, content_type)
    document = KycDocument(
        tenant_id=profile.tenant_id, profile_id=profile.id, kind=kind, storage_key=key, content_type=content_type,
        size_bytes=len(data), sha256=svc.checksum(data), original_filename=svc.safe_filename(file.filename),
        uploaded_by_user_id=current_user.id,
    )
    db.add(document)
    await db.flush()
    await record_audit_event(
        db, tenant_id=profile.tenant_id, actor_user_id=current_user.id, action="kyc.document_uploaded",
        entity_type="kyc_profile", entity_id=profile.id, metadata={"document_id": str(document.id), "kind": kind},
    )
    await db.commit()
    await db.refresh(document)
    return KycDocumentOut.model_validate(document)


async def _document(db: AsyncSession, scope: AccessScope, document_id: uuid.UUID) -> tuple[KycDocument, KycProfile]:
    document = (
        await db.execute(
            select(KycDocument).where(
                KycDocument.id == document_id, KycDocument.tenant_id == scope.tenant_id, KycDocument.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    profile = await db.get(KycProfile, document.profile_id)
    return document, profile


@router.post("/kyc/documents/{document_id}/download-url", response_model=DownloadLinkOut)
async def document_download_url(document_id: uuid.UUID, current_user: AccessScope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    document, profile = await _document(db, current_user, document_id)
    await svc.require_access(db, current_user, profile, "documents")
    token, expires = svc.download_token(document, current_user.id)
    return DownloadLinkOut(url=f"{get_settings().app_base_url.rstrip('/')}/kyc/files/{token}", expires_at=expires)


@router.get("/kyc/files/{token}", include_in_schema=False)
async def download_document(token: str, db: AsyncSession = Depends(get_db)):
    """Streams a KYC document for a valid, unexpired link. Access is re-checked on every use."""
    parsed = svc.read_download_token(token)
    if parsed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This link is invalid or has expired")
    document_id, user_id = parsed
    user = await db.get(User, user_id)
    if user is None or user.suspended_at is not None or user.deactivated_at is not None or user.tenant_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This link is invalid or has expired")
    tenant = await db.get(Tenant, user.tenant_id)
    if tenant is None or tenant.deleted_at is not None or tenant.suspended_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This link is invalid or has expired")
    scope = await build_scope(db, CurrentUser(user))
    document, profile = await _document(db, scope, document_id)
    access = await svc.access_for(db, scope, profile)
    if not access.documents:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This link is invalid or has expired")

    data = await asyncio.to_thread(storage.get, document.storage_key)
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    await record_audit_event(
        db, tenant_id=document.tenant_id, actor_user_id=user.id, action="kyc.document_accessed",
        entity_type="kyc_profile", entity_id=profile.id, metadata={"document_id": str(document.id)},
    )
    await db.commit()
    filename = f"{document.kind}-{str(document.id)[:8]}.{document.storage_key.rsplit('.', 1)[-1]}"
    return Response(
        content=data,
        media_type=document.content_type,
        headers={
            "Cache-Control": "no-store, private",
            "Content-Disposition": f'inline; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/kyc/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(document_id: uuid.UUID, current_user: AccessScope = Depends(require_manager_scope), db: AsyncSession = Depends(get_db)):
    """Removes the file from storage. The metadata row is kept as a record that it existed."""
    document, profile = await _document(db, current_user, document_id)
    await svc.require_access(db, current_user, profile, "view")
    await asyncio.to_thread(storage.delete, document.storage_key)
    document.deleted_at = datetime.now(timezone.utc)
    document.deleted_by_user_id = current_user.id
    await record_audit_event(
        db, tenant_id=document.tenant_id, actor_user_id=current_user.id, action="kyc.document_deleted",
        entity_type="kyc_profile", entity_id=profile.id, metadata={"document_id": str(document.id), "kind": document.kind},
    )
    await db.commit()
