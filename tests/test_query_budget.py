"""Guards against the per-request query count creeping back up (each query is a database round trip)."""
import pytest
from sqlalchemy import event

pytestmark = pytest.mark.asyncio


@pytest.fixture
def sql_counter(db_engine):
    statements: list[str] = []

    def count(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", count)
    yield statements
    event.remove(db_engine.sync_engine, "before_cursor_execute", count)


async def test_account_and_settings_are_one_query(client, signup, sql_counter):
    headers = await signup()
    for path in ("/users/me", "/tenant/me"):
        sql_counter.clear()
        assert (await client.get(path, headers=headers)).status_code == 200
        assert len(sql_counter) == 1, (path, sql_counter)


async def test_lists_fetch_page_and_total_together(client, signup, sql_counter):
    headers = await signup()
    for i in range(3):
        await client.post("/customers", json={"name": f"C{i}", "email": f"c{i}@example.com"}, headers=headers)

    sql_counter.clear()
    resp = await client.get("/customers", params={"limit": 2}, headers=headers)
    assert resp.headers["X-Total-Count"] == "3" and len(resp.json()) == 2
    assert len(sql_counter) <= 3, sql_counter  # auth, page+total, tenant-profile lookup

    # Past the last page the total is still reported.
    resp = await client.get("/customers", params={"limit": 2, "offset": 10}, headers=headers)
    assert resp.json() == [] and resp.headers["X-Total-Count"] == "3"


async def test_tenant_list_query_count_does_not_grow_with_rows(client, signup, make_property, make_renter, sql_counter):
    headers = await signup()
    prop = await make_property(headers, units=["A", "B", "C"])
    for i, unit in enumerate(prop["units"]):
        await make_renter(headers, prop["id"], name=f"R{i}", email=f"r{i}@example.com", unit_id=unit["id"])

    sql_counter.clear()
    resp = await client.get("/renters", headers=headers)
    assert len(resp.json()) == 3
    assert len(sql_counter) <= 4, sql_counter  # auth, page+total, customers+KYC, tenancies
