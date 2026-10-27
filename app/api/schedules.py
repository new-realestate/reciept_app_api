import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.invoices import _ctx
from app.api.invoices import _detail as invoice_detail
from app.core.access import (
    AccessScope,
    Perm,
    ensure_document_visible,
    get_scope,
    require_perm,
    scope_documents,
    validate_doc_context,
)
from app.core.audit import record_audit_event
from app.db.session import get_db
from app.models.customer import Customer
from app.models.invoice import Invoice
from app.models.schedule import PaymentSchedule, ScheduleOccurrence
from app.schemas.documents import InvoiceDetailOut, LineItemIn, Page
from app.schemas.schedules import (
    CreateInvoiceFromOccurrence,
    OccurrenceOut,
    ScheduleCreate,
    ScheduleDetailOut,
    ScheduleOut,
    ScheduleReminderSettings,
    ScheduleUpdate,
)
from app.services import invoices as invoice_svc
from app.services import notifications as notif
from app.services import reminders
from app.services import schedules as svc
from app.services.customers import get_or_create_customer

router = APIRouter(prefix="/schedules", tags=["schedules"])


async def _occurrence_outs(db: AsyncSession, occs: list[ScheduleOccurrence], today: date) -> list[OccurrenceOut]:
    ids = [o.id for o in occs]
    invoices = {}
    if ids:
        rows = (await db.execute(select(Invoice).where(Invoice.schedule_occurrence_id.in_(ids)))).scalars().all()
        invoices = {inv.schedule_occurrence_id: inv for inv in rows}
    out = []
    for o in occs:
        inv = invoices.get(o.id)
        out.append(OccurrenceOut(
            id=o.id, schedule_id=o.schedule_id, seq=o.seq, due_date=o.due_date, amount_cents=o.amount_cents,
            amount_paid_cents=o.amount_paid_cents, status=o.status, invoice_id=inv.id if inv else None,
            invoice_number=inv.number if inv else None, days_until_due=(o.due_date - today).days,
        ))
    return out


async def _schedule_out(db: AsyncSession, s: PaymentSchedule, *, detail: bool = False, today: date | None = None):
    name = (await db.execute(select(Customer.name).where(Customer.id == s.customer_id))).scalar_one_or_none()
    nxt = (
        await db.execute(
            select(ScheduleOccurrence).where(ScheduleOccurrence.schedule_id == s.id, ScheduleOccurrence.status.in_(("expected", "invoiced")))
            .order_by(ScheduleOccurrence.due_date).limit(1)
        )
    ).scalar_one_or_none()
    data: dict[str, Any] = dict(
        id=s.id, customer_id=s.customer_id, customer_name=name, description=s.description, reference_label=s.reference_label,
        amount_cents=s.amount_cents, currency=s.currency, frequency=s.frequency, first_due_date=s.first_due_date,
        end_date=s.end_date, max_occurrences=s.max_occurrences, status=s.status, reminders_enabled=s.reminders_enabled,
        reminder_overrides=s.reminder_overrides, auto_invoice=s.auto_invoice,
        next_due_date=nxt.due_date if nxt else None, next_amount_cents=nxt.amount_cents if nxt else None, created_at=s.created_at,
        property_id=s.property_id, unit_id=s.unit_id, tenancy_id=s.tenancy_id,
    )
    if not detail:
        return ScheduleOut(**data)
    occs = (await db.execute(select(ScheduleOccurrence).where(ScheduleOccurrence.schedule_id == s.id).order_by(ScheduleOccurrence.seq))).scalars().all()
    return ScheduleDetailOut(**data, occurrences=await _occurrence_outs(db, list(occs), today or date.today()))


async def _get_schedule(db: AsyncSession, scope: AccessScope, schedule_id: uuid.UUID, *, lock: bool = False) -> PaymentSchedule:
    schedule = await svc.get_schedule(db, scope.tenant_id, schedule_id, lock=lock)
    return ensure_document_visible(scope, schedule, "Schedule")


