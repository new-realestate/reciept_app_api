import base64
import email
from email import policy

import httpx

from app.services import email as email_service


def _install_mock_transport(monkeypatch, calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if str(request.url) == email_service.GOOGLE_TOKEN_URL:
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        return httpx.Response(200, json={"id": "gmail-msg-1"})

    transport = httpx.MockTransport(handler)
    real_post = httpx.post

    def fake_post(url, **kwargs):
        with httpx.Client(transport=transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(email_service.httpx, "post", fake_post)
    monkeypatch.setattr(email_service, "_token_cache", {"access_token": None, "expires_at": 0.0})
    return real_post


def _decode_raw(request: httpx.Request):
    import json

    raw = json.loads(request.content)["raw"]
    return email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)


def test_send_receipt_email_builds_message_with_pdf(monkeypatch):
    monkeypatch.setenv("GMAIL_SENDER_EMAIL", "sender@gmail.com")
    from app.core.config import get_settings

    get_settings.cache_clear()
    calls: list[httpx.Request] = []
    _install_mock_transport(monkeypatch, calls)

    result = email_service.send_receipt_email(
        to_email="payer@example.com",
        business_name="Acme <Ltd>",
        amount_display="UGX 100",
        hosted_url="https://x.test/r/abc",
        pdf_bytes=b"%PDF-1.4 fake",
        pdf_filename="R-1.pdf",
    )

    assert result == {"MessageID": "gmail-msg-1"}
    token_call, send_call = calls
    assert b"grant_type=refresh_token" in token_call.content
    assert send_call.headers["Authorization"] == "Bearer tok-1"

    msg = _decode_raw(send_call)
    assert msg["To"] == "payer@example.com"
    assert msg["From"] == "sender@gmail.com"
    assert msg["Subject"] == "Your receipt from Acme <Ltd>"
    attachments = list(msg.iter_attachments())
    assert len(attachments) == 1 and attachments[0].get_filename() == "R-1.pdf"
    get_settings.cache_clear()


def test_access_token_is_cached(monkeypatch):
    calls: list[httpx.Request] = []
    _install_mock_transport(monkeypatch, calls)

    email_service.send_verification_email(to_email="a@example.com", verify_url="https://x.test/v")
    email_service.send_password_reset_email(to_email="a@example.com", reset_url="https://x.test/r")

    token_calls = [c for c in calls if str(c.url) == email_service.GOOGLE_TOKEN_URL]
    assert len(token_calls) == 1
