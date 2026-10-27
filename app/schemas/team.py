import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

MemberRole = Literal["admin", "staff"]


class TeamMemberCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=40)
    role: MemberRole = "staff"
    # Staff only; omitted means the default staff set. Ignored for admins, who hold every permission.
    permissions: list[str] | None = None


class TeamMemberUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    role: MemberRole | None = None
    permissions: list[str] | None = None
    # Explicitly reset a staff member to the default permission set.
    reset_permissions: bool = False


class DeactivateRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class AssignmentOut(BaseModel):
    id: uuid.UUID
    property_id: uuid.UUID
    property_name: str | None = None
    user_id: uuid.UUID
    user_name: str | None = None
    assigned_by_user_id: uuid.UUID | None
    assigned_at: datetime
    revoked_by_user_id: uuid.UUID | None
    revoked_at: datetime | None
    active: bool


class TeamMemberOut(BaseModel):
    id: uuid.UUID
    full_name: str
    email: str
    phone: str | None
    role: str
    permissions: list[str]
    uses_default_permissions: bool
    is_active: bool
    deactivated_at: datetime | None
    last_login_at: datetime | None
    created_at: datetime
    assigned_property_ids: list[uuid.UUID] = []
    kyc_status: str | None = None


class PermissionCatalogOut(BaseModel):
    all: list[str]
    staff_defaults: list[str]
