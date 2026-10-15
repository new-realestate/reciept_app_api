"""Persisted, idempotent notifications for invoices, quotations and reminders.

Every message is a `NotificationEvent` row (one channel, one recipient, one occasion)
with a unique `dedupe_key`; every delivery try is a `NotificationAttempt`. Events are
processed either immediately (user-initiated sends) or by the scheduler (reminders).
Before sending, the subject is re-checked so a reminder never goes out for an invoice
that has since been paid or cancelled, and the suppression list is always honoured.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from html import escape

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.customer import Customer
from app.models.enums import DeliveryChannel
from app.models.invoice import Invoice, InvoicePayment
from app.models.notification import NotificationAttempt, NotificationEvent
from app.models.quotation import Quotation
from app.models.schedule import PaymentSchedule, ScheduleOccurrence
from app.models.suppression import Suppression
from app.models.tenant import Tenant
from app.services.documents import invoice_pdf_bytes, quotation_pdf_bytes
from app.services.email import send_document_email
from app.services.pdf import format_money
from app.services.sms import InvalidPhoneNumberError, send_text_sms, to_e164

logger = logging.getLogger(__name__)

EMAIL_RETRY_ATTEMPTS = 3
EMAIL_RETRY_BACKOFF_SECONDS = 1.0
# Failed events are retried automatically by the scheduler up to this many total attempts.
MAX_AUTO_ATTEMPTS = 3
AUTO_RETRY_BASE_DELAY = timedelta(minutes=30)
STALE_SENDING_AFTER = timedelta(minutes=15)
STALE_REMINDER_AFTER = timedelta(hours=36)

CLAIMABLE = ("scheduled", "failed")
REMINDER_KINDS = ("reminder_upcoming", "reminder_due", "reminder_overdue", "schedule_upcoming")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- enqueue / cancel


async def enqueue_event(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    subject_type: str,
    subject_id: uuid.UUID,
    channel: str,
    dedupe_key: str,
    scheduled_for: datetime | None = None,
    recipient_party: str = "customer",
    payload: dict | None = None,
    reactivate_cancelled: bool = False,
) -> NotificationEvent:
    """Create the event unless one with the same dedupe_key exists (then return that one)."""
    existing = (
        await db.execute(select(NotificationEvent).where(NotificationEvent.dedupe_key == dedupe_key))
    ).scalar_one_or_none()
    if existing is not None:
        if reactivate_cancelled and existing.status == "cancelled":
            existing.status = "scheduled"
            existing.skip_reason = None
            existing.scheduled_for = scheduled_for or utcnow()
        return existing

    event = NotificationEvent(
        tenant_id=tenant_id,
        kind=kind,
        subject_type=subject_type,
        subject_id=subject_id,
        channel=channel,
        recipient_party=recipient_party,
        scheduled_for=scheduled_for or utcnow(),
        dedupe_key=dedupe_key,
        payload=payload,
    )
    try:
        async with db.begin_nested():
            db.add(event)
            await db.flush()
    except IntegrityError:  # lost a race with a concurrent planner; the other row wins
        return (
            await db.execute(select(NotificationEvent).where(NotificationEvent.dedupe_key == dedupe_key))
        ).scalar_one()
    return event


async def cancel_pending_events(
    db: AsyncSession, *, subject_type: str, subject_id: uuid.UUID, reason: str, kinds: tuple[str, ...] | None = None
) -> int:
    stmt = (
        update(NotificationEvent)
        .where(
            NotificationEvent.subject_type == subject_type,
            NotificationEvent.subject_id == subject_id,
            NotificationEvent.status.in_(("scheduled", "failed")),
        )
        .values(status="cancelled", skip_reason=reason)
    )
    if kinds:
        stmt = stmt.where(NotificationEvent.kind.in_(kinds))
    result = await db.execute(stmt)
    return getattr(result, "rowcount", 0) or 0


# ---------------------------------------------------------------- composing


@dataclass
class Message:
    subject: str
    text: str
    html: str
    sms: str
    pdf_bytes: bytes | None = None
    pdf_filename: str | None = None


def invoice_public_url(invoice: Invoice) -> str | None:
    if not invoice.public_token or invoice.token_revoked_at:
        return None
    return f"{get_settings().app_base_url.rstrip('/')}/i/{invoice.public_token}"


def quotation_public_url(quotation: Quotation) -> str | None:
    if not quotation.public_token or quotation.token_revoked_at:
        return None
    return f"{get_settings().app_base_url.rstrip('/')}/q/{quotation.public_token}"


def _html(paragraphs: list[str], link: str | None) -> str:
    body = "".join(f"<p>{escape(p)}</p>" for p in paragraphs)
    if link:
        body += f'<p><a href="{escape(link)}">{escape(link)}</a></p>'
    return body


def _compose_invoice(
    kind: str, tenant: Tenant, invoice: Invoice, today: date, payload: dict, pdf: bytes | None
) -> Message:
    biz = tenant.business_name
    number = invoice.number or "invoice"
    outstanding = invoice.total_cents - invoice.amount_paid_cents
    out_s = format_money(outstanding, invoice.currency)
    total_s = format_money(invoice.total_cents, invoice.currency)
    due_s = invoice.due_date.strftime("%d %B %Y")
    link = invoice_public_url(invoice)

    if kind == "invoice_issued":
        subject = f"Invoice {number} from {biz}"
        lines = [f"{biz} has sent you invoice {number} for {total_s}, due on {due_s}."]
    elif kind == "reminder_upcoming":
        days = (invoice.due_date - today).days
        when = "tomorrow" if days == 1 else f"in {days} days"
        subject = f"Reminder: invoice {number} is due {when}"
        lines = [f"This is a friendly reminder that invoice {number} from {biz} is due on {due_s}.",
                 f"Amount outstanding: {out_s}."]
    elif kind == "reminder_due":
        subject = f"Invoice {number} is due today"
        lines = [f"Invoice {number} from {biz} is due today ({due_s}).", f"Amount outstanding: {out_s}."]
    elif kind == "reminder_overdue":
        days = max((today - invoice.due_date).days, 0)
        subject = f"Invoice {number} is overdue"
        lines = [f"Invoice {number} from {biz} was due on {due_s} and is now {days} day{'s' if days != 1 else ''} overdue.",
                 f"Amount outstanding: {out_s}. If you have already paid, please let us know."]
    elif kind == "payment_recorded":
        paid_s = format_money(int(payload.get("amount_cents", 0)), invoice.currency)
        subject = f"Payment received for invoice {number}"
        lines = [f"{biz} has recorded your payment of {paid_s} against invoice {number}. Thank you.",
                 "Your invoice is now fully paid." if outstanding == 0 else f"Remaining balance: {out_s}."]
        if payload.get("receipt_url"):
            lines.append(f"Your receipt: {payload['receipt_url']}")
        link = payload.get("receipt_url") or link
        pdf = None
    else:  # reminder_manual
        subject = f"Payment reminder: invoice {number}"
        lines = [f"This is a reminder from {biz} about invoice {number}, due {due_s}.",
                 f"Amount outstanding: {out_s}."]

    sms = " ".join(lines[:2]) + (f" {link}" if link else "")
    text = "\n".join(lines) + (f"\n{link}" if link else "")
    return Message(subject, text, _html(lines, link), sms, pdf, f"{number}.pdf" if pdf else None)


def _compose_quotation(tenant: Tenant, quotation: Quotation, pdf: bytes | None) -> Message:
    number = quotation.number or "quotation"
    link = quotation_public_url(quotation)
    total_s = format_money(quotation.total_cents, quotation.currency)
    valid_s = quotation.valid_until.strftime("%d %B %Y")
    lines = [f"{tenant.business_name} has sent you quotation {number} for {total_s}, valid until {valid_s}.",
             "This is an estimate, not an invoice. You can review and accept or decline it online."]
    sms = lines[0] + (f" {link}" if link else "")
    return Message(
        f"Quotation {number} from {tenant.business_name}",
        "\n".join(lines) + (f"\n{link}" if link else ""),
        _html(lines, link),
        sms,
        pdf,
        f"{number}.pdf" if pdf else None,
    )


def _compose_occurrence(kind: str, tenant: Tenant, schedule: PaymentSchedule, occ: ScheduleOccurrence, today: date) -> Message:
    amount_s = format_money(occ.amount_cents, schedule.currency)
    due_s = occ.due_date.strftime("%d %B %Y")
    days = (occ.due_date - today).days
    if days > 0:
        subject = f"Upcoming payment: {schedule.description}"
        lines = [f"{tenant.business_name}: your payment of {amount_s} for {schedule.description} is due on {due_s}."]
    elif days == 0:
        subject = f"Payment due today: {schedule.description}"
        lines = [f"{tenant.business_name}: your payment of {amount_s} for {schedule.description} is due today."]
    else:
        subject = f"Overdue payment: {schedule.description}"
        lines = [f"{tenant.business_name}: your payment of {amount_s} for {schedule.description} was due on {due_s} "
                 f"({-days} day{'s' if days != -1 else ''} ago). If you have already paid, please let us know."]
    return Message(subject, "\n".join(lines), _html(lines, None), lines[0])


def _compose_business(kind: str, tenant: Tenant, customer: Customer, label: str, amount_s: str, due: date) -> Message:
    verb = "is due today" if kind == "reminder_due" else "is overdue"
    line = f"{label} for {customer.name} ({amount_s}) {verb} (due {due.strftime('%d %B %Y')})."
    return Message(f"Payment {verb.replace('is ', '')}: {customer.name}", line, _html([line], None), line)


# ---------------------------------------------------------------- processing


async def _is_suppressed(db: AsyncSession, tenant_id: uuid.UUID, channel: str, contact: str) -> bool:
    row = (
        await db.execute(
            select(Suppression.id).where(
                Suppression.channel == DeliveryChannel(channel),
                Suppression.contact == contact,
                or_(Suppression.tenant_id == tenant_id, Suppression.tenant_id.is_(None)),
            )
        )
    ).first()
    return row is not None


def tenant_today(tenant: Tenant, now: datetime | None = None) -> date:
    from zoneinfo import ZoneInfo

    tz: tzinfo
    try:
        tz = ZoneInfo(tenant.timezone or "UTC")
    except Exception:  # noqa: BLE001 - unknown tz name falls back to UTC
        tz = timezone.utc
    return (now or utcnow()).astimezone(tz).date()


async def _finish(
    db: AsyncSession,
    event: NotificationEvent,
    *,
    status: str,
    reason: str | None = None,
    provider: str | None = None,
    message_id: str | None = None,
    response: dict | None = None,
) -> NotificationEvent:
    event.status = status
    event.skip_reason = reason if status in ("skipped", "cancelled") else None
    if status == "sent":
        event.sent_at = utcnow()
    db.add(
        NotificationAttempt(
            event_id=event.id,
            attempt_no=event.attempts,
            status=status,
            provider=provider,
            provider_message_id=message_id,
            response=response or ({"reason": reason} if reason else None),
        )
    )
    await db.flush()
    return event


async def process_event(
    db: AsyncSession, event: NotificationEvent, *, now: datetime | None = None, ignore_stale: bool = False
) -> NotificationEvent:
    """Claim, validate and deliver one event. Safe to call repeatedly: only one caller can claim it."""
    claim = await db.execute(
        update(NotificationEvent)
        .where(NotificationEvent.id == event.id, NotificationEvent.status.in_(CLAIMABLE))
        .values(status="sending", attempts=NotificationEvent.attempts + 1)
    )
    if not getattr(claim, "rowcount", 0):
        await db.refresh(event)
        return event
    await db.refresh(event)

    tenant = (await db.execute(select(Tenant).where(Tenant.id == event.tenant_id))).scalar_one()
    today = tenant_today(tenant, now)

    # Load the subject and apply the "is this still worth sending?" rules.
    customer: Customer | None = None
    invoice = quotation = schedule = occ = None
    skip: str | None = None

    if event.subject_type == "invoice":
        invoice = (await db.execute(select(Invoice).where(Invoice.id == event.subject_id, Invoice.tenant_id == tenant.id))).scalar_one_or_none()
        if invoice is None:
            skip = "invoice no longer exists"
        else:
            customer = (await db.execute(select(Customer).where(Customer.id == invoice.customer_id))).scalar_one()
            outstanding = invoice.total_cents - invoice.amount_paid_cents
            if event.kind == "payment_recorded":
                pass
            elif invoice.status == "cancelled":
                skip = "invoice is cancelled"
            elif invoice.status != "issued":
                skip = "invoice is not issued"
            elif event.kind in REMINDER_KINDS or event.kind == "reminder_manual":
                if outstanding <= 0:
                    skip = "invoice is fully paid"
                elif event.kind != "reminder_manual" and not invoice.reminders_enabled:
                    skip = "reminders are disabled for this invoice"
    elif event.subject_type == "quotation":
        quotation = (await db.execute(select(Quotation).where(Quotation.id == event.subject_id, Quotation.tenant_id == tenant.id))).scalar_one_or_none()
        if quotation is None:
            skip = "quotation no longer exists"
        else:
            customer = (await db.execute(select(Customer).where(Customer.id == quotation.customer_id))).scalar_one()
            if quotation.status not in ("sent", "viewed", "accepted"):
                skip = f"quotation is {quotation.status}"
    elif event.subject_type == "occurrence":
        occ = (await db.execute(select(ScheduleOccurrence).where(ScheduleOccurrence.id == event.subject_id, ScheduleOccurrence.tenant_id == tenant.id))).scalar_one_or_none()
        if occ is None:
            skip = "occurrence no longer exists"
        else:
            schedule = (await db.execute(select(PaymentSchedule).where(PaymentSchedule.id == occ.schedule_id))).scalar_one()
            customer = (await db.execute(select(Customer).where(Customer.id == schedule.customer_id))).scalar_one()
            if occ.status != "expected":
                skip = f"occurrence is {occ.status}"
            elif schedule.status != "active":
                skip = f"schedule is {schedule.status}"
            elif not schedule.reminders_enabled:
                skip = "reminders are disabled for this schedule"
    else:
        skip = "unknown subject"

    if skip:
        return await _finish(db, event, status="skipped", reason=skip)

    assert customer is not None

    # Recipient.
    if event.recipient_party == "business":
        contact = tenant.contact_email if event.channel == "email" else tenant.contact_phone
        raw_missing = "business has no contact email on file" if event.channel == "email" else "business has no phone on file"
    else:
        contact = customer.email if event.channel == "email" else customer.phone
        raw_missing = f"customer has no {'email' if event.channel == 'email' else 'phone'} on file"
    if not contact:
        return await _finish(db, event, status="skipped", reason=raw_missing)
    if event.channel == "sms":
        try:
            contact = to_e164(contact)
        except InvalidPhoneNumberError as exc:
            return await _finish(db, event, status="skipped", reason=str(exc))
    event.recipient = contact

    if await _is_suppressed(db, tenant.id, event.channel, contact):
        return await _finish(db, event, status="skipped", reason="recipient is on the suppression list")

    # A scheduled reminder that missed its window (outage, paused app) is dropped rather than sent
    # late and out of context, e.g. "due in 3 days" after the due date. Explicit retries bypass this.
    if (
        not ignore_stale
        and event.kind in REMINDER_KINDS
        and (now or utcnow()) - _aware(event.scheduled_for) > STALE_REMINDER_AFTER
    ):
        return await _finish(db, event, status="skipped", reason="reminder missed its send window")

    # Compose.
    payload = event.payload or {}
    if event.recipient_party == "business":
        assert invoice is not None or occ is not None
        if invoice is not None:
            msg = _compose_business(event.kind, tenant, customer, f"Invoice {invoice.number}",
                                    format_money(invoice.total_cents - invoice.amount_paid_cents, invoice.currency), invoice.due_date)
        else:
            assert schedule is not None and occ is not None
            msg = _compose_business(event.kind, tenant, customer, schedule.description,
                                    format_money(occ.amount_cents, schedule.currency), occ.due_date)
    elif invoice is not None:
        pdf = None
        if event.channel == "email" and event.kind != "payment_recorded":
            payments = (
                await db.execute(select(InvoicePayment).where(InvoicePayment.invoice_id == invoice.id).order_by(InvoicePayment.created_at))
            ).scalars().all()
            pdf = await asyncio.to_thread(invoice_pdf_bytes, invoice, tenant, customer, list(payments), today)
        msg = _compose_invoice(event.kind, tenant, invoice, today, payload, pdf)
    elif quotation is not None:
        pdf = None
        if event.channel == "email":
            pdf = await asyncio.to_thread(quotation_pdf_bytes, quotation, tenant, customer, today)
        msg = _compose_quotation(tenant, quotation, pdf)
    else:
        assert schedule is not None and occ is not None
        msg = _compose_occurrence(event.kind, tenant, schedule, occ, today)

    # Deliver (with in-request retries for email, like receipts).
    max_tries = EMAIL_RETRY_ATTEMPTS if event.channel == "email" else 1
    last_error: Exception | None = None
    for try_index in range(max_tries):
        try:
            if event.channel == "email":
                resp = await asyncio.to_thread(
                    send_document_email,
                    to_email=contact,
                    subject=msg.subject,
                    text_body=msg.text,
                    html_body=msg.html,
                    pdf_bytes=msg.pdf_bytes,
                    pdf_filename=msg.pdf_filename,
                )
                return await _finish(db, event, status="sent", provider="gmail",
                                     message_id=resp.get("MessageID"), response={"tries": try_index + 1})
            sid = await asyncio.to_thread(send_text_sms, to_phone_e164=contact, body=msg.sms)
            return await _finish(db, event, status="sent", provider="twilio", message_id=sid,
                                 response={"tries": try_index + 1})
        except Exception as exc:  # noqa: BLE001 - provider SDKs raise their own types
            last_error = exc
            logger.warning("Notification %s try %d/%d failed: %s", event.id, try_index + 1, max_tries, exc)
            if try_index < max_tries - 1:
                await asyncio.sleep(EMAIL_RETRY_BACKOFF_SECONDS * (try_index + 1))

    return await _finish(db, event, status="failed", response={"error": str(last_error), "tries": max_tries})


async def send_now(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    subject_type: str,
    subject_id: uuid.UUID,
    channels: list[str],
    payload: dict | None = None,
) -> list[NotificationEvent]:
    """User-initiated send: one event per channel, processed immediately.

    Each call is an intentional new send, so the dedupe key is unique per call.
    """
    events = []
    for channel in channels:
        event = await enqueue_event(
            db, tenant_id=tenant_id, kind=kind, subject_type=subject_type, subject_id=subject_id, channel=channel,
            dedupe_key=f"{kind}:{subject_type}:{subject_id}:{channel}:{uuid.uuid4().hex}", payload=payload,
        )
        events.append(await process_event(db, event))
    return events


async def events_with_attempts(db: AsyncSession, events: list[NotificationEvent]):
    """Serialise events together with their attempt log (audit trail of tries)."""
    from app.schemas.documents import NotificationAttemptOut, NotificationEventOut

    for e in events:
        await db.refresh(e)  # server-side defaults (created_at/updated_at) are expired after a flush
    ids = [e.id for e in events]
    attempts: list[NotificationAttempt] = []
    if ids:
        attempts = list(
            (
                await db.execute(
                    select(NotificationAttempt).where(NotificationAttempt.event_id.in_(ids)).order_by(NotificationAttempt.attempted_at)
                )
            ).scalars().all()
        )
    by_event: dict[uuid.UUID, list] = {}
    for a in attempts:
        by_event.setdefault(a.event_id, []).append(NotificationAttemptOut.model_validate(a))
    out = []
    for e in events:
        item = NotificationEventOut.model_validate(e)
        item.attempt_log = by_event.get(e.id, [])
        out.append(item)
    return out


async def retry_event(db: AsyncSession, event: NotificationEvent) -> NotificationEvent:
    if event.status not in ("failed", "skipped", "cancelled"):
        raise ValueError(f"Only failed or skipped notifications can be retried (this one is {event.status})")
    event.status = "scheduled"
    event.skip_reason = None
    await db.flush()
    return await process_event(db, event, ignore_stale=True)


async def process_due_events(db: AsyncSession, *, now: datetime | None = None, limit: int = 100) -> int:
    """Send every scheduled event that is due, and auto-retry recently failed ones with backoff."""
    now = now or utcnow()

    # Recover events stuck in "sending" after a crash.
    stale_cutoff = now - STALE_SENDING_AFTER
    await db.execute(
        update(NotificationEvent)
        .where(NotificationEvent.status == "sending", NotificationEvent.updated_at < stale_cutoff)
        .values(status="failed")
    )

    due = (
        await db.execute(
            select(NotificationEvent)
            .where(NotificationEvent.status == "scheduled", NotificationEvent.scheduled_for <= now)
            .order_by(NotificationEvent.scheduled_for)
            .limit(limit)
        )
    ).scalars().all()

    retry_candidates = (
        await db.execute(
            select(NotificationEvent)
            .where(NotificationEvent.status == "failed", NotificationEvent.attempts < MAX_AUTO_ATTEMPTS)
            .limit(limit)
        )
    ).scalars().all()
    due = [*due] + [
        e for e in retry_candidates
        if _aware(e.updated_at or now) + AUTO_RETRY_BASE_DELAY * max(e.attempts, 1) <= now
    ]

    processed = 0
    for event in due:
        await process_event(db, event, now=now)
        await db.commit()
        processed += 1
    return processed
