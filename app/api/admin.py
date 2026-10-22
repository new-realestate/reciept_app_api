"""Platform super admin API: oversee every business and user on the platform.

Every route requires a super admin's own token (not an act-as token). Actions on a
business are written to that business's audit log, attributed to the super admin and
flagged with metadata {"platform_admin": true}, so the business can see them.
"""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_audit_event
from app.core.config import get_settings
from app.core.permissions import require_platform_admin
from app.core.security import create_impersonation_token
from app.db.session import get_db
from app.models.audit import AuditLog
from app.models.customer import Customer
from app.models.enums import UserRole
from app.models.invoice import Invoice
from app.models.receipt import Receipt
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.admin import (
    AdminTenantDetail,
    AdminTenantOut,
    AdminUserOut,
    AdminUserUpdate,
    CurrencyTotal,
    ImpersonationOut,
    PlatformAuditLogOut,
    PlatformStats,
    SuspendRequest,
)
from app.schemas.documents import Page
from app.services.auth import start_password_reset

router = APIRouter(prefix="/admin", tags=["platform-admin"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _audit(db: AsyncSession, admin: User, *, tenant_id: uuid.UUID | None, action: str, entity_type: str,
                 entity_id: uuid.UUID | None, **metadata) -> None:
    await record_audit_event(
        db, tenant_id=tenant_id, actor_user_id=admin.id, action=action, entity_type=entity_type,
        entity_id=entity_id, metadata={"platform_admin": True, **metadata},
    )


async def _get_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None or tenant.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Business not found")
    return tenant


async def _get_managed_user(db: AsyncSession, user_id: uuid.UUID) -> User:
    """A business user the super admin may manage (super admins are off-limits through the API)."""
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.is_superadmin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Platform admins can't be changed here")
    return user


async def _other_owner_count(db: AsyncSession, user: User) -> int:
    return (
        await db.execute(
            select(func.count()).select_from(User).where(
                User.tenant_id == user.tenant_id, User.role == UserRole.owner, User.id != user.id
            )
        )
    ).scalar_one()


# --- Tenant (business) rows -------------------------------------------------------

def _tenant_columns():
    owner_email = (
        select(User.email).where(User.tenant_id == Tenant.id, User.role == UserRole.owner)
        .order_by(User.created_at).limit(1).correlate(Tenant).scalar_subquery()
    )
    user_count = select(func.count(User.id)).where(User.tenant_id == Tenant.id).correlate(Tenant).scalar_subquery()
    receipt_count = (
        select(func.count(Receipt.id)).where(Receipt.tenant_id == Tenant.id).correlate(Tenant).scalar_subquery()
    )
    last_login = select(func.max(User.last_login_at)).where(User.tenant_id == Tenant.id).correlate(Tenant).scalar_subquery()
    return owner_email, user_count, receipt_count, last_login


def _tenant_out(tenant: Tenant, owner_email, user_count, receipt_count, last_login) -> dict:
    return dict(
        id=tenant.id, business_name=tenant.business_name, plan_tier=tenant.plan_tier,
        default_currency=tenant.default_currency, owner_email=str(owner_email) if owner_email else None,
        user_count=user_count or 0, receipt_count=receipt_count or 0, last_login_at=last_login,
        created_at=tenant.created_at, suspended_at=tenant.suspended_at, suspended_reason=tenant.suspended_reason,
    )


def _user_out(user: User, business_name: str | None) -> AdminUserOut:
    return AdminUserOut(
        id=user.id, full_name=user.full_name, email=str(user.email), role=user.role, tenant_id=user.tenant_id,
        business_name=business_name, is_superadmin=user.is_superadmin, is_verified=user.is_verified,
        last_login_at=user.last_login_at, created_at=user.created_at, suspended_at=user.suspended_at,
        suspended_reason=user.suspended_reason,
    )


# --- Overview ---------------------------------------------------------------------

@router.get("/stats", response_model=PlatformStats)
async def platform_stats(_: User = Depends(require_platform_admin), db: AsyncSession = Depends(get_db)):
    async def count(stmt) -> int:
        return (await db.execute(stmt)).scalar_one()

    live = Tenant.deleted_at.is_(None)
    totals = (
        await db.execute(
            select(Receipt.currency, func.sum(Receipt.amount_cents), func.count(Receipt.id))
            .group_by(Receipt.currency).order_by(Receipt.currency)
        )
    ).all()
    return PlatformStats(
        businesses_total=await count(select(func.count(Tenant.id)).where(live)),
        businesses_active=await count(select(func.count(Tenant.id)).where(live, Tenant.suspended_at.is_(None))),
        businesses_suspended=await count(select(func.count(Tenant.id)).where(live, Tenant.suspended_at.is_not(None))),
        users_total=await count(select(func.count(User.id)).where(User.is_superadmin.is_(False))),
        users_suspended=await count(
            select(func.count(User.id)).where(User.is_superadmin.is_(False), User.suspended_at.is_not(None))
        ),
        signups_last_30_days=await count(select(func.count(Tenant.id)).where(Tenant.created_at >= _now() - timedelta(days=30))),
        receipts_total=await count(select(func.count(Receipt.id))),
        invoices_total=await count(select(func.count(Invoice.id))),
        receipt_totals=[CurrencyTotal(currency=c, amount_cents=int(a or 0), count=n) for c, a, n in totals],
    )


# --- Businesses -------------------------------------------------------------------

@router.get("/tenants", response_model=Page[AdminTenantOut])
async def list_tenants(
    q: str | None = Query(default=None, description="Business name or user email"),
    status_filter: str | None = Query(default=None, alias="status", pattern="^(active|suspended)$"),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    conds = [Tenant.deleted_at.is_(None)]
    if q:
        like = f"%{q.strip()}%"
        member = select(User.id).where(User.tenant_id == Tenant.id, User.email.ilike(like)).correlate(Tenant).exists()
        conds.append(or_(Tenant.business_name.ilike(like), member))
    if status_filter == "active":
        conds.append(Tenant.suspended_at.is_(None))
    elif status_filter == "suspended":
        conds.append(Tenant.suspended_at.is_not(None))

    total = (await db.execute(select(func.count(Tenant.id)).where(*conds))).scalar_one()
    rows = (
        await db.execute(
            select(Tenant, *_tenant_columns()).where(*conds)
            .order_by(Tenant.created_at.desc(), Tenant.id).limit(limit).offset(offset)
        )
    ).all()
    items = [AdminTenantOut(**_tenant_out(*row)) for row in rows]
    return Page[AdminTenantOut](items=items, total=total, limit=limit, offset=offset)


@router.get("/tenants/{tenant_id}", response_model=AdminTenantDetail)
async def get_tenant(tenant_id: uuid.UUID, _: User = Depends(require_platform_admin), db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(Tenant, *_tenant_columns()).where(Tenant.id == tenant_id))).first()
    if row is None or row[0].deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Business not found")
    tenant = row[0]
    users = (
        await db.execute(select(User).where(User.tenant_id == tenant.id).order_by(User.created_at))
    ).scalars().all()
    invoice_count = (await db.execute(select(func.count(Invoice.id)).where(Invoice.tenant_id == tenant.id))).scalar_one()
    customer_count = (
        await db.execute(select(func.count(Customer.id)).where(Customer.tenant_id == tenant.id))
    ).scalar_one()
    return AdminTenantDetail(
        **_tenant_out(*row),
        contact_email=tenant.contact_email, contact_phone=tenant.contact_phone,
        physical_address=tenant.physical_address, timezone=tenant.timezone,
        invoice_count=invoice_count, customer_count=customer_count,
        users=[_user_out(u, tenant.business_name) for u in users],
    )


@router.post("/tenants/{tenant_id}/suspend", status_code=status.HTTP_204_NO_CONTENT)
async def suspend_tenant(
    tenant_id: uuid.UUID, payload: SuspendRequest,
    admin: User = Depends(require_platform_admin), db: AsyncSession = Depends(get_db),
):
    tenant = await _get_tenant(db, tenant_id)
    tenant.suspended_at = _now()
    tenant.suspended_reason = payload.reason
    await _audit(db, admin, tenant_id=tenant.id, action="platform.tenant_suspended", entity_type="tenant",
                 entity_id=tenant.id, reason=payload.reason)
    await db.commit()


@router.post("/tenants/{tenant_id}/reactivate", status_code=status.HTTP_204_NO_CONTENT)
async def reactivate_tenant(
    tenant_id: uuid.UUID, admin: User = Depends(require_platform_admin), db: AsyncSession = Depends(get_db)
):
    tenant = await _get_tenant(db, tenant_id)
    tenant.suspended_at = None
    tenant.suspended_reason = None
    await _audit(db, admin, tenant_id=tenant.id, action="platform.tenant_reactivated", entity_type="tenant",
                 entity_id=tenant.id)
    await db.commit()


@router.post("/tenants/{tenant_id}/impersonate", response_model=ImpersonationOut)
async def impersonate_tenant(
    tenant_id: uuid.UUID, admin: User = Depends(require_platform_admin), db: AsyncSession = Depends(get_db)
):
    """Act as the business with owner rights; everything done is attributed to the super admin."""
    tenant = await _get_tenant(db, tenant_id)
    await _audit(db, admin, tenant_id=tenant.id, action="platform.impersonation_started", entity_type="tenant",
                 entity_id=tenant.id)
    await db.commit()
    token = create_impersonation_token(subject=str(admin.id), tenant_id=str(tenant.id), tenant_name=tenant.business_name)
    return ImpersonationOut(
        access_token=token, expires_in=get_settings().impersonation_token_minutes * 60,
        tenant_id=tenant.id, business_name=tenant.business_name,
    )


# --- Users ------------------------------------------------------------------------

@router.get("/users", response_model=Page[AdminUserOut])
async def list_users(
    q: str | None = Query(default=None, description="Name or email"),
    tenant_id: uuid.UUID | None = None,
    role: UserRole | None = None,
    status_filter: str | None = Query(default=None, alias="status", pattern="^(active|suspended|unverified)$"),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    conds = []
    if q:
        like = f"%{q.strip()}%"
        conds.append(or_(User.full_name.ilike(like), User.email.ilike(like)))
    if tenant_id:
        conds.append(User.tenant_id == tenant_id)
    if role:
        conds.append(User.role == role)
    if status_filter == "active":
        conds.append(User.suspended_at.is_(None))
    elif status_filter == "suspended":
        conds.append(User.suspended_at.is_not(None))
    elif status_filter == "unverified":
        conds.append(User.is_verified.is_(False))

    total = (await db.execute(select(func.count(User.id)).where(*conds))).scalar_one()
    rows = (
        await db.execute(
            select(User, Tenant.business_name).outerjoin(Tenant, Tenant.id == User.tenant_id).where(*conds)
            .order_by(User.created_at.desc(), User.id).limit(limit).offset(offset)
        )
    ).all()
    return Page[AdminUserOut](items=[_user_out(u, name) for u, name in rows], total=total, limit=limit, offset=offset)


@router.patch("/users/{user_id}", response_model=AdminUserOut)
async def update_user(
    user_id: uuid.UUID, payload: AdminUserUpdate,
    admin: User = Depends(require_platform_admin), db: AsyncSession = Depends(get_db),
):
    user = await _get_managed_user(db, user_id)
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)

    new_role = changes.get("role")
    if new_role is not None and new_role != user.role:
        if user.role == UserRole.owner and await _other_owner_count(db, user) == 0:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="This is the business's only owner; make someone else owner first")
        await _audit(db, admin, tenant_id=user.tenant_id, action="platform.user_role_changed", entity_type="user",
                     entity_id=user.id, old_role=user.role.value, new_role=UserRole(new_role).value)
        user.role = UserRole(new_role)

    if "is_verified" in changes and changes["is_verified"] != user.is_verified:
        user.is_verified = changes["is_verified"]
        if user.is_verified:
            user.email_verification_token = None
        await _audit(db, admin, tenant_id=user.tenant_id, action="platform.user_verification_changed",
                     entity_type="user", entity_id=user.id, is_verified=user.is_verified)

    await db.commit()
    await db.refresh(user)
    tenant = await db.get(Tenant, user.tenant_id) if user.tenant_id else None
    return _user_out(user, tenant.business_name if tenant else None)


@router.post("/users/{user_id}/suspend", status_code=status.HTTP_204_NO_CONTENT)
async def suspend_user(
    user_id: uuid.UUID, payload: SuspendRequest,
    admin: User = Depends(require_platform_admin), db: AsyncSession = Depends(get_db),
):
    user = await _get_managed_user(db, user_id)
    user.suspended_at = _now()
    user.suspended_reason = payload.reason
    await _audit(db, admin, tenant_id=user.tenant_id, action="platform.user_suspended", entity_type="user",
                 entity_id=user.id, reason=payload.reason)
    await db.commit()


@router.post("/users/{user_id}/reactivate", status_code=status.HTTP_204_NO_CONTENT)
async def reactivate_user(
    user_id: uuid.UUID, admin: User = Depends(require_platform_admin), db: AsyncSession = Depends(get_db)
):
    user = await _get_managed_user(db, user_id)
    user.suspended_at = None
    user.suspended_reason = None
    await _audit(db, admin, tenant_id=user.tenant_id, action="platform.user_reactivated", entity_type="user",
                 entity_id=user.id)
    await db.commit()


@router.post("/users/{user_id}/password-reset", status_code=status.HTTP_204_NO_CONTENT)
async def send_user_password_reset(
    user_id: uuid.UUID, admin: User = Depends(require_platform_admin), db: AsyncSession = Depends(get_db)
):
    user = await _get_managed_user(db, user_id)
    await _audit(db, admin, tenant_id=user.tenant_id, action="platform.password_reset_sent", entity_type="user",
                 entity_id=user.id)
    await start_password_reset(db, user)  # commits


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID, admin: User = Depends(require_platform_admin), db: AsyncSession = Depends(get_db)
):
    user = await _get_managed_user(db, user_id)
    issued = (
        await db.execute(select(func.count(Receipt.id)).where(Receipt.issued_by_user_id == user.id))
    ).scalar_one()
    if issued:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="This user has issued receipts, which must keep their issuer. Suspend them instead.")
    if user.role == UserRole.owner and await _other_owner_count(db, user) == 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="This is the business's only owner; make someone else owner first")

    await _audit(db, admin, tenant_id=user.tenant_id, action="platform.user_deleted", entity_type="user",
                 entity_id=user.id, email=str(user.email), full_name=user.full_name)
    await db.delete(user)
    await db.commit()


