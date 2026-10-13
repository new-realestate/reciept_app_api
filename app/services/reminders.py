"""Reminder policy resolution and planning.

Planning only writes `NotificationEvent` rows (idempotent via dedupe_key); sending is
done by notifications.process_due_events. Offsets are days relative to the due date:
negative = before, 0 = on the day, positive = after (overdue follow-ups).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer
from app.models.invoice import Invoice
from app.models.schedule import PaymentSchedule, ScheduleOccurrence
from app.models.tenant import Tenant
from app.services.notifications import REMINDER_KINDS, cancel_pending_events, enqueue_event, tenant_today, utcnow

DEFAULT_SETTINGS: dict = {
    "enabled": True,
    "offsets": [-3, 0, 3],
    "channels": ["email", "sms"],
    "send_hour": 9,
    "notify_business": False,
}
MAX_OFFSETS = 10
# Reminders whose send time is already further in the past than this are not created
# (e.g. an invoice issued after its "3 days before" moment) to avoid a burst of stale messages.
PAST_GRACE = timedelta(hours=24)
# Only plan this far ahead; the periodic job fills in the rest as time passes.
PLAN_HORIZON = timedelta(days=45)


class SettingsError(ValueError):
    pass


def validate_settings(raw: dict) -> dict:
    """Validate a (partial) settings dict; returns only recognised, validated keys."""
    out: dict = {}
    if "enabled" in raw:
        out["enabled"] = bool(raw["enabled"])
    if "notify_business" in raw:
        out["notify_business"] = bool(raw["notify_business"])
    if "offsets" in raw:
        offsets = raw["offsets"]
        if not isinstance(offsets, list) or len(offsets) > MAX_OFFSETS:
            raise SettingsError(f"offsets must be a list of at most {MAX_OFFSETS} whole numbers of days")
        cleaned = sorted({int(o) for o in offsets})
        if any(o < -90 or o > 365 for o in cleaned):
            raise SettingsError("offsets must be between -90 and 365 days")
        out["offsets"] = cleaned
    if "channels" in raw:
        channels = raw["channels"]
        if not isinstance(channels, list) or not set(channels) <= {"email", "sms"}:
            raise SettingsError("channels must be a list containing 'email' and/or 'sms'")
        out["channels"] = sorted(set(channels))
    if "send_hour" in raw:
        hour = int(raw["send_hour"])
        if not 0 <= hour <= 23:
            raise SettingsError("send_hour must be between 0 and 23")
        out["send_hour"] = hour
    return out


def resolve_settings(tenant: Tenant, *overrides: dict | None) -> dict:
    merged = dict(DEFAULT_SETTINGS)
    for layer in (tenant.reminder_settings, *overrides):
        if layer:
            merged.update(validate_settings(layer))
    return merged


def kind_for_offset(offset: int) -> str:
    return "reminder_upcoming" if offset < 0 else "reminder_due" if offset == 0 else "reminder_overdue"


def send_time(tenant: Tenant, day: date, hour: int) -> datetime:
    tz: tzinfo
    try:
        tz = ZoneInfo(tenant.timezone or "UTC")
    except Exception:  # noqa: BLE001
        tz = timezone.utc
    return datetime.combine(day, time(hour, 0), tzinfo=tz).astimezone(timezone.utc)


async def _plan(
    db: AsyncSession, *, tenant: Tenant, customer: Customer, subject_type: str, subject_id: uuid.UUID, due: date,
    settings: dict, now: datetime, upcoming_kind_override: str | None = None,
) -> int:
    created = 0
    if not settings["enabled"]:
        return 0
    for offset in settings["offsets"]:
        when = send_time(tenant, due + timedelta(days=offset), settings["send_hour"])
        if when < now - PAST_GRACE or when > now + PLAN_HORIZON:
            continue
        kind = kind_for_offset(offset)
        if subject_type == "occurrence":
            kind = "schedule_upcoming"
        for channel in settings["channels"]:
            if channel == "email" and not customer.email:
                continue
            if channel == "sms" and not customer.phone:
                continue
            key = f"reminder:{subject_type}:{subject_id}:{offset}:{channel}"
            await enqueue_event(
                db, tenant_id=tenant.id, kind=kind, subject_type=subject_type, subject_id=subject_id,
                channel=channel, dedupe_key=key, scheduled_for=when, reactivate_cancelled=True,
            )
            created += 1
        if settings["notify_business"] and kind in ("reminder_due", "reminder_overdue") and tenant.contact_email:
            await enqueue_event(
                db, tenant_id=tenant.id, kind=kind, subject_type=subject_type, subject_id=subject_id, channel="email",
                recipient_party="business", dedupe_key=f"reminder:{subject_type}:{subject_id}:{offset}:business",
                scheduled_for=when, reactivate_cancelled=True,
            )
    return created


async def plan_invoice_reminders(db: AsyncSession, tenant: Tenant, invoice: Invoice, customer: Customer, *, now: datetime | None = None) -> int:
    now = now or utcnow()
    if invoice.status != "issued" or invoice.total_cents - invoice.amount_paid_cents <= 0 or not invoice.reminders_enabled:
        return 0
    settings = resolve_settings(tenant, invoice.reminder_overrides)
    return await _plan(db, tenant=tenant, customer=customer, subject_type="invoice", subject_id=invoice.id,
                       due=invoice.due_date, settings=settings, now=now)


async def replan_invoice_reminders(db: AsyncSession, tenant: Tenant, invoice: Invoice, customer: Customer) -> int:
    """Cancel pending reminders and plan again from the current settings (used when settings change)."""
    await cancel_pending_events(db, subject_type="invoice", subject_id=invoice.id, reason="reminder settings changed",
                                kinds=REMINDER_KINDS)
    return await plan_invoice_reminders(db, tenant, invoice, customer)


async def plan_occurrence_reminders(
    db: AsyncSession, tenant: Tenant, schedule: PaymentSchedule, occ: ScheduleOccurrence, customer: Customer, *, now: datetime | None = None
) -> int:
    now = now or utcnow()
    if schedule.status != "active" or occ.status != "expected" or not schedule.reminders_enabled:
        return 0
    settings = resolve_settings(tenant, schedule.reminder_overrides)
    return await _plan(db, tenant=tenant, customer=customer, subject_type="occurrence", subject_id=occ.id,
                       due=occ.due_date, settings=settings, now=now)


async def plan_all(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Plan reminders for every open invoice and expected occurrence. Idempotent."""
    now = now or utcnow()
    total = 0
    invoices = (await db.execute(select(Invoice).where(Invoice.status == "issued", Invoice.reminders_enabled.is_(True)))).scalars().all()
    for inv in invoices:
        if inv.total_cents - inv.amount_paid_cents <= 0:
            continue
        tenant = (await db.execute(select(Tenant).where(Tenant.id == inv.tenant_id))).scalar_one()
        customer = (await db.execute(select(Customer).where(Customer.id == inv.customer_id))).scalar_one()
        total += await plan_invoice_reminders(db, tenant, inv, customer, now=now)

    rows = (
        await db.execute(
            select(ScheduleOccurrence, PaymentSchedule)
            .join(PaymentSchedule, PaymentSchedule.id == ScheduleOccurrence.schedule_id)
            .where(ScheduleOccurrence.status == "expected", PaymentSchedule.status == "active",
                   PaymentSchedule.reminders_enabled.is_(True))
        )
    ).all()
    for occ, schedule in rows:
        tenant = (await db.execute(select(Tenant).where(Tenant.id == occ.tenant_id))).scalar_one()
        customer = (await db.execute(select(Customer).where(Customer.id == schedule.customer_id))).scalar_one()
        total += await plan_occurrence_reminders(db, tenant, schedule, occ, customer, now=now)
    return total


__all__ = [
    "DEFAULT_SETTINGS", "SettingsError", "validate_settings", "resolve_settings", "plan_invoice_reminders",
    "replan_invoice_reminders", "plan_occurrence_reminders", "plan_all", "tenant_today",
]
