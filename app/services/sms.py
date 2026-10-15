from __future__ import annotations

import phonenumbers
from twilio.rest import Client as TwilioClient

from app.core.config import get_settings


class InvalidPhoneNumberError(ValueError):
    pass


def to_e164(raw_phone: str, default_region: str = "UG") -> str:
    try:
        parsed = phonenumbers.parse(raw_phone, default_region)
    except phonenumbers.NumberParseException as exc:
        raise InvalidPhoneNumberError(f"Could not parse phone number: {raw_phone}") from exc

    if not phonenumbers.is_valid_number(parsed):
        raise InvalidPhoneNumberError(f"Invalid phone number: {raw_phone}")

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def send_text_sms(*, to_phone_e164: str, body: str) -> str:
    """Sends a plain SMS (invoices, quotations, reminders) and returns the provider message SID."""
    settings = get_settings()
    client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
    message = client.messages.create(body=body, from_=settings.twilio_from_number, to=to_phone_e164)
    return message.sid


def send_receipt_sms(*, to_phone_e164: str, business_name: str, amount_display: str, hosted_url: str) -> str:
    """Sends the SMS and returns the provider message SID."""
    settings = get_settings()
    client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)

    body = f"{business_name}: Payment of {amount_display} received. View your receipt: {hosted_url}"

    message = client.messages.create(
        body=body,
        from_=settings.twilio_from_number,
        to=to_phone_e164,
    )
    return message.sid
