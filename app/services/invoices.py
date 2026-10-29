"""Invoice domain logic: drafts, issuing, cancelling and payment recording.

Functions here never commit; the API layer owns the transaction. Notifications are
enqueued by the API layer too, so this module stays free of delivery concerns.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_audit_event
from app.models.customer import Customer
from app.models.enums import PaymentMethodType
from app.models.invoice import Invoice, InvoiceLineItem, InvoicePayment
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.documents import (
    InvoiceCreate,
    InvoiceDetailOut,
    InvoiceOut,
    InvoicePaymentCreate,
    InvoicePaymentOut,
    InvoiceUpdate,
    LineItemIn,
    LineItemOut,
)
from app.services.doc_views import invoice_snapshot
from app.services.documents import days_overdue, derive_invoice_status, outstanding_cents
from app.services.money import LineInput, MoneyError, compute_totals
from app.services.notifications import invoice_public_url, tenant_today
from app.services.numbering import next_document_number
from app.services.receipts import create_receipt_record


def conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def new_public_token() -> str:
    return secrets.token_urlsafe(32)


def apply_lines_and_totals(doc, line_model, lines: list[LineItemIn], *, discount_percent: Decimal | None, discount_cents: int | None) -> None:
    """Replace the document's line items and recompute every stored amount (server is the source of truth)."""
    try:
        totals = compute_totals(
            [LineInput(li.quantity, li.unit_price_cents, li.tax_rate) for li in lines],
            discount_percent=discount_percent,
            discount_cents=discount_cents,
        )
    except MoneyError as exc:
        raise unprocessable(str(exc)) from exc

    doc.line_items = [
        line_model(
            position=i,
            description=li.description.strip(),
            quantity=li.quantity,
            unit_price_cents=li.unit_price_cents,
            tax_rate=li.tax_rate,
            line_total_cents=total,
        )
        for i, (li, total) in enumerate(zip(lines, totals.line_totals))
    ]
    doc.subtotal_cents = totals.subtotal_cents
    doc.discount_percent = discount_percent
    doc.discount_cents = totals.discount_cents
    doc.tax_cents = totals.tax_cents
    doc.total_cents = totals.total_cents


def lines_from_models(line_items) -> list[LineItemIn]:
    return [
        LineItemIn(description=li.description, quantity=li.quantity, unit_price_cents=li.unit_price_cents, tax_rate=li.tax_rate)
        for li in line_items
    ]


async def get_invoice(db: AsyncSession, tenant_id: uuid.UUID, invoice_id: uuid.UUID, *, lock: bool = False) -> Invoice:
    stmt = select(Invoice).where(Invoice.id == invoice_id, Invoice.tenant_id == tenant_id)
    if lock and db.get_bind().dialect.name != "sqlite":
        stmt = stmt.with_for_update(key_share=True)
    invoice = (await db.execute(stmt)).scalar_one_or_none()
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    return invoice


async def create_draft(
    db: AsyncSession, *, tenant: Tenant, user: User, customer: Customer, payload: InvoiceCreate | None = None,
    title: str | None = None, currency: str | None = None, issue_date: date | None = None, due_date: date | None = None,
    lines: list[LineItemIn] | None = None, discount_percent: Decimal | None = None, discount_cents: int | None = None,
    notes: str | None = None, payment_instructions: str | None = None, reminders_enabled: bool = True,
    quotation_id: uuid.UUID | None = None, schedule_occurrence_id: uuid.UUID | None = None,
    property_id: uuid.UUID | None = None, unit_id: uuid.UUID | None = None, tenancy_id: uuid.UUID | None = None,
) -> Invoice:
    if payload is not None:
        title, currency, issue_date, due_date = payload.title, payload.currency, payload.issue_date, payload.due_date
        lines, discount_percent, discount_cents = payload.line_items, payload.discount_percent, payload.discount_cents
        notes, payment_instructions, reminders_enabled = payload.notes, payload.payment_instructions, payload.reminders_enabled
    assert lines is not None and due_date is not None

    issue_date = issue_date or tenant_today(tenant)
    if due_date < issue_date:
        raise unprocessable("Due date cannot be before the issue date")

    invoice = Invoice(
        tenant_id=tenant.id,
        customer_id=customer.id,
        status="draft",
        title=title,
        currency=(currency or tenant.default_currency).upper(),
        issue_date=issue_date,
        due_date=due_date,
        notes=notes,
        payment_instructions=payment_instructions,
        reminders_enabled=reminders_enabled,
        quotation_id=quotation_id,
        schedule_occurrence_id=schedule_occurrence_id,
        property_id=property_id,
        unit_id=unit_id,
        tenancy_id=tenancy_id,
        created_by_user_id=user.id,
    )
    apply_lines_and_totals(invoice, InvoiceLineItem, lines, discount_percent=discount_percent, discount_cents=discount_cents)
    db.add(invoice)
    await db.flush()
    await record_audit_event(
        db, tenant_id=tenant.id, actor_user_id=user.id, action="invoice.created", entity_type="invoice", entity_id=invoice.id
    )
    return invoice


