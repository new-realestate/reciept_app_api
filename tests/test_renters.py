from datetime import date, timedelta

import pytest

pytestmark = pytest.mark.asyncio

TODAY = date.today()


async def test_assigned_staff_create_tenant_with_tenancy(client, signup, make_staff, make_property, assign, make_renter):
    owner = await signup()
    prop = await make_property(owner, units=["A1"])
    member, staff = await make_staff(owner)
    await assign(owner, prop["id"], member["id"])

    renter = await make_renter(staff, prop["id"], unit_id=prop["units"][0]["id"])
    assert renter["status"] == "active" and renter["kyc_status"] == "not_submitted"
    [tenancy] = renter["tenancies"]
    assert tenancy["reference"].startswith("TEN-") and tenancy["unit_label"] == "A1" and tenancy["status"] == "active"
    assert renter["customer"]["renter_id"] == renter["id"]

    # The customer directory shows them as a tenant.
    customers = (await client.get("/customers", params={"is_renter": "true"}, headers=owner)).json()
    assert [c["id"] for c in customers] == [renter["customer_id"]]
    detail = (await client.get(f"/properties/{prop['id']}", headers=owner)).json()
    assert detail["occupied_unit_count"] == 1 and detail["units"][0]["occupied"] is True


async def test_staff_cannot_create_tenant_on_unassigned_property_and_nothing_is_saved(
    client, signup, make_staff, make_property, assign
):
    owner = await signup()
    mine = await make_property(owner, name="Mine")
    theirs = await make_property(owner, name="Theirs")
    member, staff = await make_staff(owner)
    await assign(owner, mine["id"], member["id"])

    body = {
        "customer": {"name": "Nope", "email": "nope@example.com"},
        "tenancy": {"property_id": theirs["id"], "start_date": str(TODAY), "rent_amount_cents": 1000},
    }
    assert (await client.post("/renters", json=body, headers=staff)).status_code == 404
    # The transaction rolled back: no orphan customer was left behind.
    assert (await client.get("/customers", params={"q": "nope"}, headers=owner)).json() == []

    # Staff must attach a tenancy on one of their properties.
    no_tenancy = await client.post("/renters", json={"customer": {"name": "N", "email": "n@example.com"}}, headers=staff)
    assert no_tenancy.status_code == 422


async def test_existing_customer_becomes_tenant_without_duplicate(client, signup, make_property):
    owner = await signup()
    prop = await make_property(owner)
    cust = (await client.post("/customers", json={"name": "Kato", "email": "kato@example.com"}, headers=owner)).json()
    receipt = (await client.post(
        "/receipts",
        json={"payment_received_confirmed": True, "customer_id": cust["id"], "amount_cents": 5000, "reason": "Consulting",
              "payment_method": "cash", "paid_at": str(TODAY)},
        headers=owner,
    )).json()

    resp = await client.post("/renters", json={
        "customer_id": cust["id"],
        "tenancy": {"property_id": prop["id"], "start_date": str(TODAY), "rent_amount_cents": 1000},
    }, headers=owner)
    assert resp.status_code == 201
    renter = resp.json()
    assert renter["customer_id"] == cust["id"]
    assert len((await client.get("/customers", headers=owner)).json()) == 1

    # Earlier documents still belong to the same customer.
    docs = (await client.get(f"/customers/{cust['id']}/documents", headers=owner)).json()
    assert [r["id"] for r in docs["receipts"]] == [receipt["id"]]
    assert docs["customer"]["renter_id"] == renter["id"]

    # A second tenant profile for the same customer is refused, pointing at the existing one.
    again = await client.post("/renters", json={"customer_id": cust["id"]}, headers=owner)
    assert again.status_code == 409
    assert again.json()["error"]["message"]["renter_id"] == renter["id"]


async def test_new_tenant_details_matching_a_customer_are_flagged(client, signup, make_property):
    owner = await signup()
    prop = await make_property(owner)
    await client.post("/customers", json={"name": "Grace", "phone": "+256701234567"}, headers=owner)
    body = {
        "customer": {"name": "Grace N.", "phone": "0701234567"},
        "tenancy": {"property_id": prop["id"], "start_date": str(TODAY), "rent_amount_cents": 1000},
    }
    resp = await client.post("/renters", json=body, headers=owner)
    assert resp.status_code == 409
    assert resp.json()["error"]["message"]["matches"][0]["name"] == "Grace"

    resp = await client.post("/renters", json={**body, "allow_duplicate_customer": True}, headers=owner)
    assert resp.status_code == 201


