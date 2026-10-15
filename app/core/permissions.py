from fastapi import Depends, HTTPException, status

from app.core.deps import CurrentUser, get_current_user
from app.models.enums import UserRole

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