async def update_draft(db: AsyncSession, *, tenant: Tenant, user: User, invoice: Invoice, payload: InvoiceUpdate, customer: Customer | None) -> Invoice:
    if invoice.status != "draft":
        raise conflict("Only draft invoices can be edited. Cancel this invoice and create a new one instead.")

    data = payload.model_dump(exclude_unset=True)
    if customer is not None:
        invoice.customer_id = customer.id
    for field in ("title", "notes", "payment_instructions", "reminders_enabled", "issue_date", "due_date"):
        if field in data and data[field] is not None:
            setattr(invoice, field, data[field])
    if data.get("currency"):
        invoice.currency = data["currency"].upper()
    if invoice.due_date < invoice.issue_date:
        raise unprocessable("Due date cannot be before the issue date")

    if "line_items" in data or "discount_percent" in data or "discount_cents" in data:
        lines = payload.line_items if payload.line_items is not None else lines_from_models(invoice.line_items)
        if "discount_percent" in data or "discount_cents" in data:
            pct, cents = payload.discount_percent, payload.discount_cents
        else:
            pct, cents = invoice.discount_percent, (None if invoice.discount_percent is not None else invoice.discount_cents)
        apply_lines_and_totals(invoice, InvoiceLineItem, lines, discount_percent=pct, discount_cents=cents)

    await db.flush()
    await record_audit_event(
        db, tenant_id=tenant.id, actor_user_id=user.id, action="invoice.updated", entity_type="invoice", entity_id=invoice.id
    )
    return invoice


async def issue_invoice(db: AsyncSession, *, tenant: Tenant, user: User, invoice: Invoice, customer: Customer) -> Invoice:
    if invoice.status != "draft":
        raise conflict(f"Only draft invoices can be issued (this one is {invoice.status})")
    if invoice.total_cents <= 0:
        raise unprocessable("An invoice must have a total greater than zero")
    if not customer.email and not customer.phone:
        raise unprocessable("The customer needs an email or phone number")

    invoice.number = await next_document_number(db, tenant.id, "invoice", today=invoice.issue_date)
    invoice.status = "issued"
    invoice.issued_at = datetime.now(timezone.utc)
    invoice.public_token = new_public_token()
    invoice.token_revoked_at = None
    invoice.snapshot = invoice_snapshot(invoice, tenant, customer)
    await db.flush()
    await record_audit_event(
        db, tenant_id=tenant.id, actor_user_id=user.id, action="invoice.issued", entity_type="invoice",
        entity_id=invoice.id, metadata={"number": invoice.number},
    )
    return invoice


async def cancel_invoice(db: AsyncSession, *, tenant: Tenant, user: User, invoice: Invoice, reason: str | None) -> Invoice:
    if invoice.status == "cancelled":
        return invoice
    if invoice.amount_paid_cents > 0:
        raise conflict(
            "This invoice already has payments recorded and cannot be cancelled. "
            "Receipts that were issued for it stay valid."
        )
    invoice.status = "cancelled"
    invoice.cancelled_at = datetime.now(timezone.utc)
    invoice.cancel_reason = reason
    await db.flush()
    await record_audit_event(
        db, tenant_id=tenant.id, actor_user_id=user.id, action="invoice.cancelled", entity_type="invoice",
        entity_id=invoice.id, metadata={"reason": reason},
    )
    return invoice


async def rotate_public_link(db: AsyncSession, *, tenant: Tenant, user: User, invoice: Invoice, revoke_only: bool) -> Invoice:
    if invoice.status == "draft":
        raise conflict("Issue the invoice before sharing it")
    if revoke_only:
        invoice.token_revoked_at = datetime.now(timezone.utc)
        action = "invoice.link_revoked"
    else:
        invoice.public_token = new_public_token()
        invoice.token_revoked_at = None
        action = "invoice.link_rotated"
    await db.flush()
    await record_audit_event(db, tenant_id=tenant.id, actor_user_id=user.id, action=action, entity_type="invoice", entity_id=invoice.id)
    return invoice


