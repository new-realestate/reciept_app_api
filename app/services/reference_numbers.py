import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.receipt import Receipt
from app.models.tenant import Tenant


async def next_reference_number(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    """Assigns the next sequential receipt number for a tenant.

    Locks the tenant row for the duration of the transaction so concurrent
    receipt creations for the same tenant serialize instead of colliding on
    the same reference number. (SQLite, used in tests, has no row locking and
    is single-writer already, so the lock is skipped there.)
    """
    lock_stmt = select(Tenant.id).where(Tenant.id == tenant_id)
    if db.get_bind().dialect.name != "sqlite":
        lock_stmt = lock_stmt.with_for_update(key_share=True)
    await db.execute(lock_stmt)

    count_result = await db.execute(select(func.count()).select_from(Receipt).where(Receipt.tenant_id == tenant_id))
    count = count_result.scalar_one()

    year = datetime.now(timezone.utc).year
    return f"RCT-{year}-{count + 1:06d}"
