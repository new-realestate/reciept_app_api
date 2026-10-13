"""Payment schedules: recurrence maths and expected-payment occurrences.

An occurrence is an *expectation* (and the thing reminders attach to). It never creates
an invoice by itself unless the schedule opts in with auto_invoice; otherwise an invoice
is created only by an explicit action, and a unique link prevents duplicates.
"""
from __future__ import annotations

import calendar
import uuid
from datetime import date, timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_audit_event
from app.models.invoice import Invoice
from app.models.schedule import PaymentSchedule, ScheduleOccurrence
from app.models.tenant import Tenant

FREQUENCIES = ("once", "weekly", "monthly", "quarterly", "annually")
MONTHS_PER_STEP = {"monthly": 1, "quarterly": 3, "annually": 12}
# How far ahead occurrences are materialised.
GENERATION_HORIZON_DAYS = 90
# Hard stop so a mistyped schedule can't create thousands of rows in one pass.
MAX_GENERATED_PER_RUN = 60


def add_months(anchor: date, months: int) -> date:
    """Anchor + N months, clamped to the month's length. Always computed from the anchor,
    never from the previous result, so 31 Jan -> 28/29 Feb -> 31 Mar (no drift to the 28th)."""
    total = anchor.year * 12 + (anchor.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))


def occurrence_date(first_due: date, frequency: str, seq: int) -> date:
    if frequency == "once":
        if seq != 0:
            raise ValueError("A one-time schedule has a single occurrence")
        return first_due
    if frequency == "weekly":
        return first_due + timedelta(weeks=seq)
    return add_months(first_due, MONTHS_PER_STEP[frequency] * seq)


def _within_limits(schedule: PaymentSchedule, seq: int, due: date) -> bool:
    if schedule.frequency == "once" and seq > 0:
        return False
    if schedule.max_occurrences is not None and seq >= schedule.max_occurrences:
        return False
    if schedule.end_date is not None and due > schedule.end_date:
        return False
    return True


async def generate_occurrences(db: AsyncSession, schedule: PaymentSchedule, *, today: date) -> list[ScheduleOccurrence]:
    """Create any missing occurrences up to the horizon. Idempotent (unique schedule+seq)."""
    if schedule.status != "active":
        return []
    horizon = today + timedelta(days=GENERATION_HORIZON_DAYS)

    last_seq = (
        await db.execute(select(func.max(ScheduleOccurrence.seq)).where(ScheduleOccurrence.schedule_id == schedule.id))
    ).scalar_one_or_none()
    seq = 0 if last_seq is None else last_seq + 1

    created: list[ScheduleOccurrence] = []
    while len(created) < MAX_GENERATED_PER_RUN:
        if schedule.frequency == "once" and seq > 0:
            break
        due = occurrence_date(schedule.first_due_date, schedule.frequency, seq)
        if not _within_limits(schedule, seq, due):
            break
        # Always materialise the first occurrence; later ones only inside the horizon.
        if seq > 0 and due > horizon:
            break
        occ = ScheduleOccurrence(
            tenant_id=schedule.tenant_id, schedule_id=schedule.id, seq=seq, due_date=due,
            amount_cents=schedule.amount_cents, status="expected",
        )
        db.add(occ)
        created.append(occ)
        seq += 1
    if created:
        await db.flush()
    return created


async def get_schedule(db: AsyncSession, tenant_id: uuid.UUID, schedule_id: uuid.UUID, *, lock: bool = False) -> PaymentSchedule:
    stmt = select(PaymentSchedule).where(PaymentSchedule.id == schedule_id, PaymentSchedule.tenant_id == tenant_id)
    if lock and db.get_bind().dialect.name != "sqlite":
        stmt = stmt.with_for_update(key_share=True)
    schedule = (await db.execute(stmt)).scalar_one_or_none()
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
    return schedule


async def get_occurrence(db: AsyncSession, tenant_id: uuid.UUID, occurrence_id: uuid.UUID, *, lock: bool = False) -> ScheduleOccurrence:
    stmt = select(ScheduleOccurrence).where(ScheduleOccurrence.id == occurrence_id, ScheduleOccurrence.tenant_id == tenant_id)
    if lock and db.get_bind().dialect.name != "sqlite":
        stmt = stmt.with_for_update(key_share=True)
    occ = (await db.execute(stmt)).scalar_one_or_none()
    if occ is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Occurrence not found")
    return occ


async def set_status(db: AsyncSession, *, tenant: Tenant, user_id: uuid.UUID, schedule: PaymentSchedule, new_status: str, today: date) -> PaymentSchedule:
    """pause / resume / cancel with the allowed transitions enforced."""
    allowed = {
        "active": {"paused", "cancelled"},
        "paused": {"active", "cancelled"},
        "completed": set(),
        "cancelled": set(),
    }
    if new_status not in allowed.get(schedule.status, set()):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"A {schedule.status} schedule cannot become {new_status}")
    schedule.status = new_status
    await db.flush()
    await record_audit_event(db, tenant_id=tenant.id, actor_user_id=user_id, action=f"schedule.{new_status}",
                             entity_type="schedule", entity_id=schedule.id)
    if new_status == "active":
        await generate_occurrences(db, schedule, today=today)
    return schedule


async def complete_finished(db: AsyncSession) -> int:
    """Mark schedules completed once every possible occurrence exists and none is still expected."""
    done = 0
    schedules = (await db.execute(select(PaymentSchedule).where(PaymentSchedule.status == "active"))).scalars().all()
    for s in schedules:
        last_seq = (await db.execute(select(func.max(ScheduleOccurrence.seq)).where(ScheduleOccurrence.schedule_id == s.id))).scalar_one_or_none()
        if last_seq is None:
            continue
        if s.frequency != "once" and _within_limits(s, last_seq + 1, occurrence_date(s.first_due_date, s.frequency, last_seq + 1)):
            continue  # more occurrences still to come
        open_count = (
            await db.execute(
                select(func.count()).select_from(ScheduleOccurrence).where(
                    ScheduleOccurrence.schedule_id == s.id, ScheduleOccurrence.status.in_(("expected", "invoiced"))
                )
            )
        ).scalar_one()
        if open_count == 0:
            s.status = "completed"
            done += 1
    return done


async def sync_occurrence_from_invoice(db: AsyncSession, invoice: Invoice) -> None:
    """Keep the linked occurrence in step with its invoice (called after payments)."""
    if invoice.schedule_occurrence_id is None:
        return
    occ = (await db.execute(select(ScheduleOccurrence).where(ScheduleOccurrence.id == invoice.schedule_occurrence_id))).scalar_one_or_none()
    if occ is None:
        return
    occ.amount_paid_cents = invoice.amount_paid_cents
    if invoice.status == "issued" and invoice.total_cents - invoice.amount_paid_cents <= 0:
        occ.status = "paid"
    await db.flush()


async def release_occurrence(db: AsyncSession, invoice: Invoice) -> None:
    """A cancelled invoice frees its occurrence so a corrected invoice can be created."""
    if invoice.schedule_occurrence_id is None:
        return
    occ = (await db.execute(select(ScheduleOccurrence).where(ScheduleOccurrence.id == invoice.schedule_occurrence_id))).scalar_one_or_none()
    invoice.schedule_occurrence_id = None
    if occ is not None and occ.status == "invoiced":
        occ.status = "expected"
    await db.flush()