async def test_tenancy_history_unit_conflicts_and_ending(client, signup, make_property, make_renter):
    owner = await signup()
    prop = await make_property(owner, units=["1", "2"])
    unit1, unit2 = prop["units"][0]["id"], prop["units"][1]["id"]
    renter = await make_renter(owner, prop["id"], unit_id=unit1)

    other = await client.post("/renters", json={
        "customer": {"name": "Other", "email": "other@example.com"},
        "tenancy": {"property_id": prop["id"], "unit_id": unit1, "start_date": str(TODAY), "rent_amount_cents": 1},
    }, headers=owner)
    assert other.status_code == 409  # unit already occupied

    [first] = renter["tenancies"]
    ended = await client.post(f"/tenancies/{first['id']}/end", json={"end_date": str(TODAY + timedelta(days=1))}, headers=owner)
    assert ended.json()["status"] == "ended"
    assert (await client.get(f"/renters/{renter['id']}", headers=owner)).json()["status"] == "former"

    # A new tenancy reuses the same tenant identity; history keeps both.
    moved = await client.post(f"/renters/{renter['id']}/tenancies", json={
        "property_id": prop["id"], "unit_id": unit2, "start_date": str(TODAY + timedelta(days=2)), "rent_amount_cents": 2000,
    }, headers=owner)
    assert moved.status_code == 201
    detail = (await client.get(f"/renters/{renter['id']}", headers=owner)).json()
    assert detail["status"] == "active"
    assert {t["status"] for t in detail["tenancies"]} == {"active", "ended"}
    assert len(detail["current_tenancies"]) == 1


async def test_rent_schedule_is_created_for_the_tenancy(client, signup, make_property, make_renter):
    owner = await signup()
    prop = await make_property(owner)
    renter = await make_renter(owner, prop["id"], tenancy={
        "property_id": prop["id"], "start_date": str(TODAY), "rent_amount_cents": 300000, "create_rent_schedule": True,
    })
    [tenancy] = renter["tenancies"]
    assert tenancy["payment_schedule_id"]
    schedule = (await client.get(f"/schedules/{tenancy['payment_schedule_id']}", headers=owner)).json()
    assert schedule["tenancy_id"] == tenancy["id"] and schedule["property_id"] == prop["id"]


async def test_cross_business_customer_cannot_become_tenant(client, signup, make_property):
    owner_a = await signup("a@example.com", "A")
    owner_b = await signup("b@example.com", "B")
    cust_b = (await client.post("/customers", json={"name": "B cust", "email": "bc@example.com"}, headers=owner_b)).json()
    prop_a = await make_property(owner_a)
    resp = await client.post("/renters", json={
        "customer_id": cust_b["id"],
        "tenancy": {"property_id": prop_a["id"], "start_date": str(TODAY), "rent_amount_cents": 1},
    }, headers=owner_a)
    assert resp.status_code == 404


async def test_staff_only_see_tenants_on_their_properties(client, signup, make_staff, make_property, assign, make_renter):
    owner = await signup()
    p1 = await make_property(owner, name="P1")
    p2 = await make_property(owner, name="P2")
    member, staff = await make_staff(owner)
    await assign(owner, p1["id"], member["id"])
    r1 = await make_renter(owner, p1["id"], name="On P1", email="p1@example.com")
    r2 = await make_renter(owner, p2["id"], name="On P2", email="p2@example.com")

    assert [r["id"] for r in (await client.get("/renters", headers=staff)).json()] == [r1["id"]]
    assert (await client.get(f"/renters/{r2['id']}", headers=staff)).status_code == 404
    assert (await client.get(f"/tenancies/{r2['tenancies'][0]['id']}", headers=staff)).status_code == 404
    assert (await client.get("/renters", params={"property_id": p2["id"]}, headers=staff)).status_code == 404
