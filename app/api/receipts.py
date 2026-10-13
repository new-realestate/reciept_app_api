import asyncio
import logging
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_audit_event
from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user
from app.core.rate_limit import receipt_create_rate_limiter
from app.db.session import get_db
from app.models.customer import Customer
from app.models.delivery import DeliveryAttempt
from app.models.enums import DeliveryChannel, DeliveryStatus, ReceiptStatus
from app.models.receipt import Receipt
from app.models.suppression import Suppression
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.receipt import DeliveryAttemptOut, ReceiptCreate, ReceiptDetailOut, ReceiptOut, SendReceiptRequest
from app.services.email import send_receipt_email
from app.services.pdf import format_money, read_stored_pdf, render_receipt_html
from app.services.receipts import create_receipt_record
from app.services.sms import InvalidPhoneNumberError, send_receipt_sms, to_e164

router = APIRouter(prefix="/receipts", tags=["receipts"])
public_router = APIRouter(tags=["public"])

logger = logging.getLogger(__name__)

EMAIL_RETRY_ATTEMPTS = 3
EMAIL_RETRY_BACKOFF_SECONDS = 1.0


async def _get_or_create_customer(db: AsyncSession, tenant_id: uuid.UUID, payload: ReceiptCreate) -> Customer:
    if payload.customer_id:
        result = await db.execute(
            select(Customer).where(Customer.id == payload.customer_id, Customer.tenant_id == tenant_id)
        )
        customer = result.scalar_one_or_none()
        if customer is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
        return customer

    filters = []
    if payload.customer_email:
        filters.append(Customer.email == payload.customer_email)
    if payload.customer_phone:
        filters.append(Customer.phone == payload.customer_phone)

    if filters:
        result = await db.execute(select(Customer).where(Customer.tenant_id == tenant_id, or_(*filters)))
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing

    customer = Customer(
        tenant_id=tenant_id,
        name=payload.customer_name,
        email=payload.customer_email,
        phone=payload.customer_phone,
    )
    db.add(customer)
    await db.flush()
    return customer


@router.post(
    "",
    response_model=ReceiptOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(receipt_create_rate_limiter)],
)
async def create_receipt(
    payload: ReceiptCreate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant = (await db.execute(select(Tenant).where(Tenant.id == current_user.tenant_id))).scalar_one()
    user = (await db.execute(select(User).where(User.id == current_user.id))).scalar_one()

    if payload.corrects_receipt_id:
        original = (
            await db.execute(
                select(Receipt).where(
                    Receipt.id == payload.corrects_receipt_id, Receipt.tenant_id == current_user.tenant_id
                )
            )
        ).scalar_one_or_none()
        if original is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Original receipt not found")

    customer = await _get_or_create_customer(db, current_user.tenant_id, payload)

    receipt = await create_receipt_record(
        db,
        tenant=tenant,
        user=user,
        customer=customer,
        amount_cents=payload.amount_cents,
        currency=payload.currency,
        reason=payload.reason,
        payment_method=payload.payment_method,
        payment_reference=payload.payment_reference,
        paid_at=payload.paid_at,
        balance_cents=payload.balance_cents,
        corrects_receipt_id=payload.corrects_receipt_id,
    )
    await db.commit()
    await db.refresh(receipt)
    return receipt


@router.get("", response_model=list[ReceiptOut])
async def list_receipts(
    response: Response,
    payer_name: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    amount_cents: int | None = Query(default=None),
    status_filter: ReceiptStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=200, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Receipt).where(Receipt.tenant_id == current_user.tenant_id)

    if payer_name:
        stmt = stmt.join(Customer, Customer.id == Receipt.customer_id).where(Customer.name.ilike(f"%{payer_name}%"))
    if date_from:
        stmt = stmt.where(Receipt.paid_at >= date_from)
    if date_to:
        stmt = stmt.where(Receipt.paid_at <= date_to)
    if amount_cents is not None:
        stmt = stmt.where(Receipt.amount_cents == amount_cents)
    if status_filter:
        stmt = stmt.where(Receipt.status == status_filter)

    # Still a plain array (existing clients unchanged); the full match count is in X-Total-Count.
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    response.headers["X-Total-Count"] = str(total)
    stmt = stmt.order_by(Receipt.created_at.desc(), Receipt.id).limit(limit).offset(offset)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/{receipt_id}", response_model=ReceiptDetailOut)
async def get_receipt(
    receipt_id: uuid.UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Receipt).where(Receipt.id == receipt_id, Receipt.tenant_id == current_user.tenant_id)
    )
    receipt = result.scalar_one_or_none()
    if receipt is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Receipt not found")

    attempts_result = await db.execute(
        select(DeliveryAttempt).where(DeliveryAttempt.receipt_id == receipt.id).order_by(DeliveryAttempt.attempted_at)
    )
    attempts = attempts_result.scalars().all()

    detail = ReceiptDetailOut.model_validate(receipt)
    detail.delivery_attempts = [DeliveryAttemptOut.model_validate(a) for a in attempts]
    return detail


