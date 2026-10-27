import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import (
    AccessScope,
    Perm,
    ensure_document_visible,
    get_scope,
    require_manager_scope,
    require_perm,
    scope_documents,
    validate_doc_context,
)
from app.core.audit import record_audit_event
from app.core.deps import CurrentUser
from app.db.session import get_db
from app.models.customer import Customer
from app.models.enums import DeliveryChannel
from app.models.invoice import Invoice, InvoicePayment
from app.models.notification import NotificationEvent
from app.models.receipt import Receipt
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.documents import (
    CancelRequest,
    InvoiceCreate,
    InvoiceDetailOut,
    InvoiceOut,
    InvoicePaymentCreate,
    InvoicePaymentOut,
    InvoiceUpdate,
    NotificationEventOut,
    Page,
    SendDocumentRequest,
)
from app.services import invoices as svc
from app.services import notifications as notif
from app.services import reminders
from app.services import schedules as schedules_svc
from app.services.customers import get_or_create_customer
from app.services.documents import invoice_pdf_bytes

router = APIRouter(prefix="/invoices", tags=["invoices"])

MANUAL_REMINDER_COOLDOWN = timedelta(hours=1)


class IssueRequest(BaseModel):
    channels: list[DeliveryChannel] = Field(default_factory=list)


class InvoiceReminderSettings(BaseModel):
    enabled: bool | None = None
    offsets: list[int] | None = None
    channels: list[str] | None = None
    send_hour: int | None = None


class PaymentRecordedOut(BaseModel):
    invoice: InvoiceDetailOut
    payment: InvoicePaymentOut
    receipt_id: uuid.UUID
    receipt_url: str
    replayed: bool = False


async def _ctx(db: AsyncSession, current_user: CurrentUser) -> tuple[Tenant, User]:
    tenant = (await db.execute(select(Tenant).where(Tenant.id == current_user.tenant_id))).scalar_one()
    user = (await db.execute(select(User).where(User.id == current_user.id))).scalar_one()
    return tenant, user


async def _get_invoice(db: AsyncSession, scope: AccessScope, invoice_id: uuid.UUID, *, lock: bool = False) -> Invoice:
    invoice = await svc.get_invoice(db, scope.tenant_id, invoice_id, lock=lock)
    return ensure_document_visible(scope, invoice, "Invoice")


async def _customer_name(db: AsyncSession, customer_id: uuid.UUID) -> str | None:
    return (await db.execute(select(Customer.name).where(Customer.id == customer_id))).scalar_one_or_none()


async def _detail(db: AsyncSession, tenant: Tenant, invoice: Invoice) -> InvoiceDetailOut:
    payments = (
        await db.execute(select(InvoicePayment).where(InvoicePayment.invoice_id == invoice.id).order_by(InvoicePayment.created_at))
    ).scalars().all()
    return svc.serialize_invoice(invoice, await _customer_name(db, invoice.customer_id), notif.tenant_today(tenant), payments=list(payments))


def _receipt_url(receipt: Receipt) -> str:
    from app.core.config import get_settings

    return f"{get_settings().app_base_url.rstrip('/')}/r/{receipt.hosted_view_token}"


@router.post("", response_model=InvoiceDetailOut, status_code=status.HTTP_201_CREATED)
async def create_invoice(
    payload: InvoiceCreate, current_user: AccessScope = Depends(require_perm(Perm.invoices_issue)), db: AsyncSession = Depends(get_db)
):
    tenant, user = await _ctx(db, current_user)
    customer = await get_or_create_customer(
        db, tenant.id, customer_id=payload.customer_id, name=payload.customer_name,
        email=payload.customer_email, phone=payload.customer_phone,
    )
    ctx = await validate_doc_context(
        db, current_user, property_id=payload.property_id, unit_id=payload.unit_id, tenancy_id=payload.tenancy_id,
        customer_id=customer.id,
    )
    invoice = await svc.create_draft(
        db, tenant=tenant, user=user, customer=customer, payload=payload,
        property_id=ctx.property_id, unit_id=ctx.unit_id, tenancy_id=ctx.tenancy_id,
    )
    if payload.issue_now:
        await svc.issue_invoice(db, tenant=tenant, user=user, invoice=invoice, customer=customer)
        await reminders.plan_invoice_reminders(db, tenant, invoice, customer)
    await db.commit()
    await db.refresh(invoice)
    return await _detail(db, tenant, invoice)


