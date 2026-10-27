"""Horizontal privilege escalation and cross-business isolation, enforced by the backend."""
from datetime import date

import pytest

pytestmark = pytest.mark.asyncio

TODAY = str(date.today())


def _receipt(customer_id, **extra):
    return {"payment_received_confirmed": True, "customer_id": customer_id, "amount_cents": 1000, "reason": "Rent",
            "payment_method": "cash", "paid_at": TODAY, **extra}


async def _setup(client, signup, make_staff, make_property, assign, make_renter):
    owner = await signup()
    mine = await make_property(owner, name="Mine", units=["M1"])
    theirs = await make_property(owner, name="Theirs", units=["T1"])
    member, staff = await make_staff(owner)
    await assign(owner, mine["id"], member["id"])
    r_mine = await make_renter(owner, mine["id"], name="Mine Renter", email="mine@example.com")
    r_theirs = await make_renter(owner, theirs["id"], name="Their Renter", email="theirs@example.com")
    return owner, staff, member, mine, theirs, r_mine, r_theirs


async def test_staff_issue_receipts_only_for_assigned_properties(client, signup, make_staff, make_property, assign, make_renter):
    owner, staff, _, mine, theirs, r_mine, r_theirs = await _setup(client, signup, make_staff, make_property, assign, make_renter)

    ok = await client.post("/receipts", json=_receipt(r_mine["customer_id"], tenancy_id=r_mine["tenancies"][0]["id"]), headers=staff)
    assert ok.status_code == 201
    body = ok.json()
    assert body["property_id"] == mine["id"] and body["tenancy_id"] == r_mine["tenancies"][0]["id"]

    # Changing ids in the request doesn't get around the assignment.
    assert (await client.post("/receipts", json=_receipt(r_theirs["customer_id"], property_id=theirs["id"]), headers=staff)).status_code == 404
    assert (await client.post("/receipts", json=_receipt(r_theirs["customer_id"], tenancy_id=r_theirs["tenancies"][0]["id"]), headers=staff)).status_code == 404
    assert (await client.post("/receipts", json=_receipt(r_mine["customer_id"], unit_id=theirs["units"][0]["id"]), headers=staff)).status_code == 404
    # A tenancy must belong to the receipt's customer.
    bad = await client.post("/receipts", json=_receipt(r_theirs["customer_id"], tenancy_id=r_mine["tenancies"][0]["id"]), headers=staff)
    assert bad.status_code == 422
    # The unit must be on the given property.
    assert (await client.post("/receipts", json=_receipt(r_mine["customer_id"], property_id=mine["id"], unit_id=theirs["units"][0]["id"]),
                              headers=owner)).status_code == 422


async def test_staff_cannot_see_documents_of_unassigned_properties(client, signup, make_staff, make_property, assign, make_renter):
    owner, staff, member, mine, theirs, r_mine, r_theirs = await _setup(client, signup, make_staff, make_property, assign, make_renter)
    theirs_receipt = (await client.post("/receipts", json=_receipt(r_theirs["customer_id"], property_id=theirs["id"]), headers=owner)).json()
    mine_receipt = (await client.post("/receipts", json=_receipt(r_mine["customer_id"], property_id=mine["id"]), headers=owner)).json()
    general = (await client.post("/receipts", json=_receipt(r_mine["customer_id"]), headers=owner)).json()

    visible = {r["id"] for r in (await client.get("/receipts", headers=staff)).json()}
    assert visible == {mine_receipt["id"], general["id"]}
    assert (await client.get(f"/receipts/{theirs_receipt['id']}", headers=staff)).status_code == 404
    assert (await client.post(f"/receipts/{theirs_receipt['id']}/send", json={"channels": ["email"]}, headers=staff)).status_code == 404

    inv = (await client.post("/invoices", json={
        "customer_id": r_theirs["customer_id"], "property_id": theirs["id"], "due_date": TODAY, "issue_now": True,
        "line_items": [{"description": "Rent", "quantity": "1", "unit_price_cents": 1000}],
    }, headers=owner)).json()
    assert (await client.get(f"/invoices/{inv['id']}", headers=staff)).status_code == 404
    assert inv["id"] not in {i["id"] for i in (await client.get("/invoices", headers=staff)).json()["items"]}
    pay = await client.post(f"/invoices/{inv['id']}/payments", json={"amount_cents": 100, "paid_at": TODAY, "payment_method": "cash"}, headers=staff)
    assert pay.status_code == 404

    # The dashboard only counts what the staff member can see.
    summary = (await client.get("/dashboard/summary", params={"period": "all"}, headers=staff)).json()
    assert summary["by_currency"]["UGX"]["received_cents"] == 2000
    owner_summary = (await client.get("/dashboard/summary", params={"period": "all"}, headers=owner)).json()
    assert owner_summary["by_currency"]["UGX"]["received_cents"] == 3000

    # Revoking the assignment hides the property's documents immediately; history is untouched.
    await client.delete(f"/properties/{mine['id']}/assignments/{member['id']}", headers=owner)
    assert (await client.get(f"/receipts/{mine_receipt['id']}", headers=staff)).status_code == 404
    assert (await client.get(f"/receipts/{mine_receipt['id']}", headers=owner)).status_code == 200


