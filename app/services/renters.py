"""Renters and tenancies.

A renter always wraps an existing Customer of the same business: becoming a renter
never creates a second identity, and the customer's id (and every document issued to
it) is unchanged. Creating a renter, its customer and its first tenancy happens in the
caller's single transaction, so a failure part-way leaves nothing behind.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import and_, exists, false, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessScope, Perm, get_scoped_property, not_found
from app.core.audit import record_audit_event
from app.models.customer import Customer
from app.models.kyc import KycProfile
from app.models.property import Property, PropertyUnit
from app.models.renter import Renter, Tenancy
from app.models.schedule import PaymentSchedule
from app.models.tenant import Tenant
from app.schemas.renter import RenterCreate, TenancyCreate
from app.services import notifications as notif
from app.services import reminders
from app.services import schedules as schedules_svc
from app.services.customers import find_matches, get_customer, set_contact
from app.services.numbering import next_document_number


def renter_visible_clause(scope: AccessScope):
    """Staff see renters with a tenancy (current or past) on a property assigned to them."""
    if scope.is_manager:
        return Renter.tenant_id == scope.tenant_id
    if not scope.property_ids:
        return false()
    return and_(
        Renter.tenant_id == scope.tenant_id,
        exists().where(Tenancy.renter_id == Renter.id, Tenancy.property_id.in_(scope.property_ids)),
    )


async def get_renter(db: AsyncSession, scope: AccessScope, renter_id: uuid.UUID) -> Renter:
    renter = (
        await db.execute(select(Renter).where(Renter.id == renter_id, renter_visible_clause(scope)))
    ).scalar_one_or_none()
    if renter is None:
        raise not_found("Tenant")
    return renter


async def get_tenancy(db: AsyncSession, scope: AccessScope, tenancy_id: uuid.UUID, *, lock: bool = False) -> Tenancy:
    stmt = select(Tenancy).where(Tenancy.id == tenancy_id, Tenancy.tenant_id == scope.tenant_id)
    if lock and db.get_bind().dialect.name != "sqlite":
        stmt = stmt.with_for_update()
    tenancy = (await db.execute(stmt)).scalar_one_or_none()
    if tenancy is None or not scope.can_access_property(tenancy.property_id):
        raise not_found("Tenancy")
    return tenancy


async def _check_unit(db: AsyncSession, prop: Property, unit_id: uuid.UUID | None, *, ignore_tenancy: uuid.UUID | None = None) -> None:
    if unit_id is None:
        return
    unit = (
        await db.execute(select(PropertyUnit).where(PropertyUnit.id == unit_id, PropertyUnit.property_id == prop.id))
    ).scalar_one_or_none()
    if unit is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="The unit does not belong to this property")
    if unit.status == "archived":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="This unit is archived")
    stmt = select(Tenancy.id).where(Tenancy.unit_id == unit_id, Tenancy.status == "active")
    if ignore_tenancy is not None:
        stmt = stmt.where(Tenancy.id != ignore_tenancy)
    if (await db.execute(stmt)).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This unit already has an active tenancy")


async def create_tenancy(
    db: AsyncSession, scope: AccessScope, tenant: Tenant, renter: Renter, payload: TenancyCreate
) -> Tenancy:
    prop = await get_scoped_property(db, scope, payload.property_id)
    if prop.status != "active":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"The property is {prop.status}")
    if payload.status == "active":
        await _check_unit(db, prop, payload.unit_id)
    elif payload.unit_id is not None:
        # A draft may be prepared for a unit that is still occupied; only check it belongs to the property.
        unit = (
            await db.execute(select(PropertyUnit).where(PropertyUnit.id == payload.unit_id, PropertyUnit.property_id == prop.id))
        ).scalar_one_or_none()
        if unit is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="The unit does not belong to this property")
    if payload.create_rent_schedule and not scope.can(Perm.invoices_issue):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create payment schedules")

    tenancy = Tenancy(
        tenant_id=tenant.id,
        renter_id=renter.id,
        property_id=prop.id,
        unit_id=payload.unit_id,
        reference=await next_document_number(db, tenant.id, "tenancy", today=payload.start_date),
        start_date=payload.start_date,
        end_date=payload.end_date,
        rent_amount_cents=payload.rent_amount_cents,
        currency=(payload.currency or tenant.default_currency).upper(),
        rent_frequency=payload.rent_frequency,
        deposit_cents=payload.deposit_cents,
        status=payload.status,
        notes=payload.notes,
        created_by_user_id=scope.id,
    )
    db.add(tenancy)
    await db.flush()
    if renter.status == "former" and tenancy.status == "active":
        renter.status = "active"
    await record_audit_event(
        db, tenant_id=tenant.id, actor_user_id=scope.id, action="tenancy.created", entity_type="tenancy", entity_id=tenancy.id,
        metadata={"renter_id": str(renter.id), "property_id": str(prop.id), "unit_id": str(payload.unit_id) if payload.unit_id else None},
    )

    if payload.create_rent_schedule and payload.rent_amount_cents > 0:
        customer = await db.get(Customer, renter.customer_id)
        schedule = PaymentSchedule(
            tenant_id=tenant.id, customer_id=renter.customer_id,
            description=f"Rent — {prop.name}" + (f" ({tenancy.reference})" if tenancy.reference else ""),
            reference_label=tenancy.reference, amount_cents=payload.rent_amount_cents, currency=tenancy.currency,
            frequency=payload.rent_frequency, first_due_date=payload.start_date, end_date=payload.end_date,
            property_id=prop.id, unit_id=payload.unit_id, tenancy_id=tenancy.id, created_by_user_id=scope.id,
        )
        db.add(schedule)
        await db.flush()
        today = notif.tenant_today(tenant)
        for occ in await schedules_svc.generate_occurrences(db, schedule, today=today):
            await reminders.plan_occurrence_reminders(db, tenant, schedule, occ, customer)
        await record_audit_event(db, tenant_id=tenant.id, actor_user_id=scope.id, action="schedule.created",
                                 entity_type="schedule", entity_id=schedule.id, metadata={"tenancy_id": str(tenancy.id)})
    return tenancy


async def create_renter(db: AsyncSession, scope: AccessScope, payload: RenterCreate) -> Renter:
    """Customer (new or existing) + renter profile + optional first tenancy, without committing."""
    if not scope.is_manager and payload.tenancy is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Add a tenancy on one of your assigned properties when creating a tenant",
        )
    tenant = await db.get(Tenant, scope.tenant_id)

    if payload.customer_id is not None:
        customer = await get_customer(db, scope.tenant_id, payload.customer_id)
        if customer.status == "archived":
            raise HTTPException(status_code=422, detail="This customer is archived; restore them first")
        existing = (await db.execute(select(Renter).where(Renter.customer_id == customer.id))).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "This customer already has a tenant profile. Add a new tenancy to it instead.",
                    "renter_id": str(existing.id),
                },
            )
    else:
        data = payload.customer
        assert data is not None
        if not payload.allow_duplicate_customer:
            exact, _ = await find_matches(db, scope.tenant_id, email=data.email, phone=data.phone)
            if exact:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "message": "A customer with this email or phone number already exists. Select them instead.",
                        "matches": [{"id": str(c.id), "name": c.name, "email": c.email, "phone": c.phone} for c in exact],
                    },
                )
        customer = Customer(
            tenant_id=scope.tenant_id,
            name=data.name.strip(),
            **data.model_dump(include={"customer_type", "legal_name", "billing_address", "tax_identification_number", "notes", "contact_preference"}),
        )
        set_contact(customer, email=data.email, phone=data.phone)
        db.add(customer)
        await db.flush()
        await record_audit_event(
            db, tenant_id=scope.tenant_id, actor_user_id=scope.id, action="customer.created", entity_type="customer",
            entity_id=customer.id,
        )

    renter = Renter(
        tenant_id=scope.tenant_id, customer_id=customer.id, occupation=payload.occupation, employer=payload.employer,
        notes=payload.notes, created_by_user_id=scope.id,
    )
    db.add(renter)
    await db.flush()
    db.add(KycProfile(tenant_id=scope.tenant_id, renter_id=renter.id, legal_name=customer.legal_name or customer.name))
    await record_audit_event(
        db, tenant_id=scope.tenant_id, actor_user_id=scope.id, action="renter.created", entity_type="renter",
        entity_id=renter.id, metadata={"customer_id": str(customer.id)},
    )
    if payload.tenancy is not None:
        await create_tenancy(db, scope, tenant, renter, payload.tenancy)
    await db.flush()
    return renter


async def end_tenancy(db: AsyncSession, scope: AccessScope, tenancy: Tenancy, *, end_date, new_status: str, reason: str | None) -> None:
    if tenancy.status not in ("active", "draft"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"This tenancy is already {tenancy.status}")
    if end_date < tenancy.start_date:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="The end date is before the start date")
    tenancy.status = new_status
    tenancy.end_date = end_date
    tenancy.ended_at = datetime.now(timezone.utc)
    # Stop chasing rent on this tenancy; issued invoices and receipts are untouched.
    for schedule in (
        await db.execute(select(PaymentSchedule).where(PaymentSchedule.tenancy_id == tenancy.id, PaymentSchedule.status.in_(("active", "paused"))))
    ).scalars():
        schedule.end_date = min(schedule.end_date or end_date, end_date)
    still_active = (
        await db.execute(select(Tenancy.id).where(Tenancy.renter_id == tenancy.renter_id, Tenancy.status == "active", Tenancy.id != tenancy.id))
    ).first()
    if still_active is None:
        renter = await db.get(Renter, tenancy.renter_id)
        if renter is not None and renter.status == "active":
            renter.status = "former"
    await record_audit_event(
        db, tenant_id=scope.tenant_id, actor_user_id=scope.id, action="tenancy.ended", entity_type="tenancy",
        entity_id=tenancy.id, metadata={"status": new_status, "end_date": end_date.isoformat(), "reason": reason},
    )
