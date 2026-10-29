import re
import uuid
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.enums import UserRole

ReceiptTemplateName = Literal["classic", "modern"]

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


class TenantOut(BaseModel):
    id: uuid.UUID
    business_name: str
    logo_url: str | None
    # Accent colour (#rrggbb) used in the portal for this business and on documents; null = default.
    brand_color: str | None = None
    po_box: str | None
    physical_address: str | None
    contact_phone: str | None
    contact_email: str | None
    bank_name: str | None
    bank_account_number: str | None
    default_currency: str
    receipt_template: ReceiptTemplateName = "classic"
    timezone: str = "Africa/Kampala"

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
    default_currency: str | None = Field(default=None, min_length=3, max_length=3)
    timezone: str | None = None
    # Send null to go back to the default colour.
    brand_color: str | None = None

    @field_validator("brand_color")
    @classmethod
    def _hex_color(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not _HEX_COLOR.match(v):
            raise ValueError("Use a hex colour such as #1f7a5c")
        return v.lower()

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Unknown timezone; use an IANA name such as Africa/Kampala") from exc
        return v

    @field_validator("default_currency")
    @classmethod
    def _upper_currency(cls, v: str | None) -> str | None:
        return v.upper() if v else v


class LogoUploadResponse(BaseModel):
    logo_url: str


class SignatureUploadResponse(BaseModel):
    signature_image_url: str


class BusinessBranding(BaseModel):
    id: uuid.UUID
    name: str
    logo_url: str | None
    brand_color: str | None
    default_currency: str = "UGX"
    timezone: str = "Africa/Kampala"


class MeOut(BaseModel):
    """The signed-in user, including the signature printed on receipts they issue."""

    id: uuid.UUID
    full_name: str
    email: str
    role: UserRole
    signature_image_url: str | None
    phone: str | None = None
    permissions: list[str] = []
    is_manager: bool = False
    assigned_property_ids: list[uuid.UUID] = []
    # The business the caller works in (for a super admin acting as one, that business).
    business: BusinessBranding | None = None
