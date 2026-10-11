import uuid
from typing import Literal

from pydantic import BaseModel, EmailStr

ReceiptTemplateName = Literal["classic", "modern"]


class TenantOut(BaseModel):
    id: uuid.UUID
    business_name: str
    logo_url: str | None
    po_box: str | None
    physical_address: str | None
    contact_phone: str | None
    contact_email: str | None
    bank_name: str | None
    bank_account_number: str | None
    default_currency: str
    receipt_template: ReceiptTemplateName = "classic"

    model_config = {"from_attributes": True}


class TenantUpdate(BaseModel):
    business_name: str | None = None
    po_box: str | None = None
    physical_address: str | None = None
    contact_phone: str | None = None
    contact_email: EmailStr | None = None
    bank_name: str | None = None
    bank_account_number: str | None = None
    receipt_template: ReceiptTemplateName | None = None


class LogoUploadResponse(BaseModel):
    logo_url: str


class SignatureUploadResponse(BaseModel):
    signature_image_url: str
