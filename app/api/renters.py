"""Renters ("Tenants" in the portal) and their tenancies.

Staff need the renters.manage permission and can only reach renters with a tenancy
on a property assigned to them; new tenancies must be on such a property.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessScope, Perm, get_scoped_property, require_perm
from app.core.audit import record_audit_event
from app.db.session import get_db
from app.models.customer import Customer
from app.models.property import Property, PropertyUnit
from app.models.renter import Renter, Tenancy
from app.models.schedule import PaymentSchedule
from app.models.tenant import Tenant
from app.schemas.customer import CustomerOut
from app.schemas.renter import (
    RenterCreate,
    RenterDetailOut,
    RenterOut,
    RenterUpdate,
    TenancyCreate,
    TenancyEnd,
    TenancyOut,
    TenancyUpdate,
)
from app.services import kyc as kyc_svc
from app.services import renters as svc

router = APIRouter(tags=["renters"])

MANAGE = require_perm(Perm.renters_manage)


async def _tenancy_outs(db: AsyncSession, tenancies: list[Tenancy]) -> list[TenancyOut]:
    if not tenancies:
        return []
    prop_names = dict((await db.execute(
        select(Property.id, Property.name).where(Property.id.in_({t.property_id for t in tenancies}))
    )).all())
    unit_ids = {t.unit_id for t in tenancies if t.unit_id}
    unit_labels = (
        dict((await db.execute(select(PropertyUnit.id, PropertyUnit.label).where(PropertyUnit.id.in_(unit_ids)))).all())
        if unit_ids else {}
    )
    schedules = dict((await db.execute(
        select(PaymentSchedule.tenancy_id, PaymentSchedule.id).where(PaymentSchedule.tenancy_id.in_([t.id for t in tenancies]))
        .order_by(PaymentSchedule.created_at)
    )).all())
    out = []
    for t in tenancies:
        item = TenancyOut.model_validate(t)
        item.property_name = prop_names.get(t.property_id)
        item.unit_label = unit_labels.get(t.unit_id) if t.unit_id else None
        item.payment_schedule_id = schedules.get(t.id)
        out.append(item)
    return out


async def _renter_outs(db: AsyncSession, scope: AccessScope, renters: list[Renter], *, detail: bool = False):
    if not renters:
        return []
    customers = {c.id: c for c in (await db.execute(select(Customer).where(Customer.id.in_([r.customer_id for r in renters])))).scalars()}
    tenancy_stmt = select(Tenancy).where(Tenancy.renter_id.in_([r.id for r in renters])).order_by(Tenancy.start_date.desc())
    if not scope.is_manager:
        # Staff see only the tenancies on their assigned properties.
        tenancy_stmt = tenancy_stmt.where(Tenancy.property_id.in_(scope.property_ids or [uuid.UUID(int=0)]))
    tenancies = list((await db.execute(tenancy_stmt)).scalars().all())
    tenancy_out = {t.id: o for t, o in zip(tenancies, await _tenancy_outs(db, tenancies))}
    kyc = await kyc_svc.statuses_for_renters(db, [r.id for r in renters])

    out = []
    for r in renters:
        mine = [tenancy_out[t.id] for t in tenancies if t.renter_id == r.id]
        customer = CustomerOut.model_validate(customers[r.customer_id])
        customer.renter_id = r.id
        data = dict(
            id=r.id, customer_id=r.customer_id, customer=customer, status=r.status, occupation=r.occupation,
            employer=r.employer, notes=r.notes, kyc_status=kyc.get(r.id), created_at=r.created_at,
            current_tenancies=[t for t in mine if t.status == "active"],
        )
        out.append(RenterDetailOut(**data, tenancies=mine) if detail else RenterOut(**data))
    return out


# ------------------------------------------------------------------ renters


@router.post("/renters", response_model=RenterDetailOut, status_code=status.HTTP_201_CREATED)
async def create_renter(payload: RenterCreate, current_user: AccessScope = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    """Creates the tenant profile, its customer (or reuses one) and the first tenancy in one transaction."""
    renter = await svc.create_renter(db, current_user, payload)
    await db.commit()
    await db.refresh(renter)
    return (await _renter_outs(db, current_user, [renter], detail=True))[0]


@router.get("/renters", response_model=list[RenterOut])
async def list_renters(
    response: Response,
    q: str | None = Query(default=None, description="Search by name, email or phone"),
    status_filter: str | None = Query(default=None, alias="status", pattern="^(active|former|archived)$"),
    property_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AccessScope = Depends(MANAGE),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Renter).join(Customer, Customer.id == Renter.customer_id).where(svc.renter_visible_clause(current_user))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Customer.name.ilike(like), Customer.email.ilike(like), Customer.phone.ilike(like)))
    if status_filter:
        stmt = stmt.where(Renter.status == status_filter)
    if property_id:
        await get_scoped_property(db, current_user, property_id)
        stmt = stmt.where(Renter.id.in_(select(Tenancy.renter_id).where(Tenancy.property_id == property_id)))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    response.headers["X-Total-Count"] = str(total)
    rows = (await db.execute(stmt.order_by(Customer.name, Renter.id).limit(limit).offset(offset))).scalars().all()
    return await _renter_outs(db, current_user, list(rows))


@router.get("/renters/{renter_id}", response_model=RenterDetailOut)
async def get_renter(renter_id: uuid.UUID, current_user: AccessScope = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    renter = await svc.get_renter(db, current_user, renter_id)
    return (await _renter_outs(db, current_user, [renter], detail=True))[0]


@router.patch("/renters/{renter_id}", response_model=RenterDetailOut)
async def update_renter(
    renter_id: uuid.UUID, payload: RenterUpdate, current_user: AccessScope = Depends(MANAGE), db: AsyncSession = Depends(get_db)
):
    """Tenant-specific details only; contact details are edited on the customer."""
    renter = await svc.get_renter(db, current_user, renter_id)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("status") == "archived" and not current_user.is_manager:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the business owner or an admin can archive a tenant")
    for field, value in changes.items():
        setattr(renter, field, value)
    await record_audit_event(
        db, tenant_id=current_user.tenant_id, actor_user_id=current_user.id, action="renter.updated", entity_type="renter",
        entity_id=renter.id, metadata={"fields": sorted(changes)},
    )
    await db.commit()
    await db.refresh(renter)
    return (await _renter_outs(db, current_user, [renter], detail=True))[0]


# ------------------------------------------------------------------ tenancies


@router.post("/renters/{renter_id}/tenancies", response_model=TenancyOut, status_code=status.HTTP_201_CREATED)
async def add_tenancy(
    renter_id: uuid.UUID, payload: TenancyCreate, current_user: AccessScope = Depends(MANAGE), db: AsyncSession = Depends(get_db)
):
    """A new tenancy for an existing tenant (e.g. moving units or returning); the tenant identity is reused."""
    renter = await svc.get_renter(db, current_user, renter_id)
    if renter.status == "archived":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This tenant is archived")
    tenant = await db.get(Tenant, current_user.tenant_id)
    tenancy = await svc.create_tenancy(db, current_user, tenant, renter, payload)
    await db.commit()
    await db.refresh(tenancy)
    return (await _tenancy_outs(db, [tenancy]))[0]


@router.get("/tenancies/{tenancy_id}", response_model=TenancyOut)
async def get_tenancy(tenancy_id: uuid.UUID, current_user: AccessScope = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    return (await _tenancy_outs(db, [await svc.get_tenancy(db, current_user, tenancy_id)]))[0]


@router.patch("/tenancies/{tenancy_id}", response_model=TenancyOut)
async def update_tenancy(
    tenancy_id: uuid.UUID, payload: TenancyUpdate, current_user: AccessScope = Depends(MANAGE), db: AsyncSession = Depends(get_db)
):
    tenancy = await svc.get_tenancy(db, current_user, tenancy_id, lock=True)
    if tenancy.status not in ("draft", "active"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"A {tenancy.status} tenancy can't be changed")
    changes = payload.model_dump(exclude_unset=True)
    prop = await get_scoped_property(db, current_user, tenancy.property_id)
    new_status = changes.get("status", tenancy.status)
    new_unit = changes.get("unit_id", tenancy.unit_id)
    if new_status == "active" and (new_unit != tenancy.unit_id or tenancy.status != "active"):
        await svc._check_unit(db, prop, new_unit, ignore_tenancy=tenancy.id)
    end_date = changes.get("end_date", tenancy.end_date)
    if end_date is not None and end_date < tenancy.start_date:
        raise HTTPException(status_code=422, detail="end_date cannot be before start_date")
    for field, value in changes.items():
        setattr(tenancy, field, value)
    await record_audit_event(
        db, tenant_id=current_user.tenant_id, actor_user_id=current_user.id, action="tenancy.updated", entity_type="tenancy",
        entity_id=tenancy.id, metadata={"fields": sorted(changes)},
    )
    await db.commit()
    await db.refresh(tenancy)
    return (await _tenancy_outs(db, [tenancy]))[0]


@router.post("/tenancies/{tenancy_id}/end", response_model=TenancyOut)
async def end_tenancy(
    tenancy_id: uuid.UUID, payload: TenancyEnd, current_user: AccessScope = Depends(MANAGE), db: AsyncSession = Depends(get_db)
):
    """Ends the tenancy. The record, its documents and the tenant's history are kept."""
    tenancy = await svc.get_tenancy(db, current_user, tenancy_id, lock=True)
    await svc.end_tenancy(db, current_user, tenancy, end_date=payload.end_date, new_status=payload.status, reason=payload.reason)
    await db.commit()
    await db.refresh(tenancy)
    return (await _tenancy_outs(db, [tenancy]))[0]