# --- Audit ------------------------------------------------------------------------

@router.get("/audit-logs", response_model=Page[PlatformAuditLogOut])
async def platform_audit_logs(
    tenant_id: uuid.UUID | None = None,
    action_prefix: str | None = Query(default=None, description="Action family, e.g. platform or invoice"),
    platform_only: bool = Query(default=False, description="Only actions taken by platform admins"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Newest first, across every business (plus platform-level events with no business)."""
    conds = []
    if tenant_id:
        conds.append(AuditLog.tenant_id == tenant_id)
    if action_prefix:
        conds.append(or_(AuditLog.action == action_prefix, AuditLog.action.like(f"{action_prefix}.%")))
    if platform_only:
        conds.append(AuditLog.action.like("platform.%"))

    total = (await db.execute(select(func.count(AuditLog.id)).where(*conds))).scalar_one()
    rows = (
        await db.execute(
            select(AuditLog, User.full_name, User.email, Tenant.business_name)
            .outerjoin(User, User.id == AuditLog.actor_user_id)
            .outerjoin(Tenant, Tenant.id == AuditLog.tenant_id)
            .where(*conds)
            .order_by(AuditLog.created_at.desc(), AuditLog.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    items = [
        PlatformAuditLogOut(
            id=log.id, tenant_id=log.tenant_id, business_name=business, action=log.action,
            entity_type=log.entity_type, entity_id=log.entity_id, actor_user_id=log.actor_user_id,
            actor_name=name, actor_email=str(email) if email else None, metadata=log.metadata_,
            created_at=log.created_at,
        )
        for log, name, email, business in rows
    ]
    return Page[PlatformAuditLogOut](items=items, total=total, limit=limit, offset=offset)