@router.post("", response_model=ScheduleDetailOut, status_code=status.HTTP_201_CREATED)
async def create_schedule(payload: ScheduleCreate, current_user: AccessScope = Depends(require_perm(Perm.invoices_issue)), db: AsyncSession = Depends(get_db)):
    tenant, user = await _ctx(db, current_user)
    customer = await get_or_create_customer(
        db, tenant.id, customer_id=payload.customer_id, name=payload.customer_name,
        email=payload.customer_email, phone=payload.customer_phone,
    )
    ctx = await validate_doc_context(
        db, current_user, property_id=payload.property_id, unit_id=payload.unit_id, tenancy_id=payload.tenancy_id,
        customer_id=customer.id,
    )
    schedule = PaymentSchedule(
        property_id=ctx.property_id, unit_id=ctx.unit_id, tenancy_id=ctx.tenancy_id,
        tenant_id=tenant.id, customer_id=customer.id, description=payload.description.strip(),
        reference_label=payload.reference_label, amount_cents=payload.amount_cents,
        currency=(payload.currency or tenant.default_currency).upper(), frequency=payload.frequency,
        first_due_date=payload.first_due_date, end_date=payload.end_date, max_occurrences=payload.max_occurrences,
        reminders_enabled=payload.reminders_enabled, auto_invoice=payload.auto_invoice, created_by_user_id=user.id,
    )
    db.add(schedule)
    await db.flush()
    today = notif.tenant_today(tenant)
    occs = await svc.generate_occurrences(db, schedule, today=today)
    for occ in occs:
        await reminders.plan_occurrence_reminders(db, tenant, schedule, occ, customer)
    await record_audit_event(db, tenant_id=tenant.id, actor_user_id=user.id, action="schedule.created",
                             entity_type="schedule", entity_id=schedule.id)
    await db.commit()
    await db.refresh(schedule)
    return await _schedule_out(db, schedule, detail=True, today=today)


