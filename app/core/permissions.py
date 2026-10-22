from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, bearer_scheme, get_current_user, load_token_user
from app.db.session import get_db
from app.models.enums import UserRole
from app.models.user import User

MANAGERS = (UserRole.owner, UserRole.admin)


def require_roles(*roles: UserRole):
    """Dependency factory: 403 unless the caller's role is one of `roles`.

    Applied to the new invoicing/quotation/schedule endpoints only; existing
    receipt endpoints keep their current behaviour.
    """
    allowed = {r.value for r in roles}

    async def _checker(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        role = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
        if role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to do this")
        return current_user

    return _checker


require_manager = require_roles(*MANAGERS)


async def require_platform_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """403 unless the caller is a platform super admin using their own (not an act-as) token."""
    user, payload = await load_token_user(credentials, db)
    if not user.is_superadmin or payload.get("imp"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Platform admin access required")
    return user
