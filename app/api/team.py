"""Team management: the business owner/admins create and manage staff accounts.

Rules (enforced here, never taken from the client):
- Only owners and admins can see or manage team members.
- Only an owner can create an admin, promote someone to admin, or change an admin.
- Owners can't be edited, deactivated or demoted through this API, and nobody can
  change their own role, permissions or active state.
- A member's business is always the caller's business.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import (
    ALL_PERMISSIONS,
    DEFAULT_STAFF_PERMISSIONS,
    AccessScope,
    effective_permissions,
    require_manager_scope,
)
from app.core.audit import record_audit_event
from app.core.security import generate_token, hash_password
from app.db.session import get_db
from app.models.enums import UserRole
from app.models.property import Property, PropertyStaffAssignment
from app.models.user import User
from app.schemas.team import (
    AssignmentOut,
    DeactivateRequest,
    PermissionCatalogOut,
    TeamMemberCreate,
    TeamMemberOut,
    TeamMemberUpdate,
)
from app.services import kyc as kyc_svc
from app.services.auth import start_password_reset

router = APIRouter(prefix="/team", tags=["team"])

INVITE_VALID_HOURS = 72


def _role(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _clean_permissions(perms: list[str]) -> list[str]:
    unknown = sorted(set(perms) - ALL_PERMISSIONS)
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown permissions: {', '.join(unknown)}")
    return sorted(set(perms))


async def _out(db: AsyncSession, users: list[User]) -> list[TeamMemberOut]:
    ids = [u.id for u in users]
    assigned: dict[uuid.UUID, list[uuid.UUID]] = {}
    if ids:
        rows = await db.execute(
            select(PropertyStaffAssignment.user_id, PropertyStaffAssignment.property_id).where(
                PropertyStaffAssignment.user_id.in_(ids), PropertyStaffAssignment.revoked_at.is_(None)
            )
        )
        for user_id, property_id in rows.all():
            assigned.setdefault(user_id, []).append(property_id)
    kyc = await kyc_svc.statuses_for_users(db, ids)
    return [
        TeamMemberOut(
            id=u.id, full_name=u.full_name, email=str(u.email), phone=u.phone, role=_role(u.role),
            permissions=sorted(effective_permissions(u)), uses_default_permissions=u.permissions is None,
            is_active=u.deactivated_at is None, deactivated_at=u.deactivated_at, last_login_at=u.last_login_at,
            created_at=u.created_at, assigned_property_ids=assigned.get(u.id, []), kyc_status=kyc.get(u.id),
        )
        for u in users
    ]


async def _get_member(db: AsyncSession, scope: AccessScope, user_id: uuid.UUID) -> User:
    user = (
        await db.execute(select(User).where(User.id == user_id, User.tenant_id == scope.tenant_id, User.is_superadmin.is_(False)))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team member not found")
    return user


def _check_can_manage(scope: AccessScope, target: User) -> None:
    if target.id == scope.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can't change your own access")
    target_role = _role(target.role)
    if target_role == UserRole.owner.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="The business owner's access can't be changed here")
    if target_role == UserRole.admin.value and _role(scope.role) != UserRole.owner.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the business owner can manage admins")


@router.get("/permissions", response_model=PermissionCatalogOut)
async def permission_catalog(_: AccessScope = Depends(require_manager_scope)):
    return PermissionCatalogOut(all=sorted(ALL_PERMISSIONS), staff_defaults=sorted(DEFAULT_STAFF_PERMISSIONS))


@router.get("/members", response_model=list[TeamMemberOut])
async def list_members(current_user: AccessScope = Depends(require_manager_scope), db: AsyncSession = Depends(get_db)):
    users = (
        await db.execute(
            select(User).where(User.tenant_id == current_user.tenant_id, User.is_superadmin.is_(False))
            .order_by(User.created_at, User.id)
        )
    ).scalars().all()
    return await _out(db, list(users))


@router.post("/members", response_model=TeamMemberOut, status_code=status.HTTP_201_CREATED)
async def create_member(
    payload: TeamMemberCreate, current_user: AccessScope = Depends(require_manager_scope), db: AsyncSession = Depends(get_db)
):
    if payload.role == UserRole.admin.value and _role(current_user.role) != UserRole.owner.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the business owner can add admins")
    exists = (await db.execute(select(func.count()).select_from(User).where(User.email == payload.email))).scalar_one()
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists")

    permissions = None
    if payload.role == UserRole.staff.value and payload.permissions is not None:
        permissions = _clean_permissions(payload.permissions)

    user = User(
        tenant_id=current_user.tenant_id,
        full_name=payload.full_name.strip(),
        email=payload.email,
        phone=payload.phone,
        # Nobody knows this password; the member sets their own through the invite link.
        password_hash=hash_password(generate_token()),
        role=UserRole(payload.role),
        permissions=permissions,
        created_by_user_id=current_user.id,
    )
    db.add(user)
    await db.flush()
    await record_audit_event(
        db, tenant_id=current_user.tenant_id, actor_user_id=current_user.id, action="team.member_created",
        entity_type="user", entity_id=user.id, metadata={"role": payload.role},
    )
    await db.commit()
    await start_password_reset(db, user, valid_hours=INVITE_VALID_HOURS)
    await db.refresh(user)
    return (await _out(db, [user]))[0]


@router.get("/members/{user_id}", response_model=TeamMemberOut)
async def get_member(user_id: uuid.UUID, current_user: AccessScope = Depends(require_manager_scope), db: AsyncSession = Depends(get_db)):
    return (await _out(db, [await _get_member(db, current_user, user_id)]))[0]


@router.patch("/members/{user_id}", response_model=TeamMemberOut)
async def update_member(
    user_id: uuid.UUID,
    payload: TeamMemberUpdate,
    current_user: AccessScope = Depends(require_manager_scope),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_member(db, current_user, user_id)
    changes = payload.model_dump(exclude_unset=True)
    touches_access = any(k in changes for k in ("role", "permissions")) or payload.reset_permissions
    if touches_access:
        _check_can_manage(current_user, user)
    elif user.id != current_user.id and _role(user.role) != UserRole.staff.value:
        _check_can_manage(current_user, user)

    if payload.full_name is not None:
        user.full_name = payload.full_name.strip()
    if "phone" in changes:
        user.phone = payload.phone

    if payload.role is not None and payload.role != _role(user.role):
        if payload.role == UserRole.admin.value and _role(current_user.role) != UserRole.owner.value:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the business owner can promote to admin")
        old_role = _role(user.role)
        user.role = UserRole(payload.role)
        await record_audit_event(
            db, tenant_id=current_user.tenant_id, actor_user_id=current_user.id, action="team.role_changed",
            entity_type="user", entity_id=user.id, metadata={"old_role": old_role, "new_role": payload.role},
        )

    if payload.reset_permissions or "permissions" in changes:
        new_permissions = None if payload.reset_permissions or payload.permissions is None else _clean_permissions(payload.permissions)
        if new_permissions != user.permissions:
            user.permissions = new_permissions
            await record_audit_event(
                db, tenant_id=current_user.tenant_id, actor_user_id=current_user.id, action="team.permissions_changed",
                entity_type="user", entity_id=user.id,
                metadata={"permissions": new_permissions if new_permissions is not None else "default"},
            )

    await db.commit()
    await db.refresh(user)
    return (await _out(db, [user]))[0]


@router.post("/members/{user_id}/deactivate", response_model=TeamMemberOut)
async def deactivate_member(
    user_id: uuid.UUID,
    payload: DeactivateRequest | None = None,
    current_user: AccessScope = Depends(require_manager_scope),
    db: AsyncSession = Depends(get_db),
):
    """Blocks login and API access immediately. Assignments, receipts and history are kept."""
    user = await _get_member(db, current_user, user_id)
    _check_can_manage(current_user, user)
    if user.deactivated_at is None:
        user.deactivated_at = datetime.now(timezone.utc)
        user.deactivated_by_user_id = current_user.id
        await record_audit_event(
            db, tenant_id=current_user.tenant_id, actor_user_id=current_user.id, action="team.member_deactivated",
            entity_type="user", entity_id=user.id, metadata={"reason": payload.reason} if payload and payload.reason else None,
        )
        await db.commit()
        await db.refresh(user)
    return (await _out(db, [user]))[0]


@router.post("/members/{user_id}/reactivate", response_model=TeamMemberOut)
async def reactivate_member(
    user_id: uuid.UUID, current_user: AccessScope = Depends(require_manager_scope), db: AsyncSession = Depends(get_db)
):
    user = await _get_member(db, current_user, user_id)
    _check_can_manage(current_user, user)
    if user.deactivated_at is not None:
        user.deactivated_at = None
        user.deactivated_by_user_id = None
        await record_audit_event(
            db, tenant_id=current_user.tenant_id, actor_user_id=current_user.id, action="team.member_reactivated",
            entity_type="user", entity_id=user.id,
        )
        await db.commit()
        await db.refresh(user)
    return (await _out(db, [user]))[0]


@router.post("/members/{user_id}/resend-invite", status_code=status.HTTP_204_NO_CONTENT)
async def resend_invite(user_id: uuid.UUID, current_user: AccessScope = Depends(require_manager_scope), db: AsyncSession = Depends(get_db)):
    user = await _get_member(db, current_user, user_id)
    _check_can_manage(current_user, user)
    if user.deactivated_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Reactivate this member first")
    await record_audit_event(
        db, tenant_id=current_user.tenant_id, actor_user_id=current_user.id, action="team.invite_resent",
        entity_type="user", entity_id=user.id,
    )
    await start_password_reset(db, user, valid_hours=INVITE_VALID_HOURS)


@router.get("/members/{user_id}/assignments", response_model=list[AssignmentOut])
async def member_assignments(
    user_id: uuid.UUID, current_user: AccessScope = Depends(require_manager_scope), db: AsyncSession = Depends(get_db)
):
    """Every property this member has been assigned, including revoked assignments (history)."""
    user = await _get_member(db, current_user, user_id)
    rows = (
        await db.execute(
            select(PropertyStaffAssignment, Property.name)
            .join(Property, Property.id == PropertyStaffAssignment.property_id)
            .where(PropertyStaffAssignment.user_id == user.id, PropertyStaffAssignment.tenant_id == current_user.tenant_id)
            .order_by(PropertyStaffAssignment.assigned_at.desc())
        )
    ).all()
    return [
        AssignmentOut(
            id=a.id, property_id=a.property_id, property_name=name, user_id=a.user_id, user_name=user.full_name,
            assigned_by_user_id=a.assigned_by_user_id, assigned_at=a.assigned_at, revoked_by_user_id=a.revoked_by_user_id,
            revoked_at=a.revoked_at, active=a.revoked_at is None,
        )
        for a, name in rows
    ]
