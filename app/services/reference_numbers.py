import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.receipt import Receipt
from app.services.numbering import next_document_number

_TRAILING_DIGITS = re.compile(r"(\d+)$")


async def _highest_existing(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Largest sequence number already used by this tenant's receipts (0 if none)."""
    refs = (await db.execute(select(Receipt.reference_number).where(Receipt.tenant_id == tenant_id))).scalars().all()
    highest = 0
    for ref in refs:
        match = _TRAILING_DIGITS.search(ref or "")
        if match:
            highest = max(highest, int(match.group(1)))
    return highest


async def next_reference_number(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    """Assigns the next sequential receipt number for a tenant, e.g. RCT-2026-000042.

    Uses the per-tenant DocumentCounter (kind "receipt") under the tenant row lock, the
    same sequence mechanism as invoices and quotations, so concurrent receipt creations
    serialize and a number is never reused. The sequence continues across years, as it
    always has. A tenant with no counter yet (data from before the counter existed)
    starts after its highest existing receipt number.
    """
    return await next_document_number(
        db,
        tenant_id,
        "receipt",
        today=datetime.now(timezone.utc).date(),
        initial_value=lambda: _highest_existing(db, tenant_id),
    )