@router.get("", response_model=Page[ScheduleOut])
async def list_schedules(
    status_filter: str | None = Query(default=None, alias="status"),
    customer_id: uuid.UUID | None = None,
    property_id: uuid.UUID | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: AccessScope = Depends(get_scope),
    db: AsyncSession = Depends(get_db),
):
    conds = [PaymentSchedule.tenant_id == current_user.tenant_id]
    if status_filter:
        conds.append(PaymentSchedule.status == status_filter)
    if customer_id:
        conds.append(PaymentSchedule.customer_id == customer_id)
    if property_id:
        conds.append(PaymentSchedule.property_id == property_id)
    base = scope_documents(select(PaymentSchedule).where(*conds), PaymentSchedule, current_user)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (await db.execute(base.order_by(PaymentSchedule.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    return Page[ScheduleOut](items=[await _schedule_out(db, s) for s in rows], total=total, limit=limit, offset=offset)


@router.get("/{schedule_id}", response_model=ScheduleDetailOut)
async def get_schedule(schedule_id: uuid.UUID, current_user: AccessScope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    tenant, _ = await _ctx(db, current_user)
    s = await _get_schedule(db, current_user, schedule_id)
    return await _schedule_out(db, s, detail=True, today=notif.tenant_today(tenant))


@router.patch("/{schedule_id}", response_model=ScheduleDetailOut)
async def update_schedule(
    schedule_id: uuid.UUID, payload: ScheduleUpdate, current_user: AccessScope = Depends(require_perm(Perm.invoices_issue)), db: AsyncSession = Depends(get_db)
):
    tenant, user = await _ctx(db, current_user)
    s = await _get_schedule(db, current_user, schedule_id, lock=True)
    if s.status in ("cancelled", "completed"):
        raise HTTPException(status_code=409, detail=f"A {s.status} schedule cannot be edited")
    data = payload.model_dump(exclude_unset=True)
    for field in ("description", "reference_label", "end_date", "max_occurrences", "reminders_enabled", "auto_invoice"):
        if field in data:
            setattr(s, field, data[field])
    if s.end_date is not None and s.end_date < s.first_due_date:
        raise HTTPException(status_code=422, detail="end_date cannot be before first_due_date")
    if "amount_cents" in data and data["amount_cents"] != s.amount_cents:
        s.amount_cents = data["amount_cents"]
        # Only future expectations without an invoice change; documents already issued are untouched.
        for occ in (await db.execute(select(ScheduleOccurrence).where(
            ScheduleOccurrence.schedule_id == s.id, ScheduleOccurrence.status == "expected"))).scalars():
            occ.amount_cents = s.amount_cents
    await db.flush()
    customer = (await db.execute(select(Customer).where(Customer.id == s.customer_id))).scalar_one()
    today = notif.tenant_today(tenant)
    await svc.generate_occurrences(db, s, today=today)
    if "reminders_enabled" in data and not s.reminders_enabled:
        await _cancel_schedule_events(db, s, "reminders disabled for this schedule")
    await record_audit_event(db, tenant_id=tenant.id, actor_user_id=user.id, action="schedule.updated",
                             entity_type="schedule", entity_id=s.id)
    await db.commit()
    await db.refresh(s)
    _ = customer
    return await _schedule_out(db, s, detail=True, today=today)


async def _cancel_schedule_events(db: AsyncSession, s: PaymentSchedule, reason: str) -> None:
    occ_ids = (await db.execute(select(ScheduleOccurrence.id).where(ScheduleOccurrence.schedule_id == s.id))).scalars().all()
    for oid in occ_ids:
        await notif.cancel_pending_events(db, subject_type="occurrence", subject_id=oid, reason=reason)


async def _transition(schedule_id: uuid.UUID, new_status: str, current_user: AccessScope, db: AsyncSession):
    tenant, user = await _ctx(db, current_user)
    s = await _get_schedule(db, current_user, schedule_id, lock=True)
    today = notif.tenant_today(tenant)
    await svc.set_status(db, tenant=tenant, user_id=user.id, schedule=s, new_status=new_status, today=today)
    if new_status in ("paused", "cancelled"):
        await _cancel_schedule_events(db, s, f"schedule {new_status}")
    if new_status == "cancelled":
        for occ in (await db.execute(select(ScheduleOccurrence).where(
            ScheduleOccurrence.schedule_id == s.id, ScheduleOccurrence.status == "expected"))).scalars():
            occ.status = "skipped"
    if new_status == "active":
        customer = (await db.execute(select(Customer).where(Customer.id == s.customer_id))).scalar_one()
        for occ in (await db.execute(select(ScheduleOccurrence).where(
            ScheduleOccurrence.schedule_id == s.id, ScheduleOccurrence.status == "expected"))).scalars():
            await reminders.plan_occurrence_reminders(db, tenant, s, occ, customer)
    await db.commit()
    await db.refresh(s)
    return await _schedule_out(db, s, detail=True, today=today)


@router.post("/{schedule_id}/pause", response_model=ScheduleDetailOut)
async def pause_schedule(schedule_id: uuid.UUID, current_user: AccessScope = Depends(require_perm(Perm.invoices_issue)), db: AsyncSession = Depends(get_db)):
    return await _transition(schedule_id, "paused", current_user, db)


@router.post("/{schedule_id}/resume", response_model=ScheduleDetailOut)
async def resume_schedule(schedule_id: uuid.UUID, current_user: AccessScope = Depends(require_perm(Perm.invoices_issue)), db: AsyncSession = Depends(get_db)):
    return await _transition(schedule_id, "active", current_user, db)


@router.post("/{schedule_id}/cancel", response_model=ScheduleDetailOut)
async def cancel_schedule(schedule_id: uuid.UUID, current_user: AccessScope = Depends(require_perm(Perm.invoices_issue)), db: AsyncSession = Depends(get_db)):
    return await _transition(schedule_id, "cancelled", current_user, db)


@router.patch("/{schedule_id}/reminders", response_model=ScheduleDetailOut)
async def update_schedule_reminders(
    schedule_id: uuid.UUID, payload: ScheduleReminderSettings, current_user: AccessScope = Depends(require_perm(Perm.invoices_issue)), db: AsyncSession = Depends(get_db)
):
    tenant, user = await _ctx(db, current_user)
    s = await _get_schedule(db, current_user, schedule_id, lock=True)
    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    try:
        overrides = reminders.validate_settings({k: v for k, v in data.items() if k != "enabled"})
    except reminders.SettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if "enabled" in data:
        s.reminders_enabled = data["enabled"]
    if overrides:
        s.reminder_overrides = {**(s.reminder_overrides or {}), **overrides}
    await _cancel_schedule_events(db, s, "reminder settings changed")
    customer = (await db.execute(select(Customer).where(Customer.id == s.customer_id))).scalar_one()
    if s.status == "active" and s.reminders_enabled:
        for occ in (await db.execute(select(ScheduleOccurrence).where(
            ScheduleOccurrence.schedule_id == s.id, ScheduleOccurrence.status == "expected"))).scalars():
            await reminders.plan_occurrence_reminders(db, tenant, s, occ, customer)
    await record_audit_event(db, tenant_id=tenant.id, actor_user_id=user.id, action="schedule.reminders_updated",
                             entity_type="schedule", entity_id=s.id, metadata=data)
    await db.commit()
    await db.refresh(s)
    return await _schedule_out(db, s, detail=True, today=notif.tenant_today(tenant))


@router.post("/occurrences/{occurrence_id}/create-invoice", response_model=InvoiceDetailOut, status_code=status.HTTP_201_CREATED)
async def create_invoice_for_occurrence(
    occurrence_id: uuid.UUID,
    payload: CreateInvoiceFromOccurrence | None = None,
    current_user: AccessScope = Depends(require_perm(Perm.invoices_issue)),
    db: AsyncSession = Depends(get_db),
):
    """Explicitly turns an expected payment into an invoice. One invoice per occurrence, enforced by the database."""
    payload = payload or CreateInvoiceFromOccurrence()
    tenant, user = await _ctx(db, current_user)
    occ = await svc.get_occurrence(db, tenant.id, occurrence_id, lock=True)
    schedule = await _get_schedule(db, current_user, occ.schedule_id)
    if occ.status != "expected":
        raise HTTPException(status_code=409, detail=f"This occurrence is {occ.status}; an invoice cannot be created for it")
    if schedule.status != "active":
        raise HTTPException(status_code=409, detail=f"The schedule is {schedule.status}")
    if (await db.execute(select(Invoice.id).where(Invoice.schedule_occurrence_id == occ.id))).first():
        raise HTTPException(status_code=409, detail="An invoice already exists for this occurrence")

    customer = (await db.execute(select(Customer).where(Customer.id == schedule.customer_id))).scalar_one()
    today = notif.tenant_today(tenant)
    invoice = await invoice_svc.create_draft(
        db, tenant=tenant, user=user, customer=customer, title=schedule.description, currency=schedule.currency,
        issue_date=min(today, occ.due_date), due_date=payload.due_date or occ.due_date,
        lines=[LineItemIn(description=schedule.description, quantity=Decimal(1), unit_price_cents=occ.amount_cents)],
        payment_instructions=payload.payment_instructions, reminders_enabled=schedule.reminders_enabled,
        schedule_occurrence_id=occ.id,
        property_id=schedule.property_id, unit_id=schedule.unit_id, tenancy_id=schedule.tenancy_id,
    )
    occ.status = "invoiced"
    # The invoice now carries the reminders for this obligation; drop the occurrence's own.
    await notif.cancel_pending_events(db, subject_type="occurrence", subject_id=occ.id, reason="invoice created for this occurrence")
    if payload.issue_now:
        await invoice_svc.issue_invoice(db, tenant=tenant, user=user, invoice=invoice, customer=customer)
        await reminders.plan_invoice_reminders(db, tenant, invoice, customer)
    await db.commit()
    await db.refresh(invoice)
    return await invoice_detail(db, tenant, invoice)


@router.post("/occurrences/{occurrence_id}/skip", response_model=OccurrenceOut)
async def skip_occurrence(occurrence_id: uuid.UUID, current_user: AccessScope = Depends(require_perm(Perm.invoices_issue)), db: AsyncSession = Depends(get_db)):
    tenant, user = await _ctx(db, current_user)
    occ = await svc.get_occurrence(db, tenant.id, occurrence_id, lock=True)
    await _get_schedule(db, current_user, occ.schedule_id)  # 404 unless the caller may see the schedule
    if occ.status != "expected":
        raise HTTPException(status_code=409, detail=f"This occurrence is {occ.status} and cannot be skipped")
    occ.status = "skipped"
    await notif.cancel_pending_events(db, subject_type="occurrence", subject_id=occ.id, reason="occurrence skipped")
    await record_audit_event(db, tenant_id=tenant.id, actor_user_id=user.id, action="schedule.occurrence_skipped",
                             entity_type="schedule", entity_id=occ.schedule_id, metadata={"occurrence_id": str(occ.id)})
    await db.commit()
    return (await _occurrence_outs(db, [occ], notif.tenant_today(tenant)))[0]
