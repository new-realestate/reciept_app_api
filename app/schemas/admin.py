"""Request/response models for the platform super admin API (app/api/admin.py)."""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import PlanTier, UserRole


class CurrencyTotal(BaseModel):
    currency: str
    amount_cents: int
    count: int


class PlatformStats(BaseModel):
    businesses_total: int
    businesses_active: int
    businesses_suspended: int
    users_total: int
    users_suspended: int
    signups_last_30_days: int
    receipts_total: int
    invoices_total: int
    receipt_totals: list[CurrencyTotal]


class AdminTenantOut(BaseModel):
    id: uuid.UUID
    business_name: str
    plan_tier: PlanTier
    default_currency: str
    owner_email: str | None
    user_count: int
    receipt_count: int
    last_login_at: datetime | None
    created_at: datetime
    suspended_at: datetime | None
    suspended_reason: str | None


class AdminUserOut(BaseModel):
    id: uuid.UUID
    full_name: str
    email: str
    role: UserRole
    tenant_id: uuid.UUID | None
    business_name: str | None
    is_superadmin: bool
    is_verified: bool
    last_login_at: datetime | None
    created_at: datetime
    suspended_at: datetime | None
    suspended_reason: str | None


class AdminTenantDetail(AdminTenantOut):
    contact_email: str | None
    contact_phone: str | None
    physical_address: str | None
    timezone: str
    invoice_count: int
    customer_count: int
    users: list[AdminUserOut]


class SuspendRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class AdminUserUpdate(BaseModel):
    role: UserRole | None = None
    is_verified: bool | None = None


class ImpersonationOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds
    tenant_id: uuid.UUID
    business_name: str


class PlatformAuditLogOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID | None
    business_name: str | None
    action: str
    entity_type: str
    entity_id: uuid.UUID | None
    actor_user_id: uuid.UUID | None
    actor_name: str | None
    actor_email: str | None
    metadata: dict[str, Any] | None
    created_at: datetime
