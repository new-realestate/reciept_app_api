"""Central authorization: permission flags, property scoping and document visibility.

Every check is derived from the authenticated user loaded from the database on each
request (never from role/permission claims sent by the client), so deactivating a
member, changing their permissions or revoking a property assignment takes effect on
their very next request.

- Owners and admins ("managers") hold every permission and see every property of
  their business.
- Staff hold the flags in `users.permissions` (NULL means DEFAULT_STAFF_PERMISSIONS)
  and see only the properties actively assigned to them. Records without a property
  (general business transactions) are visible to staff who hold the relevant flag.

Lookups that fail a scope check return 404, not 403, so ids belonging to another
business or an unassigned property can't be probed.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException, status
from sqlalchemy import false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user
from app.db.session import get_db
from app.models.enums import UserRole
from app.models.property import Property, PropertyStaffAssignment, PropertyUnit
from app.models.renter import Tenancy
from app.models.user import User


class Perm(str, enum.Enum):
    customers_view = "customers.view"
    customers_manage = "customers.manage"
    renters_manage = "renters.manage"
    renters_kyc_review = "renters.kyc_review"
    receipts_issue = "receipts.issue"
    invoices_issue = "invoices.issue"
    invoices_record_payment = "invoices.record_payment"
    quotations_issue = "quotations.issue"
    documents_view = "documents.view"
    reports_view = "reports.view"


ALL_PERMISSIONS = frozenset(p.value for p in Perm)

# What a staff member can do unless the owner/admin narrows or widens it. Matches what
# staff could already do before permissions existed, so existing staff keep working.
DEFAULT_STAFF_PERMISSIONS = frozenset(ALL_PERMISSIONS - {Perm.renters_kyc_review.value, Perm.reports_view.value})

MANAGER_ROLES = (UserRole.owner, UserRole.admin)


def _role_value(role) -> str:
    return role.value if hasattr(role, "value") else str(role)


def effective_permissions(user: User, role=None) -> frozenset[str]:
    role_value = _role_value(role if role is not None else user.role)
    if role_value in {r.value for r in MANAGER_ROLES}:
        return ALL_PERMISSIONS
    if user.permissions is None:
        return DEFAULT_STAFF_PERMISSIONS
    return frozenset(p for p in user.permissions if p in ALL_PERMISSIONS)


@dataclass
class AccessScope:
    """The caller plus what they may reach. Exposes the same attributes as CurrentUser
    (id, tenant_id, role, user, impersonator_id) so endpoints can use either."""

    current: CurrentUser
    is_manager: bool
    perms: frozenset[str]
    property_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)

    @property
    def id(self) -> uuid.UUID:
        return self.current.id

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.current.tenant_id

    @property
    def role(self):
        return self.current.role

    @property
    def user(self) -> User:
        return self.current.user

    @property
    def impersonator_id(self):
        return self.current.impersonator_id

    def can(self, perm: Perm) -> bool:
        return perm.value in self.perms

    def can_access_property(self, property_id: uuid.UUID | None) -> bool:
        if property_id is None:
            return True
        return self.is_manager or property_id in self.property_ids


async def build_scope(db: AsyncSession, current: CurrentUser) -> AccessScope:
    is_manager = _role_value(current.role) in {r.value for r in MANAGER_ROLES}
    perms = effective_permissions(current.user, current.role)
    property_ids: frozenset[uuid.UUID] = frozenset()
    if not is_manager:
        rows = await db.execute(
            select(PropertyStaffAssignment.property_id).where(
                PropertyStaffAssignment.tenant_id == current.tenant_id,
                PropertyStaffAssignment.user_id == current.id,
                PropertyStaffAssignment.revoked_at.is_(None),
            )
        )
        property_ids = frozenset(rows.scalars().all())
    return AccessScope(current=current, is_manager=is_manager, perms=perms, property_ids=property_ids)


async def get_scope(
    current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> AccessScope:
    return await build_scope(db, current_user)


def require_perm(*perms: Perm):
    """Dependency factory: 403 unless the caller holds every permission in `perms`."""

    async def _checker(scope: AccessScope = Depends(get_scope)) -> AccessScope:
        missing = [p for p in perms if not scope.can(p)]
        if missing:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to do this")
        return scope

    return _checker


async def require_manager_scope(scope: AccessScope = Depends(get_scope)) -> AccessScope:
    if not scope.is_manager:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the business owner or an admin can do this")
    return scope


def not_found(what: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found")


# ------------------------------------------------------------------ properties


async def get_scoped_property(db: AsyncSession, scope: AccessScope, property_id: uuid.UUID, *, lock: bool = False) -> Property:
    stmt = select(Property).where(Property.id == property_id, Property.tenant_id == scope.tenant_id)
    if lock and db.get_bind().dialect.name != "sqlite":
        stmt = stmt.with_for_update()
    prop = (await db.execute(stmt)).scalar_one_or_none()
    if prop is None or not scope.can_access_property(prop.id):
        raise not_found("Property")
    return prop


def scope_properties(stmt, scope: AccessScope):
    stmt = stmt.where(Property.tenant_id == scope.tenant_id)
    if scope.is_manager:
        return stmt
    if not scope.property_ids:
        return stmt.where(false())
    return stmt.where(Property.id.in_(scope.property_ids))


# ------------------------------------------------------------------ documents


def document_visible(scope: AccessScope, doc, view_perm: Perm = Perm.documents_view) -> bool:
    if doc.tenant_id != scope.tenant_id:
        return False
    if scope.is_manager:
        return True
    if doc.property_id is None:
        return scope.can(view_perm)
    return doc.property_id in scope.property_ids


def ensure_document_visible(scope: AccessScope, doc, what: str, view_perm: Perm = Perm.documents_view):
    if doc is None or not document_visible(scope, doc, view_perm):
        raise not_found(what)
    return doc


def scope_documents(stmt, model, scope: AccessScope, view_perm: Perm = Perm.documents_view):
    """Restrict a select over a document model (anything with tenant_id + property_id)."""
    stmt = stmt.where(model.tenant_id == scope.tenant_id)
    if scope.is_manager:
        return stmt
    conds = []
    if scope.can(view_perm):
        conds.append(model.property_id.is_(None))
    if scope.property_ids:
        conds.append(model.property_id.in_(scope.property_ids))
    return stmt.where(or_(*conds)) if conds else stmt.where(false())


@dataclass
class DocContext:
    property_id: uuid.UUID | None = None
    unit_id: uuid.UUID | None = None
    tenancy_id: uuid.UUID | None = None


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


async def validate_doc_context(
    db: AsyncSession,
    scope: AccessScope,
    *,
    property_id: uuid.UUID | None = None,
    unit_id: uuid.UUID | None = None,
    tenancy_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
) -> DocContext:
    """Check an optional property/unit/tenancy link for a new document.

    All three may be omitted (a general business transaction). Given ids must belong to
    the caller's business, agree with one another and, for staff, sit on a property
    assigned to them. A tenancy fills in its property and unit. When `customer_id` is
    given, a tenancy must belong to that customer.
    """
    if tenancy_id is not None:
        tenancy = (
            await db.execute(select(Tenancy).where(Tenancy.id == tenancy_id, Tenancy.tenant_id == scope.tenant_id))
        ).scalar_one_or_none()
        if tenancy is None or not scope.can_access_property(tenancy.property_id):
            raise not_found("Tenancy")
        if property_id is not None and property_id != tenancy.property_id:
            raise _bad("The tenancy is on a different property")
        if unit_id is not None and unit_id != tenancy.unit_id:
            raise _bad("The tenancy is for a different unit")
        if customer_id is not None:
            from app.models.renter import Renter

            renter = await db.get(Renter, tenancy.renter_id)
            if renter is None or renter.customer_id != customer_id:
                raise _bad("The tenancy belongs to a different customer")
        property_id, unit_id = tenancy.property_id, tenancy.unit_id

    if unit_id is not None:
        unit = (
            await db.execute(select(PropertyUnit).where(PropertyUnit.id == unit_id, PropertyUnit.tenant_id == scope.tenant_id))
        ).scalar_one_or_none()
        if unit is None or not scope.can_access_property(unit.property_id):
            raise not_found("Unit")
        if property_id is not None and property_id != unit.property_id:
            raise _bad("The unit belongs to a different property")
        property_id = unit.property_id

    if property_id is not None:
        await get_scoped_property(db, scope, property_id)

    return DocContext(property_id=property_id, unit_id=unit_id, tenancy_id=tenancy_id)
