"""Business-owned properties, their units, and which staff members manage them.

Owners/admins manage the portfolio and assignments. Staff only ever see the
properties actively assigned to them (anything else is a 404).
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessScope, get_scope, get_scoped_property, require_manager_scope, scope_properties
from app.core.audit import record_audit_event
from app.db.session import get_db
from app.models.enums import UserRole
from app.models.property import Property, PropertyStaffAssignment, PropertyUnit
from app.models.renter import Tenancy
from app.models.user import User
from app.schemas.property import (
    AssignedStaffOut,
    AssignStaffRequest,
    PropertyCreate,
    PropertyDetailOut,
    PropertyOut,
    PropertyUpdate,
    UnitCreate,
    UnitOut,
    UnitUpdate,
)
from app.schemas.team import AssignmentOut

router = APIRouter(prefix="/properties", tags=["properties"])


def _role(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


async def _counts(db: AsyncSession, property_ids: list[uuid.UUID]) -> tuple[dict, dict, dict]:
    if not property_ids:
        return {}, {}, {}
    units = dict((await db.execute(
        select(PropertyUnit.property_id, func.count()).where(
            PropertyUnit.property_id.in_(property_ids), PropertyUnit.status != "archived"
        ).group_by(PropertyUnit.property_id)
    )).all())
    occupied = dict((await db.execute(
        select(Tenancy.property_id, func.count(func.distinct(Tenancy.unit_id))).where(
            Tenancy.property_id.in_(property_ids), Tenancy.status == "active", Tenancy.unit_id.is_not(None)
        ).group_by(Tenancy.property_id)
    )).all())
    tenancies = dict((await db.execute(
        select(Tenancy.property_id, func.count()).where(
            Tenancy.property_id.in_(property_ids), Tenancy.status == "active"
        ).group_by(Tenancy.property_id)
    )).all())
    return units, occupied, tenancies


async def _out(db: AsyncSession, props: list[Property]) -> list[PropertyOut]:
    units, occupied, tenancies = await _counts(db, [p.id for p in props])
    out = []
    for p in props:
        item = PropertyOut.model_validate(p)
        item.unit_count = units.get(p.id, 0)
        item.occupied_unit_count = occupied.get(p.id, 0)
        item.active_tenancy_count = tenancies.get(p.id, 0)
        out.append(item)
    return out


async def _detail(db: AsyncSession, prop: Property) -> PropertyDetailOut:
    base = (await _out(db, [prop]))[0]
    units = (
        await db.execute(select(PropertyUnit).where(PropertyUnit.property_id == prop.id).order_by(PropertyUnit.label))
    ).scalars().all()
    occupied_ids = set((await db.execute(
        select(Tenancy.unit_id).where(Tenancy.property_id == prop.id, Tenancy.status == "active", Tenancy.unit_id.is_not(None))
    )).scalars().all())
    staff_rows = (
        await db.execute(
            select(PropertyStaffAssignment, User)
            .join(User, User.id == PropertyStaffAssignment.user_id)
            .where(PropertyStaffAssignment.property_id == prop.id, PropertyStaffAssignment.revoked_at.is_(None))
            .order_by(User.full_name)
        )
    ).all()
    unit_out = []
    for u in units:
        item = UnitOut.model_validate(u)
        item.occupied = u.id in occupied_ids
        unit_out.append(item)
    return PropertyDetailOut(
        **base.model_dump(),
        units=unit_out,
        assigned_staff=[
            AssignedStaffOut(user_id=user.id, full_name=user.full_name, role=_role(user.role),
                             is_active=user.deactivated_at is None, assigned_at=a.assigned_at)
            for a, user in staff_rows
        ],
    )


# ------------------------------------------------------------------ properties


@router.post("", response_model=PropertyDetailOut, status_code=status.HTTP_201_CREATED)
async def create_property(
    payload: PropertyCreate, current_user: AccessScope = Depends(require_manager_scope), db: AsyncSession = Depends(get_db)
):
    prop = Property(tenant_id=current_user.tenant_id, created_by_user_id=current_user.id, **payload.model_dump())
    prop.name = prop.name.strip()
    db.add(prop)
    await db.flush()
    await record_audit_event(
        db, tenant_id=current_user.tenant_id, actor_user_id=current_user.id, action="property.created",
        entity_type="property", entity_id=prop.id, metadata={"name": prop.name},
    )
    await db.commit()
    await db.refresh(prop)
    return await _detail(db, prop)


@router.get("", response_model=list[PropertyOut])
async def list_properties(
    response: Response,
    q: str | None = Query(default=None, description="Search by name, address or location"),
    status_filter: str | None = Query(default=None, alias="status", pattern="^(active|inactive|archived)$"),
    assigned_to: uuid.UUID | None = Query(default=None, description="Managers only: properties assigned to this member"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AccessScope = Depends(get_scope),
    db: AsyncSession = Depends(get_db),
):
    """Managers see the whole portfolio; staff see only the properties assigned to them."""
    stmt = scope_properties(select(Property), current_user)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Property.name.ilike(like), Property.address.ilike(like), Property.location.ilike(like)))
    if status_filter:
        stmt = stmt.where(Property.status == status_filter)
    if assigned_to and current_user.is_manager:
        stmt = stmt.where(Property.id.in_(
            select(PropertyStaffAssignment.property_id).where(
                PropertyStaffAssignment.user_id == assigned_to, PropertyStaffAssignment.revoked_at.is_(None)
            )
        ))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    response.headers["X-Total-Count"] = str(total)
    rows = (await db.execute(stmt.order_by(Property.name, Property.id).limit(limit).offset(offset))).scalars().all()
    return await _out(db, list(rows))


@router.get("/{property_id}", response_model=PropertyDetailOut)
async def get_property(property_id: uuid.UUID, current_user: AccessScope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    return await _detail(db, await get_scoped_property(db, current_user, property_id))


@router.patch("/{property_id}", response_model=PropertyDetailOut)
async def update_property(
    property_id: uuid.UUID,
    payload: PropertyUpdate,
    current_user: AccessScope = Depends(require_manager_scope),
    db: AsyncSession = Depends(get_db),
):
    prop = await get_scoped_property(db, current_user, property_id, lock=True)
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    for field, value in changes.items():
        setattr(prop, field, value.strip() if field == "name" else value)
    action = "property.archived" if changes.get("status") == "archived" else "property.updated"
    await record_audit_event(
        db, tenant_id=current_user.tenant_id, actor_user_id=current_user.id, action=action,
        entity_type="property", entity_id=prop.id, metadata={"fields": sorted(changes)},
    )
    await db.commit()
    await db.refresh(prop)
    return await _detail(db, prop)


@router.post("/{property_id}/archive", response_model=PropertyDetailOut)
async def archive_property(
    property_id: uuid.UUID, current_user: AccessScope = Depends(require_manager_scope), db: AsyncSession = Depends(get_db)
):
    """Archives a property. Its tenancies, receipts and other documents are kept unchanged."""
    return await update_property(property_id, PropertyUpdate(status="archived"), current_user, db)


# ------------------------------------------------------------------ units


async def _get_unit(db: AsyncSession, prop: Property, unit_id: uuid.UUID) -> PropertyUnit:
    unit = (
        await db.execute(select(PropertyUnit).where(PropertyUnit.id == unit_id, PropertyUnit.property_id == prop.id))
    ).scalar_one_or_none()
    if unit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unit not found")
    return unit


@router.post("/{property_id}/units", response_model=UnitOut, status_code=status.HTTP_201_CREATED)
async def create_unit(
    property_id: uuid.UUID,
    payload: UnitCreate,
    current_user: AccessScope = Depends(require_manager_scope),
    db: AsyncSession = Depends(get_db),
):
    prop = await get_scoped_property(db, current_user, property_id)
    unit = PropertyUnit(tenant_id=current_user.tenant_id, property_id=prop.id, **payload.model_dump())
    unit.label = unit.label.strip()
    db.add(unit)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A unit with this label already exists") from exc
    await record_audit_event(
        db, tenant_id=current_user.tenant_id, actor_user_id=current_user.id, action="property.unit_created",
        entity_type="property", entity_id=prop.id, metadata={"unit_id": str(unit.id), "label": unit.label},
    )
    await db.commit()
    await db.refresh(unit)
    return UnitOut.model_validate(unit)


@router.get("/{property_id}/units", response_model=list[UnitOut])
async def list_units(property_id: uuid.UUID, current_user: AccessScope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    return (await _detail(db, await get_scoped_property(db, current_user, property_id))).units


@router.patch("/{property_id}/units/{unit_id}", response_model=UnitOut)
async def update_unit(
    property_id: uuid.UUID,
    unit_id: uuid.UUID,
    payload: UnitUpdate,
    current_user: AccessScope = Depends(require_manager_scope),
    db: AsyncSession = Depends(get_db),
):
    prop = await get_scoped_property(db, current_user, property_id)
    unit = await _get_unit(db, prop, unit_id)
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    for field, value in changes.items():
        setattr(unit, field, value.strip() if field == "label" else value)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A unit with this label already exists") from exc
    await record_audit_event(
        db, tenant_id=current_user.tenant_id, actor_user_id=current_user.id, action="property.unit_updated",
        entity_type="property", entity_id=prop.id, metadata={"unit_id": str(unit.id), "fields": sorted(changes)},
    )
    await db.commit()
    await db.refresh(unit)
    return UnitOut.model_validate(unit)


# ------------------------------------------------------------------ staff assignments


@router.get("/{property_id}/assignments", response_model=list[AssignmentOut])
async def property_assignments(
    property_id: uuid.UUID, current_user: AccessScope = Depends(require_manager_scope), db: AsyncSession = Depends(get_db)
):
    """Current and past staff assignments for a property, newest first."""
    prop = await get_scoped_property(db, current_user, property_id)
    rows = (
        await db.execute(
            select(PropertyStaffAssignment, User.full_name)
            .join(User, User.id == PropertyStaffAssignment.user_id)
            .where(PropertyStaffAssignment.property_id == prop.id)
            .order_by(PropertyStaffAssignment.assigned_at.desc())
        )
    ).all()
    return [
        AssignmentOut(
            id=a.id, property_id=a.property_id, property_name=prop.name, user_id=a.user_id, user_name=name,
            assigned_by_user_id=a.assigned_by_user_id, assigned_at=a.assigned_at, revoked_by_user_id=a.revoked_by_user_id,
            revoked_at=a.revoked_at, active=a.revoked_at is None,
        )
        for a, name in rows
    ]


@router.post("/{property_id}/assignments", response_model=PropertyDetailOut, status_code=status.HTTP_201_CREATED)
async def assign_staff(
    property_id: uuid.UUID,
    payload: AssignStaffRequest,
    current_user: AccessScope = Depends(require_manager_scope),
    db: AsyncSession = Depends(get_db),
):
    prop = await get_scoped_property(db, current_user, property_id, lock=True)
    if prop.status == "archived":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Archived properties can't be assigned")
    member = (
        await db.execute(select(User).where(User.id == payload.user_id, User.tenant_id == current_user.tenant_id))
    ).scalar_one_or_none()
    if member is None or member.is_superadmin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team member not found")
    if member.deactivated_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This team member is deactivated")
    if _role(member.role) == UserRole.owner.value:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Owners already manage every property")

    existing = (
        await db.execute(
            select(PropertyStaffAssignment).where(
                PropertyStaffAssignment.property_id == prop.id, PropertyStaffAssignment.user_id == member.id,
                PropertyStaffAssignment.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(PropertyStaffAssignment(
            tenant_id=current_user.tenant_id, property_id=prop.id, user_id=member.id, assigned_by_user_id=current_user.id,
        ))
        await record_audit_event(
            db, tenant_id=current_user.tenant_id, actor_user_id=current_user.id, action="property.staff_assigned",
            entity_type="property", entity_id=prop.id, metadata={"user_id": str(member.id)},
        )
        await db.commit()
    return await _detail(db, prop)


@router.delete("/{property_id}/assignments/{user_id}", response_model=PropertyDetailOut)
async def revoke_staff(
    property_id: uuid.UUID,
    user_id: uuid.UUID,
    current_user: AccessScope = Depends(require_manager_scope),
    db: AsyncSession = Depends(get_db),
):
    """Revokes access immediately; the assignment row is kept as history."""
    prop = await get_scoped_property(db, current_user, property_id, lock=True)
    assignment = (
        await db.execute(
            select(PropertyStaffAssignment).where(
                PropertyStaffAssignment.property_id == prop.id, PropertyStaffAssignment.user_id == user_id,
                PropertyStaffAssignment.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if assignment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This member is not assigned to the property")
    assignment.revoked_at = datetime.now(timezone.utc)
    assignment.revoked_by_user_id = current_user.id
    await record_audit_event(
        db, tenant_id=current_user.tenant_id, actor_user_id=current_user.id, action="property.staff_revoked",
        entity_type="property", entity_id=prop.id, metadata={"user_id": str(user_id)},
    )
    await db.commit()
    return await _detail(db, prop)
