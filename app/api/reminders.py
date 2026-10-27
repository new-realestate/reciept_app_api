"""Notification history/retry, reminder settings, upcoming/overdue payments and the dashboard summary."""
import uuid
from collections import defaultdict
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.invoices import _ctx
from app.core.access import AccessScope, get_scope, require_manager_scope, scope_documents
from app.core.audit import record_audit_event
from app.db.session import get_db
from app.models.audit import AuditLog
from app.models.customer import Customer
from app.models.invoice import Invoice, InvoicePayment
from app.models.notification import NotificationEvent
from app.models.quotation import Quotation
from app.models.receipt import Receipt
from app.models.schedule import PaymentSchedule, ScheduleOccurrence
from app.schemas.documents import NotificationEventOut, Page
from app.schemas.schedules import ReminderSettingsOut, ReminderSettingsUpdate, UpcomingPaymentOut
from app.services import notifications as notif
from app.services import reminders

router = APIRouter(tags=["reminders"])


# ---------------------------------------------------------------- notifications


def _visible_events(scope: AccessScope):
    """Restricts notification events to subjects (invoice / quotation / occurrence) the caller can see."""
    if scope.is_manager:
        return NotificationEvent.tenant_id == scope.tenant_id
    invoice_ids = scope_documents(select(Invoice.id), Invoice, scope)
    quotation_ids = scope_documents(select(Quotation.id), Quotation, scope)
    occurrence_ids = scope_documents(
        select(ScheduleOccurrence.id).join(PaymentSchedule, PaymentSchedule.id == ScheduleOccurrence.schedule_id),
        PaymentSchedule, scope,
    )
    return and_(
        NotificationEvent.tenant_id == scope.tenant_id,
        or_(
            and_(NotificationEvent.subject_type == "invoice", NotificationEvent.subject_id.in_(invoice_ids)),
            and_(NotificationEvent.subject_type == "quotation", NotificationEvent.subject_id.in_(quotation_ids)),
            and_(NotificationEvent.subject_type == "occurrence", NotificationEvent.subject_id.in_(occurrence_ids)),
        ),
    )


@router.get("/notifications", response_model=Page[NotificationEventOut])
async def list_notifications(
    status_filter: str | None = Query(default=None, alias="status"),
    kind: str | None = None,
    subject_type: str | None = None,
    subject_id: uuid.UUID | None = None,
    channel: str | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: AccessScope = Depends(get_scope),
    db: AsyncSession = Depends(get_db),
):
    conds = [_visible_events(current_user)]
    for column, value in ((NotificationEvent.status, status_filter), (NotificationEvent.kind, kind),
                          (NotificationEvent.subject_type, subject_type), (NotificationEvent.subject_id, subject_id),
                          (NotificationEvent.channel, channel)):
        if value:
            conds.append(column == value)
    total = (await db.execute(select(func.count()).select_from(NotificationEvent).where(*conds))).scalar_one()
    rows = (
        await db.execute(select(NotificationEvent).where(*conds).order_by(NotificationEvent.scheduled_for.desc()).limit(limit).offset(offset))
    ).scalars().all()
    return Page[NotificationEventOut](items=await notif.events_with_attempts(db, list(rows)), total=total, limit=limit, offset=offset)


