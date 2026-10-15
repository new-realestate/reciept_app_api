from __future__ import annotations

import base64
import time
from email.message import EmailMessage
from html import escape
from typing import Any

import httpx

from app.core.config import get_settings

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
HTTP_TIMEOUT_SECONDS = 15

_token_cache: dict[str, Any] = {"access_token": None, "expires_at": 0.0}


def _access_token() -> str:
    """Exchange the stored refresh token for an access token, cached until shortly before expiry."""
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    settings = get_settings()
    resp = httpx.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": settings.gmail_client_id,
            "client_secret": settings.gmail_client_secret,
            "refresh_token": settings.gmail_refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    if resp.is_error:
        raise RuntimeError(f"Google token refresh failed ({resp.status_code}): {resp.text}")
    payload = resp.json()
    _token_cache["access_token"] = payload["access_token"]
    _token_cache["expires_at"] = time.time() + int(payload.get("expires_in", 3600))
    return _token_cache["access_token"]


def _send(
    *,
    to_email: str,
    subject: str,
    text_body: str,
    html_body: str,
    attachment: tuple[str, bytes] | None = None,
) -> dict[str, Any]:
    settings = get_settings()

    message = EmailMessage()
    message["From"] = settings.gmail_sender_email
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    if attachment is not None:
        filename, content = attachment
        message.add_attachment(content, maintype="application", subtype="pdf", filename=filename)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    resp = httpx.post(
        GMAIL_SEND_URL,
        headers={"Authorization": f"Bearer {_access_token()}"},
        json={"raw": raw},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return {"MessageID": resp.json()["id"]}


def send_verification_email(*, to_email: str, verify_url: str) -> dict[str, Any]:
    return _send(
        to_email=to_email,
        subject="Verify your email",
        html_body=f'<p>Welcome! Please verify your email by clicking <a href="{escape(verify_url)}">here</a>.</p>',
        text_body=f"Welcome! Verify your email: {verify_url}",
    )


def send_password_reset_email(*, to_email: str, reset_url: str) -> dict[str, Any]:
    return _send(
        to_email=to_email,
        subject="Reset your password",
        html_body=(
            f'<p>Click <a href="{escape(reset_url)}">here</a> to reset your password. '
            "If you did not request this, ignore this email.</p>"
        ),
        text_body=f"Reset your password: {reset_url}",
    )


def send_document_email(
    *,
    to_email: str,
    subject: str,
    text_body: str,
    html_body: str,
    pdf_bytes: bytes | None = None,
    pdf_filename: str | None = None,
) -> dict[str, Any]:
    """Generic transactional email (invoices, quotations, reminders) with an optional PDF attachment."""
    attachment = (pdf_filename, pdf_bytes) if pdf_bytes and pdf_filename else None
    return _send(
        to_email=to_email, subject=subject, text_body=text_body, html_body=html_body, attachment=attachment
    )


def send_receipt_email(
    *,
    to_email: str,
    business_name: str,
    amount_display: str,
    hosted_url: str,
    pdf_bytes: bytes | None,
    pdf_filename: str,
    mode: str = "attachment",
) -> dict[str, Any]:
    html_body = (
        f"<p>Thank you for your payment of <strong>{escape(amount_display)}</strong> to "
        f"<strong>{escape(business_name)}</strong>.</p>"
        f'<p>You can view or download your receipt here: <a href="{escape(hosted_url)}">{escape(hosted_url)}</a></p>'
    )
    attachment = (pdf_filename, pdf_bytes) if mode == "attachment" and pdf_bytes else None

    return _send(
        to_email=to_email,
        subject=f"Your receipt from {business_name}",
        html_body=html_body,
        text_body=f"Thank you for your payment of {amount_display} to {business_name}. View your receipt: {hosted_url}",
        attachment=attachment,
    )
