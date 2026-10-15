import uuid

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer


async def get_or_create_customer(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    customer_id: uuid.UUID | None,
    name: str | None,
    email: str | None,
    phone: str | None,
) -> Customer:
    """Resolve an existing customer of this tenant, or create/reuse one from inline details."""
    if customer_id:
        customer = (
            await db.execute(select(Customer).where(Customer.id == customer_id, Customer.tenant_id == tenant_id))
        ).scalar_one_or_none()
        if customer is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
        return customer

    if not name:
        raise HTTPException(status_code=422, detail="Provide customer_id or customer_name")
    if not email and not phone:
        raise HTTPException(
            status_code=422, detail="A new customer needs an email or a phone number"
        )

    filters = []
    if email:
        filters.append(Customer.email == email)
    if phone:
        filters.append(Customer.phone == phone)
    existing = (
        await db.execute(select(Customer).where(Customer.tenant_id == tenant_id, or_(*filters)))
    ).scalars().first()
    if existing is not None:
        return existing

    customer = Customer(tenant_id=tenant_id, name=name, email=email, phone=phone)
    db.add(customer)
    await db.flush()
    return customer