@router.get("", response_model=Page[InvoiceOut])
async def list_invoices(
    status_filter: str | None = Query(default=None, alias="status"),
    customer_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    q: str | None = Query(default=None, description="Invoice number, title or customer name"),
    property_id: uuid.UUID | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: AccessScope = Depends(get_scope),
    db: AsyncSession = Depends(get_db),
):
    tenant = (await db.execute(select(Tenant).where(Tenant.id == current_user.tenant_id))).scalar_one()
    today = notif.tenant_today(tenant)

    conds = [Invoice.tenant_id == tenant.id]
    outstanding = Invoice.total_cents - Invoice.amount_paid_cents
    if status_filter:
        s = status_filter
        if s in ("draft", "cancelled"):
            conds.append(Invoice.status == s)
        elif s == "paid":
            conds += [Invoice.status == "issued", outstanding <= 0]
        elif s == "overdue":
            conds += [Invoice.status == "issued", outstanding > 0, Invoice.due_date < today]
        elif s == "partially_paid":
            conds += [Invoice.status == "issued", Invoice.amount_paid_cents > 0, outstanding > 0, Invoice.due_date >= today]
        elif s == "issued":
            conds += [Invoice.status == "issued", Invoice.amount_paid_cents == 0, Invoice.due_date >= today]
        elif s == "unpaid":  # issued and still owing, overdue or not
            conds += [Invoice.status == "issued", outstanding > 0]
        else:
            raise HTTPException(status_code=422, detail="Unknown status filter")
    if customer_id:
        conds.append(Invoice.customer_id == customer_id)
    if property_id:
        conds.append(Invoice.property_id == property_id)
    if date_from:
        conds.append(Invoice.issue_date >= date_from)
    if date_to:
        conds.append(Invoice.issue_date <= date_to)

    base = scope_documents(
        select(Invoice, Customer.name).join(Customer, Customer.id == Invoice.customer_id).where(*conds), Invoice, current_user
    )
    if q:
        like = f"%{q}%"
        base = base.where(or_(Invoice.number.ilike(like), Invoice.title.ilike(like), Customer.name.ilike(like)))

    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        await db.execute(base.order_by(Invoice.created_at.desc(), Invoice.id).limit(limit).offset(offset))
    ).all()
    items = [svc.serialize_invoice(inv, name, today) for inv, name in rows]
    return Page[InvoiceOut](items=items, total=total, limit=limit, offset=offset)


