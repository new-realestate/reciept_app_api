import pytest

pytestmark = pytest.mark.asyncio

async def test_receipts_and_customers_paginate_with_total_header(client, signup):
    headers = await signup()
    cust = (await client.post("/customers", json={"name": "A", "email": "a@example.com"}, headers=headers)).json()
    for i in range(5):
        await client.post("/customers", json={"name": f"C{i}", "email": f"c{i}@example.com"}, headers=headers)
        await client.post("/receipts", json={"customer_id": cust["id"], "amount_cents": 1000 + i, "reason": "Rent",
                                              "payment_method": "cash", "paid_at": "2026-07-01"}, headers=headers)

    everything = await client.get("/receipts", headers=headers)
    assert len(everything.json()) == 5 and everything.headers["x-total-count"] == "5"  # default behaviour unchanged

    page = await client.get("/receipts?limit=2&offset=2", headers=headers)
    assert len(page.json()) == 2 and page.headers["x-total-count"] == "5"
    ids = [r["id"] for r in everything.json()]
    assert [r["id"] for r in page.json()] == ids[2:4]

    filtered = await client.get("/receipts?amount_cents=1001&limit=10", headers=headers)
    assert len(filtered.json()) == 1 and filtered.headers["x-total-count"] == "1"

    cpage = await client.get("/customers?limit=4&offset=4", headers=headers)
    assert len(cpage.json()) == 2 and cpage.headers["x-total-count"] == "6"
    search = await client.get("/customers?q=c3", headers=headers)
    assert len(search.json()) == 1 and search.headers["x-total-count"] == "1"

    assert (await client.get("/receipts?limit=0", headers=headers)).status_code == 422
    assert (await client.get("/customers?limit=500", headers=headers)).status_code == 422