async def _send_channel(
    db: AsyncSession,
    *,
    tenant: Tenant,
    receipt: Receipt,
    customer: Customer,
    channel: DeliveryChannel,
    email_mode: str,
    attempt_number: int,
) -> DeliveryAttempt:
    settings = get_settings()

    if channel == DeliveryChannel.email:
        if not customer.email:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Customer has no email on file")
        recipient = customer.email
    else:
        if not customer.phone:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Customer has no phone on file")
        try:
            recipient = to_e164(customer.phone)
        except InvalidPhoneNumberError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    suppressed_result = await db.execute(
        select(Suppression).where(
            Suppression.channel == channel,
            Suppression.contact == recipient,
            or_(Suppression.tenant_id == tenant.id, Suppression.tenant_id.is_(None)),
        )
    )
    if suppressed_result.scalar_one_or_none() is not None:
        attempt = DeliveryAttempt(
            receipt_id=receipt.id,
            channel=channel,
            status=DeliveryStatus.failed,
            recipient=recipient,
            attempt_number=attempt_number,
            provider_response={"error": "recipient is on the suppression list"},
            resolved_at=datetime.now(timezone.utc),
        )
        db.add(attempt)
        await db.flush()
        return attempt

    hosted_url = f"{settings.app_base_url}/r/{receipt.hosted_view_token}"
    amount_display = format_money(receipt.amount_cents, receipt.currency)

    attempt = DeliveryAttempt(
        receipt_id=receipt.id, channel=channel, recipient=recipient, attempt_number=attempt_number,
        status=DeliveryStatus.queued,
    )
    db.add(attempt)
    await db.flush()

    last_error: Exception | None = None
    max_tries = EMAIL_RETRY_ATTEMPTS if channel == DeliveryChannel.email else 1

    for try_index in range(max_tries):
        try:
            if channel == DeliveryChannel.email:
                pdf_bytes = read_stored_pdf(tenant_id=str(tenant.id), receipt_id=str(receipt.id))
                response = send_receipt_email(
                    to_email=recipient,
                    business_name=tenant.business_name,
                    amount_display=amount_display,
                    hosted_url=hosted_url,
                    pdf_bytes=pdf_bytes,
                    pdf_filename=f"{receipt.reference_number}.pdf",
                    mode=email_mode,
                )
                attempt.provider = "gmail"
                attempt.provider_message_id = response.get("MessageID")
            else:
                sid = send_receipt_sms(
                    to_phone_e164=recipient,
                    business_name=tenant.business_name,
                    amount_display=amount_display,
                    hosted_url=hosted_url,
                )
                attempt.provider = "twilio"
                attempt.provider_message_id = sid

            attempt.status = DeliveryStatus.sent
            attempt.resolved_at = datetime.now(timezone.utc)
            last_error = None
            break
        except Exception as exc:  # noqa: BLE001 - provider SDKs raise their own exception types
            last_error = exc
            logger.warning(
                "Delivery attempt failed (receipt=%s channel=%s try=%d/%d): %s",
                receipt.id, channel.value, try_index + 1, max_tries, exc,
            )
            if try_index < max_tries - 1:
                await asyncio.sleep(EMAIL_RETRY_BACKOFF_SECONDS * (try_index + 1))

    if last_error is not None:
        attempt.status = DeliveryStatus.failed
        attempt.provider_response = {"error": str(last_error)}
        attempt.resolved_at = datetime.now(timezone.utc)

    await db.flush()
    return attempt