@router.get("/{invoice_id}", response_model=InvoiceDetailOut)
async def get_invoice(invoice_id: uuid.UUID, current_user: AccessScope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    tenant, _ = await _ctx(db, current_user)
    invoice = await _get_invoice(db, current_user, invoice_id)
    return await _detail(db, tenant, invoice)


@router.patch("/{invoice_id}", response_model=InvoiceDetailOut)
async def update_invoice(
    invoice_id: uuid.UUID, payload: InvoiceUpdate, current_user: AccessScope = Depends(require_perm(Perm.invoices_issue)), db: AsyncSession = Depends(get_db)
):
    tenant, user = await _ctx(db, current_user)
    invoice = await _get_invoice(db, current_user, invoice_id, lock=True)
    customer = None
    if payload.customer_id:
        customer = await get_or_create_customer(db, tenant.id, customer_id=payload.customer_id, name=None, email=None, phone=None)
    await svc.update_draft(db, tenant=tenant, user=user, invoice=invoice, payload=payload, customer=customer)
    await db.commit()
    await db.refresh(invoice)
    return await _detail(db, tenant, invoice)


@router.post("/{invoice_id}/issue", response_model=InvoiceDetailOut)
async def issue_invoice(
    invoice_id: uuid.UUID,
    payload: IssueRequest | None = None,
    current_user: AccessScope = Depends(require_perm(Perm.invoices_issue)),
    db: AsyncSession = Depends(get_db),
):
    tenant, user = await _ctx(db, current_user)
    invoice = await _get_invoice(db, current_user, invoice_id, lock=True)
    customer = (await db.execute(select(Customer).where(Customer.id == invoice.customer_id))).scalar_one()
    await svc.issue_invoice(db, tenant=tenant, user=user, invoice=invoice, customer=customer)
    await reminders.plan_invoice_reminders(db, tenant, invoice, customer)
    await db.commit()
    if payload and payload.channels:
        await notif.send_now(
            db, tenant_id=tenant.id, kind="invoice_issued", subject_type="invoice", subject_id=invoice.id,
            channels=[c.value for c in payload.channels],
        )
        await db.commit()
    await db.refresh(invoice)
    return await _detail(db, tenant, invoice)


@router.post("/{invoice_id}/cancel", response_model=InvoiceDetailOut)
async def cancel_invoice(
    invoice_id: uuid.UUID,
    payload: CancelRequest | None = None,
    current_user: AccessScope = Depends(require_manager_scope),
    db: AsyncSession = Depends(get_db),
):
    tenant, user = await _ctx(db, current_user)
    invoice = await _get_invoice(db, current_user, invoice_id, lock=True)
    await svc.cancel_invoice(db, tenant=tenant, user=user, invoice=invoice, reason=payload.reason if payload else None)
    await notif.cancel_pending_events(db, subject_type="invoice", subject_id=invoice.id, reason="invoice cancelled")
    await schedules_svc.release_occurrence(db, invoice)
    await db.commit()
    await db.refresh(invoice)
    return await _detail(db, tenant, invoice)


@router.post("/{invoice_id}/payments", response_model=PaymentRecordedOut, status_code=status.HTTP_201_CREATED)
async def record_invoice_payment(
    invoice_id: uuid.UUID,
    payload: InvoicePaymentCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
    current_user: AccessScope = Depends(require_perm(Perm.invoices_record_payment)),
    db: AsyncSession = Depends(get_db),
):
    tenant, user = await _ctx(db, current_user)
    await _get_invoice(db, current_user, invoice_id)  # 404 unless the caller may see this invoice
    invoice, payment, replayed = await svc.record_payment(
        db, tenant=tenant, user=user, invoice_id=invoice_id, payload=payload, idempotency_key=idempotency_key
    )
    receipt = (await db.execute(select(Receipt).where(Receipt.id == payment.receipt_id))).scalar_one()
    if replayed:
        response.status_code = status.HTTP_200_OK
    else:
        if invoice.amount_paid_cents >= invoice.total_cents:
            await notif.cancel_pending_events(db, subject_type="invoice", subject_id=invoice.id, reason="invoice fully paid")
        await schedules_svc.sync_occurrence_from_invoice(db, invoice)
        await db.commit()
        await db.refresh(payment)
        await db.refresh(invoice)
        # Confirmation to the customer. Best effort: a delivery failure never undoes the recorded payment.
        try:
            await notif.send_now(
                db, tenant_id=tenant.id, kind="payment_recorded", subject_type="invoice", subject_id=invoice.id,
                channels=reminders.resolve_settings(tenant)["channels"],
                payload={"amount_cents": payment.amount_cents, "receipt_url": _receipt_url(receipt)},
            )
            await db.commit()
        except Exception:  # noqa: BLE001
            await db.rollback()
    return PaymentRecordedOut(
        invoice=await _detail(db, tenant, invoice),
        payment=InvoicePaymentOut.model_validate(payment),
        receipt_id=receipt.id,
        receipt_url=_receipt_url(receipt),
        replayed=replayed,
    )


@router.get("/{invoice_id}/payments", response_model=list[InvoicePaymentOut])
async def list_invoice_payments(invoice_id: uuid.UUID, current_user: AccessScope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    invoice = await _get_invoice(db, current_user, invoice_id)
    rows = await db.execute(select(InvoicePayment).where(InvoicePayment.invoice_id == invoice.id).order_by(InvoicePayment.created_at))
    return rows.scalars().all()


@router.get("/{invoice_id}/pdf")
async def invoice_pdf(invoice_id: uuid.UUID, current_user: AccessScope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    tenant, _ = await _ctx(db, current_user)
    invoice = await _get_invoice(db, current_user, invoice_id)
    customer = (await db.execute(select(Customer).where(Customer.id == invoice.customer_id))).scalar_one()
    payments = (await db.execute(select(InvoicePayment).where(InvoicePayment.invoice_id == invoice.id))).scalars().all()
    pdf = invoice_pdf_bytes(invoice, tenant, customer, list(payments), notif.tenant_today(tenant))
    name = invoice.number or "invoice-draft"
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}.pdf"'})


@router.post("/{invoice_id}/send", response_model=list[NotificationEventOut])
async def send_invoice(
    invoice_id: uuid.UUID, payload: SendDocumentRequest, current_user: AccessScope = Depends(require_perm(Perm.invoices_issue)), db: AsyncSession = Depends(get_db)
):
    tenant, user = await _ctx(db, current_user)
    invoice = await _get_invoice(db, current_user, invoice_id)
    if invoice.status != "issued":
        raise svc.conflict("Only issued invoices can be sent")
    if invoice.token_revoked_at:
        raise svc.conflict("The public link for this invoice is revoked. Create a new link before sending.")
    events = await notif.send_now(
        db, tenant_id=tenant.id, kind="invoice_issued", subject_type="invoice", subject_id=invoice.id,
        channels=[c.value for c in payload.channels],
    )
    await record_audit_event(db, tenant_id=tenant.id, actor_user_id=user.id, action="invoice.sent", entity_type="invoice",
                             entity_id=invoice.id, metadata={"channels": [c.value for c in payload.channels]})
    await db.commit()
    return await notif.events_with_attempts(db, events)


@router.post("/{invoice_id}/remind", response_model=list[NotificationEventOut])
async def remind_now(
    invoice_id: uuid.UUID, payload: SendDocumentRequest, current_user: AccessScope = Depends(require_perm(Perm.invoices_issue)), db: AsyncSession = Depends(get_db)
):
    """Manual reminder. Only for invoices that still have a balance; limited to one per channel per hour."""
    tenant, user = await _ctx(db, current_user)
    invoice = await _get_invoice(db, current_user, invoice_id)
    if invoice.status != "issued":
        raise svc.conflict("Reminders can only be sent for issued invoices")
    if invoice.total_cents - invoice.amount_paid_cents <= 0:
        raise svc.conflict("This invoice is fully paid, so no reminder is needed")
    channels = [c.value for c in payload.channels]
    cutoff = datetime.now(timezone.utc) - MANUAL_REMINDER_COOLDOWN
    recent = (
        await db.execute(
            select(NotificationEvent.channel).where(
                NotificationEvent.subject_id == invoice.id, NotificationEvent.kind == "reminder_manual",
                NotificationEvent.status == "sent", NotificationEvent.sent_at >= cutoff,
            )
        )
    ).scalars().all()
    blocked = sorted(set(channels) & set(recent))
    if blocked:
        raise svc.conflict(f"A reminder was already sent by {', '.join(blocked)} in the last hour")
    events = await notif.send_now(
        db, tenant_id=tenant.id, kind="reminder_manual", subject_type="invoice", subject_id=invoice.id, channels=channels
    )
    await record_audit_event(db, tenant_id=tenant.id, actor_user_id=user.id, action="invoice.reminder_sent", entity_type="invoice",
                             entity_id=invoice.id, metadata={"channels": channels})
    await db.commit()
    return await notif.events_with_attempts(db, events)


@router.patch("/{invoice_id}/reminders", response_model=InvoiceDetailOut)
async def update_invoice_reminders(
    invoice_id: uuid.UUID, payload: InvoiceReminderSettings, current_user: AccessScope = Depends(require_perm(Perm.invoices_issue)), db: AsyncSession = Depends(get_db)
):
    """Enable/disable reminders for one invoice and/or override intervals; pending reminders are re-planned."""
    tenant, user = await _ctx(db, current_user)
    invoice = await _get_invoice(db, current_user, invoice_id, lock=True)
    if invoice.status in ("cancelled",):
        raise svc.conflict("Cancelled invoices have no reminders")
    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    try:
        overrides = reminders.validate_settings({k: v for k, v in data.items() if k != "enabled"})
    except reminders.SettingsError as exc:
        raise svc.unprocessable(str(exc)) from exc
    if "enabled" in data:
        invoice.reminders_enabled = data["enabled"]
    if overrides:
        invoice.reminder_overrides = {**(invoice.reminder_overrides or {}), **overrides}
    customer = (await db.execute(select(Customer).where(Customer.id == invoice.customer_id))).scalar_one()
    if invoice.status == "issued":
        await reminders.replan_invoice_reminders(db, tenant, invoice, customer)
    await record_audit_event(db, tenant_id=tenant.id, actor_user_id=user.id, action="invoice.reminders_updated",
                             entity_type="invoice", entity_id=invoice.id, metadata=data)
    await db.commit()
    await db.refresh(invoice)
    return await _detail(db, tenant, invoice)


@router.post("/{invoice_id}/rotate-link", response_model=InvoiceDetailOut)
async def rotate_link(invoice_id: uuid.UUID, current_user: AccessScope = Depends(require_manager_scope), db: AsyncSession = Depends(get_db)):
    tenant, user = await _ctx(db, current_user)
    invoice = await _get_invoice(db, current_user, invoice_id, lock=True)
    await svc.rotate_public_link(db, tenant=tenant, user=user, invoice=invoice, revoke_only=False)
    await db.commit()
    await db.refresh(invoice)
    return await _detail(db, tenant, invoice)


@router.post("/{invoice_id}/revoke-link", response_model=InvoiceDetailOut)
async def revoke_link(invoice_id: uuid.UUID, current_user: AccessScope = Depends(require_manager_scope), db: AsyncSession = Depends(get_db)):
    tenant, user = await _ctx(db, current_user)
    invoice = await _get_invoice(db, current_user, invoice_id, lock=True)
    await svc.rotate_public_link(db, tenant=tenant, user=user, invoice=invoice, revoke_only=True)
    await db.commit()
    await db.refresh(invoice)
    return await _detail(db, tenant, invoice)


@router.get("/{invoice_id}/notifications", response_model=list[NotificationEventOut])
async def invoice_notifications(invoice_id: uuid.UUID, current_user: AccessScope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    invoice = await _get_invoice(db, current_user, invoice_id)
    events = (
        await db.execute(
            select(NotificationEvent)
            .where(NotificationEvent.tenant_id == current_user.tenant_id, NotificationEvent.subject_type == "invoice",
                   NotificationEvent.subject_id == invoice.id)
            .order_by(NotificationEvent.scheduled_for.desc())
        )
    ).scalars().all()
    return await notif.events_with_attempts(db, list(events))
