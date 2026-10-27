"""Receipt creation shared by POST /receipts and invoice payment recording."""
from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_audit_event
from app.models.customer import Customer
from app.models.enums import PaymentMethodType
from app.models.receipt import Receipt
from app.models.tenant import Tenant
from app.models.user import User
from app.services.numbers import amount_to_words
from app.services.pdf import render_receipt_pdf_bytes, store_receipt_pdf
from app.services.reference_numbers import next_reference_number


async def create_receipt_record(
    db: AsyncSession,
    *,
    tenant: Tenant,
    user: User,
    customer: Customer,
    amount_cents: int,
    currency: str | None,
    reason: str,
    payment_method: PaymentMethodType,
    payment_reference: str | None,
    paid_at: date,
    balance_cents: int = 0,
    corrects_receipt_id: uuid.UUID | None = None,
    property_id: uuid.UUID | None = None,
    unit_id: uuid.UUID | None = None,
    tenancy_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
) -> Receipt:
    """Allocates the next reference number, inserts the receipt, renders and stores its PDF.

    Does not commit; the caller owns the transaction. The property context is optional
    and must already be validated (app/core/access.py validate_doc_context).
    """
    currency = currency or tenant.default_currency
    reference_number = await next_reference_number(db, tenant.id)

    receipt = Receipt(
        tenant_id=tenant.id,
        customer_id=customer.id,
        reference_number=reference_number,
        currency=currency,
        amount_cents=amount_cents,
        amount_in_words=amount_to_words(amount_cents, currency),
        balance_cents=balance_cents,
        reason=reason,
        payment_method=payment_method,
        payment_reference=payment_reference,
        paid_at=paid_at,
        issued_by_user_id=user.id,
        issuer_printed_name=user.full_name,
        signature_image_url=user.signature_image_url,
        corrects_receipt_id=corrects_receipt_id,
        property_id=property_id,
        unit_id=unit_id,
        tenancy_id=tenancy_id,
        idempotency_key=idempotency_key,
    )
    db.add(receipt)
    await db.flush()

    pdf_bytes = render_receipt_pdf_bytes(tenant=tenant, user=user, customer=customer, receipt=receipt)
    receipt.pdf_url = store_receipt_pdf(tenant_id=str(tenant.id), receipt_id=str(receipt.id), pdf_bytes=pdf_bytes)

    await record_audit_event(
        db,
        tenant_id=tenant.id,
        actor_user_id=user.id,
        action="receipt.created",
        entity_type="receipt",
        entity_id=receipt.id,
        metadata={"property_id": str(property_id)} if property_id else None,
    )
    return receipt
