import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessScope, Perm, require_perm, scope_documents
from app.core.audit import record_audit_event
from app.db.paging import paginate
from app.db.session import get_db
from app.models.customer import Customer
from app.models.invoice import Invoice
from app.models.quotation import Quotation
from app.models.receipt import Receipt
from app.models.renter import Renter
from app.models.tenant import Tenant
from app.schemas.customer import (
    CustomerCreate,
    CustomerDocumentsOut,
    CustomerDocumentSummary,
    CustomerMatches,
    CustomerOut,
    CustomerUpdate,
)
from app.services import notifications as notif
from app.services.customers import find_matches, get_customer, set_contact
from app.services.documents import derive_invoice_status, derive_quotation_status, outstanding_cents

router = APIRouter(prefix="/customers", tags=["customers"])


async def _renter_ids(db: AsyncSession, customer_ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    if not customer_ids:
        return {}
    rows = await db.execute(select(Renter.customer_id, Renter.id).where(Renter.customer_id.in_(customer_ids)))
    return dict(rows.all())


async def _out(db: AsyncSession, customers: list[Customer]) -> list[CustomerOut]:
    renters = await _renter_ids(db, [c.id for c in customers])
    out = []
    for c in customers:
        item = CustomerOut.model_validate(c)
        item.renter_id = renters.get(c.id)
        out.append(item)
    return out


@router.get("/matches", response_model=CustomerMatches)
async def customer_matches(
    email: str | None = None,
    phone: str | None = None,
    name: str | None = None,
    current_user: AccessScope = Depends(require_perm(Perm.customers_view)),
    db: AsyncSession = Depends(get_db),
):
    """Possible duplicates in this business before creating a customer: exact by email/phone, possible by name."""
    exact, possible = await find_matches(db, current_user.tenant_id, email=email, phone=phone, name=name)
    return CustomerMatches(exact=await _out(db, exact), possible=await _out(db, possible))


@router.post("", response_model=CustomerOut, status_code=201)
async def create_customer(
    payload: CustomerCreate,
    current_user: AccessScope = Depends(require_perm(Perm.customers_manage)),
    db: AsyncSession = Depends(get_db),
):
    if not payload.allow_duplicate:
        exact, _ = await find_matches(db, current_user.tenant_id, email=payload.email, phone=payload.phone)
        if exact:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "A customer with this email or phone number already exists",
                    "matches": [m.model_dump(mode="json") for m in await _out(db, exact)],
                },
            )

    customer = Customer(
        tenant_id=current_user.tenant_id,
        name=payload.name.strip(),
        **payload.model_dump(include={"customer_type", "legal_name", "billing_address", "tax_identification_number", "notes", "contact_preference"}),
    )
    set_contact(customer, email=payload.email, phone=payload.phone)
    db.add(customer)
    await db.flush()
    await record_audit_event(
        db, tenant_id=current_user.tenant_id, actor_user_id=current_user.id, action="customer.created",
        entity_type="customer", entity_id=customer.id,
    )
    await db.commit()
    await db.refresh(customer)
    return (await _out(db, [customer]))[0]


@router.get("", response_model=list[CustomerOut])
async def list_customers(
    response: Response,
    q: str | None = Query(default=None, description="Search by name, email, or phone"),
    status_filter: str | None = Query(default=None, alias="status", pattern="^(active|archived)$"),
    is_renter: bool | None = Query(default=None, description="Only customers with (true) or without (false) a tenant profile"),
    limit: int = Query(default=200, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AccessScope = Depends(require_perm(Perm.customers_view)),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Customer).where(Customer.tenant_id == current_user.tenant_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Customer.name.ilike(like), Customer.email.ilike(like), Customer.phone.ilike(like)))
    if status_filter:
        stmt = stmt.where(Customer.status == status_filter)
    if is_renter is not None:
        has_renter = select(Renter.id).where(Renter.customer_id == Customer.id).exists()
        stmt = stmt.where(has_renter if is_renter else ~has_renter)
    rows, total = await paginate(db, stmt.order_by(Customer.created_at.desc(), Customer.id), limit=limit, offset=offset)
    response.headers["X-Total-Count"] = str(total)
    return await _out(db, rows)


@router.get("/{customer_id}", response_model=CustomerOut)
async def get_customer_detail(
    customer_id: uuid.UUID,
    current_user: AccessScope = Depends(require_perm(Perm.customers_view)),
    db: AsyncSession = Depends(get_db),
):
    return (await _out(db, [await get_customer(db, current_user.tenant_id, customer_id)]))[0]


