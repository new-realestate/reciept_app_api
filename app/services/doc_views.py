"""Builds the plain-dict "view" the invoice/quotation templates render.

Issued documents render from the snapshot frozen at issue time (business details,
customer, line items, amounts), so later edits to the tenant or customer never
change a document that was already shared. Live data is only used for things
that legitimately change: payments received and the derived status.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.models.customer import Customer
from app.models.invoice import Invoice
from app.models.quotation import Quotation
from app.models.tenant import Tenant
from app.services.pdf import format_money

DEFAULT_ACCENT = "#3b3fd1"


def _fmt_date(value: date | str | None) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        value = date.fromisoformat(value)
    return value.strftime("%d %B %Y")


def _qty(value: Decimal | str) -> str:
    return format(Decimal(str(value)).normalize(), "f")


def tenant_snapshot(tenant: Tenant) -> dict:
    return {
        "business_name": tenant.business_name,
        "logo_url": tenant.logo_url,
        "brand_color": tenant.brand_color,
        "po_box": tenant.po_box,
        "physical_address": tenant.physical_address,
        "contact_phone": tenant.contact_phone,
        "contact_email": tenant.contact_email,
        "bank_name": tenant.bank_name,
        "bank_account_number": tenant.bank_account_number,
    }


def customer_snapshot(customer: Customer) -> dict:
    return {"name": customer.name, "email": customer.email, "phone": customer.phone}


def lines_snapshot(line_items) -> list[dict]:
    return [
        {
            "description": li.description,
            "quantity": str(li.quantity),
            "unit_price_cents": li.unit_price_cents,
            "tax_rate": str(li.tax_rate) if li.tax_rate is not None else None,
            "line_total_cents": li.line_total_cents,
        }
        for li in line_items
    ]


def invoice_snapshot(invoice: Invoice, tenant: Tenant, customer: Customer) -> dict:
    return {
        "tenant": tenant_snapshot(tenant),
        "customer": customer_snapshot(customer),
        "lines": lines_snapshot(invoice.line_items),
        "number": invoice.number,
        "title": invoice.title,
        "currency": invoice.currency,
        "issue_date": invoice.issue_date.isoformat(),
        "due_date": invoice.due_date.isoformat(),
        "subtotal_cents": invoice.subtotal_cents,
        "discount_percent": str(invoice.discount_percent) if invoice.discount_percent is not None else None,
        "discount_cents": invoice.discount_cents,
        "tax_cents": invoice.tax_cents,
        "total_cents": invoice.total_cents,
        "notes": invoice.notes,
        "payment_instructions": invoice.payment_instructions,
    }


def quotation_snapshot(q: Quotation, tenant: Tenant, customer: Customer) -> dict:
    # internal_notes are deliberately never part of the snapshot.
    return {
        "tenant": tenant_snapshot(tenant),
        "customer": customer_snapshot(customer),
        "lines": lines_snapshot(q.line_items),
        "number": q.number,
        "title": q.title,
        "description": q.description,
        "currency": q.currency,
        "issue_date": q.issue_date.isoformat(),
        "valid_until": q.valid_until.isoformat(),
        "subtotal_cents": q.subtotal_cents,
        "discount_percent": str(q.discount_percent) if q.discount_percent is not None else None,
        "discount_cents": q.discount_cents,
        "tax_cents": q.tax_cents,
        "total_cents": q.total_cents,
        "terms": q.terms,
        "notes": q.notes,
    }


def _line_views(src: dict) -> list[dict]:
    cur = src["currency"]
    return [
        {
            "description": li["description"],
            "quantity": _qty(li["quantity"]),
            "unit_price": format_money(li["unit_price_cents"], cur),
            "tax_rate": f"{Decimal(li['tax_rate']).normalize():f}%" if li.get("tax_rate") else "",
            "total": format_money(li["line_total_cents"], cur),
        }
        for li in src["lines"]
    ]


def _totals(src: dict) -> list[tuple[str, str, bool]]:
    cur = src["currency"]
    rows = [("Subtotal", format_money(src["subtotal_cents"], cur), False)]
    if src["discount_cents"]:
        label = f"Discount ({Decimal(src['discount_percent']).normalize():f}%)" if src.get("discount_percent") else "Discount"
        rows.append((label, "- " + format_money(src["discount_cents"], cur), False))
    if src["tax_cents"]:
        rows.append(("Tax", format_money(src["tax_cents"], cur), False))
    rows.append(("Total", format_money(src["total_cents"], cur), True))
    return rows


def invoice_view(invoice: Invoice, tenant: Tenant, customer: Customer, *, display_status: str, payments: list | None = None, logo_src: str | None = None) -> dict:
    src = invoice.snapshot or invoice_snapshot(invoice, tenant, customer)
    cur = src["currency"]
    outstanding = invoice.total_cents - invoice.amount_paid_cents
    totals = _totals(src)
    if invoice.status == "issued":
        totals.append(("Amount paid", format_money(invoice.amount_paid_cents, cur), False))
        totals.append(("Balance due", format_money(outstanding, cur), True))
    tones = {"paid": "success", "overdue": "danger", "partially_paid": "warning", "cancelled": "muted"}
    return {
        "kind": "invoice",
        "label": "INVOICE",
        "sublabel": None,
        "number": src.get("number") or "DRAFT",
        "title": src.get("title"),
        "description": None,
        "status_label": display_status.replace("_", " ").upper(),
        "status_tone": tones.get(display_status, "info"),
        "tenant": src["tenant"],
        "customer": src["customer"],
        "dates": [("Issue date", _fmt_date(src["issue_date"])), ("Due date", _fmt_date(src["due_date"]))],
        "lines": _line_views(src),
        "totals": totals,
        "notes": src.get("notes"),
        "payment_instructions": src.get("payment_instructions"),
        "terms": None,
        "payments": [
            {"date": _fmt_date(p.paid_at), "amount": format_money(p.amount_cents, cur),
             "method": p.method.replace("_", " ").title(), "reference": p.reference}
            for p in (payments or [])
        ],
        "accent": src["tenant"].get("brand_color") or DEFAULT_ACCENT,
        "logo_src": logo_src,
    }


def quotation_view(q: Quotation, tenant: Tenant, customer: Customer, *, display_status: str, logo_src: str | None = None) -> dict:
    src = q.snapshot or quotation_snapshot(q, tenant, customer)
    tones = {"accepted": "success", "rejected": "danger", "expired": "muted", "cancelled": "muted"}
    return {
        "kind": "quotation",
        "label": "QUOTATION",
        "sublabel": "Estimate only. This is not an invoice or a receipt and is not a request for payment.",
        "number": src.get("number") or "DRAFT",
        "title": src.get("title"),
        "description": src.get("description"),
        "status_label": display_status.upper(),
        "status_tone": tones.get(display_status, "info"),
        "tenant": src["tenant"],
        "customer": src["customer"],
        "dates": [("Issue date", _fmt_date(src["issue_date"])), ("Valid until", _fmt_date(src["valid_until"]))],
        "lines": _line_views(src),
        "totals": _totals(src),
        "notes": src.get("notes"),
        "payment_instructions": None,
        "terms": src.get("terms"),
        "payments": [],
        "accent": src["tenant"].get("brand_color") or DEFAULT_ACCENT,
        "logo_src": logo_src,
    }
