from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML

from app.core.config import get_settings

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)


def format_money(amount_cents: int, currency: str) -> str:
    major, minor = divmod(amount_cents, 100)
    return f"{currency} {major:,}.{minor:02d}"


def _local_file_uri(url: str | None) -> str | None:
    """Map one of our own /static upload URLs to a file:// URI.

    WeasyPrint fetches images synchronously while the API worker is busy
    generating the PDF, so fetching our own URL over HTTP can't be answered
    and the image is silently dropped. Reading from disk avoids that.
    """
    if not url:
        return None
    settings = get_settings()
    prefix = f"{settings.app_base_url.rstrip('/')}/static/"
    if not url.startswith(prefix):
        return None
    path = (Path(settings.storage_dir) / url[len(prefix):]).resolve()
    if not path.is_file() or Path(settings.storage_dir).resolve() not in path.parents:
        return None
    return path.as_uri()


RECEIPT_TEMPLATES = {
    "classic": "receipt.html",
    "modern": "receipt_modern.html",
}
DEFAULT_RECEIPT_TEMPLATE = "classic"


def render_receipt_html(*, tenant, user, customer, receipt, use_local_images: bool = False) -> str:
    template_file = RECEIPT_TEMPLATES.get(
        getattr(tenant, "receipt_template", None) or DEFAULT_RECEIPT_TEMPLATE,
        RECEIPT_TEMPLATES[DEFAULT_RECEIPT_TEMPLATE],
    )
    template = _env.get_template(template_file)
    return template.render(
        logo_src=_local_file_uri(tenant.logo_url) if use_local_images else None,
        signature_src=_local_file_uri(receipt.signature_image_url) if use_local_images else None,
        tenant=tenant,
        user=user,
        customer=customer,
        receipt=receipt,
        amount_display=format_money(receipt.amount_cents, receipt.currency),
        balance_display="Nil" if receipt.balance_cents == 0 else format_money(receipt.balance_cents, receipt.currency),
    )


def render_receipt_pdf_bytes(*, tenant, user, customer, receipt) -> bytes:
    html_content = render_receipt_html(
        tenant=tenant, user=user, customer=customer, receipt=receipt, use_local_images=True
    )
    return HTML(string=html_content, base_url=str(TEMPLATES_DIR)).write_pdf()


def local_file_uri(url: str | None) -> str | None:
    return _local_file_uri(url)


def render_document_html(view: dict, *, template: str = "document.html", **extra) -> str:
    """Render an invoice/quotation view (see services/doc_views.py). Context key is `doc`."""
    return _env.get_template(template).render(doc=view, **extra)


def render_document_pdf_bytes(view: dict) -> bytes:
    return HTML(string=render_document_html(view), base_url=str(TEMPLATES_DIR)).write_pdf()


def read_stored_pdf(*, tenant_id: str, receipt_id: str) -> bytes | None:
    settings = get_settings()
    file_path = Path(settings.storage_dir) / "pdfs" / tenant_id / f"{receipt_id}.pdf"
    if file_path.exists():
        return file_path.read_bytes()
    return None


def store_receipt_pdf(*, tenant_id: str, receipt_id: str, pdf_bytes: bytes) -> str:
    """Stores the PDF on local disk (MVP) and returns a public URL path.

    Swap this function's body for an S3/GCS upload later without changing
    callers — they only depend on getting back a URL.
    """
    settings = get_settings()
    pdf_dir = Path(settings.storage_dir) / "pdfs" / tenant_id
    pdf_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{receipt_id}.pdf"
    file_path = pdf_dir / filename
    file_path.write_bytes(pdf_bytes)

    return f"{settings.app_base_url}/static/pdfs/{tenant_id}/{filename}"
