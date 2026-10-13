import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.invoices import _ctx
from app.api.invoices import _detail as _invoice_detail
from app.core.audit import record_audit_event
from app.core.deps import CurrentUser, get_current_user
from app.core.permissions import require_manager
from app.db.session import get_db
from app.models.customer import Customer
from app.models.notification import NotificationEvent
from app.models.quotation import Quotation
from app.schemas.documents import (
    ConvertQuotationRequest,
    InvoiceDetailOut,
    NotificationEventOut,
    Page,
    QuotationCreate,
    QuotationOut,
    QuotationUpdate,
    SendDocumentRequest,
)
from app.services import notifications as notif
from app.services import quotations as svc
from app.services.customers import get_or_create_customer
from app.services.documents import quotation_pdf_bytes

router = APIRouter(prefix="/quotations", tags=["quotations"])


async def _out(db: AsyncSession, tenant, q: Quotation) -> QuotationOut:
    name = (await db.execute(select(Customer.name).where(Customer.id == q.customer_id))).scalar_one_or_none()
    inv = await svc.converted_invoice(db, q.id)
    return svc.serialize_quotation(q, name, notif.tenant_today(tenant), converted_invoice_id=inv.id if inv else None)


@router.post("", response_model=QuotationOut, status_code=status.HTTP_201_CREATED)
async def create_quotation(payload: QuotationCreate, current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    tenant, user = await _ctx(db, current_user)
    customer = await get_or_create_customer(
        db, tenant.id, customer_id=payload.customer_id, name=payload.customer_name,
        email=payload.customer_email, phone=payload.customer_phone,
    )
    q = await svc.create_draft(db, tenant=tenant, user=user, customer=customer, payload=payload)
    await db.commit()
    await db.refresh(q)
    return await _out(db, tenant, q)


@router.get("", response_model=Page[QuotationOut])
async def list_quotations(
    status_filter: str | None = Query(default=None, alias="status"),
    customer_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    q: str | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant, _ = await _ctx(db, current_user)
    today = notif.tenant_today(tenant)
    conds = [Quotation.tenant_id == tenant.id]
    open_states = ("sent", "viewed")
    if status_filter:
        if status_filter == "expired":
            conds.append(or_(Quotation.status == "expired", (Quotation.status.in_(open_states)) & (Quotation.valid_until < today)))
        elif status_filter in open_states:
            conds += [Quotation.status == status_filter, Quotation.valid_until >= today]
        elif status_filter == "pending":  # awaiting the customer's answer
            conds += [Quotation.status.in_(open_states), Quotation.valid_until >= today]
        elif status_filter in ("draft", "accepted", "rejected", "cancelled"):
            conds.append(Quotation.status == status_filter)
        else:
            raise HTTPException(status_code=422, detail="Unknown status filter")
    if customer_id:
        conds.append(Quotation.customer_id == customer_id)
    if date_from:
        conds.append(Quotation.issue_date >= date_from)
    if date_to:
        conds.append(Quotation.issue_date <= date_to)

    base = select(Quotation, Customer.name).join(Customer, Customer.id == Quotation.customer_id).where(*conds)
    if q:
        like = f"%{q}%"
        base = base.where(or_(Quotation.number.ilike(like), Quotation.title.ilike(like), Customer.name.ilike(like)))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (await db.execute(base.order_by(Quotation.created_at.desc(), Quotation.id).limit(limit).offset(offset))).all()
    items = [svc.serialize_quotation(row, name, today) for row, name in rows]
    return Page[QuotationOut](items=items, total=total, limit=limit, offset=offset)


@router.get("/{quotation_id}", response_model=QuotationOut)
async def get_quotation(quotation_id: uuid.UUID, current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    tenant, _ = await _ctx(db, current_user)
    return await _out(db, tenant, await svc.get_quotation(db, tenant.id, quotation_id))


@router.patch("/{quotation_id}", response_model=QuotationOut)
async def update_quotation(
    quotation_id: uuid.UUID, payload: QuotationUpdate, current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    tenant, user = await _ctx(db, current_user)
    q = await svc.get_quotation(db, tenant.id, quotation_id, lock=True)
    customer = None
    if payload.customer_id:
        customer = await get_or_create_customer(db, tenant.id, customer_id=payload.customer_id, name=None, email=None, phone=None)
    await svc.update_draft(db, tenant=tenant, user=user, quotation=q, payload=payload, customer=customer)
    await db.commit()
    await db.refresh(q)
    return await _out(db, tenant, q)


@router.post("/{quotation_id}/send", response_model=QuotationOut)
async def send_quotation(
    quotation_id: uuid.UUID,
    payload: SendDocumentRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Issues (if still a draft) and delivers the quotation. Calling again re-sends the same quotation."""
    tenant, user = await _ctx(db, current_user)
    q = await svc.get_quotation(db, tenant.id, quotation_id, lock=True)
    customer = (await db.execute(select(Customer).where(Customer.id == q.customer_id))).scalar_one()
    await svc.prepare_to_send(db, tenant=tenant, user=user, quotation=q, customer=customer)
    await db.commit()
    await notif.send_now(
        db, tenant_id=tenant.id, kind="quotation_sent", subject_type="quotation", subject_id=q.id,
        channels=[c.value for c in payload.channels],
    )
    await db.commit()
    await db.refresh(q)
    return await _out(db, tenant, q)


@router.post("/{quotation_id}/cancel", response_model=QuotationOut)
async def cancel_quotation(quotation_id: uuid.UUID, current_user: CurrentUser = Depends(require_manager), db: AsyncSession = Depends(get_db)):
    tenant, user = await _ctx(db, current_user)
    q = await svc.get_quotation(db, tenant.id, quotation_id, lock=True)
    await svc.cancel_quotation(db, tenant=tenant, user=user, quotation=q)
    await db.commit()
    await db.refresh(q)
    return await _out(db, tenant, q)


@router.post("/{quotation_id}/convert", response_model=InvoiceDetailOut, status_code=status.HTTP_201_CREATED)
async def convert_quotation(
    quotation_id: uuid.UUID, payload: ConvertQuotationRequest, current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Creates a draft invoice (or an issued one with issue_now) from an accepted quotation. Never creates a receipt."""
    tenant, user = await _ctx(db, current_user)
    q = await svc.get_quotation(db, tenant.id, quotation_id, lock=True)  # serialises concurrent conversions
    customer = (await db.execute(select(Customer).where(Customer.id == q.customer_id))).scalar_one()
    invoice = await svc.convert_to_invoice(db, tenant=tenant, user=user, quotation=q, customer=customer, payload=payload)
    if payload.issue_now:
        from app.services import reminders

        await reminders.plan_invoice_reminders(db, tenant, invoice, customer)
    await db.commit()
    await db.refresh(invoice)
    return await _invoice_detail(db, tenant, invoice)


@router.get("/{quotation_id}/pdf")
async def quotation_pdf(quotation_id: uuid.UUID, current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    tenant, _ = await _ctx(db, current_user)
    q = await svc.get_quotation(db, tenant.id, quotation_id)
    customer = (await db.execute(select(Customer).where(Customer.id == q.customer_id))).scalar_one()
    pdf = quotation_pdf_bytes(q, tenant, customer, notif.tenant_today(tenant))
    name = q.number or "quotation-draft"
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}.pdf"'})


@router.post("/{quotation_id}/rotate-link", response_model=QuotationOut)
async def rotate_quotation_link(quotation_id: uuid.UUID, current_user: CurrentUser = Depends(require_manager), db: AsyncSession = Depends(get_db)):
    from app.services.invoices import new_public_token

    tenant, user = await _ctx(db, current_user)
    q = await svc.get_quotation(db, tenant.id, quotation_id, lock=True)
    if q.status == "draft":
        raise svc.conflict("Send the quotation before sharing a link")
    q.public_token = new_public_token()
    q.token_revoked_at = None
    await record_audit_event(db, tenant_id=tenant.id, actor_user_id=user.id, action="quotation.link_rotated",
                             entity_type="quotation", entity_id=q.id)
    await db.commit()
    await db.refresh(q)
    return await _out(db, tenant, q)


@router.get("/{quotation_id}/notifications", response_model=list[NotificationEventOut])
async def quotation_notifications(quotation_id: uuid.UUID, current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    q = await svc.get_quotation(db, current_user.tenant_id, quotation_id)
    events = (
        await db.execute(
            select(NotificationEvent)
            .where(NotificationEvent.tenant_id == current_user.tenant_id, NotificationEvent.subject_type == "quotation",
                   NotificationEvent.subject_id == q.id)
            .order_by(NotificationEvent.scheduled_for.desc())
        )
    ).scalars().all()
    return await notif.events_with_attempts(db, list(events))
