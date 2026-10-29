import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.enums import UserRole
from app.models.tenant import Tenant
from app.models.user import User

bearer_scheme = HTTPBearer(auto_error=False)


class CurrentUser:
    """The caller, scoped to one business.

    For a platform super admin acting as a business, `tenant_id`/`role` are the
    business they act as (with owner rights) and `impersonator_id` is their own id;
    `id` stays theirs so everything they do is attributed to them.
    """

    def __init__(
        self,
        user: User,
        *,
        tenant_id: uuid.UUID | None = None,
        role: UserRole | None = None,
        impersonator_id: uuid.UUID | None = None,
        tenant: Tenant | None = None,
    ):
        self.user = user
        # The business, loaded once per request. Holding it here also keeps it in the
        # session's identity map, so db.get(Tenant, ...) elsewhere in the request is free.
        self.tenant = tenant
        self.id: uuid.UUID = user.id
        self.tenant_id: uuid.UUID = tenant_id or user.tenant_id  # type: ignore[assignment]
        self.role = role or user.role
        self.impersonator_id = impersonator_id


async def load_token_user(
    credentials: HTTPAuthorizationCredentials | None, db: AsyncSession
) -> tuple[User, dict]:
    """Decode the bearer token and load its (non-suspended) user."""
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    payload = decode_access_token(credentials.credentials)
    # Purpose-bound tokens (e.g. KYC document links) carry a "typ" and are never valid for API access.
    if payload is None or payload.get("typ") is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    user_id = payload.get("sub")
    # The user's business comes in the same query; later db.get(Tenant, ...) calls in the
    # request reuse it instead of going back to the database.
    row = (
        await db.execute(
            select(User, Tenant).outerjoin(Tenant, Tenant.id == User.tenant_id).where(User.id == uuid.UUID(user_id))
        )
    ).first()
    user = row[0] if row else None
    # Keep a reference so the business stays in the identity map for the rest of the request.
    db.info["auth_tenant"] = row[1] if row else None
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    # 401 rather than 403 so clients drop the session.
    if user.suspended_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account suspended")
    if user.deactivated_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account deactivated")
    return user, payload


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    user, payload = await load_token_user(credentials, db)

    if user.is_superadmin:
        act_tenant_id = payload.get("act_tenant_id") if payload.get("imp") else None
        if act_tenant_id is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Choose a business to act as")
        tenant = await db.get(Tenant, uuid.UUID(act_tenant_id))
        if tenant is None or tenant.deleted_at is not None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Business not found")
        return CurrentUser(user, tenant_id=tenant.id, role=UserRole.owner, impersonator_id=user.id, tenant=tenant)

    tenant = await db.get(Tenant, user.tenant_id)  # already loaded with the user
    if tenant is None or tenant.deleted_at is not None or tenant.suspended_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Business suspended")

    return CurrentUser(user, tenant=tenant)
