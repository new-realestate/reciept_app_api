import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.documents import CustomerInline, PropertyContextOut

Frequency = Literal["once", "weekly", "monthly", "quarterly", "annually"]


class ScheduleCreate(CustomerInline):
    description: str = Field(min_length=1, max_length=300)
    reference_label: str | None = Field(default=None, max_length=200)
    amount_cents: int = Field(gt=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    frequency: Frequency
    first_due_date: date
    end_date: date | None = None
    max_occurrences: int | None = Field(default=None, ge=1, le=600)
    reminders_enabled: bool = True
    auto_invoice: bool = False

    @model_validator(mode="after")
    def _check(self):
        if self.end_date is not None and self.end_date < self.first_due_date:
            raise ValueError("end_date cannot be before first_due_date")
        if self.frequency == "once" and (self.end_date or self.max_occurrences):
            raise ValueError("A one-time schedule cannot have an end date or occurrence limit")
        return self


class ScheduleUpdate(BaseModel):
    description: str | None = Field(default=None, min_length=1, max_length=300)
    reference_label: str | None = Field(default=None, max_length=200)
    amount_cents: int | None = Field(default=None, gt=0)
    end_date: date | None = None
    max_occurrences: int | None = Field(default=None, ge=1, le=600)
    reminders_enabled: bool | None = None
    auto_invoice: bool | None = None


class ScheduleReminderSettings(BaseModel):
    enabled: bool | None = None
    offsets: list[int] | None = None
    channels: list[str] | None = None
    send_hour: int | None = None


class OccurrenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    schedule_id: uuid.UUID
    seq: int
    due_date: date
    amount_cents: int
    amount_paid_cents: int
    status: str
    invoice_id: uuid.UUID | None = None
    invoice_number: str | None = None
    days_until_due: int


class ScheduleOut(PropertyContextOut):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    customer_id: uuid.UUID
    customer_name: str | None = None
    description: str
    reference_label: str | None
    amount_cents: int
    currency: str
    frequency: str
    first_due_date: date
    end_date: date | None
    max_occurrences: int | None
    status: str
    reminders_enabled: bool
    reminder_overrides: dict | None
    auto_invoice: bool
    next_due_date: date | None = None
    next_amount_cents: int | None = None
    created_at: datetime


class ScheduleDetailOut(ScheduleOut):
    occurrences: list[OccurrenceOut] = []


class CreateInvoiceFromOccurrence(BaseModel):
    issue_now: bool = True
    due_date: date | None = None  # defaults to the occurrence due date
    payment_instructions: str | None = None


class UpcomingPaymentOut(BaseModel):
    kind: Literal["invoice", "occurrence"]
    id: uuid.UUID
    customer_id: uuid.UUID
    customer_name: str | None
    description: str
    due_date: date
    amount_cents: int  # outstanding / expected
    currency: str
    status: Literal["overdue", "due_today", "upcoming"]
    days_until_due: int  # negative when overdue
    days_overdue: int
    invoice_id: uuid.UUID | None = None
    invoice_number: str | None = None
    schedule_id: uuid.UUID | None = None
    occurrence_id: uuid.UUID | None = None


class ReminderSettingsOut(BaseModel):
    enabled: bool
    offsets: list[int]
    channels: list[str]
    send_hour: int
    notify_business: bool
    timezone: str


class ReminderSettingsUpdate(BaseModel):
    enabled: bool | None = None
    offsets: list[int] | None = None
    channels: list[str] | None = None
    send_hour: int | None = None
    notify_business: bool | None = None
