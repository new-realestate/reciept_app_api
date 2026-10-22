import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_audit_event
from app.core.config import get_settings
from app.core.rate_limit import auth_rate_limiter
from app.core.security import create_access_token, generate_token, hash_password, verify_password
from app.db.session import get_db
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.auth import (
    LoginRequest,
    PasswordResetConfirm,
    PasswordResetRequest,
    SignupRequest,
    SignupResponse,
    TokenResponse,
    VerifyRequest,
)
from app.services.auth import start_password_reset
from app.services.email import send_verification_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(auth_rate_limiter)])


@router.post("/signup", response_model=SignupResponse, status_code=status.HTTP_201_CREATED)
async def signup(payload: SignupRequest, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(User).where(User.email == payload.email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    tenant = Tenant(business_name=payload.business_name)
    db.add(tenant)
    await db.flush()

    verification_token = generate_token()
    user = User(
        tenant_id=tenant.id,
        full_name=payload.full_name,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role="owner",
        email_verification_token=verification_token,
    )
    db.add(user)
    await db.flush()

    await record_audit_event(
        db, tenant_id=tenant.id, actor_user_id=user.id, action="user.signup", entity_type="user", entity_id=user.id
    )
    await db.commit()

    settings = get_settings()
    verify_url = f"{settings.app_base_url}/auth/verify?token={verification_token}"
    try:
        send_verification_email(to_email=user.email, verify_url=verify_url)
    except Exception:
        # The account is already committed; don't fail signup because email is down.
        logger.exception("Failed to send verification email to %s", user.email)

    return SignupResponse(tenant_id=tenant.id, user_id=user.id, email=user.email)


@router.post("/verify", status_code=status.HTTP_200_OK)
async def verify_email(payload: VerifyRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email_verification_token == payload.token))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid verification token")

    user.is_verified = True
    user.email_verification_token = None
    await db.commit()
    return {"detail": "Email verified"}


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    if user.suspended_at is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account has been suspended")
    if not user.is_superadmin:
        tenant = await db.get(Tenant, user.tenant_id)
        if tenant is None or tenant.deleted_at is not None or tenant.suspended_at is not None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This business has been suspended")

    user.last_login_at = datetime.now(timezone.utc)
    await record_audit_event(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="platform.login" if user.is_superadmin else "user.login",
        entity_type="user",
        entity_id=user.id,
    )
    await db.commit()

    token = create_access_token(
        subject=str(user.id),
        tenant_id=str(user.tenant_id) if user.tenant_id else None,
        role=user.role.value,
        superadmin=user.is_superadmin,
    )
    return TokenResponse(access_token=token)


@router.post("/password-reset/request", status_code=status.HTTP_200_OK)
async def request_password_reset(payload: PasswordResetRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()

    # Always return 200 regardless of whether the email exists, to avoid
    # leaking which addresses are registered.
    if user is not None:
        await start_password_reset(db, user)

    return {"detail": "If that email is registered, a reset link has been sent"}


@router.post("/password-reset/confirm", status_code=status.HTTP_200_OK)
async def confirm_password_reset(payload: PasswordResetConfirm, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.password_reset_token == payload.token))
    user = result.scalar_one_or_none()

    if (
        user is None
        or user.password_reset_expires_at is None
        or user.password_reset_expires_at < datetime.now(timezone.utc)
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired reset token")

    user.password_hash = hash_password(payload.new_password)
    user.password_reset_token = None
    user.password_reset_expires_at = None
    await db.commit()
    return {"detail": "Password updated"}
