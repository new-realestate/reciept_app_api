import json
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, ProgrammingError

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import AsyncSessionLocal
from app.models.enums import UserRole
from app.models.tenant import Tenant
from app.models.user import User

logger = logging.getLogger(__name__)


def _load_seed_users() -> list[dict]:
    raw = get_settings().seed_default_users.strip()
    if not raw:
        return []
    try:
        users = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("SEED_DEFAULT_USERS is not valid JSON; skipping user seeding")
        return []
    return users if isinstance(users, list) else []


async def seed_default_users() -> None:
    """Idempotently create pre-verified users; existing emails are left untouched."""
    for entry in _load_seed_users():
        try:
            email = entry["email"]
            password = entry["password"]
            role = UserRole(entry.get("role", "owner"))
            full_name = entry.get("full_name") or email
            business_name = entry.get("business_name") or f"{full_name}'s Business"
        except (KeyError, ValueError, TypeError) as exc:
            logger.error("Invalid seed user entry %r: %s", entry, exc)
            continue

        try:
            async with AsyncSessionLocal() as session:
                existing = await session.scalar(select(User.id).where(User.email == email))
                if existing:
                    continue
                tenant = Tenant(business_name=business_name)
                session.add(tenant)
                await session.flush()
                session.add(
                    User(
                        tenant_id=tenant.id,
                        full_name=full_name,
                        email=email,
                        password_hash=hash_password(password),
                        role=role,
                        is_verified=True,
                        email_verification_token=None,
                    )
                )
                await session.commit()
                logger.info("Seeded default user %s (%s)", email, role.value)
        except IntegrityError:
            logger.info("Seed user %s was created concurrently; skipping", email)
        except ProgrammingError:
            logger.warning("Users table missing; run `alembic upgrade head` before seeding")
            return


async def seed_superadmin() -> None:
    """Idempotently create the platform super admin from SUPERADMIN_EMAIL/SUPERADMIN_PASSWORD.

    An existing account with that email is never modified (so its password can be
    changed later without the env var resetting it); a business user with that
    email is not promoted.
    """
    settings = get_settings()
    email = settings.superadmin_email.strip()
    if not email:
        return
    if not settings.superadmin_password:
        logger.error("SUPERADMIN_EMAIL is set but SUPERADMIN_PASSWORD is empty; skipping super admin seeding")
        return

    try:
        async with AsyncSessionLocal() as session:
            existing = await session.scalar(select(User).where(User.email == email))
            if existing is not None:
                if not existing.is_superadmin:
                    logger.error("SUPERADMIN_EMAIL %s belongs to a business user; not promoting it", email)
                return
            session.add(
                User(
                    tenant_id=None,
                    full_name=settings.superadmin_name or email,
                    email=email,
                    password_hash=hash_password(settings.superadmin_password),
                    role=UserRole.owner,
                    is_superadmin=True,
                    is_verified=True,
                )
            )
            await session.commit()
            logger.info("Seeded platform super admin %s", email)
    except IntegrityError:
        logger.info("Super admin %s was created concurrently; skipping", email)
    except ProgrammingError:
        logger.warning("Users table missing; run `alembic upgrade head` before seeding")
