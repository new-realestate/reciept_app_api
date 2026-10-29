import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.customer import CustomerFields, CustomerOut

RentFrequency = Literal["once", "weekly", "monthly", "quarterly", "annually"]


class NewCustomerIn(CustomerFields):
    name: str = Field(min_length=1, max_length=300)
    email: str | None = None
    phone: str | None = None

    @model_validator(mode="after")
    def check_contact(self):
        if not self.email and not self.phone:
            raise ValueError("Either email or phone must be provided")
        return self


class TenancyCreate(BaseModel):
    property_id: uuid.UUID
    unit_id: uuid.UUID | None = None
    start_date: date
    end_date: date | None = None
    rent_amount_cents: int = Field(ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    rent_frequency: RentFrequency = "monthly"
    deposit_cents: int = Field(default=0, ge=0)
    status: Literal["draft", "active"] = "active"
    notes: str | None = Field(default=None, max_length=2000)
    # Also set up a payment schedule (with reminders) for the rent; needs the invoices.issue permission.
    create_rent_schedule: bool = False

    @model_validator(mode="after")
    def _check(self):
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date cannot be before start_date")
        return self


class RenterCreate(BaseModel):
    """Either reuse an existing customer (customer_id) or create one (customer)."""

    customer_id: uuid.UUID | None = None
    customer: NewCustomerIn | None = None
    # Create a new customer even though one with the same email/phone exists.
    allow_duplicate_customer: bool = False
    occupation: str | None = Field(default=None, max_length=200)
    employer: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)
    tenancy: TenancyCreate | None = None

    @model_validator(mode="after")
    def _check(self):
        if (self.customer_id is None) == (self.customer is None):
            raise ValueError("Provide exactly one of customer_id or customer")
        return self


class RenterUpdate(BaseModel):
    status: Literal["active", "former", "archived"] | None = None
    occupation: str | None = Field(default=None, max_length=200)
    employer: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)


class TenancyUpdate(BaseModel):
    unit_id: uuid.UUID | None = None
    end_date: date | None = None
    rent_amount_cents: int | None = Field(default=None, ge=0)
    rent_frequency: RentFrequency | None = None
    deposit_cents: int | None = Field(default=None, ge=0)
    status: Literal["draft", "active"] | None = None
    notes: str | None = Field(default=None, max_length=2000)


class TenancyEnd(BaseModel):
    end_date: date
    status: Literal["ended", "terminated"] = "ended"
    reason: str | None = Field(default=None, max_length=500)


class TenancyOut(BaseModel):
    id: uuid.UUID
    reference: str
    renter_id: uuid.UUID
    property_id: uuid.UUID
    property_name: str | None = None
    unit_id: uuid.UUID | None
    unit_label: str | None = None
    start_date: date
    end_date: date | None
    rent_amount_cents: int
    currency: str
    rent_frequency: str
    deposit_cents: int
    status: str
    notes: str | None
    payment_schedule_id: uuid.UUID | None = None
    ended_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class RenterOut(BaseModel):
    id: uuid.UUID
    customer_id: uuid.UUID
    customer: CustomerOut
    status: str
    occupation: str | None
    employer: str | None
    notes: str | None
    kyc_status: str | None = None
    current_tenancies: list[TenancyOut] = []
    created_at: datetime


class RenterDetailOut(RenterOut):
    tenancies: list[TenancyOut] = []
