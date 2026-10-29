"""Background work: occurrence generation, auto-invoicing, quotation expiry, reminder planning and sending.

One in-process APScheduler job (this app runs a single instance). Every step is idempotent
(unique keys / status checks), so overlapping or repeated runs and restarts are safe, and
the same `run_cycle` can later be moved to a cron job unchanged.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.customer import Customer
from app.models.invoice import Invoice
from app.models.schedule import PaymentSchedule, ScheduleOccurrence
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.documents import LineItemIn
from app.services import invoices as invoice_svc
from app.services import notifications as notif
from app.services import quotations as quotation_svc
from app.services import reminders
from app.services import schedules as schedule_svc

logger = logging.getLogger(__name__)

AUTO_INVOICE_LEAD_DAYS = 7


async def _tenants(db: AsyncSession) -> dict:
    return {t.id: t for t in (await db.execute(select(Tenant).where(Tenant.deleted_at.is_(None)))).scalars().all()}


async def generate_all_occurrences(db: AsyncSession, tenants: dict, now: datetime) -> int:
    count = 0
    for s in (await db.execute(select(PaymentSchedule).where(PaymentSchedule.status == "active"))).scalars().all():
        tenant = tenants.get(s.tenant_id)
        if tenant is None:
            continue
        count += len(await schedule_svc.generate_occurrences(db, s, today=notif.tenant_today(tenant, now)))
    return count


async def auto_invoice(db: AsyncSession, tenants: dict, now: datetime) -> int:
    """Only for schedules that opted in (auto_invoice): issue the invoice shortly before the due date."""
    created = 0
    rows = (
        await db.execute(
            select(ScheduleOccurrence, PaymentSchedule)
            .join(PaymentSchedule, PaymentSchedule.id == ScheduleOccurrence.schedule_id)
            .where(PaymentSchedule.status == "active", PaymentSchedule.auto_invoice.is_(True),
                   ScheduleOccurrence.status == "expected")
        )
    ).all()
    for occ, schedule in rows:
        tenant = tenants.get(schedule.tenant_id)
        if tenant is None:
            continue
        today = notif.tenant_today(tenant, now)
        if occ.due_date > today + timedelta(days=AUTO_INVOICE_LEAD_DAYS):
            continue
        if (await db.execute(select(Invoice.id).where(Invoice.schedule_occurrence_id == occ.id))).first():
            continue
        user = (await db.execute(select(User).where(User.id == schedule.created_by_user_id))).scalar_one_or_none()
        customer = (await db.execute(select(Customer).where(Customer.id == schedule.customer_id))).scalar_one()
        if user is None or (not customer.email and not customer.phone):
            continue
        try:
            async with db.begin_nested():
                invoice = await invoice_svc.create_draft(
                    db, tenant=tenant, user=user, customer=customer, title=schedule.description, currency=schedule.currency,
                    issue_date=min(today, occ.due_date), due_date=occ.due_date,
                    lines=[LineItemIn(description=schedule.description, quantity=Decimal(1), unit_price_cents=occ.amount_cents)],
                    reminders_enabled=schedule.reminders_enabled, schedule_occurrence_id=occ.id,
                    property_id=schedule.property_id, unit_id=schedule.unit_id, tenancy_id=schedule.tenancy_id,
                )
                await invoice_svc.issue_invoice(db, tenant=tenant, user=user, invoice=invoice, customer=customer)
                occ.status = "invoiced"
                await notif.cancel_pending_events(db, subject_type="occurrence", subject_id=occ.id, reason="invoice created for this occurrence")
        except Exception:  # noqa: BLE001 - one bad schedule must not stop the cycle
            logger.exception("Auto-invoice failed for occurrence %s", occ.id)
            continue
        await reminders.plan_invoice_reminders(db, tenant, invoice, customer, now=now)
        await notif.send_now(db, tenant_id=tenant.id, kind="invoice_issued", subject_type="invoice", subject_id=invoice.id,
                             channels=reminders.resolve_settings(tenant, schedule.reminder_overrides)["channels"])
        created += 1
    return created


async def run_cycle(session_factory, *, now: datetime | None = None) -> dict:
    """One full pass. Each step commits separately and failures are logged, never raised."""
    now = now or notif.utcnow()
    stats: dict = {}

    async def step(name: str, fn):
        async with session_factory() as db:
            try:
                tenants = await _tenants(db)
                stats[name] = await fn(db, tenants)
                await db.commit()
            except Exception:  # noqa: BLE001
                await db.rollback()
                logger.exception("Scheduler step %s failed", name)
                stats[name] = "error"

    async def expire(db, tenants):
        return await quotation_svc.expire_due(db, today_by_tenant={tid: notif.tenant_today(t, now) for tid, t in tenants.items()})

    await step("occurrences", lambda db, t: generate_all_occurrences(db, t, now))
    await step("auto_invoice", lambda db, t: auto_invoice(db, t, now))
    await step("quotations_expired", expire)
    await step("reminders_planned", lambda db, t: reminders.plan_all(db, now=now))
    await step("schedules_completed", lambda db, t: schedule_svc.complete_finished(db))

    async with session_factory() as db:
        try:
            stats["notifications_processed"] = await notif.process_due_events(db, now=now)
        except Exception:  # noqa: BLE001
            await db.rollback()
            logger.exception("Scheduler step notifications failed")
            stats["notifications_processed"] = "error"
    return stats


def start_scheduler():
    """Starts the periodic job; returns the scheduler (call .shutdown() on exit)."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from app.db.session import AsyncSessionLocal

    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_cycle, "interval", seconds=settings.reminder_poll_seconds, args=[AsyncSessionLocal],
        id="reminder-cycle", max_instances=1, coalesce=True,
        next_run_time=notif.utcnow() + timedelta(seconds=15),
    )
    scheduler.start()
    return scheduler
