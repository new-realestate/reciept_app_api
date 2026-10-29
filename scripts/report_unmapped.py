"""Read-only report: existing records that might belong to a property but aren't linked.

Documents created before properties existed have no property/tenancy context, and the
migration deliberately does not guess one. This lists the hints a business owner can use
to link them by hand (payment schedule reference labels, receipt reasons that mention
rent), plus customers that share an email or phone within a business (possible
duplicates to review). Nothing is changed.

Usage (from reciept_app_api/, with DATABASE_URL set or in .env):
    python scripts/report_unmapped.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, or_, select  # noqa: E402

from app.db.session import AsyncSessionLocal  # noqa: E402
from app.models.customer import Customer  # noqa: E402
from app.models.receipt import Receipt  # noqa: E402
from app.models.schedule import PaymentSchedule  # noqa: E402
from app.models.tenant import Tenant  # noqa: E402

RENT_WORDS = ("rent", "tenancy", "lease", "deposit")


async def main() -> None:
    async with AsyncSessionLocal() as db:
        tenants = {t.id: t.business_name for t in (await db.execute(select(Tenant))).scalars()}

        print("== Payment schedules without a property (reference label is a hint) ==")
        rows = (await db.execute(
            select(PaymentSchedule.tenant_id, PaymentSchedule.id, PaymentSchedule.reference_label, PaymentSchedule.description)
            .where(PaymentSchedule.property_id.is_(None)).order_by(PaymentSchedule.tenant_id)
        )).all()
        for tenant_id, sid, label, desc in rows:
            print(f"  [{tenants.get(tenant_id)}] schedule {sid}: label={label!r} description={desc!r}")
        print(f"  total: {len(rows)}\n")

        print("== Receipts without a property whose reason mentions rent ==")
        like = [func.lower(Receipt.reason).like(f"%{w}%") for w in RENT_WORDS]
        counts = (await db.execute(
            select(Receipt.tenant_id, func.count()).where(Receipt.property_id.is_(None), or_(*like)).group_by(Receipt.tenant_id)
        )).all()
        for tenant_id, count in counts:
            print(f"  [{tenants.get(tenant_id)}] {count} receipt(s)")
        print()

        print("== Possible duplicate customers (same email or E.164 phone within a business) ==")
        for column in (func.lower(Customer.email), Customer.phone_normalized):
            dupes = (await db.execute(
                select(Customer.tenant_id, column, func.count()).where(column.is_not(None))
                .group_by(Customer.tenant_id, column).having(func.count() > 1)
            )).all()
            for tenant_id, value, count in dupes:
                print(f"  [{tenants.get(tenant_id)}] {count} customers share {value}")
    print("\nNo changes were made. Link records from the portal after reviewing them.")


if __name__ == "__main__":
    asyncio.run(main())