async def _load_receipt_with_customer(db: AsyncSession, receipt_id: uuid.UUID, tenant_id: uuid.UUID):
    result = await db.execute(select(Receipt).where(Receipt.id == receipt_id, Receipt.tenant_id == tenant_id))
    receipt = result.scalar_one_or_none()
    if receipt is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Receipt not found")

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
    customer = (await db.execute(select(Customer).where(Customer.id == receipt.customer_id))).scalar_one()
    return receipt, tenant, customer


@router.post("/{receipt_id}/send", response_model=list[DeliveryAttemptOut])
async def send_receipt(
    receipt_id: uuid.UUID,
    payload: SendReceiptRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    receipt, tenant, customer = await _load_receipt_with_customer(db, receipt_id, current_user.tenant_id)

    attempts = []
    for channel in payload.channels:
        existing_count = await db.execute(
            select(DeliveryAttempt).where(DeliveryAttempt.receipt_id == receipt.id, DeliveryAttempt.channel == channel)
        )
        attempt_number = len(existing_count.scalars().all()) + 1
        attempt = await _send_channel(
            db,
            tenant=tenant,
            receipt=receipt,
            customer=customer,
            channel=channel,
            email_mode=payload.email_mode,
            attempt_number=attempt_number,
        )
        attempts.append(attempt)

    await record_audit_event(
        db,
        tenant_id=tenant.id,
        actor_user_id=current_user.id,
        action="receipt.sent",
        entity_type="receipt",
        entity_id=receipt.id,
        metadata={"channels": [c.value for c in payload.channels]},
    )
    await db.commit()
    for a in attempts:
        await db.refresh(a)
    return attempts


@router.post("/{receipt_id}/resend", response_model=list[DeliveryAttemptOut])
async def resend_receipt(
    receipt_id: uuid.UUID,
    payload: SendReceiptRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    receipt, tenant, customer = await _load_receipt_with_customer(db, receipt_id, current_user.tenant_id)

    attempts = []
    for channel in payload.channels:
        existing_count = await db.execute(
            select(DeliveryAttempt).where(DeliveryAttempt.receipt_id == receipt.id, DeliveryAttempt.channel == channel)
        )
        attempt_number = len(existing_count.scalars().all()) + 1
        attempt = await _send_channel(
            db,
            tenant=tenant,
            receipt=receipt,
            customer=customer,
            channel=channel,
            email_mode=payload.email_mode,
            attempt_number=attempt_number,
        )
        attempts.append(attempt)

    await record_audit_event(
        db,
        tenant_id=tenant.id,
        actor_user_id=current_user.id,
        action="receipt.resent",
        entity_type="receipt",
        entity_id=receipt.id,
        metadata={"channels": [c.value for c in payload.channels]},
    )
    await db.commit()
    for a in attempts:
        await db.refresh(a)
    return attempts


@public_router.get("/r/{hosted_view_token}", response_class=HTMLResponse)
async def hosted_receipt_view(hosted_view_token: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Receipt).where(Receipt.hosted_view_token == hosted_view_token))
    receipt = result.scalar_one_or_none()
    if receipt is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Receipt not found")

    tenant = (await db.execute(select(Tenant).where(Tenant.id == receipt.tenant_id))).scalar_one()
    customer = (await db.execute(select(Customer).where(Customer.id == receipt.customer_id))).scalar_one()
    user = (await db.execute(select(User).where(User.id == receipt.issued_by_user_id))).scalar_one_or_none()

    html = render_receipt_html(tenant=tenant, user=user, customer=customer, receipt=receipt)
    return HTMLResponse(content=html)
