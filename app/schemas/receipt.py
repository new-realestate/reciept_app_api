import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator

from app.models.enums import DeliveryChannel, DeliveryStatus, PaymentMethodType, ReceiptStatus


class ReceiptCreate(BaseModel):
    customer_id: uuid.UUID | None = None

    # Inline customer details — used to create-or-reuse a customer when
    # customer_id is not supplied.
    customer_name: str | None = None
    customer_email: str | None = None
    customer_phone: str | None = None

    amount_cents: int = Field(gt=0)
    currency: str | None = None  # defaults to tenant's default_currency
    reason: str = Field(min_length=1)
    payment_method: PaymentMethodType
    payment_reference: str | None = None
    paid_at: date
    balance_cents: int = Field(default=0, ge=0)
    corrects_receipt_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def check_customer(self):
        if not self.customer_id and not self.customer_name:
            raise ValueError("Provide either customer_id or customer_name (+ email/phone)")
        if not self.customer_id and not (self.customer_email or self.customer_phone):
            raise ValueError("Inline customer requires an email or phone")
        return self


class DeliveryAttemptOut(BaseModel):
    id: uuid.UUID
    channel: DeliveryChannel
    status: DeliveryStatus
    recipient: str
    provider: str | None
    provider_response: dict | None = None
    attempt_number: int
    attempted_at: datetime
    resolved_at: datetime | None

    model_config = {"from_attributes": True}


class ReceiptOut(BaseModel):
    id: uuid.UUID
    reference_number: str
    status: ReceiptStatus
    currency: str
    amount_cents: int
    amount_in_words: str
    balance_cents: int
    reason: str
    payment_method: PaymentMethodType
    payment_reference: str | None
    paid_at: date
    customer_id: uuid.UUID
    issuer_printed_name: str
    signature_image_url: str | None
    corrects_receipt_id: uuid.UUID | None
    hosted_view_token: str
    pdf_url: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ReceiptDetailOut(ReceiptOut):
    delivery_attempts: list[DeliveryAttemptOut] = []
    # For sharing from the portal (e.g. a WhatsApp click-to-chat link).
    customer_name: str | None = None
    customer_phone: str | None = None
    business_name: str | None = None


class SendReceiptRequest(BaseModel):
    channels: list[DeliveryChannel] = Field(min_length=1)
    email_mode: str = Field(default="attachment", pattern="^(attachment|link)$")