@router.patch("/{customer_id}", response_model=CustomerOut)
async def update_customer(
    customer_id: uuid.UUID,
    payload: CustomerUpdate,
    current_user: AccessScope = Depends(require_perm(Perm.customers_manage)),
    db: AsyncSession = Depends(get_db),
):
    """Edits contact details. Issued documents keep the details frozen when they were issued."""
    customer = await get_customer(db, current_user.tenant_id, customer_id)
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is None:
        raise HTTPException(status_code=422, detail="Name cannot be empty")
    email = changes.pop("email", customer.email)
    phone = changes.pop("phone", customer.phone)
    if not email and not phone:
        raise HTTPException(status_code=422, detail="A customer needs an email or a phone number")
    for field, value in changes.items():
        setattr(customer, field, value)
    set_contact(customer, email=email, phone=phone)
    await record_audit_event(
        db, tenant_id=current_user.tenant_id, actor_user_id=current_user.id, action="customer.updated",
        entity_type="customer", entity_id=customer.id, metadata={"fields": sorted(payload.model_dump(exclude_unset=True))},
    )
    await db.commit()
    await db.refresh(customer)
    return (await _out(db, [customer]))[0]


async def _set_status(customer_id: uuid.UUID, new_status: str, current_user: AccessScope, db: AsyncSession):
    customer = await get_customer(db, current_user.tenant_id, customer_id)
    if customer.status != new_status:
        customer.status = new_status
        await record_audit_event(
            db, tenant_id=current_user.tenant_id, actor_user_id=current_user.id,
            action="customer.archived" if new_status == "archived" else "customer.restored",
            entity_type="customer", entity_id=customer.id,
        )
        await db.commit()
        await db.refresh(customer)
    return (await _out(db, [customer]))[0]


@router.post("/{customer_id}/archive", response_model=CustomerOut)
async def archive_customer(
    customer_id: uuid.UUID,
    current_user: AccessScope = Depends(require_perm(Perm.customers_manage)),
    db: AsyncSession = Depends(get_db),
):
    return await _set_status(customer_id, "archived", current_user, db)


@router.post("/{customer_id}/restore", response_model=CustomerOut)
async def restore_customer(
    customer_id: uuid.UUID,
    current_user: AccessScope = Depends(require_perm(Perm.customers_manage)),
    db: AsyncSession = Depends(get_db),
):
    return await _set_status(customer_id, "active", current_user, db)


@router.get("/{customer_id}/documents", response_model=CustomerDocumentsOut)
async def customer_documents(
    customer_id: uuid.UUID,
    current_user: AccessScope = Depends(require_perm(Perm.customers_view)),
    db: AsyncSession = Depends(get_db),
):
    """The customer's receipts, invoices and quotations the caller may see, plus what they still owe."""
    customer = await get_customer(db, current_user.tenant_id, customer_id)
    tenant = await db.get(Tenant, current_user.tenant_id)
    today = notif.tenant_today(tenant)

    receipts = (
        await db.execute(
            scope_documents(select(Receipt), Receipt, current_user)
            .where(Receipt.customer_id == customer.id).order_by(Receipt.paid_at.desc(), Receipt.created_at.desc())
        )
    ).scalars().all()
    invoices = (
        await db.execute(
            scope_documents(select(Invoice), Invoice, current_user)
            .where(Invoice.customer_id == customer.id).order_by(Invoice.issue_date.desc(), Invoice.created_at.desc())
        )
    ).scalars().all()
    quotations = (
        await db.execute(
            scope_documents(select(Quotation), Quotation, current_user)
            .where(Quotation.customer_id == customer.id).order_by(Quotation.issue_date.desc(), Quotation.created_at.desc())
        )
    ).scalars().all()

    outstanding: dict[str, int] = {}
    invoice_rows = []
    for inv in invoices:
        owed = outstanding_cents(inv) if inv.status == "issued" else 0
        if owed > 0:
            outstanding[inv.currency] = outstanding.get(inv.currency, 0) + owed
        invoice_rows.append(CustomerDocumentSummary(
            id=inv.id, kind="invoice", number=inv.number, status=derive_invoice_status(inv, today), date=inv.issue_date,
            currency=inv.currency, amount_cents=inv.total_cents, outstanding_cents=owed, property_id=inv.property_id,
        ))

    return CustomerDocumentsOut(
        customer=(await _out(db, [customer]))[0],
        receipts=[
            CustomerDocumentSummary(
                id=r.id, kind="receipt", number=r.reference_number, status=r.status.value, date=r.paid_at,
                currency=r.currency, amount_cents=r.amount_cents, property_id=r.property_id,
            )
            for r in receipts
        ],
        invoices=invoice_rows,
        quotations=[
            CustomerDocumentSummary(
                id=q.id, kind="quotation", number=q.number, status=derive_quotation_status(q, today), date=q.issue_date,
                currency=q.currency, amount_cents=q.total_cents, property_id=q.property_id,
            )
            for q in quotations
        ],
        outstanding_by_currency=outstanding,
    )
