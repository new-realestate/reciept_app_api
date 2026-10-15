"""Cross-dialect column type helpers.

Production runs on PostgreSQL and uses its native UUID/JSONB/CITEXT/ENUM
types (see schema.sql). Tests run against SQLite for speed, so these
columns fall back to portable equivalents there via `with_variant`.
"""

import enum

from sqlalchemy import JSON, String, Uuid
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import CITEXT, JSONB


def UUIDType():
    return Uuid(as_uuid=True, native_uuid=True)


def CIText():
    return String().with_variant(CITEXT(), "postgresql")


def JSONBType():
    return JSON().with_variant(JSONB(), "postgresql")


def PortableEnum(enum_cls: type[enum.Enum], name: str):
    return SAEnum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e], native_enum=True)
