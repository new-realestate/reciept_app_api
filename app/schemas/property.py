import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

PropertyType = Literal["residential", "commercial", "mixed", "land", "other"]
PropertyStatus = Literal["active", "inactive", "archived"]
UnitType = Literal["apartment", "room", "office", "shop", "house", "other"]
UnitStatus = Literal["active", "inactive", "archived"]


class PropertyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    property_type: PropertyType = "residential"
    address: str | None = Field(default=None, max_length=500)
    location: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=2000)


class PropertyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    property_type: PropertyType | None = None
    address: str | None = Field(default=None, max_length=500)
    location: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=2000)
    status: PropertyStatus | None = None


class UnitCreate(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    unit_type: UnitType = "apartment"
    default_rent_cents: int | None = Field(default=None, ge=0)


class UnitUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=100)
    unit_type: UnitType | None = None
    status: UnitStatus | None = None
    default_rent_cents: int | None = Field(default=None, ge=0)


class UnitOut(BaseModel):
    id: uuid.UUID
    property_id: uuid.UUID
    label: str
    unit_type: str
    status: str
    default_rent_cents: int | None
    occupied: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


class AssignedStaffOut(BaseModel):
    user_id: uuid.UUID
    full_name: str
    role: str
    is_active: bool
    assigned_at: datetime


class PropertyOut(BaseModel):
    id: uuid.UUID
    name: str
    property_type: str
    address: str | None
    location: str | None
    description: str | None
    status: str
    unit_count: int = 0
    occupied_unit_count: int = 0
    active_tenancy_count: int = 0
    created_at: datetime
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class PropertyDetailOut(PropertyOut):
    units: list[UnitOut] = []
    assigned_staff: list[AssignedStaffOut] = []


class AssignStaffRequest(BaseModel):
    user_id: uuid.UUID