async def record_payment(
    db: AsyncSession, *, tenant: Tenant, user: User, invoice_id: uuid.UUID, payload: InvoicePaymentCreate, idempotency_key: str | None
) -> tuple[Invoice, InvoicePayment, bool]:
    """Allocate a payment received against an invoice and issue its receipt, atomically.

    Returns (invoice, payment, replayed). A repeated idempotency key returns the original
    payment instead of recording a second one.
    """
    key = idempotency_key or payload.idempotency_key

    async def _replay() -> tuple[Invoice, InvoicePayment, bool] | None:
        if not key:
            return None
        prior = (
            await db.execute(
                select(InvoicePayment).where(InvoicePayment.tenant_id == tenant.id, InvoicePayment.idempotency_key == key)
            )
        ).scalar_one_or_none()
        if prior is None:
            return None
        if prior.invoice_id != invoice_id:
            raise conflict("This idempotency key was already used for a different invoice")
        return await get_invoice(db, tenant.id, invoice_id), prior, True

    if (replayed := await _replay()) is not None:
        return replayed

    invoice = await get_invoice(db, tenant.id, invoice_id, lock=True)  # row lock serialises concurrent payments
    if (replayed := await _replay()) is not None:  # a concurrent request with the same key finished while we waited
        return replayed
    if invoice.status != "issued":
        raise conflict(f"Payments can only be recorded against issued invoices (this one is {invoice.status})")

    outstanding = outstanding_cents(invoice)
    if outstanding <= 0:
        raise conflict("This invoice is already fully paid")
    if payload.amount_cents > outstanding:
        raise unprocessable(
            f"Payment of {payload.amount_cents} exceeds the outstanding balance of {outstanding}. "
            "Overpayments are not supported."
        )
    if payload.paid_at > tenant_today(tenant):
        raise unprocessable("Payment date cannot be in the future")
    try:
        method = PaymentMethodType(payload.payment_method)
    except ValueError as exc:
        raise unprocessable(f"Unknown payment method: {payload.payment_method}") from exc

    customer = (await db.execute(select(Customer).where(Customer.id == invoice.customer_id))).scalar_one()
    remaining = outstanding - payload.amount_cents
    receipt = await create_receipt_record(
        db,
        tenant=tenant,
        user=user,
        customer=customer,
        amount_cents=payload.amount_cents,
        currency=invoice.currency,
        reason=f"Payment for invoice {invoice.number}",
        payment_method=method,
        payment_reference=payload.payment_reference,
        paid_at=payload.paid_at,
        balance_cents=remaining,
        property_id=invoice.property_id,
        unit_id=invoice.unit_id,
        tenancy_id=invoice.tenancy_id,
    )
    payment = InvoicePayment(
        tenant_id=tenant.id,
        invoice_id=invoice.id,
        receipt_id=receipt.id,
        amount_cents=payload.amount_cents,
        paid_at=payload.paid_at,
        method=method.value,
        reference=payload.payment_reference,
        idempotency_key=key,
        recorded_by_user_id=user.id,
    )
    db.add(payment)
    invoice.amount_paid_cents += payload.amount_cents
    await db.flush()
    await record_audit_event(
        db, tenant_id=tenant.id, actor_user_id=user.id, action="invoice.payment_recorded", entity_type="invoice",
        entity_id=invoice.id, metadata={"amount_cents": payload.amount_cents, "receipt_id": str(receipt.id), "remaining_cents": remaining},
    )
    payment._receipt = receipt  # type: ignore[attr-defined]  # transient: lets the API build the receipt link
    return invoice, payment, False


# ------------------------------------------------------------------ serialisation


def serialize_invoice(invoice: Invoice, customer_name: str | None, today: date, *, payments: list[InvoicePayment] | None = None):
    out_cls = InvoiceDetailOut if payments is not None else InvoiceOut
    data: dict[str, Any] = dict(
        id=invoice.id, number=invoice.number, status=invoice.status,
        display_status=derive_invoice_status(invoice, today), customer_id=invoice.customer_id,
        customer_name=customer_name, title=invoice.title, currency=invoice.currency,
        issue_date=invoice.issue_date, due_date=invoice.due_date, subtotal_cents=invoice.subtotal_cents,
        discount_percent=invoice.discount_percent, discount_cents=invoice.discount_cents, tax_cents=invoice.tax_cents,
        total_cents=invoice.total_cents, amount_paid_cents=invoice.amount_paid_cents,
        outstanding_cents=outstanding_cents(invoice), days_overdue=days_overdue(invoice, today),
        notes=invoice.notes, payment_instructions=invoice.payment_instructions,
        reminders_enabled=invoice.reminders_enabled, quotation_id=invoice.quotation_id,
        property_id=invoice.property_id, unit_id=invoice.unit_id, tenancy_id=invoice.tenancy_id,
        public_url=invoice_public_url(invoice), issued_at=invoice.issued_at, cancelled_at=invoice.cancelled_at,
        cancel_reason=invoice.cancel_reason, created_at=invoice.created_at,
        line_items=[LineItemOut.model_validate(li) for li in invoice.line_items],
    )
    if payments is not None:
        data["payments"] = [InvoicePaymentOut.model_validate(p) for p in payments]
    return out_cls(**data)
