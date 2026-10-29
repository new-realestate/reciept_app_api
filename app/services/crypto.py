"""Field-level encryption for sensitive KYC values (identity document numbers).

Uses Fernet (AES-128-CBC + HMAC-SHA256) with KYC_ENCRYPTION_KEY. Values are never
logged; callers show the masked form (mask_identifier) everywhere except an explicit,
audited reveal.
"""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, status

from app.core.config import get_settings


class EncryptionUnavailable(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Identity details can't be stored right now: KYC encryption is not configured",
        )


@lru_cache
def _fernet(key: str) -> Fernet:
    return Fernet(key.encode())


def _cipher() -> Fernet:
    key = get_settings().kyc_encryption_key
    if not key:
        raise EncryptionUnavailable()
    try:
        return _fernet(key)
    except ValueError as exc:  # malformed key
        raise EncryptionUnavailable() from exc


def encrypt_text(value: str) -> str:
    return _cipher().encrypt(value.encode()).decode()


def decrypt_text(token: str) -> str:
    try:
        return _cipher().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise HTTPException(status_code=500, detail="Stored identity details could not be decrypted") from exc


def last4(value: str) -> str:
    cleaned = "".join(ch for ch in value if ch.isalnum())
    return cleaned[-4:]


def mask_identifier(last_four: str | None) -> str | None:
    return f"••••{last_four}" if last_four else None
