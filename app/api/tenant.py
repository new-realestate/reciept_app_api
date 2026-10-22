import asyncio
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_audit_event
from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user
from app.db.session import get_db
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.tenant import LogoUploadResponse, SignatureUploadResponse, TenantOut, TenantUpdate
from app.services import storage

router = APIRouter(tags=["tenant"])

ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


async def _get_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return tenant


async def _save_image(upload: UploadFile, subdir: str, stem: str) -> str:
    if upload.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported image type")

    contents = await upload.read()
    if len(contents) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Image too large (max 5MB)")

    settings = get_settings()
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[upload.content_type]
    # A fresh filename per upload so the URL changes and browsers don't keep
    # showing the previously cached image after a replace.
    key = f"{subdir}/{stem}-{uuid.uuid4().hex[:8]}.{ext}"
    await asyncio.to_thread(storage.put, key, contents, upload.content_type)
    # Remove earlier uploads for this owner (stem is the owner's UUID).
    await asyncio.to_thread(storage.delete_prefix, f"{subdir}/{stem}", keep=key)

    return f"{settings.app_base_url}/static/{key}"


@router.get("/tenant/me", response_model=TenantOut)
async def get_my_tenant(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    tenant = await _get_tenant(db, current_user.tenant_id)
    return tenant


@router.patch("/tenant/me", response_model=TenantOut)
async def update_my_tenant(
    payload: TenantUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant = await _get_tenant(db, current_user.tenant_id)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(tenant, field, value)

    await record_audit_event(
        db,
        tenant_id=tenant.id,
        actor_user_id=current_user.id,
        action="tenant.letterhead_updated",
        entity_type="tenant",
        entity_id=tenant.id,
    )
    await db.commit()
    await db.refresh(tenant)
    return tenant


@router.post("/tenant/logo", response_model=LogoUploadResponse)
async def upload_logo(
    file: UploadFile = File(...),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant = await _get_tenant(db, current_user.tenant_id)
    logo_url = await _save_image(file, "logos", str(tenant.id))
    tenant.logo_url = logo_url
    await db.commit()
    return LogoUploadResponse(logo_url=logo_url)


@router.post("/users/me/signature", response_model=SignatureUploadResponse)
async def upload_signature(
    file: UploadFile = File(...),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one()

    signature_url = await _save_image(file, "signatures", str(user.id))
    user.signature_image_url = signature_url
    await db.commit()
    return SignatureUploadResponse(signature_image_url=signature_url)
