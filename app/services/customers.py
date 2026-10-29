"""Customers: the recipients of receipts, invoices and quotations.

A customer belongs to one business and exists independently of any property or
tenancy. Matching for duplicates is always scoped to the caller's business and uses
exact identifiers (case-insensitive email, E.164 phone); a name-only match is only
ever a hint and is never merged automatically.
"""

import uuid

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer
from app.services.sms import InvalidPhoneNumberError, to_e164


def normalize_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    try:
        return to_e164(phone)
    except InvalidPhoneNumberError:
        return None


def normalize_email(email: str | None) -> str | None:
    email = (email or "").strip()
    return email.lower() or None


def set_contact(customer: Customer, *, email: str | None, phone: str | None) -> None:
    customer.email = (email or "").strip() or None
    customer.phone = (phone or "").strip() or None
    customer.phone_normalized = normalize_phone(customer.phone)


def _identifier_filters(email: str | None, phone: str | None) -> list:
    filters = []
    if (norm_email := normalize_email(email)) is not None:
        filters.append(func.lower(Customer.email) == norm_email)
    if phone:
        filters.append(Customer.phone == phone.strip())
        if (norm_phone := normalize_phone(phone)) is not None:
            filters.append(Customer.phone_normalized == norm_phone)
    return filters


async def find_matches(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    email: str | None,
    phone: str | None,
    name: str | None = None,
    exclude_id: uuid.UUID | None = None,
) -> tuple[list[Customer], list[Customer]]:
    """(exact matches by email/phone, possible matches by name only) within one business."""
    base = select(Customer).where(Customer.tenant_id == tenant_id)
    if exclude_id is not None:
        base = base.where(Customer.id != exclude_id)

    exact: list[Customer] = []
    if filters := _identifier_filters(email, phone):
        exact = list((await db.execute(base.where(or_(*filters)).order_by(Customer.created_at, Customer.id))).scalars().all())

    possible: list[Customer] = []
    if name and name.strip():
        exact_ids = {c.id for c in exact}
        rows = (
            await db.execute(base.where(func.lower(Customer.name) == name.strip().lower()).order_by(Customer.created_at).limit(10))
        ).scalars().all()
        possible = [c for c in rows if c.id not in exact_ids]
    return exact, possible


async def get_customer(db: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID) -> Customer:
    customer = (
        await db.execute(select(Customer).where(Customer.id == customer_id, Customer.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    return customer


async def get_or_create_customer(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    customer_id: uuid.UUID | None,
    name: str | None,
    email: str | None,
    phone: str | None,
) -> Customer:
    """Resolve an existing customer of this tenant, or reuse/create one from inline details.

    Inline details reuse the oldest customer with the same email or phone number, so
    issuing a document never silently creates a second record for a known contact.
    """
    if customer_id:
        customer = await get_customer(db, tenant_id, customer_id)
        if customer.status == "archived":
            raise HTTPException(status_code=422, detail="This customer is archived; restore them before issuing documents")
        return customer

    if not name:
        raise HTTPException(status_code=422, detail="Provide customer_id or customer_name")
    if not email and not phone:
        raise HTTPException(
            status_code=422, detail="A new customer needs an email or a phone number"
        )

    exact, _ = await find_matches(db, tenant_id, email=email, phone=phone)
    if exact:
        return exact[0]

    customer = Customer(tenant_id=tenant_id, name=name.strip())
    set_contact(customer, email=email, phone=phone)
    db.add(customer)
    await db.flush()
    return customer
