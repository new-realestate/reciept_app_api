import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

CustomerType = Literal["individual", "business"]
ContactPreference = Literal["email", "sms", "phone", "none"]


class CustomerFields(BaseModel):
    customer_type: CustomerType = "individual"
    legal_name: str | None = Field(default=None, max_length=300)
    billing_address: str | None = Field(default=None, max_length=1000)
    tax_identification_number: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)
    contact_preference: ContactPreference | None = None


class CustomerCreate(CustomerFields):
    name: str = Field(min_length=1, max_length=300)
    email: str | None = None
    phone: str | None = None
    # Set after the user has seen the possible matches and still wants a separate record.
    allow_duplicate: bool = False

    @model_validator(mode="after")
    def check_contact(self):
        if not self.email and not self.phone:
            raise ValueError("Either email or phone must be provided")
        return self


class CustomerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    email: str | None = None
    phone: str | None = None
    customer_type: CustomerType | None = None
    legal_name: str | None = Field(default=None, max_length=300)
    billing_address: str | None = Field(default=None, max_length=1000)
    tax_identification_number: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)
    contact_preference: ContactPreference | None = None


class CustomerOut(BaseModel):
    id: uuid.UUID
    name: str
    email: str | None
    phone: str | None
    customer_type: str = "individual"
    legal_name: str | None = None
    billing_address: str | None = None
    tax_identification_number: str | None = None
    notes: str | None = None
    contact_preference: str | None = None
    status: str = "active"
    renter_id: uuid.UUID | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class CustomerMatches(BaseModel):
    exact: list[CustomerOut]
    possible: list[CustomerOut]


class CustomerDocumentSummary(BaseModel):
    id: uuid.UUID
    kind: Literal["receipt", "invoice", "quotation"]
    number: str | None
    status: str
    date: date
    currency: str
    amount_cents: int
    outstanding_cents: int | None = None
    property_id: uuid.UUID | None = None


class CustomerDocumentsOut(BaseModel):
    customer: CustomerOut
    receipts: list[CustomerDocumentSummary]
    invoices: list[CustomerDocumentSummary]
    quotations: list[CustomerDocumentSummary]
    outstanding_by_currency: dict[str, int]
