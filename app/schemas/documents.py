"""Schemas shared by invoices and quotations."""
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import DeliveryChannel

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class LineItemIn(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    quantity: Decimal = Field(gt=0, max_digits=12, decimal_places=3)
    unit_price_cents: int = Field(ge=0)
    tax_rate: Decimal | None = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)


class LineItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int
    description: str
    quantity: Decimal
    unit_price_cents: int
    tax_rate: Decimal | None
    line_total_cents: int


class CustomerInline(BaseModel):
    customer_id: uuid.UUID | None = None
    customer_name: str | None = None
    customer_email: str | None = None
    customer_phone: str | None = None


class SendDocumentRequest(BaseModel):
    channels: list[DeliveryChannel] = Field(min_length=1)


class NotificationAttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    attempt_no: int
    status: str
    provider: str | None
    provider_message_id: str | None
    response: dict | None
    attempted_at: datetime


class NotificationEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    subject_type: str
    subject_id: uuid.UUID
    channel: str
    recipient_party: str
    recipient: str | None
    scheduled_for: datetime
    status: str
    skip_reason: str | None
    attempts: int
    sent_at: datetime | None
    created_at: datetime
    attempt_log: list[NotificationAttemptOut] = []


class _DiscountMixin(BaseModel):
    discount_percent: Decimal | None = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)
    discount_cents: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _one_discount(self):
        if self.discount_percent is not None and self.discount_cents:
            raise ValueError("Provide either discount_percent or discount_cents, not both")
        return self


class InvoiceCreate(CustomerInline, _DiscountMixin):
    title: str | None = Field(default=None, max_length=300)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    issue_date: date | None = None  # defaults to today in the business timezone
    due_date: date
    line_items: list[LineItemIn] = Field(min_length=1, max_length=100)
    notes: str | None = None
    payment_instructions: str | None = None
    reminders_enabled: bool = True
    issue_now: bool = False


class InvoiceUpdate(_DiscountMixin):
    """Drafts only. Omitted fields are left unchanged."""

    customer_id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=300)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    issue_date: date | None = None
    due_date: date | None = None
    line_items: list[LineItemIn] | None = Field(default=None, min_length=1, max_length=100)
    notes: str | None = None
    payment_instructions: str | None = None
    reminders_enabled: bool | None = None


class InvoicePaymentCreate(BaseModel):
    amount_cents: int = Field(gt=0)
    paid_at: date
    payment_method: str
    payment_reference: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)


class InvoicePaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    invoice_id: uuid.UUID
    receipt_id: uuid.UUID
    amount_cents: int
    paid_at: date
    method: str
    reference: str | None
    created_at: datetime


class CancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    number: str | None
    status: str  # stored: draft | issued | cancelled
    display_status: str  # derived: draft | issued | partially_paid | paid | overdue | cancelled
    customer_id: uuid.UUID
    customer_name: str | None = None
    title: str | None
    currency: str
    issue_date: date
    due_date: date
    subtotal_cents: int
    discount_percent: Decimal | None
    discount_cents: int
    tax_cents: int
    total_cents: int
    amount_paid_cents: int
    outstanding_cents: int
    days_overdue: int
    notes: str | None
    payment_instructions: str | None
    reminders_enabled: bool
    quotation_id: uuid.UUID | None
    public_url: str | None
    issued_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason: str | None
    created_at: datetime
    line_items: list[LineItemOut]


class InvoiceDetailOut(InvoiceOut):
    payments: list[InvoicePaymentOut] = []


class QuotationCreate(CustomerInline, _DiscountMixin):
    title: str | None = Field(default=None, max_length=300)
    description: str | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    issue_date: date | None = None
    valid_until: date
    line_items: list[LineItemIn] = Field(min_length=1, max_length=100)
    terms: str | None = None
    notes: str | None = None
    internal_notes: str | None = None


class QuotationUpdate(_DiscountMixin):
    customer_id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=300)
    description: str | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    issue_date: date | None = None
    valid_until: date | None = None
    line_items: list[LineItemIn] | None = Field(default=None, min_length=1, max_length=100)
    terms: str | None = None
    notes: str | None = None
    internal_notes: str | None = None


class QuotationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    number: str | None
    status: str  # stored
    display_status: str  # derived (expired when past valid_until and still open)
    customer_id: uuid.UUID
    customer_name: str | None = None
    title: str | None
    description: str | None
    currency: str
    issue_date: date
    valid_until: date
    subtotal_cents: int
    discount_percent: Decimal | None
    discount_cents: int
    tax_cents: int
    total_cents: int
    terms: str | None
    notes: str | None
    internal_notes: str | None
    public_url: str | None
    sent_at: datetime | None
    viewed_at: datetime | None
    responded_at: datetime | None
    accepted_name: str | None
    response_comment: str | None
    converted_invoice_id: uuid.UUID | None = None
    created_at: datetime
    line_items: list[LineItemOut]


class ConvertQuotationRequest(BaseModel):
    issue_date: date | None = None
    due_date: date
    payment_instructions: str | None = None
    issue_now: bool = False