async def test_general_documents_need_the_view_permission(client, signup, make_staff):
    owner = await signup()
    cust = (await client.post("/customers", json={"name": "C", "email": "c@example.com"}, headers=owner)).json()
    general = (await client.post("/receipts", json=_receipt(cust["id"]), headers=owner)).json()
    _, staff = await make_staff(owner, permissions=["receipts.issue"])
    assert (await client.get(f"/receipts/{general['id']}", headers=staff)).status_code == 404
    assert (await client.get("/receipts", headers=staff)).json() == []


async def test_one_business_cannot_reach_another(client, signup, make_property, make_renter):
    owner_a = await signup("a@example.com", "A")
    owner_b = await signup("b@example.com", "B")
    prop_a = await make_property(owner_a)
    renter_a = await make_renter(owner_a, prop_a["id"])
    receipt_a = (await client.post("/receipts", json=_receipt(renter_a["customer_id"], property_id=prop_a["id"]), headers=owner_a)).json()

    assert (await client.get(f"/receipts/{receipt_a['id']}", headers=owner_b)).status_code == 404
    assert (await client.get(f"/customers/{renter_a['customer_id']}", headers=owner_b)).status_code == 404
    assert (await client.get(f"/customers/{renter_a['customer_id']}/documents", headers=owner_b)).status_code == 404
    assert (await client.get(f"/renters/{renter_a['id']}", headers=owner_b)).status_code == 404
    assert (await client.get(f"/renters/{renter_a['id']}/kyc", headers=owner_b)).status_code == 404
    # B can't issue documents to A's customer or against A's property.
    assert (await client.post("/receipts", json=_receipt(renter_a["customer_id"]), headers=owner_b)).status_code == 404
    cust_b = (await client.post("/customers", json={"name": "B", "email": "bb@example.com"}, headers=owner_b)).json()
    assert (await client.post("/receipts", json=_receipt(cust_b["id"], property_id=prop_a["id"]), headers=owner_b)).status_code == 404
    assert (await client.post("/invoices", json={
        "customer_id": cust_b["id"], "tenancy_id": renter_a["tenancies"][0]["id"], "due_date": TODAY,
        "line_items": [{"description": "x", "quantity": "1", "unit_price_cents": 1}],
    }, headers=owner_b)).status_code == 404


async def test_history_survives_staff_deactivation_and_property_archive(client, signup, make_staff, make_property, assign, make_renter):
    owner = await signup()
    prop = await make_property(owner)
    member, staff = await make_staff(owner)
    await assign(owner, prop["id"], member["id"])
    renter = await make_renter(staff, prop["id"])
    receipt = (await client.post("/receipts", json=_receipt(renter["customer_id"], tenancy_id=renter["tenancies"][0]["id"]), headers=staff)).json()

    await client.post(f"/team/members/{member['id']}/deactivate", headers=owner)
    await client.post(f"/properties/{prop['id']}/archive", headers=owner)

    kept = (await client.get(f"/receipts/{receipt['id']}", headers=owner)).json()
    assert kept["issued_by_user_id"] == member["id"] and kept["tenancy_id"] == renter["tenancies"][0]["id"]
    assert kept["issuer_printed_name"] == "Sam Staff"
    detail = (await client.get(f"/renters/{renter['id']}", headers=owner)).json()
    assert detail["tenancies"][0]["status"] == "active"
