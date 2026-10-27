"""One-query pagination: the page of rows and the total match count together.

A separate COUNT(*) query costs a full database round trip; `count(*) OVER ()` returns
the total alongside each row of the same query instead.
"""

from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def paginate(db: AsyncSession, stmt: Select, *, limit: int, offset: int) -> tuple[list[Any], int]:
    """Runs an ordered `stmt` for one page. Returns (rows, total).

    Each row is what `stmt` selects: a single entity/value for one-column selects,
    otherwise a tuple.
    """
    single = len(stmt.column_descriptions) == 1  # one entity or one value, not its columns
    counted = stmt.add_columns(func.count().over().label("_total")).limit(limit).offset(offset)
    result = (await db.execute(counted)).all()
    if result:
        total = result[-1][-1]
        rows = [r[0] if single else tuple(r[:-1]) for r in result]
        return rows, total
    if offset == 0:
        return [], 0
    # Past the last page: the window count isn't available, so count separately.
    total = (await db.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))).scalar_one()
    return [], total