async def _event(db: AsyncSession, scope: AccessScope, event_id: uuid.UUID) -> NotificationEvent:
    event = (
        await db.execute(select(NotificationEvent).where(NotificationEvent.id == event_id, _visible_events(scope)))
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    return event


@router.post("/notifications/{event_id}/retry", response_model=NotificationEventOut)
async def retry_notification(event_id: uuid.UUID, current_user: AccessScope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Retry a failed (or skipped) notification. The subject is re-checked first, so a paid/cancelled invoice still won't be chased."""
    event = await _event(db, current_user, event_id)
    try:
        await notif.retry_event(db, event)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return (await notif.events_with_attempts(db, [event]))[0]


@router.post("/notifications/{event_id}/cancel", response_model=NotificationEventOut)
async def cancel_notification(event_id: uuid.UUID, current_user: AccessScope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Stop a scheduled (not yet sent) reminder."""
    event = await _event(db, current_user, event_id)
    if event.status not in ("scheduled", "failed"):
        raise HTTPException(status_code=409, detail=f"A {event.status} notification cannot be cancelled")
    event.status = "cancelled"
    event.skip_reason = "cancelled by user"
    await record_audit_event(db, tenant_id=event.tenant_id, actor_user_id=current_user.id, action="reminder.cancelled",
                             entity_type="notification", entity_id=event.id)
    await db.commit()
    return (await notif.events_with_attempts(db, [event]))[0]


# ---------------------------------------------------------------- settings


def _settings_out(tenant) -> ReminderSettingsOut:
    return ReminderSettingsOut(**reminders.resolve_settings(tenant), timezone=tenant.timezone)


@router.get("/reminder-settings", response_model=ReminderSettingsOut)
async def get_reminder_settings(current_user: AccessScope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    tenant, _ = await _ctx(db, current_user)
    return _settings_out(tenant)


@router.put("/reminder-settings", response_model=ReminderSettingsOut)
async def put_reminder_settings(
    payload: ReminderSettingsUpdate, current_user: AccessScope = Depends(require_manager_scope), db: AsyncSession = Depends(get_db)
):
    """Business-wide defaults. Applies to reminders planned from now on; open invoices are re-planned."""
    tenant, user = await _ctx(db, current_user)
    try:
        cleaned = reminders.validate_settings(payload.model_dump(exclude_none=True))
    except reminders.SettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    tenant.reminder_settings = {**(tenant.reminder_settings or {}), **cleaned}
    await db.flush()
    open_invoices = (await db.execute(select(Invoice).where(Invoice.tenant_id == tenant.id, Invoice.status == "issued"))).scalars().all()
    for inv in open_invoices:
        if inv.reminder_overrides is None:
            customer = (await db.execute(select(Customer).where(Customer.id == inv.customer_id))).scalar_one()
            await reminders.replan_invoice_reminders(db, tenant, inv, customer)
    await record_audit_event(db, tenant_id=tenant.id, actor_user_id=user.id, action="reminder.settings_updated",
                             entity_type="tenant", entity_id=tenant.id, metadata=cleaned)
    await db.commit()
    await db.refresh(tenant)
    return _settings_out(tenant)


# ---------------------------------------------------------------- upcoming / overdue


def _status_for(due: date, today: date) -> Literal["overdue", "due_today", "upcoming"]:
    return "overdue" if due < today else "due_today" if due == today else "upcoming"


async def collect_payments(
    db: AsyncSession, tenant_id: uuid.UUID, today: date, *, customer_id: uuid.UUID | None = None,
    schedule_id: uuid.UUID | None = None, date_from: date | None = None, date_to: date | None = None,
    scope: AccessScope | None = None,
) -> list[UpcomingPaymentOut]:
    """Open invoices (balance owed) plus expected schedule payments that have no invoice yet."""
    out: list[UpcomingPaymentOut] = []

    inv_stmt = (
        select(Invoice, Customer.name, ScheduleOccurrence.schedule_id)
        .join(Customer, Customer.id == Invoice.customer_id)
        .outerjoin(ScheduleOccurrence, ScheduleOccurrence.id == Invoice.schedule_occurrence_id)
        .where(Invoice.tenant_id == tenant_id, Invoice.status == "issued", Invoice.total_cents - Invoice.amount_paid_cents > 0)
    )
    if scope is not None:
        inv_stmt = scope_documents(inv_stmt, Invoice, scope)
    if customer_id:
        inv_stmt = inv_stmt.where(Invoice.customer_id == customer_id)
    if schedule_id:
        inv_stmt = inv_stmt.where(ScheduleOccurrence.schedule_id == schedule_id)
    if date_from:
        inv_stmt = inv_stmt.where(Invoice.due_date >= date_from)
    if date_to:
        inv_stmt = inv_stmt.where(Invoice.due_date <= date_to)
    for inv, name, sched_id in (await db.execute(inv_stmt)).all():
        due = inv.due_date
        out.append(UpcomingPaymentOut(
            kind="invoice", id=inv.id, customer_id=inv.customer_id, customer_name=name,
            description=inv.title or f"Invoice {inv.number}", due_date=due,
            amount_cents=inv.total_cents - inv.amount_paid_cents, currency=inv.currency, status=_status_for(due, today),
            days_until_due=(due - today).days, days_overdue=max((today - due).days, 0), invoice_id=inv.id,
            invoice_number=inv.number, schedule_id=sched_id, occurrence_id=inv.schedule_occurrence_id,
        ))

    occ_stmt = (
        select(ScheduleOccurrence, PaymentSchedule, Customer.name)
        .join(PaymentSchedule, PaymentSchedule.id == ScheduleOccurrence.schedule_id)
        .join(Customer, Customer.id == PaymentSchedule.customer_id)
        .where(ScheduleOccurrence.tenant_id == tenant_id, ScheduleOccurrence.status == "expected", PaymentSchedule.status == "active")
    )
    if scope is not None:
        occ_stmt = scope_documents(occ_stmt, PaymentSchedule, scope)
    if customer_id:
        occ_stmt = occ_stmt.where(PaymentSchedule.customer_id == customer_id)
    if schedule_id:
        occ_stmt = occ_stmt.where(PaymentSchedule.id == schedule_id)
    if date_from:
        occ_stmt = occ_stmt.where(ScheduleOccurrence.due_date >= date_from)
    if date_to:
        occ_stmt = occ_stmt.where(ScheduleOccurrence.due_date <= date_to)
    for occ, sched, name in (await db.execute(occ_stmt)).all():
        due = occ.due_date
        out.append(UpcomingPaymentOut(
            kind="occurrence", id=occ.id, customer_id=sched.customer_id, customer_name=name, description=sched.description,
            due_date=due, amount_cents=occ.amount_cents, currency=sched.currency, status=_status_for(due, today),
            days_until_due=(due - today).days, days_overdue=max((today - due).days, 0), schedule_id=sched.id,
            occurrence_id=occ.id,
        ))
    out.sort(key=lambda r: (r.due_date, r.description))
    return out


@router.get("/payments/upcoming", response_model=Page[UpcomingPaymentOut])
async def upcoming_payments(
    status_filter: Literal["all", "overdue", "due_today", "upcoming"] = Query(default="all", alias="status"),
    window_days: int = Query(default=30, ge=1, le=365, description="How far ahead 'upcoming' looks (ignored when date_to is given)"),
    customer_id: uuid.UUID | None = None,
    schedule_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: AccessScope = Depends(get_scope),
    db: AsyncSession = Depends(get_db),
):
    tenant, _ = await _ctx(db, current_user)
    today = notif.tenant_today(tenant)
    rows = await collect_payments(db, tenant.id, today, customer_id=customer_id, schedule_id=schedule_id,
                                  date_from=date_from, date_to=date_to, scope=current_user)
    horizon = today + timedelta(days=window_days)
    rows = [r for r in rows if r.status == "overdue" or date_to is not None or r.due_date <= horizon]
    if status_filter != "all":
        rows = [r for r in rows if r.status == status_filter]
    return Page[UpcomingPaymentOut](items=rows[offset: offset + limit], total=len(rows), limit=limit, offset=offset)


@router.get("/payments/overdue", response_model=Page[UpcomingPaymentOut])
async def overdue_payments(
    customer_id: uuid.UUID | None = None,
    schedule_id: uuid.UUID | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: AccessScope = Depends(get_scope),
    db: AsyncSession = Depends(get_db),
):
    tenant, _ = await _ctx(db, current_user)
    today = notif.tenant_today(tenant)
    rows = [r for r in await collect_payments(db, tenant.id, today, customer_id=customer_id, schedule_id=schedule_id, scope=current_user)
            if r.status == "overdue"]
    rows.sort(key=lambda r: -r.days_overdue)
    return Page[UpcomingPaymentOut](items=rows[offset: offset + limit], total=len(rows), limit=limit, offset=offset)


# ---------------------------------------------------------------- dashboard


class CurrencyTotals(BaseModel):
    invoiced_cents: int = 0
    received_cents: int = 0
    received_on_invoices_cents: int = 0
    outstanding_cents: int = 0
    overdue_cents: int = 0
    overdue_count: int = 0
    due_today_cents: int = 0
    due_today_count: int = 0
    upcoming_7_cents: int = 0
    upcoming_7_count: int = 0
    upcoming_30_cents: int = 0
    upcoming_30_count: int = 0
    pending_quotations_cents: int = 0
    pending_quotations_count: int = 0


class RecentReceipt(BaseModel):
    id: uuid.UUID
    reference_number: str
    customer_name: str | None
    amount_cents: int
    currency: str
    paid_at: date


class ActivityItem(BaseModel):
    action: str
    entity_type: str
    entity_id: uuid.UUID | None
    created_at: str


class DashboardSummary(BaseModel):
    period: str
    period_start: date | None
    period_end: date
    as_of: date
    default_currency: str
    by_currency: dict[str, CurrencyTotals]
    recent_payments: list[RecentReceipt]
    recent_activity: list[ActivityItem]


def _period_range(period: str, today: date) -> tuple[date | None, date]:
    if period == "this_month":
        return today.replace(day=1), today
    if period == "last_30_days":
        return today - timedelta(days=29), today
    if period == "this_year":
        return today.replace(month=1, day=1), today
    if period == "all":
        return None, today
    raise HTTPException(status_code=422, detail="period must be one of this_month, last_30_days, this_year, all")


@router.get("/dashboard/summary", response_model=DashboardSummary)
async def dashboard_summary(
    period: str = Query(default="this_month"),
    current_user: AccessScope = Depends(get_scope),
    db: AsyncSession = Depends(get_db),
):
    """Period-bound figures (invoiced, received) are kept apart from point-in-time ones
    (outstanding, overdue, upcoming), and never mixed: invoiced is not money received."""
    tenant, _ = await _ctx(db, current_user)
    today = notif.tenant_today(tenant)
    start, end = _period_range(period, today)
    totals: dict[str, CurrencyTotals] = defaultdict(CurrencyTotals)

    inv_conds = [Invoice.tenant_id == tenant.id, Invoice.status == "issued", Invoice.issue_date <= end]
    if start:
        inv_conds.append(Invoice.issue_date >= start)
    inv_q = scope_documents(select(Invoice.currency, func.sum(Invoice.total_cents)).where(*inv_conds), Invoice, current_user)
    for cur, amount in (await db.execute(inv_q.group_by(Invoice.currency))).all():
        totals[cur].invoiced_cents = int(amount or 0)

    rc_conds = [Receipt.tenant_id == tenant.id, Receipt.status == "issued", Receipt.paid_at <= end]
    if start:
        rc_conds.append(Receipt.paid_at >= start)
    rc_q = scope_documents(select(Receipt.currency, func.sum(Receipt.amount_cents)).where(*rc_conds), Receipt, current_user)
    for cur, amount in (await db.execute(rc_q.group_by(Receipt.currency))).all():
        totals[cur].received_cents = int(amount or 0)

    ip_conds = [InvoicePayment.tenant_id == tenant.id, InvoicePayment.paid_at <= end]
    if start:
        ip_conds.append(InvoicePayment.paid_at >= start)
    ip_q = scope_documents(
        select(Invoice.currency, func.sum(InvoicePayment.amount_cents)).join(Invoice, Invoice.id == InvoicePayment.invoice_id)
        .where(*ip_conds), Invoice, current_user,
    )
    for cur, amount in (await db.execute(ip_q.group_by(Invoice.currency))).all():
        totals[cur].received_on_invoices_cents = int(amount or 0)

    for row in await collect_payments(db, tenant.id, today, scope=current_user):
        t = totals[row.currency]
        if row.kind == "invoice":
            t.outstanding_cents += row.amount_cents
        if row.status == "overdue":
            t.overdue_cents += row.amount_cents
            t.overdue_count += 1
        elif row.status == "due_today":
            t.due_today_cents += row.amount_cents
            t.due_today_count += 1
        if row.status != "overdue" and row.days_until_due <= 7:
            t.upcoming_7_cents += row.amount_cents
            t.upcoming_7_count += 1
        if row.status != "overdue" and row.days_until_due <= 30:
            t.upcoming_30_cents += row.amount_cents
            t.upcoming_30_count += 1

    q_q = scope_documents(
        select(Quotation.currency, func.sum(Quotation.total_cents), func.count()).where(
            Quotation.tenant_id == tenant.id, Quotation.status.in_(("sent", "viewed")), Quotation.valid_until >= today
        ), Quotation, current_user,
    )
    for cur, amount, count in (await db.execute(q_q.group_by(Quotation.currency))).all():
        totals[cur].pending_quotations_cents = int(amount or 0)
        totals[cur].pending_quotations_count = int(count or 0)

    totals.setdefault(tenant.default_currency, CurrencyTotals())

    recent = (await db.execute(
        scope_documents(select(Receipt, Customer.name).join(Customer, Customer.id == Receipt.customer_id), Receipt, current_user)
        .order_by(Receipt.created_at.desc()).limit(8)
    )).all()
    # Business-wide activity is for managers; staff see their own.
    activity_q = select(AuditLog).where(AuditLog.tenant_id == tenant.id)
    if not current_user.is_manager:
        activity_q = activity_q.where(AuditLog.actor_user_id == current_user.id)
    activity = (await db.execute(activity_q.order_by(AuditLog.created_at.desc()).limit(12))).scalars().all()

    return DashboardSummary(
        period=period, period_start=start, period_end=end, as_of=today, default_currency=tenant.default_currency,
        by_currency=dict(totals),
        recent_payments=[RecentReceipt(id=r.id, reference_number=r.reference_number, customer_name=n, amount_cents=r.amount_cents,
                                       currency=r.currency, paid_at=r.paid_at) for r, n in recent],
        recent_activity=[ActivityItem(action=a.action, entity_type=a.entity_type, entity_id=a.entity_id,
                                      created_at=a.created_at.isoformat()) for a in activity],
    )
