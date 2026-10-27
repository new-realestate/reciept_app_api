"""File storage for uploads (logos, signatures) and generated PDFs.

Objects are addressed by a relative key such as ``pdfs/<tenant>/<receipt>.pdf``
and exposed at ``{app_base_url}/static/<key>`` (see the /static route in
app/main.py), so stored URLs don't depend on the backend in use.

STORAGE_BACKEND=s3 stores them in Neon Object Storage (S3-compatible; the AWS_*
env vars supply credentials, endpoint and region). Otherwise they go under
STORAGE_DIR on local disk. Calls are blocking; use asyncio.to_thread from async code.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path, PurePosixPath

from app.core.config import get_settings


def is_valid_key(key: str) -> bool:
    parts = PurePosixPath(key).parts
    return bool(parts) and not key.startswith("/") and "\\" not in key and all(p not in ("", ".", "..") for p in parts)


# Objects under this prefix (KYC identity documents) are never served by the public
# /static route; they are only streamed through authorized, short-lived links.
PRIVATE_PREFIX = "private/"


def is_public_key(key: str) -> bool:
    return is_valid_key(key) and not key.startswith(PRIVATE_PREFIX)


def _check_key(key: str) -> None:
    if not is_valid_key(key):
        raise ValueError(f"Invalid storage key: {key!r}")


def _use_s3() -> bool:
    return get_settings().storage_backend.lower() == "s3"


@lru_cache
def _s3_client():
    import boto3
    from botocore.config import Config

    settings = get_settings()
    # Neon requires path-style addressing and SigV4. Empty values fall back to boto3's default chain.
    return boto3.client(
        "s3",
        endpoint_url=settings.aws_endpoint_url_s3 or None,
        region_name=settings.aws_region or None,
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _local_path(key: str) -> Path:
    return Path(get_settings().storage_dir) / key


def put(key: str, data: bytes, content_type: str) -> None:
    _check_key(key)
    if _use_s3():
        _s3_client().put_object(Bucket=get_settings().storage_bucket, Key=key, Body=data, ContentType=content_type)
        return
    path = _local_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def get(key: str) -> bytes | None:
    if not is_valid_key(key):
        return None
    if _use_s3():
        client = _s3_client()
        try:
            response = client.get_object(Bucket=get_settings().storage_bucket, Key=key)
        except client.exceptions.NoSuchKey:
            return None
        return response["Body"].read()
    path = _local_path(key)
    return path.read_bytes() if path.is_file() else None


def delete(key: str) -> None:
    _check_key(key)
    if _use_s3():
        _s3_client().delete_object(Bucket=get_settings().storage_bucket, Key=key)
        return
    _local_path(key).unlink(missing_ok=True)


def delete_prefix(prefix: str, *, keep: str | None = None) -> None:
    """Delete every object whose key starts with ``prefix``, except ``keep``."""
    if _use_s3():
        client = _s3_client()
        bucket = get_settings().storage_bucket
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                if obj["Key"] != keep:
                    client.delete_object(Bucket=bucket, Key=obj["Key"])
        return
    directory, _, stem = prefix.rpartition("/")
    target_dir = Path(get_settings().storage_dir) / directory
    if not target_dir.is_dir():
        return
    for old in target_dir.glob(f"{stem}*"):
        if f"{directory}/{old.name}" != keep:
            old.unlink(missing_ok=True)
