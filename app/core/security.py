import secrets
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import get_settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(*, subject: str, tenant_id: str | None, role: str, superadmin: bool = False) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {"sub": subject, "tenant_id": tenant_id, "role": role, "exp": expire}
    if superadmin:
        payload["sa"] = True
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_impersonation_token(*, subject: str, tenant_id: str, tenant_name: str) -> str:
    """Short-lived token letting a platform super admin act as owner of one business.

    `sub` stays the super admin, so everything they do is attributed to them.
    """
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.impersonation_token_minutes)
    payload = {
        "sub": subject,
        "tenant_id": tenant_id,
        "role": "owner",
        "sa": True,
        "imp": True,
        "act_tenant_id": tenant_id,
        "act_tenant_name": tenant_name,
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict | None:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None


def generate_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)
