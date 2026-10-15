"""Quotation domain logic. Never commits; the API layer owns the transaction."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_audit_event
from app.models.customer import Customer
from app.models.invoice import Invoice
from app.models.quotation import Quotation, QuotationLineItem
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.documents import (
    ConvertQuotationRequest,
    LineItemIn,
    LineItemOut,
    QuotationCreate,
    QuotationOut,
    QuotationUpdate,
)
from app.services import invoices as invoice_svc
from app.services.doc_views import quotation_snapshot
from app.services.documents import derive_quotation_status
from app.services.invoices import apply_lines_and_totals, conflict, lines_from_models, new_public_token, unprocessable
from app.services.notifications import quotation_public_url, tenant_today
from app.services.numbering import next_document_number


async def get_quotation(db: AsyncSession, tenant_id: uuid.UUID, quotation_id: uuid.UUID, *, lock: bool = False) -> Quotation:
    stmt = select(Quotation).where(Quotation.id == quotation_id, Quotation.tenant_id == tenant_id)
    if lock and db.get_bind().dialect.name != "sqlite":
        stmt = stmt.with_for_update(key_share=True)
    quotation = (await db.execute(stmt)).scalar_one_or_none()
    if quotation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quotation not found")
    return quotation


async def create_draft(db: AsyncSession, *, tenant: Tenant, user: User, customer: Customer, payload: QuotationCreate) -> Quotation:
    issue_date = payload.issue_date or tenant_today(tenant)
    if payload.valid_until < issue_date:
        raise unprocessable("The validity date cannot be before the issue date")
    q = Quotation(
        tenant_id=tenant.id,
        customer_id=customer.id,
        status="draft",
        title=payload.title,
        description=payload.description,
        currency=(payload.currency or tenant.default_currency).upper(),
        issue_date=issue_date,
        valid_until=payload.valid_until,
        terms=payload.terms,
        notes=payload.notes,
        internal_notes=payload.internal_notes,
        created_by_user_id=user.id,
    )
    apply_lines_and_totals(q, QuotationLineItem, payload.line_items,
                           discount_percent=payload.discount_percent, discount_cents=payload.discount_cents)
    db.add(q)
    await db.flush()
    await record_audit_event(db, tenant_id=tenant.id, actor_user_id=user.id, action="quotation.created",
                             entity_type="quotation", entity_id=q.id)
    return q


async def update_draft(db: AsyncSession, *, tenant: Tenant, user: User, quotation: Quotation, payload: QuotationUpdate,
                       customer: Customer | None) -> Quotation:
    if quotation.status != "draft":
        raise conflict("Only draft quotations can be edited. Cancel it and create a new quotation instead.")
    data = payload.model_dump(exclude_unset=True)
    if customer is not None:
        quotation.customer_id = customer.id
    for field in ("title", "description", "terms", "notes", "internal_notes", "issue_date", "valid_until"):
        if field in data and data[field] is not None:
            setattr(quotation, field, data[field])
    if data.get("currency"):
        quotation.currency = data["currency"].upper()
    if quotation.valid_until < quotation.issue_date:
        raise unprocessable("The validity date cannot be before the issue date")
    if "line_items" in data or "discount_percent" in data or "discount_cents" in data:
        lines = payload.line_items if payload.line_items is not None else lines_from_models(quotation.line_items)
        if "discount_percent" in data or "discount_cents" in data:
            pct, cents = payload.discount_percent, payload.discount_cents
        else:
            pct, cents = quotation.discount_percent, (None if quotation.discount_percent is not None else quotation.discount_cents)
        apply_lines_and_totals(quotation, QuotationLineItem, lines, discount_percent=pct, discount_cents=cents)
    await db.flush()
    await record_audit_event(db, tenant_id=tenant.id, actor_user_id=user.id, action="quotation.updated",
                             entity_type="quotation", entity_id=quotation.id)
    return quotation


async def prepare_to_send(db: AsyncSession, *, tenant: Tenant, user: User, quotation: Quotation, customer: Customer) -> Quotation:
    """Draft -> sent: assigns the number and public link and freezes the shared contents.

    For an already-sent quotation this is a no-op so it can be re-shared (resend).
    """
    if quotation.status == "draft":
        if quotation.total_cents <= 0:
            raise unprocessable("A quotation must have a total greater than zero")
        if quotation.valid_until < tenant_today(tenant):
            raise unprocessable("The validity date has already passed. Extend it before sending.")
        if not customer.email and not customer.phone:
            raise unprocessable("The customer needs an email or phone number")
        quotation.number = await next_document_number(db, tenant.id, "quotation", today=quotation.issue_date)
        quotation.status = "sent"
        quotation.sent_at = datetime.now(timezone.utc)
        quotation.public_token = new_public_token()
        quotation.snapshot = quotation_snapshot(quotation, tenant, customer)
        await db.flush()
        await record_audit_event(db, tenant_id=tenant.id, actor_user_id=user.id, action="quotation.sent",
                                 entity_type="quotation", entity_id=quotation.id, metadata={"number": quotation.number})
    elif quotation.status in ("sent", "viewed"):
        if derive_quotation_status(quotation, tenant_today(tenant)) == "expired":
            raise conflict("This quotation has expired")
        if quotation.token_revoked_at:
            quotation.public_token = new_public_token()
            quotation.token_revoked_at = None
    else:
        raise conflict(f"A {quotation.status} quotation cannot be sent")
    return quotation


async def cancel_quotation(db: AsyncSession, *, tenant: Tenant, user: User, quotation: Quotation) -> Quotation:
    if quotation.status == "cancelled":
        return quotation
    if quotation.status in ("accepted", "rejected", "expired"):
        raise conflict(f"A {quotation.status} quotation cannot be cancelled")
    quotation.status = "cancelled"
    quotation.cancelled_at = datetime.now(timezone.utc)
    await db.flush()
    await record_audit_event(db, tenant_id=tenant.id, actor_user_id=user.id, action="quotation.cancelled",
                             entity_type="quotation", entity_id=quotation.id)
    return quotation


# ---------------------------------------------------------------- public responses


async def get_by_token(db: AsyncSession, token: str, *, lock: bool = False) -> Quotation | None:
    stmt = select(Quotation).where(Quotation.public_token == token, Quotation.token_revoked_at.is_(None))
    if lock and db.get_bind().dialect.name != "sqlite":
        stmt = stmt.with_for_update(key_share=True)
    return (await db.execute(stmt)).scalar_one_or_none()


async def mark_viewed(db: AsyncSession, quotation: Quotation) -> None:
    """sent -> viewed, once. Viewing never changes anything else."""
    if quotation.status == "sent":
        quotation.status = "viewed"
        quotation.viewed_at = datetime.now(timezone.utc)
        await db.flush()
        await record_audit_event(db, tenant_id=quotation.tenant_id, actor_user_id=None, action="quotation.viewed",
                                 entity_type="quotation", entity_id=quotation.id)


async def respond(db: AsyncSession, quotation: Quotation, *, tenant: Tenant, accept: bool, name: str | None, comment: str | None) -> tuple[Quotation, bool]:
    """Customer accepts or rejects. Returns (quotation, changed). Repeating the same answer is a harmless no-op."""
    target = "accepted" if accept else "rejected"
    if quotation.status == target:
        return quotation, False
    if quotation.status not in ("sent", "viewed"):
        raise conflict(f"This quotation is {quotation.status} and can no longer be answered")
    if derive_quotation_status(quotation, tenant_today(tenant)) == "expired":
        raise conflict("This quotation has expired")

    quotation.status = target
    quotation.responded_at = datetime.now(timezone.utc)
    if accept:
        quotation.accepted_name = (name or "").strip()[:200] or None
    else:
        quotation.response_comment = (comment or "").strip()[:1000] or None
    # Preserve exactly what was agreed to.
    snap = dict(quotation.snapshot or {})
    snap["response"] = {
        "result": target,
        "at": quotation.responded_at.isoformat(),
        "accepted_name": quotation.accepted_name,
        "comment": quotation.response_comment,
    }
    quotation.snapshot = snap
    await db.flush()
    await record_audit_event(db, tenant_id=quotation.tenant_id, actor_user_id=None, action=f"quotation.{target}",
                             entity_type="quotation", entity_id=quotation.id)
    return quotation, True


async def expire_due(db: AsyncSession, *, today_by_tenant: dict[uuid.UUID, date]) -> int:
    """Materialise 'expired' for sent/viewed quotations whose validity date has passed."""
    expired = 0
    for tenant_id, today in today_by_tenant.items():
        result = await db.execute(
            update(Quotation)
            .where(Quotation.tenant_id == tenant_id, Quotation.status.in_(("sent", "viewed")), Quotation.valid_until < today)
            .values(status="expired")
        )
        expired += getattr(result, "rowcount", 0) or 0
    return expired


# ---------------------------------------------------------------- conversion


async def converted_invoice(db: AsyncSession, quotation_id: uuid.UUID) -> Invoice | None:
    return (await db.execute(select(Invoice).where(Invoice.quotation_id == quotation_id))).scalar_one_or_none()


async def convert_to_invoice(
    db: AsyncSession, *, tenant: Tenant, user: User, quotation: Quotation, customer: Customer, payload: ConvertQuotationRequest
) -> Invoice:
    if quotation.status != "accepted":
        raise conflict("Only accepted quotations can be converted to an invoice")
    if await converted_invoice(db, quotation.id) is not None:
        raise conflict("This quotation has already been converted to an invoice")

    snap = quotation.snapshot or {}
    source_lines = snap.get("lines") or []
    lines = [
        LineItemIn(
            description=li["description"], quantity=Decimal(li["quantity"]), unit_price_cents=li["unit_price_cents"],
            tax_rate=Decimal(li["tax_rate"]) if li.get("tax_rate") is not None else None,
        )
        for li in source_lines
    ]
    pct = Decimal(snap["discount_percent"]) if snap.get("discount_percent") is not None else None
    invoice = await invoice_svc.create_draft(
        db, tenant=tenant, user=user, customer=customer, title=snap.get("title") or quotation.title,
        currency=quotation.currency, issue_date=payload.issue_date, due_date=payload.due_date, lines=lines,
        discount_percent=pct, discount_cents=None if pct is not None else snap.get("discount_cents"),
        notes=snap.get("notes"), payment_instructions=payload.payment_instructions, quotation_id=quotation.id,
    )
    await record_audit_event(db, tenant_id=tenant.id, actor_user_id=user.id, action="quotation.converted",
                             entity_type="quotation", entity_id=quotation.id, metadata={"invoice_id": str(invoice.id)})
    if payload.issue_now:
        await invoice_svc.issue_invoice(db, tenant=tenant, user=user, invoice=invoice, customer=customer)
    return invoice


# ---------------------------------------------------------------- serialisation


def serialize_quotation(q: Quotation, customer_name: str | None, today: date, *, converted_invoice_id: uuid.UUID | None = None) -> QuotationOut:
    return QuotationOut(
        id=q.id, number=q.number, status=q.status, display_status=derive_quotation_status(q, today),
        customer_id=q.customer_id, customer_name=customer_name, title=q.title, description=q.description,
        currency=q.currency, issue_date=q.issue_date, valid_until=q.valid_until, subtotal_cents=q.subtotal_cents,
        discount_percent=q.discount_percent, discount_cents=q.discount_cents, tax_cents=q.tax_cents,
        total_cents=q.total_cents, terms=q.terms, notes=q.notes, internal_notes=q.internal_notes,
        public_url=quotation_public_url(q), sent_at=q.sent_at, viewed_at=q.viewed_at, responded_at=q.responded_at,
        accepted_name=q.accepted_name, response_comment=q.response_comment, converted_invoice_id=converted_invoice_id,
        created_at=q.created_at, line_items=[LineItemOut.model_validate(li) for li in q.line_items],
    )
