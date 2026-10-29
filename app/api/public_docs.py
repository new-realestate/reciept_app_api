"""Public, unauthenticated invoice and quotation pages, addressed only by an unguessable token.

These routes expose a read-only view (plus accept/reject for quotations) and nothing else:
no internal notes, no user details, no other customers' data.
"""
from fastapi import APIRouter, Depends, Form, HTTPException, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rate_limit import public_doc_rate_limiter, public_response_rate_limiter
from app.db.session import get_db
from app.models.customer import Customer
from app.models.invoice import Invoice, InvoicePayment
from app.models.tenant import Tenant
from app.services import quotations as qsvc
from app.services.documents import derive_quotation_status, invoice_html, invoice_pdf_bytes, quotation_html, quotation_pdf_bytes
from app.services.notifications import tenant_today

router = APIRouter(tags=["public"], dependencies=[Depends(public_doc_rate_limiter)])

NO_STORE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer"}
MESSAGES = {
    "accepted": ("Thank you. Your acceptance has been recorded and the business has been notified.", "ok"),
    "rejected": ("Your response has been recorded. Thank you for letting us know.", "ok"),
    "unavailable": ("This quotation can no longer be answered.", "bad"),
}


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")


async def _invoice_by_token(db: AsyncSession, token: str):
    invoice = (
        await db.execute(
            select(Invoice).where(Invoice.public_token == token, Invoice.token_revoked_at.is_(None), Invoice.status != "draft")
        )
    ).scalar_one_or_none()
    if invoice is None:
        raise _not_found()
    tenant = await db.get_one(Tenant, invoice.tenant_id)
    customer = (await db.execute(select(Customer).where(Customer.id == invoice.customer_id))).scalar_one()
    payments = (
        await db.execute(select(InvoicePayment).where(InvoicePayment.invoice_id == invoice.id).order_by(InvoicePayment.created_at))
    ).scalars().all()
    return invoice, tenant, customer, list(payments)


@router.get("/i/{token}", response_class=HTMLResponse)
async def public_invoice(token: str, db: AsyncSession = Depends(get_db)):
    invoice, tenant, customer, payments = await _invoice_by_token(db, token)
    html = invoice_html(invoice, tenant, customer, payments, tenant_today(tenant), template="public_document.html",
                        pdf_href=f"/i/{token}/pdf", respond=None, message=None)
    return HTMLResponse(content=html, headers=NO_STORE)


@router.get("/i/{token}/pdf")
async def public_invoice_pdf(token: str, db: AsyncSession = Depends(get_db)):
    invoice, tenant, customer, payments = await _invoice_by_token(db, token)
    pdf = invoice_pdf_bytes(invoice, tenant, customer, payments, tenant_today(tenant))
    return Response(content=pdf, media_type="application/pdf",
                    headers={**NO_STORE, "Content-Disposition": f'inline; filename="{invoice.number}.pdf"'})


async def _quotation_context(db: AsyncSession, token: str):
    quotation = await qsvc.get_by_token(db, token)
    if quotation is None or quotation.status == "draft":
        raise _not_found()
    tenant = await db.get_one(Tenant, quotation.tenant_id)
    customer = (await db.execute(select(Customer).where(Customer.id == quotation.customer_id))).scalar_one()
    return quotation, tenant, customer


@router.get("/q/{token}", response_class=HTMLResponse)
async def public_quotation(token: str, r: str | None = None, db: AsyncSession = Depends(get_db)):
    quotation, tenant, customer = await _quotation_context(db, token)
    today = tenant_today(tenant)
    if quotation.status == "sent" and derive_quotation_status(quotation, today) != "expired":
        await qsvc.mark_viewed(db, quotation)
        await db.commit()

    can_answer = quotation.status in ("sent", "viewed") and derive_quotation_status(quotation, today) != "expired"
    message, tone = MESSAGES.get(r or "", (None, None))
    html = quotation_html(
        quotation, tenant, customer, today, template="public_document.html", pdf_href=f"/q/{token}/pdf",
        respond={"accept_href": f"/q/{token}/accept", "reject_href": f"/q/{token}/reject"} if can_answer else None,
        message=message, message_tone=tone,
    )
    return HTMLResponse(content=html, headers=NO_STORE)


@router.get("/q/{token}/pdf")
async def public_quotation_pdf(token: str, db: AsyncSession = Depends(get_db)):
    quotation, tenant, customer = await _quotation_context(db, token)
    pdf = quotation_pdf_bytes(quotation, tenant, customer, tenant_today(tenant))
    return Response(content=pdf, media_type="application/pdf",
                    headers={**NO_STORE, "Content-Disposition": f'inline; filename="{quotation.number}.pdf"'})


async def _respond(token: str, db: AsyncSession, *, accept: bool, name: str | None, comment: str | None):
    quotation = await qsvc.get_by_token(db, token, lock=True)
    if quotation is None or quotation.status == "draft":
        raise _not_found()
    tenant = await db.get_one(Tenant, quotation.tenant_id)
    try:
        await qsvc.respond(db, quotation, tenant=tenant, accept=accept, name=name, comment=comment)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_409_CONFLICT:
            raise
        await db.rollback()
        return RedirectResponse(f"/q/{token}?r=unavailable", status_code=status.HTTP_303_SEE_OTHER, headers=NO_STORE)
    await db.commit()
    return RedirectResponse(f"/q/{token}?r={'accepted' if accept else 'rejected'}",
                            status_code=status.HTTP_303_SEE_OTHER, headers=NO_STORE)


@router.post("/q/{token}/accept", dependencies=[Depends(public_response_rate_limiter)])
async def accept_quotation(token: str, name: str = Form(..., min_length=2, max_length=200), confirm: str = Form(...),
                           db: AsyncSession = Depends(get_db)):
    if confirm != "yes":
        raise HTTPException(status_code=422, detail="Please confirm your acceptance")
    return await _respond(token, db, accept=True, name=name, comment=None)


@router.post("/q/{token}/reject", dependencies=[Depends(public_response_rate_limiter)])
async def reject_quotation(token: str, comment: str | None = Form(default=None, max_length=1000), db: AsyncSession = Depends(get_db)):
    return await _respond(token, db, accept=False, name=None, comment=comment)
