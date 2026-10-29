import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import generate_token
from app.models.user import User
from app.services.email import send_password_reset_email

logger = logging.getLogger(__name__)


async def start_password_reset(db: AsyncSession, user: User, *, valid_hours: int = 1) -> None:
    """Issue a reset token (1 hour by default), commit it, and email the reset link (email failures are logged).

    Also used to invite a new team member: they choose their password through the same link.
    """
    reset_token = generate_token()
    user.password_reset_token = reset_token
    user.password_reset_expires_at = datetime.now(timezone.utc) + timedelta(hours=valid_hours)
    await db.commit()

    settings = get_settings()
    reset_url = f"{settings.app_base_url}/auth/password-reset/confirm?token={reset_token}"
    try:
        send_password_reset_email(to_email=user.email, reset_url=reset_url)
    except Exception:
        logger.exception("Failed to send password reset email to %s", user.email)
