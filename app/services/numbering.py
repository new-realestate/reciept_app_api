import uuid
from collections.abc import Awaitable, Callable
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import DocumentCounter
from app.models.tenant import Tenant

PREFIXES = {"invoice": "INV", "quotation": "QUO", "receipt": "RCT", "tenancy": "TEN"}


async def next_document_number(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    kind: str,
    *,
    today: date | None = None,
    initial_value: Callable[[], Awaitable[int]] | None = None,
) -> str:
    """Next sequential number for a tenant, e.g. INV-2026-000007.

    The tenant row is locked (same approach as receipt numbering) so concurrent
    issues serialize; the counter is a real sequence, so deleting drafts or
    cancelling documents never causes a number to be reused.

    `initial_value` seeds a counter that doesn't exist yet (e.g. from numbers issued
    before the counter was introduced); it runs under the same lock.
    """
    lock_stmt = select(Tenant.id).where(Tenant.id == tenant_id)
    if db.get_bind().dialect.name != "sqlite":
        lock_stmt = lock_stmt.with_for_update(key_share=True)
    await db.execute(lock_stmt)

    counter = (
        await db.execute(
            select(DocumentCounter).where(DocumentCounter.tenant_id == tenant_id, DocumentCounter.kind == kind)
        )
    ).scalar_one_or_none()
    if counter is None:
        start = await initial_value() if initial_value is not None else 0
        counter = DocumentCounter(tenant_id=tenant_id, kind=kind, last_value=start)
        db.add(counter)
    counter.last_value += 1
    await db.flush()

    year = (today or date.today()).year
    return f"{PREFIXES[kind]}-{year}-{counter.last_value:06d}"
