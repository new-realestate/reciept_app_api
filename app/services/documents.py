"""Derived status and rendering helpers for invoices and quotations."""
from __future__ import annotations

from datetime import date

from app.models.customer import Customer
from app.models.invoice import Invoice, InvoicePayment
from app.models.quotation import Quotation
from app.models.tenant import Tenant
from app.services.doc_views import invoice_view, quotation_view
from app.services.pdf import local_file_uri, render_document_html, render_document_pdf_bytes


def outstanding_cents(invoice: Invoice) -> int:
    return invoice.total_cents - invoice.amount_paid_cents


def derive_invoice_status(invoice: Invoice, today: date) -> str:
    """draft | issued | partially_paid | paid | overdue | cancelled.

    Overdue means: issued, money still owed, and the due date has passed. An unpaid
    invoice that is not yet due is simply "issued", never "overdue".
    """
    if invoice.status in ("draft", "cancelled"):
        return invoice.status
    outstanding = outstanding_cents(invoice)
    if outstanding <= 0:
        return "paid"
    if invoice.due_date < today:
        return "overdue"
    if invoice.amount_paid_cents > 0:
        return "partially_paid"
    return "issued"


def days_overdue(invoice: Invoice, today: date) -> int:
    if invoice.status != "issued" or outstanding_cents(invoice) <= 0:
        return 0
    return max((today - invoice.due_date).days, 0)


def derive_quotation_status(quotation: Quotation, today: date) -> str:
    """A sent/viewed quotation past its validity date reads as expired even before the job marks it."""
    if quotation.status in ("sent", "viewed") and quotation.valid_until < today:
        return "expired"
    return quotation.status


def _pdf_logo(view_source_tenant: dict) -> str | None:
    return local_file_uri(view_source_tenant.get("logo_url"))


def invoice_html(invoice: Invoice, tenant: Tenant, customer: Customer, payments: list[InvoicePayment], today: date,
                 *, template: str = "document.html", **extra) -> str:
    view = invoice_view(invoice, tenant, customer, display_status=derive_invoice_status(invoice, today), payments=payments)
    return render_document_html(view, template=template, **extra)


def invoice_pdf_bytes(invoice: Invoice, tenant: Tenant, customer: Customer, payments: list[InvoicePayment], today: date) -> bytes:
    view = invoice_view(invoice, tenant, customer, display_status=derive_invoice_status(invoice, today), payments=payments)
    view["logo_src"] = _pdf_logo(view["tenant"])
    return render_document_pdf_bytes(view)


def quotation_html(quotation: Quotation, tenant: Tenant, customer: Customer, today: date,
                   *, template: str = "document.html", **extra) -> str:
    view = quotation_view(quotation, tenant, customer, display_status=derive_quotation_status(quotation, today))
    return render_document_html(view, template=template, **extra)


def quotation_pdf_bytes(quotation: Quotation, tenant: Tenant, customer: Customer, today: date) -> bytes:
    view = quotation_view(quotation, tenant, customer, display_status=derive_quotation_status(quotation, today))
    view["logo_src"] = _pdf_logo(view["tenant"])
    return render_document_pdf_bytes(view)
